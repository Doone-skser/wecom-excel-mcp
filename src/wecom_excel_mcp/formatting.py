"""Cell formatting and pictures decoded from the sheet payload, and their HTML rendering."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from .table import Range, col_letter

if TYPE_CHECKING:
    from .decoder import SheetData

@dataclass(frozen=True)
class Style:
    bold: bool = False
    color: str | None = None  # "#RRGGBB"
    size: float | None = None  # pt
    font: str | None = None
    fill: str | None = None
    h_align: str | None = None  # left / center / right
    v_align: str | None = None  # top / middle / bottom
    wrap: bool = False
    border: bool = False

    def css(self) -> str:
        parts = []
        if self.bold:
            parts.append("font-weight:bold")
        if self.color:
            parts.append(f"color:{self.color}")
        if self.size:
            parts.append(f"font-size:{self.size:g}pt")
        if self.font:
            parts.append(f"font-family:{_css_font(self.font)}")
        if self.fill:
            parts.append(f"background:{self.fill}")
        if self.h_align:
            parts.append(f"text-align:{self.h_align}")
        if self.v_align:
            parts.append(f"vertical-align:{self.v_align}")
        if self.wrap:
            parts.append("white-space:pre-wrap")
        if self.border:
            parts.append("border:1px solid #999")
        return ";".join(parts)


def _css_font(name: str) -> str:
    return "'" + name.replace("\\", "").replace("'", "") + "'"


def run_css(run: Style, base: Style | None) -> str:
    """CSS for a rich-text run: only the font properties that differ from its cell's style."""
    base = base or Style()
    parts = []
    if run.bold != base.bold:
        parts.append("font-weight:" + ("bold" if run.bold else "normal"))
    if run.color and run.color != base.color:
        parts.append(f"color:{run.color}")
    if run.size and run.size != base.size:
        parts.append(f"font-size:{run.size:g}pt")
    if run.font and run.font != base.font:
        parts.append(f"font-family:{_css_font(run.font)}")
    return ";".join(parts)


@dataclass(frozen=True)
class CellImage:
    url: str
    width: float  # px as displayed in the cell
    height: float

    @property
    def original_url(self) -> str:
        """The uploaded file byte for byte; the plain URL serves a recompressed copy.

        `imageView2/0` must be the first query item; the `w`/`h` items are only metadata.
        """
        u = urlparse(self.url)
        return u._replace(query="imageView2/0", fragment="").geturl()

    @property
    def natural_size(self) -> tuple[int, int] | None:
        q = parse_qs(urlparse(self.url).query)
        try:
            return int(q["w"][0]), int(q["h"][0])
        except (KeyError, ValueError):
            return None


def _esc(text: str) -> str:
    return html.escape(text).replace("\n", "<br>")


def _span(text: str, css: str) -> str:
    return f'<span style="{css}">{_esc(text)}</span>' if css else _esc(text)


def _in(rng: Range, r: int, c: int) -> bool:
    return r >= rng.r0 and (rng.r1 is None or r <= rng.r1) and c >= rng.c0 and (rng.c1 is None or c <= rng.c1)


def images_in(sheet: SheetData, rng: Range) -> list[tuple[int, int, CellImage]]:
    return [(r, c, img) for (r, c), img in sorted(sheet.images.items()) if _in(rng, r, c)]


def render_html(
    sheet: SheetData, rng: Range, max_rows: int, show_formulas: bool = False
) -> tuple[str, tuple[int, int] | None, bool]:
    """Render the occupied part of `rng` as an HTML table with merges, styles and picture markers.

    Returns (html, (first_row, last_row) rendered or None, truncated). Rows between the first and last occupied row are all
    emitted (empty ones included) so rowspans stay aligned.
    """
    occupied = [k for k in (*sheet.cells, *sheet.images) if _in(rng, *k)]
    if not occupied:
        return "", None, False
    r_lo, r_hi = min(r for r, _ in occupied), max(r for r, _ in occupied)
    c_lo, c_hi = rng.c0, max(c for _, c in occupied)
    for r0, c0, r1, c1 in sheet.merges:  # widen to merges that start inside the occupied block
        if r_lo <= r0 <= r_hi and c_lo <= c0 <= c_hi:
            r_hi, c_hi = max(r_hi, r1), max(c_hi, c1)
    if rng.r1 is not None:
        r_hi = min(r_hi, rng.r1)
    if rng.c1 is not None:
        c_hi = min(c_hi, rng.c1)
    truncated = r_hi - r_lo + 1 > max_rows
    if truncated:
        r_hi = r_lo + max_rows - 1

    spans: dict[tuple[int, int], tuple[int, int]] = {}
    covered: set[tuple[int, int]] = set()
    for r0, c0, r1, c1 in sheet.merges:
        if r0 > r_hi or r1 < r_lo or c0 > c_hi or c1 < c_lo:
            continue
        a0, b0 = max(r0, r_lo), max(c0, c_lo)
        a1, b1 = min(r1, r_hi), min(c1, c_hi)
        spans[(a0, b0)] = (a1 - a0 + 1, b1 - b0 + 1)
        covered.update((r, c) for r in range(a0, a1 + 1) for c in range(b0, b1 + 1) if (r, c) != (a0, b0))

    classes: dict[str, str] = {}  # css -> class name

    def cls(r: int, c: int, empty: bool) -> str:
        st = sheet.styles.get((r, c))
        if st is None or (empty and not (st.fill or st.border)):  # font styles on empty cells are noise
            return ""
        css = st.css()
        if not css:
            return ""
        return f' class="{classes.setdefault(css, f"s{len(classes) + 1}")}"'

    rows = []
    head = ["<tr><th></th>"]
    for c in range(c_lo, c_hi + 1):
        w = sheet.col_widths.get(c)
        head.append(f'<th style="width:{round(w)}px">{col_letter(c)}</th>' if w else f"<th>{col_letter(c)}</th>")
    rows.append("".join(head) + "</tr>")
    for r in range(r_lo, r_hi + 1):
        ht = sheet.row_heights.get(r)
        tr = [f'<tr style="height:{round(ht * 4 / 3)}px">' if ht else "<tr>", f"<th>{r + 1}</th>"]
        for c in range(c_lo, c_hi + 1):
            if (r, c) in covered:
                continue
            v = sheet.formulas[(r, c)] if show_formulas and (r, c) in sheet.formulas else sheet.cells.get((r, c), "")
            runs = None if show_formulas and (r, c) in sheet.formulas else sheet.runs.get((r, c))
            if runs:
                text = "".join(_span(t, run_css(st, sheet.styles.get((r, c)))) for t, st in runs)
            else:
                text = _esc(str(v).strip() if (r, c) in sheet.images else str(v))
            img = sheet.images.get((r, c))
            if img:
                text += f'<img alt="picture" width="{round(img.width)}" height="{round(img.height)}">'
            attrs = f' id="{col_letter(c)}{r + 1}"' if text else ""  # addresses only where there is content
            rs, cs = spans.get((r, c), (1, 1))
            if rs > 1:
                attrs += f' rowspan="{rs}"'
            if cs > 1:
                attrs += f' colspan="{cs}"'
            tr.append(f"<td{attrs}{cls(r, c, not text)}>{text}</td>")
        rows.append("".join(tr) + "</tr>")
    style = "".join(f".{n}{{{css}}}" for css, n in classes.items())
    out = (f"<style>{style}</style>\n" if style else "") + "<table>\n" + "\n".join(rows) + "\n</table>"
    return out, (r_lo, r_hi), truncated
