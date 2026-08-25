"""Generate the PWA icons: the idle Blackwall, on black.

Writes PNGs with zlib and struct rather than pulling in Pillow - the whole
project otherwise needs no image library, and this runs once.

    python scripts/make_icons.py
"""

from __future__ import annotations

import pathlib
import struct
import sys
import zlib

import numpy as np

OUT = pathlib.Path(__file__).resolve().parents[1] / "erebus" / "server" / "static" / "icons"

#: (size, maskable). Maskable icons need the art inside a safe circle, so the
#: line is drawn shorter and the glow tighter.
SIZES = [(192, False), (512, False), (192, True), (512, True)]


def write_png(path: pathlib.Path, rgb: np.ndarray) -> None:
    """Encode an (H, W, 3) uint8 array as a PNG."""
    height, width, _ = rgb.shape
    # Each scanline is prefixed with a filter byte (0 = none).
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def render(size: int, maskable: bool) -> np.ndarray:
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    cy = (size - 1) / 2.0
    # Normalised distance from the centreline, and from the centre horizontally.
    d = np.abs(y - cy) / size
    span = np.abs(x - cy) / size

    core_w = 0.0026
    core = (core_w / (d + core_w)) ** 1.8

    halo_w = 0.030
    halo = (halo_w / (d + halo_w)) ** 2.6 * 0.55

    # Brightest in the middle, tapering out - the same envelope the shader uses.
    envelope = 0.15 + 0.85 * np.exp(-(span ** 2) * (14.0 if maskable else 6.0))
    field = (core + halo) * envelope

    # Deep maroon -> red -> white-pink, matching the wall's palette.
    deep = np.array([0.26, 0.000, 0.035], dtype=np.float32)
    red = np.array([1.00, 0.085, 0.160], dtype=np.float32)
    hot = np.array([1.00, 0.800, 0.860], dtype=np.float32)

    t1 = np.clip(field * 1.45, 0, 1)[..., None]
    t2 = np.clip((field - 0.85) * 1.7, 0, 1)[..., None]
    rgb = deep * (1 - t1) + red * t1
    rgb = rgb * (1 - t2) + hot * t2
    rgb = rgb * field[..., None]

    # Filmic rolloff, same as the shader, so the icon matches the app.
    rgb = rgb / (rgb + 0.85)
    rgb = np.power(np.clip(rgb, 0, 1), 0.82)

    if maskable:
        # Keep the art off the edges; launchers crop to a circle.
        keep = np.sqrt((x - cy) ** 2 + (y - cy) ** 2) / (size / 2)
        rgb *= np.clip(1.6 - keep * 1.6, 0, 1)[..., None]

    ink = np.array([0.02, 0.008, 0.012], dtype=np.float32)
    rgb = np.maximum(rgb, ink)
    return (np.clip(rgb, 0, 1) * 255).astype(np.uint8)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for size, maskable in SIZES:
        name = f"erebus-{size}{'-maskable' if maskable else ''}.png"
        path = OUT / name
        write_png(path, render(size, maskable))
        print(f"  {name:<28} {path.stat().st_size / 1024:5.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
