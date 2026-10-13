"""Parser for HWiNFO CSV sensor logs.

Quirks handled:
  * cp1252 encoding (the degree sign), with utf-8 fallback
  * unpadded dates like ``9.10.2026`` and ``H:M:S.mmm`` times
  * duplicate column names (disambiguated as ``name#2``)
  * footer: a repeated header row plus a sensor-group row whose group names can
    contain unquoted commas (``[C:, D:]``), which breaks column alignment
  * ``Yes``/``No`` columns (mapped to 1/0) and empty cells (NaN)
"""
from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

NAN = float("nan")
_DATE_RE = re.compile(r"^\d{1,4}[./-]\d{1,2}[./-]\d{1,4}$")
_UNIT_RE = re.compile(r"\[([^\[\]]*)\]\s*$")


def open_text(path: str):
    with open(path, "rb") as fh:
        head = fh.read(65536)
    if head.startswith(b"\xef\xbb\xbf"):
        enc = "utf-8-sig"
    else:
        try:
            head.decode("utf-8")
            enc = "utf-8"
        except UnicodeDecodeError as e:
            # a multibyte char cut at the 64 KiB boundary is still utf-8
            enc = "utf-8" if e.start >= len(head) - 3 else "cp1252"
    return open(path, encoding=enc, errors="replace", newline="")


def _to_float(v: str) -> float:
    if v == "No":
        return 0.0
    if v == "Yes":
        return 1.0
    try:
        return float(v)
    except ValueError:
        return NAN


@dataclass
class Column:
    index: int
    name: str  # unique display name
    raw: str
    unit: str
    group: str = ""
    is_flag: bool = False


@dataclass
class Log:
    path: str
    columns: list[Column]
    times: list[float]  # epoch seconds, treating timestamps as naive/UTC
    data: dict[int, list[float]] = field(default_factory=dict)
    rows: int = 0

    def find(self, patterns: list[str] | None, regex: bool = False) -> list[Column]:
        """Columns whose name (or group) matches any pattern; case-insensitive.
        A pattern that is an integer selects that column index; an exact unique
        name wins over substring matches."""
        cols = self.columns[:]
        if not patterns:
            return cols
        out: list[Column] = []
        seen = set()
        for p in patterns:
            hits: list[Column] = []
            if p.isdigit() and int(p) < len(cols):
                hits = [cols[int(p)]]
            else:
                exact = [c for c in cols if c.name.lower() == p.lower()]
                if exact:
                    hits = exact
                elif regex:
                    rx = re.compile(p, re.I)
                    hits = [c for c in cols if rx.search(c.name)]
                else:
                    pl = p.lower()
                    hits = [c for c in cols if pl in c.name.lower()]
            for c in hits:
                if c.index not in seen:
                    seen.add(c.index)
                    out.append(c)
        return out


def _detect_order(sample: list[str]) -> str:
    sep = "." if "." in sample[0] else "/" if "/" in sample[0] else "-"
    parts = [s.split(sep) for s in sample]
    if len(parts[0][0]) == 4:
        return "ymd"
    for p in parts:
        if int(p[0]) > 12:
            return "dmy"
        if int(p[1]) > 12:
            return "mdy"
    return "dmy"


def _make_ts(order: str):
    cache: dict[str, int] = {}

    def day(d: str) -> int:
        v = cache.get(d)
        if v is None:
            p = re.split(r"[./-]", d)
            a, b, c = map(int, p)
            y, m, dd = (a, b, c) if order == "ymd" else (c, b, a) if order == "dmy" else (c, a, b)
            v = int(datetime(y, m, dd, tzinfo=timezone.utc).timestamp())
            cache[d] = v
        return v

    def ts(d: str, t: str) -> float:
        h, m, s = t.split(":")
        return day(d) + int(h) * 3600 + int(m) * 60 + float(s)

    return ts


def _unique_names(raw: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in raw:
        seen[n] = seen.get(n, 0) + 1
        out.append(n if seen[n] == 1 else f"{n}#{seen[n]}")
    return out


def _align_groups(group_row: list[str], ncols: int) -> list[str]:
    """Merge fragments split by commas inside group names (they start with a
    space, e.g. ' D:]') back onto the previous cell."""
    merged: list[str] = []
    for cell in group_row:
        if merged and cell.startswith(" "):
            merged[-1] += "," + cell
        else:
            merged.append(cell)
    while len(merged) > ncols and merged[-1] == "":  # trailing empty cell(s) past the last sensor
        merged.pop()
    if len(merged) != ncols:  # cannot trust alignment
        return [""] * ncols
    return merged


def load(path: str, columns: list[str] | None = None, regex: bool = False,
         date_order: str | None = None) -> Log:
    """Load a log. ``columns`` limits which sensor columns are converted to
    floats (patterns as in ``Log.find``); the time axis is always loaded."""
    with open_text(path) as fh:
        reader = csv.reader(fh)
        header = next(reader)
        if len(header) < 3 or header[0] != "Date":
            raise ValueError(f"{path}: not a HWiNFO log (first column is {header[0]!r}, expected 'Date')")
        names = _unique_names(header)
        cols = []
        for i, n in enumerate(header):
            m = _UNIT_RE.search(n)
            cols.append(Column(i, names[i], n, m.group(1) if m else "", "", bool(m and m.group(1) == "Yes/No")))
        sensors = Log(path, cols, [])
        wanted = [c.index for c in sensors.find(columns, regex) if c.index >= 2] if columns else list(range(2, len(cols)))
        data = {i: [] for i in wanted}
        times: list[float] = []
        ts = None
        n = len(header)
        group_row: list[str] | None = None
        for row in reader:
            if not row:
                continue
            if _DATE_RE.match(row[0]):
                if ts is None:
                    ts = _make_ts(date_order or _detect_order([row[0]]))
                if len(row) < n - 1:
                    continue  # truncated final line from an interrupted write
                times.append(ts(row[0], row[1]))
                for i in wanted:
                    data[i].append(_to_float(row[i]) if i < len(row) else NAN)
            elif row[0] == "Date":
                continue  # repeated header (footer, or a sensor-set change)
            elif row[0] == "" and row[1] == "" and len(row) > 2:
                group_row = row
        if group_row:
            for c, g in zip(cols, _align_groups(group_row, n)):
                c.group = g
    sensors.times, sensors.data, sensors.rows = times, data, len(times)
    return sensors


def percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return NAN
    k = (len(sorted_vals) - 1) * q
    lo = math.floor(k)
    hi = math.ceil(k)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)
