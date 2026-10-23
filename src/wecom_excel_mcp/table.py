"""A1 ranges and grid rendering for decoded sheets."""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass

Cell = str | float | int

_A1_PART = re.compile(r"^([A-Za-z]*)(\d*)$")


def col_letter(idx: int) -> str:
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


@dataclass
class Range:
    """0-based inclusive bounds; None = unbounded."""

    r0: int = 0
    r1: int | None = None
    c0: int = 0
    c1: int | None = None


def parse_range(a1: str | None) -> Range:
    """Parse 'B2:D10', 'A:C', '3:20', 'B5'. Empty -> whole sheet."""
    if not a1 or not a1.strip():
        return Range()
    parts = a1.replace("$", "").strip().split(":")
    if len(parts) > 2:
        raise ValueError(f"bad range {a1!r}")
    parsed = []
    for p in parts:
        m = _A1_PART.match(p.strip())
        if not m or not (m.group(1) or m.group(2)):
            raise ValueError(f"bad range {a1!r}")
        col = col_index(m.group(1)) if m.group(1) else None
        row = int(m.group(2)) - 1 if m.group(2) else None
        if row is not None and row < 0:
            raise ValueError(f"bad range {a1!r}: rows start at 1")
        parsed.append((row, col))
    (ra, ca), (rb, cb) = parsed[0], parsed[-1]
    if len(parts) == 1:  # single cell, or a single row/column
        rb, cb = ra, ca
    lo = lambda a, b: min(a, b) if a is not None and b is not None else a if a is not None else b  # noqa: E731
    hi = lambda a, b: max(a, b) if a is not None and b is not None else a if a is not None else b  # noqa: E731
    return Range(lo(ra, rb) or 0, hi(ra, rb), lo(ca, cb) or 0, hi(ca, cb))


def build_grid(
    cells: dict[tuple[int, int], Cell], rng: Range, max_rows: int | None = None
) -> tuple[list[int], list[int], list[list[Cell]]]:
    """Return (row_numbers, col_numbers, grid) trimmed to the occupied area inside rng.

    With `max_rows`, the grid holds only the first `max_rows` rows; row_numbers still lists all of them."""
    keys = [
        (r, c)
        for (r, c) in cells
        if r >= rng.r0 and (rng.r1 is None or r <= rng.r1) and c >= rng.c0 and (rng.c1 is None or c <= rng.c1)
    ]
    if not keys:
        return [], [], []
    rows = sorted({r for r, _ in keys})
    c_lo = rng.c0
    c_hi = max(c for _, c in keys)
    cols = list(range(c_lo, c_hi + 1))
    grid = [[cells.get((r, c), "") for c in cols] for r in rows[:max_rows]]
    return rows, cols, grid


def _fmt(v: Cell) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def render(rows: list[int], cols: list[int], grid: list[list[Cell]], fmt: str, header: bool) -> str:
    letters = [col_letter(c) for c in cols]
    if fmt == "json":
        return json.dumps(
            {"columns": letters, "rows": [{"row": r + 1, "values": vals} for r, vals in zip(rows, grid)]},
            ensure_ascii=False,
        )
    if fmt == "records":
        if not grid:
            return "[]"
        keys = [str(_fmt(h)) or letters[i] for i, h in enumerate(grid[0])] if header else letters
        body = grid[1:] if header else grid
        body_rows = rows[1:] if header else rows
        return json.dumps(
            [{"_row": r + 1, **{k: v for k, v in zip(keys, vals) if v != ""}} for r, vals in zip(body_rows, body)],
            ensure_ascii=False,
        )
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["row", *letters])
        for r, vals in zip(rows, grid):
            w.writerow([r + 1, *map(_fmt, vals)])
        return buf.getvalue()
    if fmt == "markdown":
        esc = lambda s: _fmt(s).replace("|", "\\|").replace("\r", "").replace("\n", "<br>")  # noqa: E731
        lines = ["| row | " + " | ".join(letters) + " |", "|---" * (len(letters) + 1) + "|"]
        lines += [f"| {r + 1} | " + " | ".join(map(esc, vals)) + " |" for r, vals in zip(rows, grid)]
        return "\n".join(lines)
    raise ValueError(f"unknown format {fmt!r}")
