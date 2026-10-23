from pathlib import Path

import pytest

from wecom_excel_mcp.decoder import DecodeError, decode_related_sheet

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return decode_related_sheet((FIX / f"{name}.b64").read_text())


def test_plain_strings_header_row():
    d = load("plain_strings")
    assert d.sheet_id == "BB08J2"
    assert [d.cells[(0, c)] for c in range(4)] == ["key", "简体", "繁体", "中东"]
    assert not d.unknown_types


def test_reads_every_column_not_just_first_two():
    # the original C++ decoder only returned columns A-B
    d = load("plain_strings")
    assert {c for _, c in d.cells} >= set(range(7))


def test_rich_text_runs_are_concatenated():
    d = load("rich_strings")
    assert d.cells[(22, 4)] == "Sample text 1064Sample text 1065Sample text 1066"


def test_style_only_cells_are_skipped():
    # 1170 cell records in this payload; 544 carry only a style and no value
    assert len(load("plain_strings").cells) == 626


def test_bad_payload_raises():
    with pytest.raises(DecodeError):
        decode_related_sheet("not-base64!!")


def test_numbers_inline_and_from_table():
    d = load("numbers_formulas")
    assert d.cells[(1, 3)] == 1  # inline small int
    assert d.cells[(1, 2)] == 400  # number table
    assert d.cells[(9, 2)] == 6.4
    assert not d.unknown_types


def test_formula_cached_value_and_text():
    d = load("numbers_formulas")
    assert d.cells[(3, 4)] == 2260 and d.formulas[(3, 4)] == "=C4*D4"
    assert d.cells[(1, 6)] == 4876.36  # float noise rounded away
    assert d.formulas[(1, 6)] == "=SUM(E2:E22)-SUM(F2:F22)"
    assert d.formulas[(48, 4)] == "=SUM(D53+D54+D55)"


def test_date_formatted_number_rendered_as_datetime():
    d = load("numbers_formulas")
    assert d.cells[(39, 2)] == "2026-07-15 17:57:58"
    assert d.cells[(39, 1)] == 11  # same row, plain number


@pytest.mark.parametrize("name", ["plain_strings", "rich_strings", "numbers_formulas"])
def test_native_matches_python(name, monkeypatch):
    from wecom_excel_mcp import decoder

    if decoder._native is None:
        pytest.skip("Rust extension not built")
    monkeypatch.setattr(decoder, "USE_NATIVE", True)
    native = load(name)
    monkeypatch.setattr(decoder, "USE_NATIVE", False)
    assert native == load(name)
