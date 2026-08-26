"""Build a minimal JPEG carrying known EXIF, for testing the parser.

Constructed byte by byte rather than produced by a library, so the expected
values are known exactly rather than being whatever the library happened to
write.
"""

from __future__ import annotations

import struct

#: Ground truth the tests assert against.
MAKE = "TestCam"
MODEL = "Model-1"
DATETIME = "2026:08:26 14:30:00"
LAT_DMS = (51, 30, 26.64)      # 51.507400 N
LON_DMS = (0, 7, 40.08)        # 0.127800 W
LAT = 51.507400
LON = -0.127800


def _entry(tag: int, kind: int, count: int, payload: bytes) -> bytes:
    assert len(payload) == 4
    return struct.pack("<HHI", tag, kind, count) + payload


def _rational(value: float, precision: int = 100) -> bytes:
    return struct.pack("<II", int(round(value * precision)), precision)


def build(with_gps: bool = True, with_exif: bool = True) -> bytes:
    if not with_exif:
        # A JPEG with no APP1 at all - what an upload pipeline leaves you.
        return b"\xff\xd8" + b"\xff\xdb\x00\x04\x00\x00" + b"\xff\xd9"

    make = MAKE.encode() + b"\x00"
    model = MODEL.encode() + b"\x00"
    datetime_bytes = DATETIME.encode() + b"\x00"

    # Layout is computed rather than hardcoded so the fixture stays correct if
    # any of the strings change.
    ifd0_at = 8
    ifd0_entries = 4 if with_gps else 3
    ifd0_size = 2 + ifd0_entries * 12 + 4
    data_at = ifd0_at + ifd0_size

    make_at = data_at
    model_at = make_at + len(make)
    exif_ifd_at = model_at + len(model)

    exif_size = 2 + 1 * 12 + 4
    datetime_at = exif_ifd_at + exif_size
    gps_ifd_at = datetime_at + len(datetime_bytes)

    gps_size = 2 + 4 * 12 + 4
    lat_at = gps_ifd_at + gps_size
    lon_at = lat_at + 24

    # --- IFD0 -------------------------------------------------------------
    ifd0 = struct.pack("<H", ifd0_entries)
    ifd0 += _entry(0x010F, 2, len(make), struct.pack("<I", make_at))
    ifd0 += _entry(0x0110, 2, len(model), struct.pack("<I", model_at))
    ifd0 += _entry(0x8769, 4, 1, struct.pack("<I", exif_ifd_at))
    if with_gps:
        ifd0 += _entry(0x8825, 4, 1, struct.pack("<I", gps_ifd_at))
    ifd0 += struct.pack("<I", 0)

    # --- Exif sub-IFD ------------------------------------------------------
    exif = struct.pack("<H", 1)
    exif += _entry(0x9003, 2, len(datetime_bytes), struct.pack("<I", datetime_at))
    exif += struct.pack("<I", 0)

    body = ifd0 + make + model + exif + datetime_bytes

    # --- GPS sub-IFD -------------------------------------------------------
    if with_gps:
        gps = struct.pack("<H", 4)
        gps += _entry(0x0001, 2, 2, b"N\x00\x00\x00")
        gps += _entry(0x0002, 5, 3, struct.pack("<I", lat_at))
        gps += _entry(0x0003, 2, 2, b"W\x00\x00\x00")
        gps += _entry(0x0004, 5, 3, struct.pack("<I", lon_at))
        gps += struct.pack("<I", 0)

        lat_data = b"".join(_rational(v) for v in LAT_DMS)
        lon_data = b"".join(_rational(v) for v in LON_DMS)
        body += gps + lat_data + lon_data

    tiff = b"II" + struct.pack("<HI", 42, ifd0_at) + body
    app1 = b"Exif\x00\x00" + tiff
    segment = b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
    return b"\xff\xd8" + segment + b"\xff\xd9"


if __name__ == "__main__":
    import pathlib

    here = pathlib.Path(__file__).parent
    (here / "with_gps.jpg").write_bytes(build(with_gps=True))
    (here / "no_exif.jpg").write_bytes(build(with_exif=False))
    print("wrote fixtures")
