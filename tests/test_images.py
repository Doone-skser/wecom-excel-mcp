import io

import pytest
from PIL import Image

from wecom_excel_mcp.images import MAX_PIXELS, OVERLAP, TILE, _cuts, tile_image


def _png(w, h, mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


def test_small_picture_is_returned_untouched():
    data = _png(800, 600)
    [t] = tile_image(data, "png")
    assert t.data == data and t.label == ""


@pytest.mark.parametrize("size", [1093, 1337, 2000, 5000])
def test_cuts_cover_with_overlap_and_max_tile(size):
    cuts = _cuts(size)
    assert cuts[0][0] == 0 and cuts[-1][1] == size
    assert all(b - a <= TILE for a, b in cuts)
    assert all(prev[1] - nxt[0] >= OVERLAP for prev, nxt in zip(cuts, cuts[1:]))


def test_large_screenshot_is_tiled_at_full_resolution():
    tiles = tile_image(_png(1337, 1199), "png")  # 1.6 MP: would be downscaled whole
    assert len(tiles) == 4
    assert tiles[0].label == "tile 1/4, x 0-693, y 0-624"
    for t in tiles:
        w, h = Image.open(io.BytesIO(t.data)).size
        assert w <= TILE and h <= TILE and w * h <= MAX_PIXELS


def test_tall_jpeg_stays_jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (900, 3000), "white").save(buf, "JPEG")
    tiles = tile_image(buf.getvalue(), "jpeg")
    assert len(tiles) == 3 and {t.format for t in tiles} == {"jpeg"}


def test_unreadable_data_passes_through():
    [t] = tile_image(b"not an image", "png")
    assert t.data == b"not an image"
