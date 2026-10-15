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


def is_noise(log: Log, c: Column) -> bool:
    """Sensors that are settings, not readings, or known-broken (e.g. VRAM usage reading 100k+ MB)."""
    if "Limit" in c.name and c.unit in ("\u00b0C", "W", "A"):
        return True
    if c.unit == "MB" and "Memory Usage" in c.name:
        return max((v for v in log.data[c.index] if v == v), default=0) > 64000
    return False


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
    names = [c for c in names if any(v == v and v != 0 for v in log.data[c.index])  # drop idle (all-zero) sensors
             and not is_noise(log, c)]
    shown = {c.index for c in names}
    hot = sorted((c for c in log.columns[2:] if c.unit == "\u00b0C" and c.index not in shown and "Limit" not in c.name),
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


def parse_run(spec: str) -> tuple[str, str, str | None]:
    """[LABEL=]FILE[@START..END] -> (label, file, window)."""
    label, eq, rest = spec.partition("=")
    if not eq or "/" in label or "\\" in label or "." in label:
        label, rest = "", spec
    path, at, win = rest.rpartition("@")
    if not at or ".." not in win:
        path, win = rest, ""
    return label or path.rsplit("/", 1)[-1], path, win or None


def cmd_runs(args):
    """N-way comparison: one table, one column per run (avg/max). Each run is [LABEL=]FILE[@START..END]."""
    runs = [parse_run(r) for r in args.run]
    cols = (args.column or []) + _when_cols(args.when)
    loaded = []
    for label, path, win in runs:
        log = load(path, columns=cols, regex=args.regex, date_order=args.date_order)
        loaded.append((label, log, select_rows(log, win, args.when)))
    names = [c.name for c in loaded[0][1].find(args.column, args.regex) if c.index in loaded[0][1].data]
    header = ["column"] + [f"{label} ({len(r)})" for label, _, r in loaded]
    rows, res = [], []
    for n in names:
        row, per = [n], {}
        for label, log, r in loaded:
            col = next((c for c in log.columns if c.name == n and c.index in log.data), None)
            st = col_stats(log, col, r) if col else {"n": 0}
            per[label] = st
            row.append(f"{fmt_v(st['avg'])}/{fmt_v(st['max'])}" if st.get("n") else "-")
        rows.append(row)
        res.append({"column": n, "runs": per})
    note = f"avg/max per run; (n) = samples" + (f"; filtered by {args.when}" if args.when else "")
    out(args, res, note + "\n" + table(rows, header))


def _when_cols(when: str | None) -> list[str]:
    m = re.match(r"^(.*?)\s*(?:>=|<=|>|<)", when) if when else None
    return [m.group(1)] if m else []


def cmd_hist(args):
    """Time spent per value band for each chosen sensor (default bands: 5 equal ranges)."""
    log = load(args.file, columns=args.column, regex=args.regex, date_order=args.date_order)
    rows = select_rows(log, args.window, args.when)
    dt = interval_stats(log)[0] or 1.0
    blocks, res = [], []
    for ci in log.data:
        c = log.columns[ci]
        vals = [log.data[ci][i] for i in rows if log.data[ci][i] == log.data[ci][i]]
        if not vals:
            continue
        if args.edges:
            edges = [float(x) for x in args.edges.split(",")]
        else:
            lo, hi = min(vals), max(vals)
            step = (hi - lo) / args.bins or 1.0
            edges = [lo + step * k for k in range(1, args.bins)]
        bounds = [-math.inf] + edges + [math.inf]
        counts = [0] * (len(bounds) - 1)
        for v in vals:
            for k in range(len(counts)):
                if bounds[k] <= v < bounds[k + 1]:
                    counts[k] += 1
                    break
        lines, items = [c.name], []
        for k, n in enumerate(counts):
            label = f"<{fmt_v(bounds[1])}" if k == 0 else f">={fmt_v(bounds[k])}" if k == len(counts) - 1 else f"{fmt_v(bounds[k])}-{fmt_v(bounds[k + 1])}"
            pct = 100 * n / len(vals)
            lines.append(f"  {label:>12}  {fmt_dur(n * dt):>9}  {pct:5.1f}%  " + "#" * round(pct / 4))
            items.append({"band": label, "seconds": n * dt, "pct": pct})
        blocks.append("\n".join(lines))
        res.append({"column": c.name, "bands": items})
    out(args, res, "\n\n".join(blocks))


def cmd_corr(args):
    """Which sensors move with a target sensor (Pearson r). Correlation is not causation."""
    log = load(args.file, date_order=args.date_order)
    tgt = log.find([args.target])
    if not tgt:
        raise ValueError(f"no column matches {args.target!r}")
    tgt = tgt[0]
    rows = select_rows(log, args.window, args.when)
    y = [log.data[tgt.index][i] for i in rows]
    ym = statistics.fmean(v for v in y if v == v)
    yc = [(v - ym) if v == v else 0.0 for v in y]
    yn = math.sqrt(sum(v * v for v in yc))
    if yn == 0:
        raise ValueError(f"{tgt.name} is constant over the selected samples")
    res = []
    for ci, vals in log.data.items():
        if ci == tgt.index or log.columns[ci].is_flag:
            continue
        x = [vals[i] for i in rows]
        good = [v for v in x if v == v]
        if len(good) < 0.95 * len(x) or max(good) == min(good):
            continue
        xm = statistics.fmean(good)
        xc = [(v - xm) if v == v else 0.0 for v in x]
        xn = math.sqrt(sum(v * v for v in xc))
        r = sum(a * b for a, b in zip(xc, yc)) / (xn * yn)
        if abs(r) >= 0.985 and not args.all:
            continue  # near-duplicates of the target (same sensor via another view)
        res.append((r, log.columns[ci]))
    res.sort(key=lambda t: -abs(t[0]))
    top = res[:args.top]
    text = f"correlation with {tgt.name} over {len(rows)} samples (association, not cause)\n" + table(
        [[c.name, c.unit, f"{r:+.2f}"] for r, c in top], ["sensor", "unit", "r"])
    out(args, [{"column": c.name, "r": r} for r, c in top], text)


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

    s = add("runs", cmd_runs, "N-way table: one column per run, each [LABEL=]FILE[@START..END]")
    s.add_argument("run", nargs="+"); colopts(s, True)
    s.add_argument("--when", metavar="COL>=VALUE", help="only samples where a condition holds in every run")
    s = add("hist", cmd_hist, "time spent per value band (e.g. how long above 80 C)"); s.add_argument("file"); colopts(s, True)
    s.add_argument("--bins", type=int, default=5); s.add_argument("--edges", help="comma-separated band edges, e.g. 70,80,85,90")
    s.add_argument("--window", metavar="START..END"); s.add_argument("--when", metavar="COL>=VALUE")
    s = add("corr", cmd_corr, "sensors that move with a target sensor"); s.add_argument("file"); s.add_argument("target")
    s.add_argument("--top", type=int, default=12); s.add_argument("--all", action="store_true", help="include near-duplicate sensors (|r|>=0.985)"); s.add_argument("--window", metavar="START..END"); s.add_argument("--when", metavar="COL>=VALUE")

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except (ValueError, FileNotFoundError) as e:
        print(f"hwlog: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
