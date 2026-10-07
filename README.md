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

This made the original monitoring goal clearer: it was not merely trying to duplicate
goesrecv's decoder-lock flag. It was intended as an **early operational warning** for a
rooftop antenna system, where the useful action was to notice degradation before a long run
of weather imagery was lost.

That is why the historical monitor used the stricter condition:

```text
reed_solomon_errors == 0  -> Normal
reed_solomon_errors != 0  -> Error / degraded
```

The 2026 hardening keeps this historical sensitivity but adds dwell-time / hysteresis and
alert cooldown so a marginal signal hovering near the threshold does not create a mail storm.

## Why Viterbi errors can be high while reception is still normal

The source code explains an important result from the original antenna-offset experiments:
`viterbi_errors` and `reed_solomon_errors` are measuring different stages of a concatenated
error-correction chain.

In `goestools`, `Viterbi::compareSoft()` does **not** count residual payload errors after
decoding. It re-encodes the Viterbi decoder's selected output and compares the resulting hard
bits against the sign / MSB of the received soft symbols. In other words, the value is closer
to a disagreement count between the noisy received convolutional-code stream and the codeword
selected by the Viterbi decoder.

That means a large `viterbi_errors` value can coexist with a perfectly recovered frame: the
inner Viterbi decoder may be correcting a large amount of channel corruption while still
producing the right bytes for the next stage.

The packetizer then de-randomizes the decoded frame and runs the outer CCSDS Reed-Solomon
decoder. `goestools` processes four interleaved 255-byte RS blocks (223 data + 32 parity per
block). Its Reed-Solomon wrapper returns:

```text
0    -> no data-byte correction was required
> 0  -> one or more data bytes were corrected successfully
-1   -> at least one RS block was not correctable
```

The packetizer then defines its lock flag as:

```cpp
lock_ = (rv >= 0);
details->ok = lock_;
```

So the experimental progression when slowly moving the antenna off-axis is expected to look
roughly like this:

```text
good RF margin
    -> low/moderate Viterbi disagreement
    -> Viterbi fully reconstructs the frame
    -> RS = 0
    -> normal reception

lower RF margin
    -> Viterbi disagreement can become quite large
    -> Viterbi still fully reconstructs the frame
    -> RS = 0
    -> reception can still remain normal

near the FEC cliff
    -> Viterbi occasionally leaves residual byte errors
    -> RS starts correcting them
    -> RS > 0, ok may still be 1
    -> decoder still has a valid frame, but very little margin remains

past the cliff
    -> residual errors exceed the outer RS capability in a block
    -> RS = -1
    -> ok = 0
    -> frame is not published as valid
```

This also explains an apparent contradiction in the 2023 observations: a sample with
`reed_solomon_errors = 11, ok = 1` is not itself a corrupt output frame. That particular frame
was still successfully repaired by Reed-Solomon. The practical problem is that once the link
has degraded enough for non-zero RS correction to appear, nearby frames can rapidly alternate
between "correctable" and "uncorrectable" as RF conditions fluctuate. For an image / file
receive chain, losing only some frames is already enough to make sustained content reception
nearly unusable.

So the original `reed_solomon_errors == 0` threshold was conservative, but it had a strong
empirical meaning: it detected the point where errors had begun to escape the inner Viterbi
decoder and reach the outer code, rather than waiting until the decoder's binary `ok` flag
finally dropped.

### Interpreting the preserved samples

For the Viterbi implementation used here, `compareSoft()` compares roughly one convolutionally
encoded frame's worth of bits. The exact count is an implementation metric, not a BER
measurement, but the preserved values illustrate the trend well:

```text
viterbi_errors =   27, RS =  0, ok = 1  -> strong / clean baseline
viterbi_errors =  847, RS = 11, ok = 1  -> degraded, still correctable
viterbi_errors = 2105, RS = -1, ok = 0  -> uncorrectable failure
```

The important experimental observation was broader than those three samples: during repeated
slow antenna misalignment tests, `viterbi_errors` could become large while
`reed_solomon_errors` remained zero and content reception was still normal. Once
`reed_solomon_errors` became non-zero, useful reception was usually already close to failure.

## How this criterion was derived from goesrecv-monitor

The controlled receive test established the stricter reception-health rule. [`sam210723/goesrecv-monitor`](https://github.com/sam210723/goesrecv-monitor) then made the distinction visible in practice: its lock indicator and its Reed-Solomon statistic are displayed separately.

In `goesrecv-monitor` v1.3, the program treats the two indicators separately:

- the red/green `LOCKED` / `UNLOCKED` display comes directly from the decoder JSON `ok` field;
- the Reed-Solomon correction count is read independently from `reed_solomon_errors` and is
  displayed / plotted as a separate statistic;
- the large-view background colour is also driven by `ok`, while the RS count remains visible.

This means a real decoder sample such as:

```text
reed_solomon_errors = 11
ok = 1
```

appears as **LOCKED** in `goesrecv-monitor`, but at the same time visibly shows non-zero
Reed-Solomon correction activity.

That behavior provided the practical clue behind the original 2023 monitor: for unattended
operation, "the decoder still has lock" was not the same thing as "reception is clean enough
to treat as normal". The monitor therefore used the stricter operational criterion
`reed_solomon_errors == 0`.

This distinction is consistent with `goestools` itself: `ok` is true when the Reed-Solomon
result is correctable (`rv >= 0`), whereas `reed_solomon_errors` reports how many bytes had to
be corrected and uses `-1` for an uncorrectable packet.

So the relationship is:

```text
upstream decoder / goesrecv-monitor:
    ok == 1
        -> packet remains correctable / decoder lock is retained

original goesrecv_check operational rule:
    reed_solomon_errors == 0
        -> reception treated as Normal
    reed_solomon_errors != 0
        -> reception treated as Error / degraded
```

The public release keeps that historical distinction instead of collapsing both concepts
into a single `ok` flag.

## Original reception criterion retained

The comments at the top of the original 2023 script preserve three real decoder-stat examples:

```text
Normal:
reed_solomon_errors = 0,   ok = 1

Error:
reed_solomon_errors = -1,  ok = 0

Error:
reed_solomon_errors = 11,  ok = 1
```

That third sample is important. The original monitor was **not** simply mirroring goesrecv's
`ok` field.

In `goestools`, `reed_solomon_errors` is the number of bytes corrected by Reed-Solomon, and
`-1` means the packet was not correctable. The decoder sets its own `ok` / lock flag when the
Reed-Solomon result is `>= 0`. Therefore a packet can legitimately contain:

```text
reed_solomon_errors = 11
ok = 1
```

meaning that the decoder still considers the packet correctable / locked, while the original
monitor considers the reception abnormal because Reed-Solomon correction was already needed.

For this project, the historical operational rule is therefore fixed as:

```text
reed_solomon_errors == 0   -> Normal reception
reed_solomon_errors != 0   -> Error / degraded reception
```

The original comment says `Error(reed_solomon_errors>0)`, but one of the preserved error
samples is `-1`; the public version therefore formalizes the actual observed behavior as
**non-zero**, not merely `> 0`.

This is intentionally stricter than upstream `ok`. The public release no longer presents
`ok` as an equivalent selectable criterion, because doing so would miss the
`reed_solomon_errors = 11, ok = 1` case that the original tool explicitly classified as an
error.

The HTTP status response still exposes both the confirmed monitor state and the raw upstream
diagnostic values (`ok` and `reed_solomon_errors`) so users can see the distinction directly.

## Protocol handling

The decoder statistics connection uses the same 8-byte nanomsg/SP handshake visible in
`goesrecv-monitor`:

```text
client -> server: 00 53 50 00 00 21 00 00
server -> client: 00 53 50 00 00 20 00 00
```

Each statistics message is then read as an 8-byte nanomsg header followed by the exact message
length declared in the header. The payload is newline-terminated JSON, for example fields such
as:

- `timestamp`
- `skipped_symbols`
- `viterbi_errors`
- `reed_solomon_errors`
- `ok`

The public version uses exact-length socket reads rather than assuming one `recv()` call
contains one complete message.

## Configuration

Copy the example:

```bash
cp config.example.json config.json
```

Important defaults:

```json
{
  "goesrecv": {
    "host": "127.0.0.1",
    "decoder_port": 6002
  },
  "lock": {
    "loss_confirm_seconds": 5,
    "recovery_confirm_seconds": 5,
    "alert_cooldown_seconds": 60
  },
  "http": {
    "bind": "127.0.0.1",
    "port": 8083
  }
}
```

### E-mail credentials

No SMTP password is stored in the repository or JSON config. Put it in the environment
variable named by `email.password_env`:

```bash
export GOESRECV_MONITOR_SMTP_PASSWORD='your-password-or-app-password'
```

Then set `email.enabled` to `true` and fill in your SMTP username, sender and receivers in
`config.json`.

## Run

Python 3.9+; no third-party Python packages are required.

```bash
python3 goesrecv_lock_monitor.py --config config.json
```

Validate configuration only:

```bash
python3 goesrecv_lock_monitor.py --config config.json --check-config
```

## HTTP API

The API binds to `127.0.0.1` by default. Do not expose it publicly unless you intentionally
configure network access and understand the security implications.

Endpoints:

- `/goesrecv` — latest raw decoder statistics JSON
- `/signal`, `/lock`, `/signallock` — confirmed operational reception state and transition history; also includes raw upstream `ok` and `reed_solomon_errors` diagnostics
- `/health` — goesrecv statistics-connection health

Example `/signal` response:

```json
{
  "connected": true,
  "normal": true,
  "locked": true,
  "decoder_ok": 1,
  "reed_solomon_errors": 0,
  "lastLostLockTime": "2026-10-08T01:00:00+08:00",
  "lastSuccessLockTime": "2026-10-08T01:01:00+08:00",
  "lostLockTimes": [],
  "successLockTimes": []
}
```

## systemd

An example unit is included in `systemd/goesrecv-lock-monitor.service`.

Install to a suitable directory, create your private `config.json`, provide the SMTP password
through an `EnvironmentFile` if e-mail is enabled, and adjust the paths in the unit before
starting it.

This replaces the original one-second shell polling watchdog with the operating system's
native service restart mechanism.

## About alert flapping

The public defaults require five continuous seconds of the candidate state before confirming
loss or recovery, plus a 60-second same-state alert cooldown. Both are configurable.

That solves the specific failure mode that prevented the original project from being released:
rapid lock / unlock oscillation near the RF threshold should no longer be able to generate a
notification storm.

This is still a monitoring aid, not a safety-critical alarm system. Thresholds should be tuned
for the actual receiver and signal environment.

## Relationship to upstream projects

- [`sam210723/goestools`](https://github.com/sam210723/goestools) — goesrecv statistics source
- [`sam210723/goesrecv-monitor`](https://github.com/sam210723/goesrecv-monitor) — reference
  implementation for the statistics connection / nanomsg handshake

No upstream source code is copied into this repository; the monitor communicates with the
published statistics endpoint.

## License

MIT License. See [LICENSE](LICENSE).
