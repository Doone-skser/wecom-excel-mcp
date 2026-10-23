"""Decoder for the WeCom (企业微信) online-sheet `related_sheet` payload.

Rewritten from an earlier C++ extension that read only the first two columns of string cells,
generalised to every column, every tab, numbers, formulas and dates. The payload is
base64(zlib(protobuf)); instead of fully decoding the protobuf tree like the C++
version, it walks only the paths that carry cell data, which keeps it fast enough
in pure Python.

Layout (field numbers, reverse-engineered):

    1 {                                  root message
      5 { 1: block_type, ... }           repeated blocks
      5 { 1: 5,                          block type 5 = one merged range
          6 { 3 { 2: first_row  3: first_col  4: last_row  5: last_col } }
      }
      5 { 1: 13,                         block type 13 = row/column sizes
          14 { 4 { 1: 1=rows|2=cols  2 { 1: first  2: last }
                   3 { 15 { 1: row height pt (double) } }
                   4 { 9 { 1: col width px (double) } | 27 { 1: col width in chars (double) } } } }
      }
      5 { 1: 18,                         block type 18 = sheet cell data
          19 {
            3 { 1: sheet_id, 4: rows, 5: cols }
            4 {                          style table
              1 {                        repeated cell styles (xf); an empty sub-message means index 0
                1 { 1: h_align 2/3/4=left/center/right  3: v_align 1/2/3=top/middle/bottom  7 { 1: wrap } }
                7 { 1: num_fmt_id }  9 { 1: font }  11 { 1: fill }  13 { 1: border }
              }
              2 { 1 { 1: num_fmt_id }  3 { 1: format_code } }   custom number formats
              3 { 1 { 1: name }  7 { 1: bold }  21 { 5 { 1: "AARRGGBB" } }  23 { 1: size_pt } }   fonts
              4 { 1 { 1 { 5 { 1: "AARRGGBB" } }  5: pattern 2=solid } }        fills
              5 { 5|7|9|11 { 1 { 5 { 1: colour } }  3: line_style } }           borders: left/right/top/bottom
            }
            5 {                          value tables
              1 { 1: text }              plain strings          (cell type 4)
              2 { 3 { 1 { 1: font }  3 { 1: text } } }   rich strings, runs with their own font (cell type 6)
              3 { 1: double }            numbers                (cell type 2)
              4 { 1 { 1: type  3 { 1: index } }  3 { 1: template } }
                                         formula instances      (cell type 5): cached value + template
              5 { 1 { 1 { 1 { 1: text } | 3 { ref } } } }
                                         formula templates: text pieces and R1C1-relative refs
            }
            6 {                          repeated cells
              1: row  2: col             0-based, omitted when 0
              3 { 1: value_type  2 { 1: index }  4 { 1: style_index }
                  16: 5  17 { 1: '["<picture url>", width_px, height_px]' } }   picture in cell
            }
          }
      }
    }

Number index: values below 128 are the integer itself; 129+ index the number table.
Formula ref: 1/3/5/7 = first row / first col / last row / last col, each relative to the
formula cell when the matching flag 13/15/17/19 is 1, otherwise absolute; 21 { 1: sheet_id }
is the tab referred to. A tab's payload also carries cell blocks (block type 18) of the tabs
its formulas refer to, at their own addresses.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import struct
import zlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .formatting import CellImage, Style

try:
    from . import _native  # Rust build of this module (rust/src/lib.rs)
except ImportError:  # pragma: no cover - source checkout without the extension
    _native = None

# Set WECOM_EXCEL_PURE_PYTHON=1 to force the pure-Python decoder.
USE_NATIVE = os.environ.get("WECOM_EXCEL_PURE_PYTHON", "") in ("", "0")

MERGE_BLOCK_TYPE = 5
SIZE_BLOCK_TYPE = 13
CELL_BLOCK_TYPE = 18
CELL_KIND_PICTURE = 5
TYPE_NUMBER = 2
TYPE_STRING = 4
TYPE_FORMULA = 5
TYPE_RICH_STRING = 6
INLINE_INT_LIMIT = 129  # index 128 is the integer 128 itself
NUMBER_TABLE_OFFSET = 129

_VARINT, _FIXED64, _LENDELIM, _FIXED32 = 0, 1, 2, 5

# Excel built-in number formats that are dates/times.
_BUILTIN_DATE_FMTS = {14: "date", 15: "date", 16: "date", 17: "date", 22: "datetime", 18: "time", 19: "time",
                      20: "time", 21: "time", 45: "time", 46: "time", 47: "time"}
_EXCEL_EPOCH = datetime(1899, 12, 30)
_H_ALIGN = {2: "left", 3: "center", 4: "right"}
_V_ALIGN = {1: "top", 2: "middle", 3: "bottom"}
_BORDER_SIDES = (5, 7, 9, 11)


class DecodeError(ValueError):
    pass


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise DecodeError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result & 0xFFFF_FFFF_FFFF_FFFF, pos  # a 10th byte can carry bits past 64
        shift += 7
        if shift > 63:
            raise DecodeError("varint too long")


def _fields(buf: bytes, start: int = 0, end: int | None = None):
    """Yield (field_no, wire_type, value) where LENDELIM values are (start, end) offsets."""
    pos = start
    end = len(buf) if end is None else end
    while pos < end:
        key, pos = _read_varint(buf, pos)
        fno, wt = key >> 3, key & 7
        if wt == _VARINT:
            val, pos = _read_varint(buf, pos)
        elif wt == _LENDELIM:
            ln, pos = _read_varint(buf, pos)
            if pos + ln > end:
                raise DecodeError("length-delimited field overruns buffer")
            val = (pos, pos + ln)
            pos += ln
        elif wt in (_FIXED64, _FIXED32):
            width = 8 if wt == _FIXED64 else 4
            if pos + width > end:
                raise DecodeError("truncated fixed-width field")
            val = buf[pos : pos + width]
            pos += width
        else:
            raise DecodeError(f"unsupported wire type {wt}")
        yield fno, wt, val


def _sub(buf: bytes, span: tuple[int, int], fno: int):
    """Yield sub-spans of field `fno` inside message `span`."""
    for f, wt, v in _fields(buf, *span):
        if f == fno and wt == _LENDELIM:
            yield v


def _text(buf: bytes, span: tuple[int, int]) -> str:
    return buf[span[0] : span[1]].decode("utf-8", errors="replace")


def _signed(v: int) -> int:
    return v - (1 << 64) if v >= 1 << 63 else v


def _int_in(buf: bytes, span, fno: int = 1, default: int = 0) -> int:
    """Value of varint field `fno` inside the message at `span` (proto default when absent)."""
    for f, wt, v in _fields(buf, *span):
        if f == fno and wt == _VARINT:
            return _signed(v)
    return default


def _col_letter(idx: int) -> str:
    s = ""
    idx += 1
    while idx > 0:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


@dataclass
class SheetData:
    sheet_id: str = ""
    cells: dict[tuple[int, int], str | float | int] = field(default_factory=dict)
    formulas: dict[tuple[int, int], str] = field(default_factory=dict)
    styles: dict[tuple[int, int], Style] = field(default_factory=dict)
    images: dict[tuple[int, int], CellImage] = field(default_factory=dict)
    runs: dict[tuple[int, int], list[tuple[str, Style]]] = field(default_factory=dict)  # mixed-format text
    merges: list[tuple[int, int, int, int]] = field(default_factory=list)  # r0, c0, r1, c1 (inclusive)
    col_widths: dict[int, float] = field(default_factory=dict)  # px
    row_heights: dict[int, float] = field(default_factory=dict)  # pt
    unknown_types: Counter = field(default_factory=Counter)

    def update(self, other: SheetData) -> None:
        """Merge a later chunk of the same tab into this one."""
        self.cells.update(other.cells)
        self.formulas.update(other.formulas)
        self.styles.update(other.styles)
        self.images.update(other.images)
        self.runs.update(other.runs)
        self.merges.extend(m for m in other.merges if m not in self.merges)
        self.col_widths.update(other.col_widths)
        self.row_heights.update(other.row_heights)
        self.unknown_types.update(other.unknown_types)

    @property
    def max_row(self) -> int:
        return max((r for r, _ in self.cells), default=-1)

    @property
    def max_col(self) -> int:
        return max((c for _, c in self.cells), default=-1)


@dataclass
class _Tables:
    plain: list[str] = field(default_factory=list)
    rich: list[str] = field(default_factory=list)
    rich_runs: list[list[tuple[str, Style]] | None] = field(default_factory=list)  # None: one uniform format
    numbers: list[float] = field(default_factory=list)
    formulas: list[tuple[int | None, int, int]] = field(default_factory=list)  # (cached type, cached idx, template)
    templates: list[list] = field(default_factory=list)  # pieces: str or ref dict
    style_kinds: list[str | None] = field(default_factory=list)  # per style index: None/"date"/"time"/"datetime"
    styles: list[Style] = field(default_factory=list)  # per style index


def _date_kind(code: str) -> str | None:
    code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code.split(";")[0]).lower()
    has_date = "y" in code or "d" in code
    has_time = "h" in code or "s" in code
    if has_date and has_time:
        return "datetime"
    return "date" if has_date else "time" if has_time else None


def _argb(buf: bytes, span) -> str | None:
    """`{ 5 { 1: "AARRGGBB" } }` -> "#RRGGBB"."""
    for c in _sub(buf, span, 5):
        for s in _sub(buf, c, 1):
            argb = _text(buf, s)
            if len(argb) in (6, 8):
                return "#" + argb[-6:].upper()
    return None


def _read_font(buf: bytes, span) -> dict:
    font: dict = {}
    for f, wt, v in _fields(buf, *span):
        if wt != _LENDELIM:
            continue
        if f == 1:
            font["font"] = next((_text(buf, s) for s in _sub(buf, v, 1)), None) or None
        elif f == 7:
            font["bold"] = bool(_int_in(buf, v))
        elif f == 21:
            font["color"] = _argb(buf, v)
        elif f == 23:
            font["size"] = _int_in(buf, v) or None
    return font


def _read_fill(buf: bytes, span) -> str | None:
    for pattern in _sub(buf, span, 1):
        if _int_in(buf, pattern, 5) == 2:  # solid; 1 = none (a colour may still be recorded)
            return next((_argb(buf, c) for c in _sub(buf, pattern, 1)), None)
    return None


def _read_border(buf: bytes, span) -> bool:
    return any(_int_in(buf, side, 3) for f, wt, side in _fields(buf, *span) if f in _BORDER_SIDES and wt == _LENDELIM)


def _read_styles(buf: bytes, span, t: _Tables) -> None:
    custom: dict[int, str] = {}
    xfs: list[dict[int, tuple[int, int]]] = []  # per xf: field -> span (first occurrence)
    fonts: list[dict] = []
    fills: list[str | None] = []
    borders: list[bool] = []
    for f, wt, v in _fields(buf, *span):
        if wt != _LENDELIM:
            continue
        if f == 1:
            xf: dict[int, tuple[int, int]] = {}
            for g, gwt, gv in _fields(buf, *v):
                if gwt == _LENDELIM:
                    xf.setdefault(g, gv)
            xfs.append(xf)
        elif f == 2:
            fid = next((_int_in(buf, i) for i in _sub(buf, v, 1)), 0)
            for code in _sub(buf, v, 3):
                for s in _sub(buf, code, 1):
                    custom[fid] = _text(buf, s)
        elif f == 3:
            fonts.append(_read_font(buf, v))
        elif f == 4:
            fills.append(_read_fill(buf, v))
        elif f == 5:
            borders.append(_read_border(buf, v))

    def ref(xf, fno, table, default):
        if fno not in xf:
            return default
        i = _int_in(buf, xf[fno])
        return table[i] if 0 <= i < len(table) else default

    t.style_kinds, t.styles = [], []
    for xf in xfs:
        fmt_id = _int_in(buf, xf[7]) if 7 in xf else 0
        t.style_kinds.append(_date_kind(custom[fmt_id]) if fmt_id in custom else _BUILTIN_DATE_FMTS.get(fmt_id))
        al = xf.get(1)
        t.styles.append(Style(
            **ref(xf, 9, fonts, {}),
            fill=ref(xf, 11, fills, None),
            border=ref(xf, 13, borders, False),
            h_align=_H_ALIGN.get(_int_in(buf, al, 1)) if al else None,
            v_align=_V_ALIGN.get(_int_in(buf, al, 3)) if al else None,
            wrap=bool(al) and any(_int_in(buf, w) for w in _sub(buf, al, 7)),
        ))


def _read_ref(buf: bytes, span) -> dict:
    ref: dict = {k: 0 for k in (1, 3, 5, 7, 13, 15, 17, 19)}
    for f, wt, v in _fields(buf, *span):
        if f in ref and wt == _LENDELIM:
            ref[f] = _int_in(buf, v)
        elif f == 21 and wt == _LENDELIM:
            ref["sheet"] = next((_text(buf, s) for s in _sub(buf, v, 1)), None)
    return ref


def _read_value_tables(buf: bytes, span, t: _Tables) -> None:
    for f, wt, v in _fields(buf, *span):
        if wt != _LENDELIM:
            continue
        if f == 1:
            t.plain.append("".join(_text(buf, s) for s in _sub(buf, v, 1)))
        elif f == 2:
            runs = []
            for run in _sub(buf, v, 3):
                text = "".join(_text(buf, s) for inner in _sub(buf, run, 3) for s in _sub(buf, inner, 1))
                font = next((_read_font(buf, fs) for fmt in _sub(buf, run, 1) for fs in _sub(buf, fmt, 1)), {})
                runs.append((text, Style(**font)))
            t.rich.append("".join(text for text, _ in runs))
            t.rich_runs.append(runs if len({st for _, st in runs}) > 1 else None)
        elif f == 3:
            num = 0.0
            for g, gwt, gv in _fields(buf, *v):
                if g == 1 and gwt == _FIXED64:
                    num = struct.unpack("<d", gv)[0]
            t.numbers.append(num)
        elif f == 4:
            ctype, cidx, tmpl = None, 0, 0
            for g, gwt, gv in _fields(buf, *v):
                if g == 1 and gwt == _LENDELIM:
                    ctype = _int_in(buf, gv, 1, None)
                    for idx_span in _sub(buf, gv, 3):
                        cidx = _int_in(buf, idx_span)
                elif g == 3 and gwt == _LENDELIM:
                    tmpl = _int_in(buf, gv)
            t.formulas.append((ctype, cidx, tmpl))
        elif f == 5:
            pieces: list = []
            for body in _sub(buf, v, 1):
                for piece in _sub(buf, body, 1):
                    for g, gwt, gv in _fields(buf, *piece):
                        if g == 1 and gwt == _LENDELIM:
                            pieces.extend(_text(buf, s) for s in _sub(buf, gv, 1))
                        elif g == 3 and gwt == _LENDELIM:
                            pieces.append(_read_ref(buf, gv))
            t.templates.append(pieces)


def _sheet_prefix(name: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name) and not re.fullmatch(r"[A-Za-z]{1,3}[0-9]+", name):
        return name + "!"
    return "'" + name.replace("'", "''") + "'!"


def _render_formula(pieces: list, row: int, col: int, sheet: str = "", sheet_names: dict[str, str] | None = None) -> str:
    out = []
    for p in pieces:
        if isinstance(p, str):
            out.append(p)
            continue
        ref_sheet = p.get("sheet")
        if ref_sheet and ref_sheet != sheet:
            out.append(_sheet_prefix((sheet_names or {}).get(ref_sheet, ref_sheet)))

        def addr(rk, ck, rflag, cflag):
            r = p[rk] + row if p[rflag] else p[rk]
            c = p[ck] + col if p[cflag] else p[ck]
            return f"{'' if p[cflag] else '$'}{_col_letter(c)}{'' if p[rflag] else '$'}{r + 1}"

        start = addr(1, 3, 13, 15)
        end = addr(5, 7, 17, 19)
        out.append(start if start == end else f"{start}:{end}")
    return "".join(out)


def _excel_date(serial: float, kind: str) -> str | None:
    """None when the date falls outside years 1-9999."""
    try:
        dt = _EXCEL_EPOCH + timedelta(days=serial)
        dt = (dt + timedelta(microseconds=500_000)).replace(microsecond=0)
    except (OverflowError, ValueError):  # out of range, or NaN
        return None
    # Not strftime: its %Y does not zero-pad years below 1000 on glibc.
    date, time = f"{dt.year:04}-{dt.month:02}-{dt.day:02}", f"{dt.hour:02}:{dt.minute:02}:{dt.second:02}"
    return date if kind == "date" else time if kind == "time" else f"{date} {time}"


def _clean_number(n: float | int) -> float | int:
    if isinstance(n, float):
        if n.is_integer():
            return int(n)
        return round(n, 10)  # drop float noise like 4876.3600000000015
    return n


class _Resolver:
    def __init__(self, t: _Tables, out: SheetData, sheet: str = "", sheet_names: dict[str, str] | None = None):
        self.t, self.out = t, out
        self.sheet, self.sheet_names = sheet, sheet_names

    def number(self, idx: int):
        if idx < INLINE_INT_LIMIT:
            return idx
        i = idx - NUMBER_TABLE_OFFSET
        if 0 <= i < len(self.t.numbers):
            return self.t.numbers[i]
        self.out.unknown_types[f"bad_number_index:{idx}"] += 1
        return None

    def value(self, vtype: int, idx: int, row: int, col: int, depth: int = 0):
        t = self.t
        if vtype == TYPE_STRING:
            table = t.plain
        elif vtype == TYPE_RICH_STRING:
            table = t.rich
            if depth == 0 and 0 <= idx < len(t.rich_runs) and t.rich_runs[idx]:
                self.out.runs[(row, col)] = t.rich_runs[idx]
        elif vtype == TYPE_NUMBER:
            return self.number(idx)
        elif vtype == TYPE_FORMULA and depth == 0:
            if not 0 <= idx < len(t.formulas):
                self.out.unknown_types[f"bad_formula_index:{idx}"] += 1
                return None
            ctype, cidx, tmpl = t.formulas[idx]
            if 0 <= tmpl < len(t.templates):
                self.out.formulas[(row, col)] = _render_formula(
                    t.templates[tmpl], row, col, self.sheet, self.sheet_names
                )
            return "" if ctype is None else self.value(ctype, cidx, row, col, depth + 1)
        else:
            self.out.unknown_types[vtype] += 1
            return None
        if 0 <= idx < len(table):
            return table[idx]
        self.out.unknown_types[f"bad_index:{vtype}"] += 1
        return None


_PLAIN = Style()


def _picture(buf: bytes, span) -> CellImage | None:
    for s in _sub(buf, span, 1):
        try:
            pic = json.loads(_text(buf, s))
            if not isinstance(pic, list) or len(pic) < 3:
                return None
            url, w, h = pic[:3]
            if not isinstance(url, str) or not all(
                isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in (w, h)
            ):
                return None
            url.encode()  # raises on a lone surrogate, which is not a usable URL
            return CellImage(url, float(w), float(h))
        except (ValueError, OverflowError):  # bad JSON or a huge int size
            return None
    return None


def _read_merge(buf: bytes, block, out: SheetData) -> None:
    for m in _sub(buf, block, 6):
        for rng in _sub(buf, m, 3):
            v = {f: val for f, wt, val in _fields(buf, *rng) if wt == _VARINT}
            out.merges.append((v.get(2, 0), v.get(3, 0), v.get(4, 0), v.get(5, 0)))


def _double_in(buf: bytes, span) -> float | None:
    for f, wt, v in _fields(buf, *span):
        if f == 1 and wt == _FIXED64:
            return struct.unpack("<d", v)[0]
    return None


def _read_sizes(buf: bytes, block, out: SheetData) -> None:
    for sizes in _sub(buf, block, 14):
        for entry in _sub(buf, sizes, 4):
            axis = _int_in(buf, entry, 1)
            first = last = 0
            for r in _sub(buf, entry, 2):
                first, last = _int_in(buf, r, 1), _int_in(buf, r, 2)
            size = None
            if axis == 1:
                for props in _sub(buf, entry, 3):
                    for h in _sub(buf, props, 15):
                        size = _double_in(buf, h)
                target = out.row_heights
            elif axis == 2:
                for props in _sub(buf, entry, 4):
                    for f, wt, v in _fields(buf, *props):
                        if f == 9 and wt == _LENDELIM:  # px
                            size = _double_in(buf, v)
                        elif f == 27 and wt == _LENDELIM and size is None:  # Excel character units
                            chars = _double_in(buf, v)
                            size = chars * 7 + 5 if chars is not None else None
                target = out.col_widths
            else:
                continue
            if size is not None and last - first < 20000:
                target.update(dict.fromkeys(range(first, last + 1), size))


def _read_cells(
    buf: bytes, sheet_span, out: SheetData, t: _Tables, sheet: str = "", sheet_names: dict[str, str] | None = None
) -> None:
    resolver = _Resolver(t, out, sheet, sheet_names)
    for cell in _sub(buf, sheet_span, 6):
        row = col = 0
        value_span = None
        for f, wt, v in _fields(buf, *cell):
            if f == 1 and wt == _VARINT:
                row = v
            elif f == 2 and wt == _VARINT:
                col = v
            elif f == 3 and wt == _LENDELIM:
                value_span = v
        if value_span is None:
            continue

        vtype = None
        index = 0
        style = None
        kind = None
        extra = None
        for f, wt, v in _fields(buf, *value_span):
            if f == 1 and wt == _VARINT:
                vtype = _signed(v)
            elif f == 2 and wt == _LENDELIM:
                index = _int_in(buf, v)
            elif f == 4 and wt == _LENDELIM:
                style = _int_in(buf, v)
            elif f == 16 and wt == _VARINT:
                kind = v
            elif f == 17 and wt == _LENDELIM:
                extra = v
        if style is not None and 0 <= style < len(t.styles) and t.styles[style] != _PLAIN:
            out.styles[(row, col)] = t.styles[style]
        if kind == CELL_KIND_PICTURE and extra is not None:
            img = _picture(buf, extra)
            if img:
                out.images[(row, col)] = img
            else:
                out.unknown_types["bad_picture"] += 1
        if vtype is None:  # style-only (empty) cell
            continue

        value = resolver.value(vtype, index, row, col)
        if value is None:
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            kind = t.style_kinds[style] if style is not None and 0 <= style < len(t.style_kinds) else None
            date = _excel_date(value, kind) if kind else None
            value = date if date is not None else _clean_number(value)
        out.cells[(row, col)] = value


def _block_sheet(buf: bytes, block, fno: int) -> str | None:
    """Sheet id of a block: `fno { 3 { 1: sheet_id } }`."""
    for inner in _sub(buf, block, fno):
        for meta in _sub(buf, inner, 3):
            for f, wt, v in _fields(buf, *meta):
                if f == 1 and wt == _LENDELIM:
                    return _text(buf, v)
    return None


def decode_related_sheet(
    b64_data: str, sheet_id: str | None = None, sheet_names: dict[str, str] | None = None
) -> SheetData:
    """Decode one tab's cells. The payload also carries cell blocks of the tabs its formulas refer to;
    given `sheet_id`, blocks of other tabs are skipped (otherwise everything is merged).
    `sheet_names` maps tab ids to names for cross-tab references in formulas (else the id is shown).

    Uses the Rust decoder (`_native`) when it is built, else the pure-Python one below."""
    try:
        raw = zlib.decompress(base64.b64decode(b64_data))
    except (ValueError, zlib.error) as e:
        raise DecodeError(f"payload is not base64+zlib: {e}") from e
    if _native is not None and USE_NATIVE:
        return _decode_native(raw, sheet_id, sheet_names)
    return _decode_python(raw, sheet_id, sheet_names)


def _decode_native(raw: bytes, sheet_id: str | None, sheet_names: dict[str, str] | None) -> SheetData:
    try:
        (sid, cells, formulas, style_list, cell_styles, images, runs, merges, col_widths, row_heights,
         unknown) = _native.decode_raw(raw, sheet_id, sheet_names)
    except ValueError as e:
        raise DecodeError(str(e)) from e
    styles = [Style(*s) for s in style_list]
    return SheetData(
        sheet_id=sid,
        cells=cells,
        formulas=formulas,
        styles={k: styles[i] for k, i in cell_styles.items()},
        images={k: CellImage(*img) for k, img in images.items()},
        runs={k: [(text, styles[i]) for text, i in r] for k, r in runs.items()},
        merges=merges,
        col_widths=col_widths,
        row_heights=row_heights,
        unknown_types=Counter(dict(unknown)),
    )


def _decode_python(raw: bytes, sheet_id: str | None, sheet_names: dict[str, str] | None) -> SheetData:
    out = SheetData(sheet_id=sheet_id or "")
    tables = _Tables()

    def other_sheet(block, fno: int) -> bool:
        owner = _block_sheet(raw, block, fno)
        return sheet_id is not None and owner is not None and owner != sheet_id

    for root in _sub(raw, (0, len(raw)), 1):
        for block in _sub(raw, root, 5):
            block_type = None
            sheet_span = None
            for f, wt, v in _fields(raw, *block):
                if f == 1 and wt == _VARINT:
                    block_type = v
                elif f == 19 and wt == _LENDELIM:
                    sheet_span = v
            if block_type == MERGE_BLOCK_TYPE:
                if not other_sheet(block, 6):
                    _read_merge(raw, block, out)
                continue
            if block_type == SIZE_BLOCK_TYPE:
                if not other_sheet(block, 14):
                    _read_sizes(raw, block, out)
                continue
            if block_type != CELL_BLOCK_TYPE or sheet_span is None:
                continue
            owner = _block_sheet(raw, block, 19)
            if other_sheet(block, 19):
                continue  # a tab this one's formulas refer to
            if sheet_id is None:
                out.sheet_id = owner or out.sheet_id
            value_tables = list(_sub(raw, sheet_span, 5))
            style_tables = list(_sub(raw, sheet_span, 4))
            if value_tables:  # each block carries its own tables; otherwise keep the previous ones
                tables = _Tables(style_kinds=tables.style_kinds, styles=tables.styles)
                for vt in value_tables:
                    _read_value_tables(raw, vt, tables)
            for st in style_tables:
                _read_styles(raw, st, tables)
            _read_cells(raw, sheet_span, out, tables, owner or "", sheet_names)
    return out
