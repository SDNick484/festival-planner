#!/usr/bin/env python3
"""Generate the app icons (standard library only: zlib and struct write the PNGs).

A solid teal square with a white tent. The shape stays inside the central 60% so the same files
work as maskable icons. Usage: make_icons.py [OUTPUT_DIR]   (default festival_planner/static/icons)
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

BACKGROUND = (0x1F, 0x6F, 0x5C)
FOREGROUND = (0xFF, 0xFF, 0xFF)
SIZES = (192, 512)
SAMPLES = 4  # per axis, for smooth edges


def _inside(x: float, y: float) -> bool:
    """The tent in unit coordinates: a triangle with a door cut out of its base."""
    apex, left, right, base = (0.5, 0.25), 0.2, 0.8, 0.72
    if not apex[1] <= y <= base:
        return False
    reach = (y - apex[1]) / (base - apex[1]) * (right - 0.5)
    if abs(x - 0.5) > reach:
        return False
    door = y > 0.5 and abs(x - 0.5) < (y - 0.5) * 0.45
    return not door and left <= x <= right


def render(size: int) -> bytes:
    rows = bytearray()
    for py in range(size):
        rows.append(0)  # PNG filter type: none
        for px in range(size):
            hits = sum(
                _inside((px + (i + 0.5) / SAMPLES) / size, (py + (j + 0.5) / SAMPLES) / size)
                for i in range(SAMPLES)
                for j in range(SAMPLES)
            )
            weight = hits / (SAMPLES * SAMPLES)
            rows.extend(
                round(b + (f - b) * weight) for b, f in zip(BACKGROUND, FOREGROUND, strict=True)
            )
    return bytes(rows)


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def png(size: int) -> bytes:
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(render(size), 9))
        + _chunk(b"IEND", b"")
    )


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else Path("festival_planner/static/icons")
    out.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        (out / f"icon-{size}.png").write_bytes(png(size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
