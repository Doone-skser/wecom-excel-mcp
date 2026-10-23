import json

import pytest

from wecom_excel_mcp.client import WecomError, check_image_url, parse_doc_ref
from wecom_excel_mcp.table import Range, build_grid, col_index, col_letter, parse_range, render


@pytest.mark.parametrize("i,s", [(0, "A"), (25, "Z"), (26, "AA"), (51, "AZ"), (701, "ZZ"), (702, "AAA")])
def test_col_letters_roundtrip(i, s):
    assert col_letter(i) == s
    assert col_index(s) == i


@pytest.mark.parametrize(
    "a1,expected",
    [
        (None, Range()),
        ("B2:D10", Range(1, 9, 1, 3)),
        ("D10:B2", Range(1, 9, 1, 3)),
        ("A:C", Range(0, None, 0, 2)),
        ("3:20", Range(2, 19, 0, None)),
        ("B5", Range(4, 4, 1, 1)),
        ("$A$1:$B$2", Range(0, 1, 0, 1)),
    ],
)
def test_parse_range(a1, expected):
    assert parse_range(a1) == expected


@pytest.mark.parametrize("bad", ["A0", "1A", "A1:B2:C3", "!!"])
def test_parse_range_rejects(bad):
    with pytest.raises(ValueError):
        parse_range(bad)


CELLS = {(0, 0): "name", (0, 1): "n", (2, 0): "x|y", (2, 1): 3.0, (3, 2): "z"}


def test_grid_skips_empty_rows_and_pads_columns():
    rows, cols, grid = build_grid(CELLS, Range())
    assert rows == [0, 2, 3]
    assert cols == [0, 1, 2]
    assert grid[0] == ["name", "n", ""]


def test_grid_respects_range():
    rows, cols, grid = build_grid(CELLS, parse_range("B1:B3"))
    assert (rows, cols, grid) == ([0, 2], [1], [["n"], [3.0]])


def test_render_formats():
    rows, cols, grid = build_grid(CELLS, Range())
    assert render(rows, cols, grid, "csv", True).splitlines()[:2] == ["row,A,B,C", "1,name,n,"]
    assert "x\\|y" in render(rows, cols, grid, "markdown", True)
    recs = json.loads(render(rows, cols, grid, "records", True))
    assert recs[0] == {"_row": 3, "name": "x|y", "n": 3.0}
    assert json.loads(render(rows, cols, grid, "json", True))["rows"][1]["row"] == 3


def test_parse_doc_ref():
    url = "https://doc.weixin.qq.com/sheet/e3_ExampleSheet0000000000000002?scode=ALgA&tab=XYZ123"
    assert parse_doc_ref(url) == ("e3_ExampleSheet0000000000000002", "XYZ123")
    assert parse_doc_ref(" e3_ExampleSheet0000000000000002 ") == ("e3_ExampleSheet0000000000000002", None)
    with pytest.raises(WecomError):
        parse_doc_ref("https://example.com/nothing")



@pytest.mark.parametrize(
    "url",
    ["http://wdcdn.qpic.cn/x", "https://api.internal.example.com/x", "https://qpic.cn.evil.com/x", "file:///etc/passwd"],
)
def test_image_urls_limited_to_wecom_cdn(url):
    with pytest.raises(WecomError, match="not a WeCom image CDN URL"):
        check_image_url(url)
    check_image_url("https://wdcdn.qpic.cn/MTY_1?w=1&h=1")


def test_build_grid_max_rows_keeps_all_row_numbers():
    rows, cols, grid = build_grid(CELLS, Range(), max_rows=1)
    assert rows == build_grid(CELLS, Range())[0]
    assert len(grid) == 1
