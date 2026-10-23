import base64
import struct
import zlib

import pytest

from wecom_excel_mcp.decoder import SheetData, decode_related_sheet
from wecom_excel_mcp.formatting import CellImage, Style, images_in, render_html
from wecom_excel_mcp.table import parse_range


# Minimal protobuf encoder, enough to build `related_sheet` payloads by field number.
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def v(fno: int, n: int) -> bytes:
    return _varint(fno << 3) + _varint(n)


def m(fno: int, *children: bytes) -> bytes:
    body = b"".join(children)
    return _varint(fno << 3 | 2) + _varint(len(body)) + body


def s(fno: int, text: str) -> bytes:
    raw = text.encode()
    return _varint(fno << 3 | 2) + _varint(len(raw)) + raw


def d(fno: int, x: float) -> bytes:
    return _varint(fno << 3 | 1) + struct.pack("<d", x)


def payload(*blocks: bytes) -> str:
    return base64.b64encode(zlib.compress(m(1, *blocks))).decode()


PIC_URL = "https://wdcdn.qpic.cn/abc_123?w=800&h=600"

STYLES = m(
    4,
    # xf0: centred/middle/wrapped, font 0, fill 0, border 0 (empty sub-messages mean index 0)
    m(1, m(1, v(1, 3), v(3, 2), m(7, v(1, 1))), m(9), m(11), m(13)),
    m(1, m(9, v(1, 1)), m(11, v(1, 1))),  # xf1: font 1, fill 1 (pattern "none": no fill)
    m(1),  # xf2: plain
    m(1, m(1, v(1, 4)), m(7, v(1, 300))),  # xf3: right-aligned, custom date format
    m(2, m(1, v(1, 300)), m(3, s(1, "yyyy-mm-dd"))),
    m(3, m(7, v(1, 1)), m(21, m(5, s(1, "FFDE3C36"))), m(23, v(1, 12))),  # font 0: bold red 12pt
    m(3, m(1, s(1, "Microsoft YaHei")), m(23, v(1, 8))),  # font 1: 8pt YaHei
    m(4, m(1, m(1, m(5, s(1, "FF99DDFF"))), v(5, 2)), v(4, 1)),  # fill 0: solid
    m(4, m(1, m(1, m(5, s(1, "FF98D7B6"))), v(5, 1))),  # fill 1: none, colour recorded anyway
    m(5, *(m(side, m(1, m(5, s(1, "FF00A3F5"))), v(3, 4)) for side in (5, 7, 9, 11))),  # border 0: dashed
)


def _cell(row, col, *value):
    return m(6, *([v(1, row)] if row else []), *([v(2, col)] if col else []), m(3, *value))


SHEET = m(
    5,
    v(1, 18),
    m(
        19,
        m(3, s(1, "SH1")),
        STYLES,
        m(5, m(1, s(1, "类别")), m(1, s(1, " ")), m(1, s(1, "月度")), m(3, d(1, 46218.0)),
          m(2, m(3, m(1, m(1, m(23, v(1, 10)))), m(3, s(1, "red "))),  # rich string 0: two runs
               m(3, m(1, m(1, m(7, v(1, 1)), m(21, m(5, s(1, "FFFF0000"))))), m(3, s(1, "<b>")))),
          m(2, m(3, m(1, m(1, m(23, v(1, 10)))), m(3, s(1, "same "))),  # rich string 1: uniform
               m(3, m(1, m(1, m(23, v(1, 10)))), m(3, s(1, "font"))))),
        _cell(0, 0, v(1, 4), m(2), m(4)),  # A1 "类别", xf0
        _cell(1, 1, v(1, 4), m(2, v(1, 1)), m(4, v(1, 1)), v(16, 5),
              m(17, s(1, f'["{PIC_URL}",400,300.5]'))),  # B2 picture, xf1
        _cell(1, 2, v(1, 2), m(2, v(1, 129)), m(4, v(1, 3))),  # C2 = number table[0] as a date, xf3
        _cell(2, 2, m(4, v(1, 0))),  # C3 style only (xf0 has a fill)
        _cell(3, 0, v(1, 4), m(2, v(1, 2)), m(4, v(1, 2))),  # A4 "月度", plain xf2
        _cell(5, 0, v(1, 6), m(2), m(4, v(1, 1))),  # A6 rich string 0, xf1 (8pt YaHei)
        _cell(5, 1, v(1, 6), m(2, v(1, 1))),  # B6 rich string 1
    ),
)
MERGE = m(5, v(1, 5), m(6, m(3, s(1, "SH1"), v(2, 3), v(4, 4), v(5, 1))))  # A4:B5
SIZES = m(
    5,
    v(1, 13),
    m(
        14,
        m(3, s(1, "SH1")),
        m(4, v(1, 1), m(2, v(1, 0), v(2, 1)), m(3, m(15, d(1, 24.0)), m(21, v(1, 1)))),  # rows 1-2: 24pt
        m(4, v(1, 2), m(2), m(4, m(9, d(1, 150.0)))),  # col A: 150px
        m(4, v(1, 2), m(2, v(1, 1), v(2, 2)), m(4, m(27, d(1, 13.671875)))),  # cols B-C: default chars
    ),
)


@pytest.fixture
def sheet() -> SheetData:
    return decode_related_sheet(payload(MERGE, SIZES, SHEET))


def test_styles(sheet):
    a1 = sheet.styles[(0, 0)]
    assert a1 == Style(bold=True, color="#DE3C36", size=12, fill="#99DDFF", h_align="center",
                       v_align="middle", wrap=True, border=True)
    assert sheet.styles[(1, 1)] == Style(size=8, font="Microsoft YaHei")  # pattern "none" fill is not a fill
    assert sheet.styles[(1, 2)] == Style(h_align="right")
    assert (3, 0) not in sheet.styles  # plain style is not stored
    assert sheet.styles[(2, 2)].fill == "#99DDFF"  # style-only cell keeps its style
    assert not sheet.unknown_types


def test_rich_text_runs(sheet):
    assert sheet.cells[(5, 0)] == "red <b>"
    assert sheet.runs[(5, 0)] == [("red ", Style(size=10)), ("<b>", Style(bold=True, color="#FF0000"))]
    assert sheet.cells[(5, 1)] == "same font" and (5, 1) not in sheet.runs  # one format: nothing to keep


def test_render_rich_text_only_differences_from_cell(sheet):
    html, _, _ = render_html(sheet, parse_range("A6"), 10)
    # cell is 8pt YaHei: run 1 differs in size, run 2 in weight and colour; text is escaped
    assert '<span style="font-size:10pt">red </span><span style="font-weight:bold;color:#FF0000">&lt;b&gt;</span>' in html
    assert "font-family:'Microsoft YaHei'" in html


def test_original_picture_url():
    img = CellImage("https://wdcdn.qpic.cn/abc_1?w=800&h=600", 1, 1)
    assert img.original_url == "https://wdcdn.qpic.cn/abc_1?imageView2/0"


def test_custom_date_format_still_applies(sheet):
    assert sheet.cells[(1, 2)] == "2026-07-15"


def test_picture_cells(sheet):
    img = sheet.images[(1, 1)]
    assert img == CellImage(PIC_URL, 400.0, 300.5)
    assert img.natural_size == (800, 600)
    assert sheet.cells[(1, 1)] == " "  # the placeholder text WeCom stores under a picture
    assert [(r, c) for r, c, _ in images_in(sheet, parse_range("B1:B9"))] == [(1, 1)]
    assert images_in(sheet, parse_range("C1:C9")) == []


def test_bad_picture_json_is_reported():
    bad = m(5, v(1, 18), m(19, m(5, m(1, s(1, "x"))), _cell(0, 0, v(1, 4), m(2), v(16, 5), m(17, s(1, "not json")))))
    data = decode_related_sheet(payload(bad))
    assert data.images == {} and data.unknown_types["bad_picture"] == 1


def test_merges_and_sizes(sheet):
    assert sheet.merges == [(3, 0, 4, 1)]
    assert sheet.row_heights == {0: 24.0, 1: 24.0}
    assert sheet.col_widths[0] == 150.0
    assert sheet.col_widths[1] == sheet.col_widths[2] == pytest.approx(100.7, abs=0.01)


def test_update_merges_chunks(sheet):
    a = decode_related_sheet(payload(MERGE, SHEET))
    b = decode_related_sheet(payload(MERGE, SIZES))
    a.update(b)
    assert a.merges == [(3, 0, 4, 1)]  # not duplicated
    assert a.row_heights == sheet.row_heights and a.images == sheet.images


def test_render_html(sheet):
    html, span, truncated = render_html(sheet, parse_range("A1:D5"), 100)
    assert span == (0, 4) and not truncated  # extends to row 5 for the A4:B5 merge
    assert ".s1{font-weight:bold;color:#DE3C36;font-size:12pt;background:#99DDFF;text-align:center" in html
    assert '<td id="A1" class="s1">类别</td>' in html
    assert '<td id="B2" class="s2"><img alt="picture" width="400" height="300"></td>' in html
    assert '<td id="A4" rowspan="2" colspan="2">月度</td>' in html
    assert '<th style="width:150px">A</th>' in html
    assert '<tr style="height:32px"><th>1</th>' in html
    row3 = html.split("<th>3</th>")[1].split("</tr>")[0]
    assert '<td class="s1"></td>' in row3  # empty cell keeps its fill/border, not its font
    row5 = html.split("<th>5</th>")[1].split("</tr>")[0]
    assert row5.count("<td") == 1  # only C5: A5:B5 are covered by the merge


def test_render_html_truncation_and_empty(sheet):
    html, span, truncated = render_html(sheet, parse_range("A1:D5"), 2)
    assert span == (0, 1) and truncated and "<th>3</th>" not in html
    assert render_html(sheet, parse_range("B2:D5"), 10)[1] == (1, 1)  # only row 2 is occupied in B:D
    assert render_html(sheet, parse_range("Z1:Z9"), 10) == ("", None, False)


def test_blocks_of_referenced_tabs_are_skipped():
    # A tab's payload also carries the tabs its formulas refer to, at overlapping addresses.
    other = m(5, v(1, 18), m(19, m(3, s(1, "SH2")), m(5, m(1, s(1, "other"))),
                             _cell(0, 0, v(1, 4), m(2)), _cell(9, 9, v(1, 4), m(2))))
    other_merge = m(5, v(1, 5), m(6, m(3, s(1, "SH2"), v(2, 7), v(4, 8))))
    data = decode_related_sheet(payload(other_merge, MERGE, SHEET, other), "SH1")
    assert data.sheet_id == "SH1"
    assert data.cells[(0, 0)] == "类别"
    assert (9, 9) not in data.cells
    assert data.merges == [(3, 0, 4, 1)]


def test_cross_tab_reference_is_named():
    def ref(sheet, row):
        return m(3, m(1, v(1, row)), m(3, v(1, 1)), m(5, v(1, row)), m(7, v(1, 1)), m(21, s(1, sheet)))

    tmpl = m(5, m(1, m(1, m(1, s(1, "=")), ref("SH2", 4), m(1, s(1, "+")), ref("SH1", 0))))
    block = m(5, v(1, 18), m(19, m(3, s(1, "SH1")), m(5, m(4, m(3, v(1, 0))), tmpl), _cell(0, 0, v(1, 5), m(2))))
    data = decode_related_sheet(payload(block), "SH1", {"SH2": "关卡 倍率"})
    assert data.formulas[(0, 0)] == "='关卡 倍率'!$B$5+$B$1"


def _numbers_block(*cells):
    return m(5, v(1, 18), m(19, m(3, s(1, "SH1")), STYLES, m(5, m(3, d(1, 2.5)), m(3, d(1, 1e12))), *cells))


def test_number_index_128_is_inline():
    data = decode_related_sheet(payload(_numbers_block(
        _cell(0, 0, v(1, 2), m(2, v(1, 127))),
        _cell(0, 1, v(1, 2), m(2, v(1, 128))),
        _cell(0, 2, v(1, 2), m(2, v(1, 129))),  # number table[0]
    )))
    assert [data.cells[(0, c)] for c in range(3)] == [127, 128, 2.5]
    assert not data.unknown_types


def test_date_out_of_range_stays_a_number():
    # xf3 has a date format; 1e12 days is past year 9999
    data = decode_related_sheet(payload(_numbers_block(_cell(0, 0, v(1, 2), m(2, v(1, 130)), m(4, v(1, 3))))))
    assert data.cells[(0, 0)] == 10**12


def test_truncated_fixed_field_raises():
    from wecom_excel_mcp.decoder import DecodeError

    with pytest.raises(DecodeError):
        decode_related_sheet(payload(m(5, v(1, 18), m(19, m(5, m(3, b"\x09\x00\x00"))))))


def test_huge_size_range_is_ignored_without_panicking():
    sizes = m(5, v(1, 13), m(14, m(4, v(1, 1), m(2, v(1, 1 << 63), v(2, 0)), m(3, m(15, d(1, 24.0))))))
    assert decode_related_sheet(payload(sizes)).row_heights == {}


def test_formula_with_huge_absolute_ref():
    ref = m(3, m(1, v(1, (1 << 63) - 1)), m(3, v(1, 0)), m(5, v(1, (1 << 63) - 1)), m(7, v(1, 0)))
    tmpl = m(5, m(1, m(1, m(1, s(1, "=")), ref)))
    block = m(5, v(1, 18), m(19, m(3, s(1, "SH1")), m(5, m(4, m(3, v(1, 0))), tmpl), _cell(0, 0, v(1, 5), m(2))))
    assert decode_related_sheet(payload(block)).formulas[(0, 0)] == "=$A$9223372036854775808"


@pytest.mark.parametrize(("serial", "want"), [
    (48379.92002893518, "2032-06-14 22:04:50"),  # rounding like timedelta(days=...)
    (2958465.99999537, 2958465.99999537),  # rounds past 9999-12-31: stays a number
    (-693593.000001, -693593.000001),  # before 0001-01-01
])
def test_datetime_edges(serial, want):
    styles = m(4, m(1, m(7, v(1, 300))), m(2, m(1, v(1, 300)), m(3, s(1, "yyyy-mm-dd hh:mm:ss"))))
    block = m(5, v(1, 18), m(19, m(3, s(1, "SH1")), styles, m(5, m(3, d(1, serial))),
                             _cell(0, 0, v(1, 2), m(2, v(1, 129)), m(4))))
    assert decode_related_sheet(payload(block)).cells[(0, 0)] == want


@pytest.mark.parametrize("raw", ['"x12"', '["u", true, false]', '["u", "1", 2]', '["u", NaN, 1]', '[1, 2, 3]'])
def test_malformed_picture_json_is_reported(raw):
    block = m(5, v(1, 18), m(19, m(3, s(1, "SH1")), _cell(0, 0, v(16, 5), m(17, s(1, raw)))))
    data = decode_related_sheet(payload(block))
    assert data.images == {} and data.unknown_types["bad_picture"] == 1
