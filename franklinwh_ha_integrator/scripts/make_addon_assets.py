#!/usr/bin/env python3
"""Generate the add-on's icon.png and logo.png.

The Supervisor shows a blank tile for an add-on with no icon, and a bare
heading where the logo would be. Both are cosmetic and both are the first
thing a user sees.

Written as a generator rather than committed binaries alone so the mark can be
regenerated if the brand colour moves — and with a hand-rolled PNG encoder
rather than Pillow, because this runs once in a while and is not worth a
dependency that would then appear in every wheel-availability check.

    python3 scripts/make_addon_assets.py
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

ADDON = Path(__file__).resolve().parent.parent / "franklinwh_ha_integrator"

ACCENT = (0xF9, 0x73, 0x16)      # --accent, orange-500
SURFACE = (0x0F, 0x17, 0x2A)     # slate-900, the app's own background
EDGE = (0x1E, 0x29, 0x3B)        # slate-800
TRANSPARENT = (0, 0, 0, 0)


def _png(width: int, height: int, pixels: list[list[tuple]]) -> bytes:
    """Encode RGBA rows as a PNG."""
    raw = bytearray()
    for row in pixels:
        raw.append(0)                       # filter type 0 (None)
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def _rounded(x: float, y: float, w: int, h: int, radius: float) -> bool:
    """Inside a rounded rectangle occupying the whole canvas."""
    cx = min(max(x, radius), w - radius)
    cy = min(max(y, radius), h - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


# A lightning bolt in a 0..1 square, as a polygon. Matches the app's own
# ⚡ mark rather than inventing a second one.
BOLT = [
    (0.58, 0.04), (0.24, 0.55), (0.45, 0.55),
    (0.38, 0.96), (0.76, 0.42), (0.54, 0.42),
]


def _in_polygon(px: float, py: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if (y0 > py) != (y1 > py):
            xint = x0 + (py - y0) * (x1 - x0) / (y1 - y0)
            if px < xint:
                inside = not inside
    return inside


def _mark(size: int, pad: float, canvas_w: int, canvas_h: int,
          tile: bool) -> list[list[tuple]]:
    """The bolt, optionally on a rounded tile.

    Sampled 3x3 per pixel: a bolt is all diagonals, and without antialiasing
    the edges stair-step badly at 128px.
    """
    rows = []
    ox = (canvas_w - size) / 2
    oy = (canvas_h - size) / 2
    samples = [(i + 0.5) / 3 for i in range(3)]

    for y in range(canvas_h):
        row = []
        for x in range(canvas_w):
            hits_bolt = 0
            hits_tile = 0
            for sy in samples:
                for sx in samples:
                    fx, fy = x + sx, y + sy
                    if tile and _rounded(fx, fy, canvas_w, canvas_h, canvas_w * 0.22):
                        hits_tile += 1
                    bx = (fx - ox - size * pad) / (size * (1 - 2 * pad))
                    by = (fy - oy - size * pad) / (size * (1 - 2 * pad))
                    if 0 <= bx <= 1 and 0 <= by <= 1 and _in_polygon(bx, by, BOLT):
                        hits_bolt += 1

            total = len(samples) ** 2
            if hits_bolt:
                alpha = int(255 * hits_bolt / total)
                if tile and hits_tile:
                    base = SURFACE
                    blend = tuple(
                        int(base[i] + (ACCENT[i] - base[i]) * hits_bolt / total)
                        for i in range(3)
                    )
                    row.append((*blend, 255))
                else:
                    row.append((*ACCENT, alpha))
            elif tile and hits_tile:
                shade = int(255 * hits_tile / total)
                row.append((*SURFACE, shade))
            else:
                row.append(TRANSPARENT)
        rows.append(row)
    return rows


def main() -> None:
    # 128x128 is the Supervisor's icon size.
    icon = _mark(128, pad=0.16, canvas_w=128, canvas_h=128, tile=True)
    (ADDON / "icon.png").write_bytes(_png(128, 128, icon))
    print(f"  icon.png   128x128  {(ADDON / 'icon.png').stat().st_size:,} bytes")

    # The logo sits on the add-on page, wider than it is tall.
    logo = _mark(150, pad=0.10, canvas_w=250, canvas_h=150, tile=False)
    (ADDON / "logo.png").write_bytes(_png(250, 150, logo))
    print(f"  logo.png   250x150  {(ADDON / 'logo.png').stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
