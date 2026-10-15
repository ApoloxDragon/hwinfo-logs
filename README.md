# hwlog

Fast, compact summaries of [HWiNFO](https://www.hwinfo.com/) CSV sensor logs, built to be used by an LLM
as a skill (see `skill/SKILL.md`) instead of reading 300-column raw rows. Standard library only.

    python3 -m hwlog summary log.CSV
    python3 -m hwlog timeline log.CSV -c Tctl -c "GPU Temperature" -b 60
    python3 -m hwlog events log.CSV --temp-limit 85
    python3 -m hwlog compare before.CSV after.CSV -c "Package Power"
    python3 -m hwlog compare log.CSV --window-a 20:15..21:02 --window-b 21:05..21:13 -c "Hot Spot" --when "GPU Utilization>=90"

    python3 -m hwlog runs "60%=a.CSV@22:00..23:10" "90%=b.CSV@20:15..21:02" -c "Hot Spot"
    python3 -m hwlog hist log.CSV -c "Hot Spot" --edges 70,80,85,90
    python3 -m hwlog corr log.CSV "GPU Hot Spot Temperature"

Handles cp1252 headers, unpadded `d.m.yyyy` dates, duplicate sensor names, the repeated-header
and sensor-group footer, and `Yes`/`No` flag columns. A 30 MB / 21k-row log loads in about 3 s.

Install: `pip install .` gives an `hwlog` command. The skill lives in `skill/hwinfo-logs/SKILL.md`.
`info` and `summary` warn when PresentMon tracked a non-game process (e.g. `dwm.exe`) or the FPS sensor read 0 under load.

Tests: `python3 -m unittest discover -s tests -t .`
