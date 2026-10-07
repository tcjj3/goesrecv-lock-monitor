#!/usr/bin/env python3
"""goesrecv lock monitor and alert service.

Historical operational version: 2023-05 to 2023-06
Public cleanup / hardening: 2026-10-08

The 2023 version was used to monitor a rooftop GK-2A receiving system. This
public version removes site-specific credentials and configuration, makes the
nanomsg/TCP parsing explicit, and adds hysteresis/debouncing plus alert
rate-limiting to prevent notification storms near the lock threshold.
"""

from __future__ import annotations

import argparse
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import smtplib
import socket
import ssl
import threading
import time
from typing import Any, Optional

NN_INIT = b"\x00\x53\x50\x00\x00\x21\x00\x00"
NN_RESPONSE = b"\x00\x53\x50\x00\x00\x20\x00\x00"

DEFAULT_CONFIG: dict[str, Any] = {
    "goesrecv": {
        "host": "127.0.0.1",
        "decoder_port": 6002,
        "socket_timeout_seconds": 10.0,
        "reconnect_delay_seconds": 2.0,
    },
    "lock": {
        "loss_confirm_seconds": 5.0,
        "recovery_confirm_seconds": 5.0,
        "alert_cooldown_seconds": 60.0,
        "history_limit": 500,
        "notify_on_initial_state": False,
    },
    "http": {
        "enabled": True,
        "bind": "127.0.0.1",
        "port": 8083,
        "cors_origin": "",
    },
    "email": {
        "enabled": False,
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "starttls": True,
        "username": "",
        "password_env": "GOESRECV_MONITOR_SMTP_PASSWORD",
        "sender": "",
        "receivers": [],
        "display_name": "goesrecv lock monitor",
        "subject_prefix": "Satellite signal status",
        "max_attempts": 3,
    },
}


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        user_config = json.load(fh)
    return deep_merge(DEFAULT_CONFIG, user_config)


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("socket closed")
        data.extend(chunk)
    return bytes(data)


@dataclass(frozen=True)
class StateTransition:
    locked: bool
    timestamp: str


class LockStateMachine:
    def __init__(self, config: dict[str, Any]):
        self.loss_confirm = float(config["loss_confirm_seconds"])
        self.recovery_confirm = float(config["recovery_confirm_seconds"])
        self.cooldown = float(config["alert_cooldown_seconds"])
        self.notify_on_initial = bool(config.get("notify_on_initial_state", False))
        limit = max(1, int(config.get("history_limit", 500)))

        self.confirmed: Optional[bool] = None
        self.candidate: Optional[bool] = None
        self.candidate_since: Optional[float] = None
        self.last_alert_monotonic: dict[bool, float] = {True: float("-inf"), False: float("-inf")}
        self.lost_history = deque(maxlen=limit)
        self.recovered_history = deque(maxlen=limit)
        self.last_lost = ""
        self.last_recovered = ""

    def observe(self, raw_locked: bool, monotonic_now: Optional[float] = None) -> Optional[StateTransition]:
        now_mono = time.monotonic() if monotonic_now is None else monotonic_now

        if self.confirmed is not None and raw_locked == self.confirmed:
            self.candidate = None
            self.candidate_since = None
            return None

        if self.candidate != raw_locked:
            self.candidate = raw_locked
            self.candidate_since = now_mono
            return None

        assert self.candidate_since is not None
        required = self.recovery_confirm if raw_locked else self.loss_confirm
        if now_mono - self.candidate_since < required:
            return None

        initial = self.confirmed is None
        self.confirmed = raw_locked
        self.candidate = None
        self.candidate_since = None

        stamp = now_text()
        if initial:
            if not self.notify_on_initial:
                return None
        else:
            if raw_locked:
                self.last_recovered = stamp
                self.recovered_history.append(stamp)
            else:
                self.last_lost = stamp
                self.lost_history.append(stamp)

        if now_mono - self.last_alert_monotonic[raw_locked] < self.cooldown:
            return None

        self.last_alert_monotonic[raw_locked] = now_mono
        return StateTransition(raw_locked, stamp)


class SharedState:
    def __init__(self, lock_config: dict[str, Any]):
        self.mutex = threading.Lock()
        self.machine = LockStateMachine(lock_config)
        self.latest_stats: dict[str, Any] = {}
        self.connected = False
        self.last_stats_time = ""
        self.last_error = ""

    def set_connected(self, connected: bool, error: str = "") -> None:
        with self.mutex:
            self.connected = connected
            self.last_error = error

    def update_stats(self, stats: dict[str, Any]) -> None:
        with self.mutex:
            self.latest_stats = stats
            self.last_stats_time = now_text()
            self.last_error = ""

    def observe(self, raw_locked: bool) -> Optional[StateTransition]:
        with self.mutex:
            return self.machine.observe(raw_locked)

    def snapshot(self) -> dict[str, Any]:
        with self.mutex:
            confirmed = self.machine.confirmed
            return {
                "connected": self.connected,
                "normal": confirmed,
                "locked": confirmed,  # legacy / operational alias used by the original API
                "decoder_ok": self.latest_stats.get("ok"),
                "reed_solomon_errors": self.latest_stats.get("reed_solomon_errors"),
                "last_stats_time": self.last_stats_time,
                "last_error": self.last_error,
              "last_lost_lock_time": self.machine.last_lost,
                "last_recovered_lock_time": self.machine.last_recovered,
                "lost_lock_times": list(self.machine.lost_history),
                "recovered_lock_times": list(self.machine.recovered_history),
                "latest_stats": deepcopy(self.latest_stats),
            }


class AlertWorker(threading.Thread):
    def __init__(self, config: dict[str, Any]):
        super().__init__(name="alert-worker", daemon=True)
        self.config = config
        self.events: queue.Queue[StateTransition] = queue.Queue(maxsize=100)

    def enqueue(self, transition: StateTransition) -> None:
        if not self.config.get("enabled", False):
            return
        try:
            self.events.put_nowait(transition)
        except queue.Full:
            print("Alert queue is full; dropping alert", flush=True)

    def run(self) -> None:
        while True:
            transition = self.events.get()
            try:
                self._send_with_retry(transition)
            finally:
                self.events.task_done()

    def _send_with_retry(self, transition: StateTransition) -> None:
        attempts = max(1, int(self.config.get("max_attempts", 3)))
        delay = 2
        for attempt in range(1, attempts + 1):
            try:
                self._send(transition)
                print(f"Alert sent: locked={transition.locked} at {transition.timestamp}", flush=True)
                return
            except Exception as exc:
                print(f"Alert attempt {attempt}/{attempts} failed: {exc}", flush=True)
                if attempt < attempts:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)

    def _send(self, transition: StateTransition) -> None:
        cfg = self.config
        receivers = list(cfg.get("receivers", []))
        sender = cfg.get("sender") or cfg.get("username")
        username = cfg.get("username") or sender
        password_env = cfg.get("password_env", "GOESRECV_MONITOR_SMTP_PASSWORD")
        password = os.environ.get(password_env, "")

        if not cfg.get("smtp_host") or not sender or not receivers:
            raise RuntimeError("email is enabled but smtp_host/sender/receivers are incomplete")
        if username and not password:
            raise RuntimeError(f"SMTP password environment variable is empty: {password_env}")

        state_text = "NORMAL RECEPTION RESTORED" if transition.locked else "RECEPTION ERROR DETECTED"
        subject = f"{cfg.get('subject_prefix', 'Satellite signal status')} - {state_text}"
        body = f"{state_text}\nTime: {transition.timestamp}\n"
        msg = MIMEText(body, "plain", "utf-8")
        display = cfg.get("display_name", "goesrecv lock monitor")
        msg["From"] = f"{display} <{sender}>"
        msg["To"] = ", ".join(receivers)
        msg["Subject"] = subject

        with smtplib.SMTP(ЩY™ЦИњЫ]ЪЬЭ—K[ќ
Щ™Л™Щ]
њЫ]ЬЬќ‹NКJK[Y[Э]LMJH\ИЫ]‚€Ы]™ZК
B€Y€Щ™Л™Щ]
њЭ\ќИ‹ќYJN‚€Ы]њЭ\ќКЫЫќ^\ЬЫЬ™X]WЩY][ШЫЫќ^

JB€Ы]™ZК
B€Y€\Щ\›[YN‚€Ы]›ЩЪ[Љ\Щ\›[YK\ЬЭЫЬ™
B€Ы]њЩ[™XZ[
Щ[™\‹™XЩZ]™\њЛ\ЩЛ\ЧЬЭљ[™К
JB‚‚™Y€™XЩ\[Ы—Ъ\ЧЫ›Ь›X[
Э]О€XЭЬЭ‹[ћWJHO€›ЫЫ‚€€€”™]\›€HЬљYЪ[[[Ыљ]Ь‰ЬИЬ\][Ы[™XЩ\[Ы€Э]K‚‚€\ЭЬљXШ[ќ[N‚€™YYЬЫЫЫ[Ы—Щ\њ›ЬњИOHO€›Ь›X[™XЩ\[Ы‚€™YYЬЫЫЫ[Ы—Щ\њ›ЬњИOHO€\њ›Ь€ИYЬYY™XЩ\[Ы‚‚€\И\И[ќ[ќ[Ы[HЭљXЭ\€[€ЫЩ\Ь™XЭ‰ЬИЭЫ€ЪШљY[‚€HЫЬњ™XЭX›HXЪЩ]Ш[€Э[]™H›Ы‹^™\›И™YYTЫЫЫ[Ы€ЫЬњ™XЭ[ЫњВ€Ъ[HЪШ™[XZ[њИќYK‚€€€‚€ћN‚€™]\›€[ќ
Э]Л™Щ]
њ™YYЬЫЫЫ[Ы—Щ\њ›ЬњИ‹LJJHOH€^Щ\
\Q\њ›Ь‹[YQ\њ›ЬЉN‚€™]\›€[ЩB‚‚‚™Y€XZЩWЪ[™\ЉЪ\™Y€Ъ\™YЭ]KШЫЫ™љYО€XЭЬЭ‹[ћWJN‚€ЫЬњЧЫЬљYЪ[€HЭЉШЫЫ™љYЛ™Щ]
ЫЬњЧЫЬљYЪ[€‹€ЉJB‚€Ы\ЬИ[™\Љ\ЩR™\]Y\Э[™\ЉN‚€Щ\ќ™\—Э™\њЪ[Ы€H™ЫЩ\Ь™XЭ‹[ШЪЛ[[Ыљ]Ь‹МKЊ‚‚€Y€ЪњЫЫЉЩ[‹Э]\О€[ќ^[ШY€[ћJHO€›Ы™N‚€]HHњЫЫ‹™[\К^[ШY[њЭ\™WШ\ШЪZOQ[ЩKЩ\\]ЬњПJ‹‹Ћ€ЉJK™[ЫЩJќ]‹NЉB€Щ[‹њЩ[™Ь™\ЬЫњЩJЭ]\КB€Щ[‹њЩ[™ЪXY\ЉђЫЫќ[ќU\H‹\XШ][Ы‹ЪњЫЫЋИЪ\њЩ]]]‹NЉB€Щ[‹њЩ[™ЪXY\ЉђЫЫќ[ќS[™Э‹ЭЉ[Љ]JJJB€Y€ЫЬњЧЫЬљYЪ[Ћ‚€Щ[‹њЩ[™ЪXY\ЉђXШЩ\ЬЛPЫЫќ›ЫP[ЭЛSЬљYЪ[€‹ЫЬњЧЫЬљYЪ[ЉB€Щ[‹™[™ЪXY\њК
B€Щ[‹ќЩљ[KќЬљ]J]JB‚€Y€ЧССU
Щ[ЉHO€›Ы™N‚€Y€Щ[‹њ]OH‹ИЋ‚€Щ[‹њЩ[™Ь™\ЬЫњЩJМЉB€Щ[‹њЩ[™ЪXY\Љ“ШШ][Ы€‹‹ЬЪYЫ[ЉB€Щ[‹™[™ЪXY\њК
B€™]\›‚‚€Ы\ЪЭHЪ\™YњЫ\ЪЭ

B€Y€Щ[‹њ]OH‹ЩЫЩ\Ь™XЭ€Ћ‚€Щ[‹—ЪњЫЫЉЊЫ\ЪЭИ›]\ЭЬЭ]И—JB€™]\›‚€Y€Щ[‹њ][€И‹ЬЪYЫ[‹‹ЫШЪИ‹‹ЬЪYЫ[ШЪИџN‚€Щ[‹—ЪњЫЫЉЊВ€ЫЫ›™XЭYЋ€Ы\ЪЭИЫЫ›™XЭY—K€››Ь›X[Ћ€Ы\ЪЭИ››Ь›X[—K€›ШЪЩYЋ€Ы\ЪЭИ›ШЪЩY—K€™XЫЩ\—ЫЪИЋ€Ы\ЪЭИ™XЫЩ\—ЫЪИ—K€њ™YYЬЫЫЫ[Ы—Щ\њ›ЬњИЋ€Ы\ЪЭИњ™YYЬЫЫЫ[Ы—Щ\њ›ЬњИ—K€›\ЭЬЭШЪХ[YHЋ€Ы\ЪЭИ›\ЭЫЬЭЫШЪЧЭ[YH—K€›\ЭЭXШЩ\ЬУШЪХ[YHЋ€Ы\ЪЭИ›\ЭЬ™XЫЭ™\™YЫШЪЧЭ[YH—K€›ЬЭШЪХ[Y\ИЋ€Ы\ЪЭИ›ЬЭЫШЪЧЭ[Y\И—K€њЭXШЩ\ЬУШЪХ[Y\ИЋ€Ы\ЪЭИњ™XЫЭ™\™YЫШЪЧЭ[Y\И—K€JB€™]\›‚€Y€Щ[‹њ]OH‹ЪX[Ћ‚€Щ[‹—ЪњЫЫЉЊY€Ы\ЪЭИЫЫ›™XЭY—H[ЩHLЛВ€ЫЫ›™XЭYЋ€Ы\ЪЭИЫЫ›™XЭY—K€›\ЭЬЭ]ЧЭ[YHЋ€Ы\ЪЭИ›\ЭЬЭ]ЧЭ[YH—K€›\ЭЩ\њ›Ь€Ћ€Ы\ЪЭИ›\ЭЩ\њ›Ь€—K€JB€™]\›‚‚€Щ[‹—ЪњЫЫЉИ™\њ›Ь€Ћ€››Э›Э[™џJB‚€Y€ЩЧЫY\ЬШYЩJЩ[‹›]€Э‹
\™ЬО€[ћJHO€›Ы™N‚€™]\›‚‚€™]\›€[™\‚‚‚™Y€Э\ќЪЬЩ\ќ™\ЉЪ\™Y€Ъ\™YЭ]KЫЫ™љYО€XЭЬЭ‹[ћWJHO€Ь[Ы[Х™XY[™ТЩ\ќ™\—N‚€Y€›ЭЫЫ™љYЛ™Щ]
™[X›Y‹ќYJN‚€™]\›€›Ы™B€Y™\ЬИH
ЭЉЫЫ™љYЛ™Щ]
љ[™‹ЊLЌЛЊЊЊHЉJK[ќ
ЫЫ™љYЛ™Щ]
њЬќ‹КJJB€Щ\ќ™\€H™XY[™ТЩ\ќ™\ЉY™\ЬЛXZЩWЪ[™\ЉЪ\™YЫЫ™љYКJB€Щ\ќ™\‹™Y[[Ы—Э™XYИHќYB€™XYH™XY[™Л•™XY
\™Щ]\Щ\ќ™\‹њЩ\ќ™WЩ›Ь™]™\‹[YOHљ\Щ\ќ™\€‹Y[[ЫЏUќYJB€™XYњЭ\ќ

B€љ[ќ
€’Э]\ИTH\Э[љ[™ИЫ€ШY™\ЬЦМ_NћШY™\ЬЦМW_H‹›\ЪUќYJB€™]\›€Щ\ќ™\‚‚‚™Y€Э]ЧЫЫЬ
ЫЫ™љYО€XЭЬЭ‹[ћWKЪ\™Y€Ъ\™YЭ]K[\ќО€[\ќЫЬљЩ\ЉHO€›Ы™N‚€ШЩ™ИHЫЫ™љYЦИ™ЫЩ\Ь™XЭ€—B€Щ™ИHЫЫ™љYЦИ›ШЪИ—B€ЬЭHЭЉШЩ™ЦИљЬЭ—JB€ЬќH[ќ
ШЩ™ЦИ™XЫЩ\—ЬЬќ—JB€[Y[Э]H›Ш]
ШЩ™Л™Щ]
њЫШЪЩ]Э[Y[Э]ЬЩXЫЫ™И‹LЊ
JB€™XЫЫ›™XЭЩ[^HH›Ш]
ШЩ™Л™Щ]
њ™XЫЫ›™XЭЩ[^WЬЩXЫЫ™И‹‹Њ
JB‚€Ъ[HќYN‚€ћN‚€љ[ќ
€ђЫЫ›™XЭ[™ИИЫЩ\Ь™XЭ€XЫЩ\€Э]И]ЪЬЭNћЬЬќK‹‹€‹›\ЪUќYJB€Ъ]ЫШЪЩ]Ь™X]WШЫЫ›™XЭ[ЫЉ
ЬЭЬќ
K[Y[Э]][Y[Э]
H\ИЫШЪО‚€ЫШЪЛњЩ][Y[Э]
[Y[Э]
B€ЫШЪЛњЩ[™[
“—ТS’U
B€™\ЬЫњЩHH™XЭ—Щ^XЭ
ЫШЪЛ
B€Y€™\ЬЫњЩHOH“—Ф‘TФУ”СN‚€Z\ЩHќ[ќ[YQ\њ›ЬЉ€ќ[™^XЭY[›Ы\ЩИ[™ЪZЩH™\ЬЫњЩN€Ь™\ЬЫњЩKљ^
	И	К_HЉB‚€Ъ\™YњЩ]ШЫЫ›™XЭY
ќYJB€љ[ќ
™ЫЩ\Ь™XЭ€XЫЩ\€Э]ИЫЫ›™XЭY‹›\ЪUќYJB‚€Ъ[HќYN‚€XY\€H™XЭ—Щ^XЭ
ЫШЪЛ
B€\ЩЧЫ[€HXY\–НЧB€Y€\ЩЧЫ[€OH‚€ЫЫќ[ќYB€^[ШYH™XЭ—Щ^XЭ
ЫШЪЛ\ЩЧЫ[ЉB€^H^[ШY™XЫЩJ\ШЪZH‹\њ›ЬњПHњЭљXЭЉKњњЭљ\
—€ЉB€Э]ИHњЫЫ‹›ШYК^
B€Y€›Э\Ъ[њЭ[ЩJЭ]ЛXЭ
N‚€ЫЫќ[ќYB‚€Ъ\™Yќ\]WЬЭ]КЭ]КB€[њЪ][Ы€HЪ\™Y›ШњЩ\ќ™J™XЩ\[Ы—Ъ\ЧЫ›Ь›X[
Э]КJB€Y€[њЪ][Ы€\И›Э›Ы™N‚€Э]HH““Ф“PS‘PСTSУ€‘TХФ‘Q€Y€[њЪ][Ы‹›ШЪЩY[ЩH”‘PСTSУ€T”“Ф€UPХQ‚€љ[ќ
€ћЬЭ]_N€Э[њЪ][Ы‹ќ[Y\Э[\H‹›\ЪUќYJB€[\ќЛ™[њ]Y]YJ[њЪ][ЫЉB‚€^Щ\Щ^X›Ш\™[ќ\њќ\‚€Z\ЩB€^Щ\^Щ\[Ы€\И^О‚€Ъ\™YњЩ]ШЫЫ›™XЭY
[ЩKЭЉ^КJB€љ[ќ
€™ЫЩ\Ь™XЭ€ЫЫ›™XЭ[Ы€\њ›ЬЋ€Щ^ЯNИ™]ћZ[™И[€Ь™XЫЫ›™XЭЩ[^N™Я\И‹›\ЪUќYJB€[YKњЫY\
™XЫЫ›™XЭЩ[^JB‚‚™Y€\њЩWШ\™ЬК
HO€\™Ь\њЩK“[Y\ЬXЩN‚€\њЩ\€H\™Ь\њЩKђ\™Э[Y[ќ\њЩ\Љ\ШЬљ\[ЫЏH’XY\ЬИЫЩ\Ь™XЭ€ЪYЫ[[ШЪИ[Ыљ]Ь€[™[\ќЩ\ќљXЩHЉB€\њЩ\‹YШ\™Э[Y[ќ
‹KXЫЫ™љYИ‹\OT]Y][T]
ЫЫ™љYЛљњЫЫ€ЉK[H’”УУ€ЫЫ™љYИ]ЉB€\њЩ\‹YШ\™Э[Y[ќ
‹KXЪXЪЛXЫЫ™љYИ‹XЭ[ЫЏHњЭЬ™WЭќYH‹[Hќ[Y]HЫЫ™љYИ[™^]ЉB€™]\›€\њЩ\‹њ\њЩWШ\™ЬК
B‚‚™Y€XZ[Љ
HO€›Ы™N‚€\™ЬИH\њЩWШ\™ЬК
B€ЫЫ™љYИHШYШЫЫ™љYК\™ЬЛЫЫ™љYКB€Y€\™ЬЛЪXЪЧШЫЫ™љYО‚€љ[ќ
ђЫЫ™љYИТИЉB€™]\›‚‚€Ъ\™YHЪ\™YЭ]JЫЫ™љYЦИ›ШЪИ—JB€[\ќИH[\ќЫЬљЩ\ЉЫЫ™љYЦИ™[XZ[—JB€[\ќЛњЭ\ќ

B€HЭ\ќЪЬЩ\ќ™\ЉЪ\™YЫЫ™љYЦИљ—JB‚€ћN‚€Э]ЧЫЫЬ
ЫЫ™љYЛЪ\™Y[\ќКB€^Щ\Щ^X›Ш\™[ќ\њќ\‚€љ[ќ
”ЭЬ[™И‹›\ЪUќYJB€љ[[N‚€Y€\И›Э›Ы™N‚€њЪ]ЭЫЉ
B€њЩ\ќ™\—ШЫЬЩJ
B‚‚љY€ЧЫ[YWЧИOH—ЧЫXZ[—ЧИЋ‚€XZ[Љ
B