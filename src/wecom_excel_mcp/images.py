"""Splitting large pictures into tiles the model sees at full resolution.

Vision models downscale any image over ~1568 px on the long edge or ~1.15 megapixels before
looking at it, which blurs small text in screenshots. Tiles of at most 1092 px a side (the
largest square that is never downscaled) keep every pixel; neighbouring tiles overlap so text
on a cut line is whole in at least one of them.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass

from PIL import Image as PILImage

MAX_EDGE = 1568
MAX_PIXELS = 1_150_000
TILE = 1092
OVERLAP = 48


@dataclass
class Tile:
    data: bytes
    format: str  # "png" / "jpeg" / ...
    label: str  # "" for an untiled picture, else e.g. "tile 2/4, x 0-692, y 575-1199"


def _needs_tiling(w: int, h: int) -> bool:
    return w > MAX_EDGE or h > MAX_EDGE or w * h > MAX_PIXELS


def _cuts(size: int) -> list[tuple[int, int]]:
    """Evenly spaced [start, end) spans of at most TILE px covering `size`, overlapping by >= OVERLAP."""
    n = max(1, math.ceil((size - OVERLAP) / (TILE - OVERLAP)))
    if n == 1:
        return [(0, size)]
    length = min(TILE, math.ceil((size + (n - 1) * OVERLAP) / n))
    step = (size - length) / (n - 1)
    return [(round(i * step), round(i * step) + length) for i in range(n)]


def tile_image(data: bytes, fmt: str) -> list[Tile]:
    """The picture itself if the model would see it at full size, else its tiles (row by row).

    Anything Pillow cannot open is returned unchanged.
    """
    try:
        img = PILImage.open(io.BytesIO(data))
        w, h = img.size
    except Exception:
        return [Tile(data, fmt, "")]
    if not _needs_tiling(w, h):
        return [Tile(data, fmt, "")]
    out_fmt = "jpeg" if img.format == "JPEG" else "png"
    if out_fmt == "jpeg" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    boxes = [(x0, y0, x1, y1) for y0, y1 in _cuts(h) for x0, x1 in _cuts(w)]
    tiles = []
    for i, (x0, y0, x1, y1) in enumerate(boxes, 1):
        buf = io.BytesIO()
        img.crop((x0, y0, x1, y1)).save(buf, out_fmt, **({"quality": 95} if out_fmt == "jpeg" else {}))
        tiles.append(Tile(buf.getvalue(), out_fmt, f"tile {i}/{len(boxes)}, x {x0}-{x1}, y {y0}-{y1}"))
    return tiles
