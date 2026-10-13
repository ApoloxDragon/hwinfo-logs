# hwlog

Fast, compact summaries of [HWiNFO](https://www.hwinfo.com/) CSV sensor logs, built to be used by an LLM
as a skill (see `skill/SKILL.md`) instead of reading 300-column raw rows. Standard library only.

    python3 -m hwlog summary log.CSV
    python3 -m hwlog timeline log.CSV -c Tctl -c "GPU Temperature" -b 60
    python3 -m hwlog events log.CSV --temp-limit 85
    python3 -m hwlog compare before.CSV after.CSV -c "Package Power"

Handles cp1252 headers, unpadded `d.m.yyyy` dates, duplicate sensor names, the repeated-header
and sensor-group footer, and `Yes`/`No` flag columns. A 30 MB / 21k-row log loads in about 3 s.

Tests: `python3 -m unittest discover -s tests -t .`
