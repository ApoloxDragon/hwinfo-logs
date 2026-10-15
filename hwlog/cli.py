"""hwlog command line: compact, LLM-friendly summaries of HWiNFO CSV logs."""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from datetime import datetime, timezone

from .parser import Log, Column, load, percentile

# Sensors shown by default in `summary`. Each entry lists alternative name substrings
# (AMD / Intel / NVIDIA / laptop variants); the first alternative that matches a column wins.
HIGHLIGHTS = [
    ("Total CPU Usage",), ("Core Clocks (avg)",),
    ("CPU (Tctl/Tdie)", "CPU Package [", "CPU Die (average)"),
    ("CPU Package Power", "CPU PPT"), ("IA Cores Power",),
    ("Core Temperatures (avg)",),
    ("GPU Temperature",), ("GPU Hot Spot", "GPU Memory Junction"),
    ("GPU Utilization", "GPU Core Load", "GPU D3D Usage"),
    ("GPU PPT (Sustained)", "GPU Power [W]", "GPU Power (Total)", "Total Graphics Power"),
    ("GPU Clock",), ("GPU Memory Usage", "GPU Memory Allocated"),
    ("Physical Memory Load",), ("Framerate [FPS]", "Framerate Presented (avg)"),
    ("Frame Time Presented (avg)",), ("Drive Temperature",),
    ("Battery", "Charge Level", "Charge Rate"),
]
EXTRA_HOTTEST = 3  # also show the hottest N temperature sensors not already listed
DEFAULT_LIMITS = {"°C": 90.0}


def fmt_t(t: float, short: bool = False) -> str:
    d = datetime.fromtimestamp(t, tz=timezone.utc)
    return d.strftime("%H:%M:%S" if short else "%Y-%m-%d %H:%M:%S")


def fmt_dur(s: float) -> str:
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h{m:02d}m{sec:02d}s" if h else f"{m}m{sec:02d}s" if m else f"{sec}s"


def fmt_v(v: float) -> str:
    if v != v:
        return "-"
    if abs(v) >= 1000:
        return f"{v:.0f}"
    return f"{v:.1f}" if abs(v) >= 10 else f"{v:.3g}" if v else "0"


def table(rows: list[list[str]], header: list[str]) -> str:
    rows = [header] + rows
    w = [max(len(r[i]) for r in rows) for i in range(len(header))]
    lines = ["  ".join(c.ljust(w[i]) if i == 0 else c.rjust(w[i]) for i, c in enumerate(r)) for r in rows]
    return "\n".join(lines)


def out(args, obj, text: str):
    print(json.dumps(obj, indent=1, default=lambda x: None) if args.json else text)


def interval_stats(log: Log):
    t = log.times
    d = [b - a for a, b in zip(t, t[1:])]
    if not d:
        return 0.0, []
    med = statistics.median(d)
    gaps = [(t[i], d[i]) for i in range(len(d)) if d[i] > max(3 * med, med + 5)]
    return med, gaps


def col_stats(log: Log, c: Column, rows: list[int] | None = None) -> dict:
    """Stats for a column, optionally restricted to the given row indexes."""
    vals = log.data[c.index]
    rows = range(len(vals)) if rows is None else rows
    clean = sorted(vals[i] for i in rows if vals[i] == vals[i])
    if not clean:
        return {"column": c.name, "n": 0}
    imax = max(rows, key=lambda i: vals[i] if vals[i] == vals[i] else -math.inf)
    return {
        "column": c.name, "n": len(clean), "min": clean[0], "avg": sum(clean) / len(clean),
        "p50": percentile(clean, .5), "p95": percentile(clean, .95), "p99": percentile(clean, .99),
        "max": clean[-1], "max_at": fmt_t(log.times[imax], True),
    }


def stats_table(log: Log, cols: list[Column]) -> tuple[list[dict], str]:
    st = [col_stats(log, c) for c in cols]
    rows = [[s["column"], fmt_v(s.get("min", math.nan)), fmt_v(s.get("avg", math.nan)),
             fmt_v(s.get("p95", math.nan)), fmt_v(s.get("max", math.nan)), s.get("max_at", "-")] for s in st]
    return st, table(rows, ["column", "min", "avg", "p95", "max", "max@"])


def episodes(times, vals, pred, min_gap=5.0):
    """Contiguous runs where pred(v) is true -> (start, end, peak_value, peak_time)."""
    res, cur = [], None
    for t, v in zip(times, vals):
        if v == v and pred(v):
            if cur and t - cur[1] <= min_gap:
                cur[1] = t
                if (v > cur[2]) if cur[4] else (v < cur[2]):
                    cur[2], cur[3] = v, t
            else:
                if cur:
                    res.append(cur)
                cur = [t, t, v, t, True]
        elif cur and t - cur[1] > min_gap:
            res.append(cur)
            cur = None
    if cur:
        res.append(cur)
    return [(a, b, p, pt) for a, b, p, pt, _ in res]


# ---- diagnostics ------------------------------------------------------------

NON_GAME_PROCESSES = {"dwm.exe", "explorer.exe", "steamwebhelper.exe", "msedge.exe", "chrome.exe", "firefox.exe"}
FPS_COLUMNS = ["Framerate [FPS]", "Framerate Presented (avg)"]
GPU_LOAD_COLUMNS = ["GPU Utilization", "GPU Core Load", "GPU D3D Usage"]
DIAG_COLUMNS = FPS_COLUMNS + GPU_LOAD_COLUMNS


def presentmon_target(log: Log) -> str | None:
    for c in log.columns:
        m = re.match(r"PresentMon \[(.+)\]", c.group)
        if m:
            return m.group(1)
    return None


def _first(log: Log, names: list[str]) -> Column | None:
    for n in names:
        hit = [c for c in log.find([n]) if c.index in log.data]
        if hit:
            return hit[0]
    return None


def log_warnings(log: Log) -> list[str]:
    """Problems that make FPS / frame-time numbers misleading. Needs the FPS and GPU load columns loaded."""
    out = []
    target = presentmon_target(log)
    if target and target.lower() in NON_GAME_PROCESSES:
        out.append(f"PresentMon was tracking {target}, not a game: 1% lows and frame times describe that process, "
                   "not the game. Select the game's exe in HWiNFO's PresentMon settings before logging.")
    fps, load = _first(log, FPS_COLUMNS), _first(log, GPU_LOAD_COLUMNS)
    if fps and load:
        busy_zero = [1.0 if (f == 0 and l >= 80) else 0.0
                     for f, l in zip(log.data[fps.index], log.data[load.index])]
        eps = [e for e in episodes(log.times, busy_zero, lambda v: v >= 1) if e[1] - e[0] >= 30]
        if eps:
            total = sum(b - a for a, b, _, _ in eps)
            out.append(f"{fps.name} read 0 while GPU load was >=80% for {fmt_dur(total)} total "
                       f"({len(eps)} stretch{'es' if len(eps) > 1 else ''}, first {fmt_t(eps[0][0], True)}-{fmt_t(eps[0][1], True)}): "
                       "sensor dropout, so FPS/frame-time stats are unreliable there. Temperatures and power are unaffected.")
    return out


# ---- commands -------------------------------------------------------------

def cmd_info(args):
    for path in args.files:
        log = load(path, columns=DIAG_COLUMNS, date_order=args.date_order)
        med, gaps = interval_stats(log)
        warns = log_warnings(log)
        groups: dict[str, int] = {}
        for c in log.columns[2:]:
            groups[c.group or "(unknown)"] = groups.get(c.group or "(unknown)", 0) + 1
        info = {
            "file": path, "rows": log.rows, "columns": len(log.columns) - 2,
            "start": fmt_t(log.times[0]) if log.times else None,
            "end": fmt_t(log.times[-1]) if log.times else None,
            "duration_s": (log.times[-1] - log.times[0]) if log.times else 0,
            "interval_s": med, "gaps": [(fmt_t(t, True), g) for t, g in gaps], "groups": groups,
            "presentmon_target": presentmon_target(log), "warnings": warns,
        }
        lines = [f"{path}", f"  {info['start']} -> {info['end']}  ({fmt_dur(info['duration_s'])}, {log.rows} rows, ~{med:g}s interval, {info['columns']} sensors)"]
        if gaps:
            lines.append("  gaps: " + ", ".join(f"{fmt_t(t, True)} (+{g:.0f}s)" for t, g in gaps[:8]) + (" ..." if len(gaps) > 8 else ""))
        if set(groups) == {"(unknown)"}:
            lines.append(f"  sensor groups: unavailable ({groups['(unknown)']} sensors; file has no footer, so HWiNFO was likely still logging when it was copied)")
        else:
            lines.append("  sensor groups:")
            lines += [f"    {n:4d}  {g}" for g, n in groups.items()]
        if info["presentmon_target"]:
            lines.append(f"  PresentMon target: {info['presentmon_target']}")
        lines += [f"  WARNING: {w}" for w in warns]
        out(args, info, "\n".join(lines))


def cmd_columns(args):
    log = load(args.file, columns=["__none__"], date_order=args.date_order)
    cols = log.find(args.column, args.regex)[2 if not args.column else 0:]
    if args.group:
        cols = [c for c in cols if args.group.lower() in c.group.lower()]
    rows = [[str(c.index), c.name, c.unit, c.group] for c in cols]
    out(args, [vars(c) for c in cols], table(rows, ["idx", "name", "unit", "group"]))


def cmd_stats(args):
    log = load(args.file, columns=args.column, regex=args.regex, date_order=args.date_order)
    cols = [log.columns[i] for i in log.data]
    st, text = stats_table(log, cols)
    out(args, st, f"{args.file}  {fmt_t(log.times[0])} -> {fmt_t(log.times[-1])}\n{text}")


def cmd_summary(args):
    log = load(args.file, date_order=args.date_order)
    names = []
    for alts in HIGHLIGHTS:
        for a in alts:
            hit = log.find([a])
            if hit:
                names.append(hit[0])
                break
    med, gaps = interval_stats(log)
    head = f"{args.file}\n{fmt_t(log.times[0])} -> {fmt_t(log.times[-1])}  ({fmt_dur(log.times[-1] - log.times[0])}, {log.rows} rows, ~{med:g}s)"
    names = [c for c in names if any(v == v and v != 0 for v in log.data[c.index])]  # drop idle (all-zero) sensors
    shown = {c.index for c in names}
    hot = sorted((c for c in log.columns[2:] if c.unit == "\u00b0C" and c.index not in shown),
                 key=lambda c: -max((v for v in log.data[c.index] if v == v), default=-math.inf))
    names += hot[:EXTRA_HOTTEST]
    st, text = stats_table(log, names)
    ev = find_events(log, args)
    warns = log_warnings(log)
    wtext = "".join(f"WARNING: {w}\n" for w in warns)
    out(args, {"stats": st, "events": ev, "warnings": warns}, f"{head}\n{wtext}{text}\n\n{render_events(ev, 12)}")


def _aggregate(kind, col, eps):
    """Collapse a sensor's episodes into one row: count, total/longest duration, first start, peak."""
    row = {"kind": kind, "column": col.name, "episodes": len(eps), "total_s": sum(b - a for a, b, _, _ in eps),
           "longest_s": max(b - a for a, b, _, _ in eps), "first": fmt_t(eps[0][0], True)}
    if not col.is_flag:  # a flag's "peak" is always 1
        peak = max(eps, key=lambda e: e[2])
        row.update(peak=peak[2], peak_at=fmt_t(peak[3], True))
    return row


def find_events(log: Log, args) -> list[dict]:
    ev = []
    for c in log.columns[2:]:
        vals = log.data[c.index]
        if c.is_flag:
            on = sum(1 for v in vals if v == 1)
            if on == 0 or on == len(vals):
                continue  # never set, or constant (e.g. Chassis Intrusion): not an event
            ev.append(_aggregate("flag", c, episodes(log.times, vals, lambda v: v >= 1)))
        elif c.unit == "\u00b0C":
            lim = args.temp_limit
            eps = episodes(log.times, vals, lambda v: v >= lim)
            if eps:
                ev.append(_aggregate(f"temp>={lim:g}", c, eps))
    ev.sort(key=lambda e: -e["total_s"])
    for t, g in interval_stats(log)[1]:
        ev.append({"kind": "gap", "column": "(sampling)", "episodes": 1, "total_s": g, "longest_s": g,
                   "first": fmt_t(t, True)})
    return ev


def render_events(ev: list[dict], limit: int = 25) -> str:
    if not ev:
        return "events: none (no flags raised, no temps over limit, no gaps)"
    rows = [[e["kind"], e["column"], str(e["episodes"]), fmt_dur(e["total_s"]), fmt_dur(e["longest_s"]),
             e["first"], f"{fmt_v(e['peak'])}@{e['peak_at']}" if "peak" in e else ""] for e in ev[:limit]]
    more = f"\n... {len(ev) - limit} more (use --json or --limit)" if len(ev) > limit else ""
    return "events (one row per sensor):\n" + table(rows, ["kind", "column", "n", "total", "longest", "first", "peak"]) + more


def cmd_events(args):
    log = load(args.file, date_order=args.date_order)
    ev = find_events(log, args)
    for spec in args.above or []:
        name, _, thr = spec.rpartition("=")
        for c in log.find([name]):
            eps = episodes(log.times, log.data[c.index], lambda v, t=float(thr): v >= t)
            if eps:
                ev.append(_aggregate(f">={thr}", c, eps))
    out(args, ev, render_events(ev, args.limit))


def cmd_timeline(args):
    log = load(args.file, columns=args.column, regex=args.regex, date_order=args.date_order)
    cols = [log.columns[i] for i in log.data]
    t0 = log.times[0]
    lo, hi = parse_window(f"{args.start or ''}..{args.end or ''}", log)
    b = args.bucket
    buckets: dict[int, list[int]] = {}
    for i, t in enumerate(log.times):
        if lo <= t <= hi:
            buckets.setdefault(int((t - lo) // b), []).append(i)
    rows = []
    for k in sorted(buckets):
        idx = buckets[k]
        row = [fmt_t(lo + k * b, True)]
        for c in cols:
            v = [log.data[c.index][i] for i in idx if log.data[c.index][i] == log.data[c.index][i]]
            row.append(f"{fmt_v(sum(v) / len(v))}/{fmt_v(max(v))}" if v and not args.avg else fmt_v(sum(v) / len(v)) if v else "-")
        rows.append(row)
    suffix = "" if args.avg else " (avg/max)"
    header = ["time"] + [c.name for c in cols]
    out(args, {"header": header, "rows": rows}, f"bucket={b:g}s{suffix}\n" + table(rows, header))


def _parse_offset(s: str, log: Log) -> float:
    """HH:MM[:SS] clock time on the log's first day (rolling over midnight), or +N seconds."""
    if s.startswith("+"):
        return log.times[0] + float(s[1:])
    parts = [float(x) for x in s.split(":")]
    sec = parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) > 2 else 0)
    day0 = log.times[0] - log.times[0] % 86400
    t = day0 + sec
    # a clock time before the log starts means the next day, but only if the log actually runs that long
    return t + 86400 if t < log.times[0] and t + 86400 <= log.times[-1] else t


def parse_window(spec: str, log: Log) -> tuple[float, float]:
    """'START..END' where each end is HH:MM[:SS] or +seconds from log start; either may be empty."""
    lo, sep, hi = spec.partition("..")
    if not sep:
        raise ValueError(f"window {spec!r} must look like START..END, e.g. 20:15..21:02 or +0..+600")
    start = _parse_offset(lo, log) if lo else log.times[0]
    end = _parse_offset(hi, log) if hi else log.times[-1]
    return start, end + 86400 if end < start else end  # an end clock time before the start is the next day


def select_rows(log: Log, window: str | None, when: str | None) -> list[int]:
    lo, hi = parse_window(window, log) if window else (log.times[0], log.times[-1])
    rows = [i for i, t in enumerate(log.times) if lo <= t <= hi]
    if when:
        m = re.match(r"^(.*?)\s*(>=|<=|>|<)\s*(-?[\d.]+)$", when)
        if not m:
            raise ValueError(f"--when {when!r} must look like 'GPU Utilization>=90'")
        cols = log.find([m.group(1)])
        if not cols:
            raise ValueError(f"--when: no column matches {m.group(1)!r}")
        vals, thr, op = log.data[cols[0].index], float(m.group(3)), m.group(2)
        test = {">=": lambda v: v >= thr, "<=": lambda v: v <= thr, ">": lambda v: v > thr, "<": lambda v: v < thr}[op]
        rows = [i for i in rows if vals[i] == vals[i] and test(vals[i])]
    return rows


def cmd_compare(args):
    """Compare two logs, or two time windows of one log (omit B). --window-a/--window-b trim either side;
    --when keeps only samples where a condition holds (e.g. matching GPU load)."""
    if not args.b and not (args.window_a and args.window_b):
        raise ValueError("compare needs a second log, or both --window-a and --window-b to compare windows of one log")
    cols = (args.column or []) + ([re.match(r"^(.*?)\s*(?:>=|<=|>|<)", args.when).group(1)] if args.when and re.match(r"^(.*?)\s*(?:>=|<=|>|<)", args.when) else [])
    a = load(args.a, columns=cols, regex=args.regex, date_order=args.date_order)
    b = load(args.b, columns=cols, regex=args.regex, date_order=args.date_order) if args.b else a
    ra, rb = select_rows(a, args.window_a, args.when), select_rows(b, args.window_b, args.when)
    wanted = {c.name for c in a.find(args.column, args.regex)}
    bmap = {c.name: c for c in b.columns if c.index in b.data}
    rows, res = [], []
    for ca in (a.columns[i] for i in a.data):
        cb = bmap.get(ca.name)
        if ca.name not in wanted or not cb:
            continue
        sa, sb = col_stats(a, ca, ra), col_stats(b, cb, rb)
        if not sa["n"] or not sb["n"]:
            continue
        d = sb["avg"] - sa["avg"]
        res.append({"column": ca.name, "a": sa, "b": sb, "avg_delta": d})
        rows.append([ca.name, fmt_v(sa["avg"]), fmt_v(sb["avg"]), f"{d:+.3g}", fmt_v(sa["max"]), fmt_v(sb["max"])])

    def label(name, log, w, r):
        span = f"{fmt_t(log.times[r[0]], True)}-{fmt_t(log.times[r[-1]], True)}" if r else "empty"
        return f"{name}: {args.b if (name == 'B' and args.b) else args.a}  window={w or 'all'}  {len(r)} samples ({span})"

    note = f"\nsamples filtered by: {args.when}" if args.when else ""
    out(args, res, f"{label('A', a, args.window_a, ra)}\n{label('B', b, args.window_b, rb)}{note}\n"
        + table(rows, ["column", "avgA", "avgB", "d(avg)", "maxA", "maxB"]))


def main(argv=None):
    p = argparse.ArgumentParser(prog="hwlog", description=__doc__)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--date-order", choices=["dmy", "mdy", "ymd"], help="override date format autodetection")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help, files=False):
        s = sub.add_parser(name, help=help)
        s.set_defaults(fn=fn)
        if files:
            s.add_argument("files", nargs="+")
        return s

    def colopts(s, required=False):
        s.add_argument("-c", "--column", action="append", required=required, help="name substring, exact name, or index; repeatable")
        s.add_argument("--regex", action="store_true", help="treat -c as regular expressions")

    add("info", cmd_info, "overview of one or more logs", files=True)
    s = add("columns", cmd_columns, "list sensor columns"); s.add_argument("file"); colopts(s)
    s.add_argument("-g", "--group", help="filter by sensor group substring")
    s = add("stats", cmd_stats, "min/avg/p95/max for chosen columns"); s.add_argument("file"); colopts(s, True)
    s = add("summary", cmd_summary, "key sensors plus events in one call"); s.add_argument("file")
    s.add_argument("--temp-limit", type=float, default=DEFAULT_LIMITS["°C"], help="°C threshold for events (default 90)")
    s = add("events", cmd_events, "flags raised, over-temperature episodes, sampling gaps"); s.add_argument("file")
    s.add_argument("--temp-limit", type=float, default=DEFAULT_LIMITS["°C"])
    s.add_argument("--above", action="append", metavar="COLUMN=VALUE", help="extra episodes where a column >= value")
    s.add_argument("--limit", type=int, default=25, help="max rows to print (default 25)")
    s = add("timeline", cmd_timeline, "time-bucketed avg/max for chosen columns"); s.add_argument("file"); colopts(s, True)
    s.add_argument("-b", "--bucket", type=float, default=60, help="bucket size in seconds (default 60)")
    s.add_argument("--start", help="HH:MM[:SS] or +seconds from log start"); s.add_argument("--end")
    s.add_argument("--avg", action="store_true", help="show only the average")
    s = add("compare", cmd_compare, "compare two logs, or two time windows of one log"); s.add_argument("a"); s.add_argument("b", nargs="?", help="second log; omit to compare two windows of A"); colopts(s, True)
    s.add_argument("--window-a", metavar="START..END", help="restrict A, e.g. 20:15..21:02 or +0..+600")
    s.add_argument("--window-b", metavar="START..END", help="restrict B (or the second window when B is omitted)")
    s.add_argument("--when", metavar="COL>=VALUE", help="only samples where a condition holds on each side, e.g. 'GPU Utilization>=90'")

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except (ValueError, FileNotFoundError) as e:
        print(f"hwlog: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
