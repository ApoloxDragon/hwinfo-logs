---
name: hwinfo-logs
description: Analyze HWiNFO CSV sensor logs (temperatures, clocks, power, throttling, FPS) without reading the raw file. Use when the user shares or mentions a HWiNFO log/.CSV, or asks about thermals, throttling, or performance from sensor data.
---

# HWiNFO log analysis

Never `cat`/`head` a HWiNFO log: rows have 280-310 columns and tens of thousands of lines.
Use the `hwlog` CLI (stdlib-only Python; run from the repo root as `python3 -m hwlog ...`).

Workflow:
1. `hwlog summary FILE` - key sensors (min/avg/p95/max + time of max) plus events. Start here.
2. `hwlog info FILE...` - time range, sample interval, sampling gaps, sensor groups
   (the group list names the hardware and the PresentMon-tracked process, e.g. the game).
3. `hwlog columns FILE -c gpu -g PresentMon` - find exact sensor names / indexes.
4. `hwlog stats FILE -c "Tctl" -c "GPU Temperature"` - stats for chosen sensors.
5. `hwlog timeline FILE -c Tctl -c "Total CPU Usage" -b 60 --start 21:30 --end 22:00` - avg/max per bucket.
6. `hwlog events FILE --temp-limit 85 --above "GPU Utilization=98"` - episodes over a threshold,
   raised Yes/No flags (throttling, WHEA), sampling gaps.
7. `hwlog compare A B -c Tctl -c "Package Power"` - before/after (e.g. undervolt, cooler swap).

Add `--json` (before the subcommand) for machine-readable output.

Notes:
- `-c` takes a name substring, exact name, or column index; repeat it. Duplicate sensor names are suffixed `#2`, `#3`.
- Timestamps are the log's local time; `--date-order dmy|mdy|ymd` overrides autodetection.
- A flag column that is constant for the whole log (e.g. Chassis Intrusion = Yes) is not reported as an event.
- Idle sensors (all zero, such as FPS without a game running) are hidden from `summary`.
- Logs are time-boxed by HWiNFO's logging start, so the filename date is not reliable; trust `info`.
