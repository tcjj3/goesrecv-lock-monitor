# goesrecv-lock-monitor

Headless signal-lock monitoring, HTTP status API and alert service for
[`goestools/goesrecv`](https://github.com/pietern/goestools)-style decoder telemetry.

This project originated as `goesrecv_check`, a tool used in 2023 to monitor a rooftop GK-2A
receiver. The practical problem was simple: strong wind or accidental contact could move the
rooftop antenna enough to lose lock, and an unnoticed loss of lock could create long gaps in
received weather imagery.

## What it does

The monitor connects to the goesrecv **decoder statistics publisher** (normally TCP port
`6002`), performs the same nanomsg/SP handshake used by
[`sam210723/goesrecv-monitor`](https://github.com/sam210723/goesrecv-monitor), parses the
published JSON statistics, and turns them into an unattended operational status service.

It provides:

- signal-lock / loss-of-lock state tracking;
- configurable debounce / hysteresis for marginal RF conditions;
- lock-loss and recovery history;
- optional e-mail alerts;
- alert cooldown / rate limiting;
- a small JSON HTTP API;
- automatic reconnect to the goesrecv statistics publisher.

## Why the public version differs from the 2023 operational version

The original version was usable in operation, but its lock transition logic was not smooth
enough around marginal signal conditions. When the receiver repeatedly crossed the lock
threshold, notifications could flap badly and generate excessive e-mail traffic.

The 2026 public cleanup keeps the original purpose and zero-RS-error reception criterion, but replaces the
transition logic with a small state machine:

```text
raw lock sample
      |
      +-- remains LOST for loss_confirm_seconds ------> confirmed LOST
      |
      +-- remains LOCKED for recovery_confirm_seconds -> confirmed LOCKED

confirmed transition
      |
      +-- record history
      +-- update API state
      +-- alert only if cooldown permits
```

This means a few bad or good samples near the threshold do not immediately create a new
operator alert.

## Code history

The project predates its public release.

Preserved timestamps from the original working directory / archive:

- `goesrecv_check.sh` — **2023-05-22 23:02**
- `goesrecv_daemon.sh` — **2023-05-22 23:02**
- `goesrecv_check.py` — **2023-06-19 17:04** in the original working copy shown before the
  credential-redaction edit
- `goesrecv datas.txt` — **2020-08-04 12:07**; this is an earlier protocol / handshake note,
  not the start date of the lock-monitor project

A redacted copy of `goesrecv_check.py` was modified again on **2026-10-08** only to remove
private credentials before review.

The public cleanup / hardening pass is dated **2026-10-08**.

## How the original threshold was experimentally identified

The 2023 criterion was not chosen from documentation first; it was derived from a small
controlled receive test.

The test sequence was:

1. start from the strongest / correctly aligned antenna position and record decoder stats;
2. disconnect the RF signal cable and record the no-signal state;
3. deliberately move the antenna far enough out of alignment that useful content could no
   longer be received, and record several additional samples;
4. compare the resulting decoder-stat combinations.

Representative samples preserved in the original script comments were:

| Observed receive condition | `skipped_symbols` | `viterbi_errors` | `reed_solomon_errors` | `ok` |
| --- | ---: | ---: | ---: | ---: |
| Strong / normal reception | 0 | 27 | 0 | 1 |
| Severe failure / no usable signal | 12352 | 2105 | -1 | 0 |
| Degraded / unusable reception sample | 0 | 847 | 11 | 1 |

The third row was the important discovery. It showed that `ok = 1` did not necessarily mean
"normal reception" in the operational sense needed by the unattended station.

Looking at `goestools` explains exactly why:

```cpp
rv = reedSolomon_.run(...);
details->reedSolomonBytes = rv;

// We have a lock if this packet was correctable
lock_ = (rv >= 0);
details->ok = lock_;
```

`ReedSolomon::run()` returns:

- `0` when no data bytes needed Reed-Solomon correction;
- a positive value when one or more data bytes were corrected successfully;
- `-1` when the frame was not correctable.

So the three observed regimes can be interpreted as:

```text
RS = 0,   ok = 1  -> clean / normal frame
RS > 0,   ok = 1  -> degraded but still correctable frame
RS = -1,  ok = 0  -> uncorrectable frame / decoder lock lost
```

