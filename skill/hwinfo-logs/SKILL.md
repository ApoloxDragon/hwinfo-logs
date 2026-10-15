---
name: hwinfo-logs
description: Analyze HWiNFO CSV sensor logs (temps, clocks, power, throttling, FPS) with the hwlog CLI instead of reading the raw file. Use when the user shares or mentions a HWiNFO log/.CSV or asks about thermals, throttling or performance from sensor data.
---

# HWiNFO log analysis

A HWiNFO log has 280-480 columns and 10k-30k rows (up to 30 MB). **Never `cat`, `head` or Read the raw file.**
Use the `hwlog` CLI: it prints compact summaries (~500-1500 tokens per call).

Run `hwlog ...` if installed (`pip install git+https://github.com/ApoloxDragon/hwinfo-logs`), otherwise
`python3 -m hwlog ...` from the directory containing the `hwlog/` package.

## Workflow
1. `hwlog info FILE...` - time range, interval, sampling gaps, hardware groups, **PresentMon target, WARNINGS**.
2. `hwlog summary FILE` - key sensors (min/avg/p95/max, time of max) + events + warnings. Usually enough on its own.
3. `hwlog columns FILE -c gpu [-g group]` - find exact sensor names/indexes.
4. `hwlog stats FILE -c "Tctl" -c "GPU Temperature"` - stats for chosen sensors.
5. `hwlog timeline FILE -c Tctl -c "Total CPU Usage" -b 60 [--start 21:30 --end 22:00]` - avg/max per time bucket.
6. `hwlog events FILE --temp-limit 85 [--above "GPU Utilization=98"]` - flags, over-temp episodes, gaps (one row per sensor).
7. `hwlog compare A B -c ...` - two logs. One log, two windows: `hwlog compare FILE --window-a 20:15..21:02 --window-b 21:05..21:13 -c ...`
   `--when "GPU Utilization>=90"` keeps only samples at matching load.

`-c` takes a name substring, exact name or column index (repeatable). Duplicate names get `#2`, `#3`. Windows are `START..END`
(`HH:MM[:SS]` or `+seconds`). `--json` (before the subcommand) gives machine-readable output.

## Reading results correctly (lessons learned)
- **Check `info` warnings first.** If PresentMon tracked `dwm.exe` (or any non-game process), 1% lows and frame times are
  not the game's. If FPS read 0 while GPU load was high, FPS stats are unreliable there; temps/power are fine.
  `Framerate [FPS]` comes from the GPU driver; `Framerate Presented/Displayed` and frame times come from PresentMon.
- **Compare like with like.** Same game/map, same vsync/FPS cap/fan curve, similar load, steady-state windows (skip loading
  screens and the first minutes). A different map or workload can fake a settings effect. Say so when it is not controlled.
- **Don't treat noise as a result.** One run per setting has unknown run-to-run variance; a ~4% FPS difference is noise.
- **Intel "Performance Limit - *" / "* Limit Reasons" flags** fire constantly, even at idle. Never call them throttling on
  their own; confirm high temps or dropped clocks with `timeline`/`stats`.
- **Limit-style sensors** ("GPU Thermal Limit [C]", "...Limit [%]") are settings/percent-of-limit, not readings. "Hotspot
  Thermal Limit [%]" is useful: temp / pct gives the limit (RX 7600 hotspot limit ~110 C).
- Some sensors are broken: AMD "GPU Memory Usage [MB]" can read 30,000-300,000 on an 8 GB card. Ignore it.
- A constant flag (e.g. Chassis Intrusion = Yes the whole log) is not an event. Gaps of minutes are usually sleep/suspend.
- Logs without a footer (HWiNFO still running when copied) have no sensor-group names; hardware must come from column names.
- Filename dates are unreliable; trust `info`. Timestamps are the log's local time.
- With vsync on, frame rates snap to refresh-rate steps and the GPU idles between frames; prefer temps/power for conclusions.
