"""What a file can tell you about where it came from.

Hashes, container type, and embedded metadata - most usefully EXIF, which on an
unprocessed camera or phone image carries the make, the model, the capture time
and often the exact coordinates.

Parsed here in pure stdlib rather than via Pillow or exifread. That is not
purism: this reads bytes out of files handed to you by strangers, and every
dependency in that path is attack surface. The parser only ever walks
structure and slices, and never evaluates anything.

Absence of EXIF is itself a finding and is reported as one. Every major
platform strips it on upload, so a photo "straight off someone's phone" with no
EXIF has been through something - which is worth knowing before you build on it.
"""

from __future__ import annotations

import hashlib
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

log = logging.getLogger("erebus.osint.media")

#: Container signatures. Extensions lie; magic bytes lie less.
SIGNATURES = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"RIFF", "riff (avi/webp/wav)"),
    (b"\x1a\x45\xdf\xa3", "matroska (mkv/webm)"),
    (b"PK\x03\x04", "zip (also docx/xlsx/odt)"),
    (b"\x00\x00\x00\x18ftyp", "mp4"),
    (b"\x00\x00\x00\x20ftyp", "mp4"),
]

# EXIF tag numbers we care about, per IFD.
_IFD0 = {0x010F: "make", 0x0110: "model", 0x0131: "software",
         0x0132: "datetime", 0x8298: "copyright", 0x013B: "artist"}
_EXIF = {0x9003: "datetime_original", 0x9004: "datetime_digitized",
         0xA002: "width", 0xA003: "height", 0x829A: "exposure_time",
         0x829D: "f_number", 0x8827: "iso", 0xA430: "camera_owner",
         0xA431: "body_serial", 0xA433: "lens_make", 0xA434: "lens_model"}
_GPS = {0x0000: "gps_version", 0x0001: "lat_ref", 0x0002: "lat",
        0x0003: "lon_ref", 0x0004: "lon", 0x0005: "alt_ref",
        0x0006: "alt", 0x0007: "gps_time", 0x001D: "gps_date"}

_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}

#: Which extensions legitimately go with which detected type. Substring
#: matching is not good enough here - "jpg" is not a substring of "jpeg", so
#: naive comparison flags every ordinary photo as disguised, and a checker that
#: cries wolf on normal files is worse than no checker.
EXTENSIONS = {
    "image/jpeg": {"jpg", "jpeg", "jpe", "jfif"},
    "image/png": {"png"},
    "image/gif": {"gif"},
    "application/pdf": {"pdf"},
    "riff (avi/webp/wav)": {"avi", "webp", "wav"},
    "matroska (mkv/webm)": {"mkv", "webm"},
    "zip (also docx/xlsx/odt)": {
        "zip", "docx", "xlsx", "pptx", "odt", "ods", "odp", "epub", "jar", "apk",
    },
    "mp4": {"mp4", "m4v", "m4a", "mov"},
}


@dataclass
class MediaReport:
    path: str
    bytes: int = 0
    sha256: str = ""
    md5: str = ""
    detected_type: str | None = None
    claimed_type: str | None = None
    exif: dict[str, Any] = field(default_factory=dict)
    gps: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    reverse_search: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            f"{Path(self.path).name}",
            f"  type      {self.detected_type or 'unknown'}"
            + (f"   (extension claims {self.claimed_type})"
               if self.claimed_type and self.claimed_type != self.detected_type
               else ""),
            f"  bytes     {self.bytes}",
            f"  sha256    {self.sha256}",
        ]
        if self.exif:
            lines.append("  exif")
            for key, value in self.exif.items():
                lines.append(f"    {key:<20} {value}")
        if self.gps:
            lines.append("  location")
            lines.append(f"    coordinates          {self.gps['lat']:.6f}, "
                         f"{self.gps['lon']:.6f}")
            lines.append(f"    maps                 {self.gps['maps']}")
            if self.gps.get("altitude") is not None:
                lines.append(f"    altitude             {self.gps['altitude']:.0f} m")
        for note in self.notes:
            lines.append(f"  ! {note}")
        return "\n".join(lines)


def _sniff(head: bytes) -> str | None:
    for signature, name in SIGNATURES:
        if head.startswith(signature):
            return name
    # ftyp boxes carry a length prefix that varies.
    if len(head) > 12 and head[4:8] == b"ftyp":
        return f"mp4/mov ({head[8:12].decode('ascii', 'replace')})"
    return None


def _rational(raw: bytes, order: str) -> float | None:
    numerator, denominator = struct.unpack(order + "II", raw[:8])
    return numerator / denominator if denominator else None


def _read_ifd(blob: bytes, offset: int, order: str,
              wanted: dict[int, str]) -> tuple[dict, dict]:
    """Return (values, pointers) for one IFD. Never raises on malformed input."""
    values: dict[str, Any] = {}
    pointers: dict[int, int] = {}
    try:
        count = struct.unpack_from(order + "H", blob, offset)[0]
    except struct.error:
        return values, pointers

    for index in range(count):
        entry = offset + 2 + index * 12
        try:
            tag, kind, length, payload = struct.unpack_from(
                order + "HHI4s", blob, entry
            )
        except struct.error:
            break

        # Sub-IFD pointers: EXIF and GPS.
        if tag in (0x8769, 0x8825):
            pointers[tag] = struct.unpack(order + "I", payload)[0]
            continue

        name = wanted.get(tag)
        if name is None:
            continue

        size = _TYPE_SIZES.get(kind, 1) * length
        if size > 4:
            start = struct.unpack(order + "I", payload)[0]
            raw = blob[start : start + size]
        else:
            raw = payload[:size]
        if len(raw) < size:
            continue

        try:
            if kind == 2:                                  # ASCII
                values[name] = raw.split(b"\x00")[0].decode("utf-8", "replace").strip()
            elif kind == 3:                                # SHORT
                numbers = struct.unpack(order + "H" * length, raw[: 2 * length])
                values[name] = numbers[0] if length == 1 else list(numbers)
            elif kind == 4:                                # LONG
                numbers = struct.unpack(order + "I" * length, raw[: 4 * length])
                values[name] = numbers[0] if length == 1 else list(numbers)
            elif kind in (5, 10):                          # RATIONAL
                parts = [_rational(raw[i * 8 :], order) for i in range(length)]
                values[name] = parts[0] if length == 1 else parts
            elif kind == 1:                                # BYTE
                values[name] = list(raw[:length])
        except (struct.error, UnicodeDecodeError):
            continue

    return values, pointers


def _to_degrees(parts, ref) -> float | None:
    """EXIF stores coordinates as degrees/minutes/seconds rationals."""
    if not parts or len(parts) < 3 or any(p is None for p in parts[:3]):
        return None
    degrees = parts[0] + parts[1] / 60.0 + parts[2] / 3600.0
    if isinstance(ref, str) and ref.upper() in ("S", "W"):
        degrees = -degrees
    return degrees


def read_exif(data: bytes) -> tuple[dict, dict | None, list[str]]:
    """Parse EXIF from a JPEG. Returns (fields, gps, notes)."""
    notes: list[str] = []
    if not data.startswith(b"\xff\xd8"):
        return {}, None, notes

    # Walk JPEG segments for APP1/Exif.
    position = 2
    exif_blob = None
    while position < len(data) - 4:
        if data[position] != 0xFF:
            break
        marker = data[position + 1]
        if marker in (0xD8, 0xD9):
            position += 2
            continue
        try:
            length = struct.unpack_from(">H", data, position + 2)[0]
        except struct.error:
            break
        if marker == 0xE1 and data[position + 4 : position + 10] == b"Exif\x00\x00":
            exif_blob = data[position + 10 : position + 2 + length]
            break
        if marker == 0xDA:      # start of scan; metadata is behind us
            break
        position += 2 + length

    if exif_blob is None:
        notes.append(
            "No EXIF present. Every major platform strips it on upload, so "
            "this has been through something - it is not straight off a camera."
        )
        return {}, None, notes

    if exif_blob[:2] == b"II":
        order = "<"
    elif exif_blob[:2] == b"MM":
        order = ">"
    else:
        return {}, None, ["EXIF block present but its byte order is invalid."]

    try:
        first_ifd = struct.unpack_from(order + "I", exif_blob, 4)[0]
    except struct.error:
        return {}, None, ["EXIF block is truncated."]

    fields, pointers = _read_ifd(exif_blob, first_ifd, order, _IFD0)

    if 0x8769 in pointers:
        sub, _ = _read_ifd(exif_blob, pointers[0x8769], order, _EXIF)
        fields.update(sub)

    gps = None
    if 0x8825 in pointers:
        raw_gps, _ = _read_ifd(exif_blob, pointers[0x8825], order, _GPS)
        lat = _to_degrees(raw_gps.get("lat"), raw_gps.get("lat_ref"))
        lon = _to_degrees(raw_gps.get("lon"), raw_gps.get("lon_ref"))
        if lat is not None and lon is not None:
            gps = {
                "lat": lat,
                "lon": lon,
                "maps": f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}",
                "osm": f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=17/{lat:.6f}/{lon:.6f}",
            }
            altitude = raw_gps.get("alt")
            if isinstance(altitude, (int, float)):
                if raw_gps.get("alt_ref") in (1, [1]):
                    altitude = -altitude
                gps["altitude"] = altitude
            if raw_gps.get("gps_date"):
                gps["date"] = raw_gps["gps_date"]
            notes.append(
                "Coordinates present. Treat as a lead, not proof - EXIF is "
                "trivially editable and the clock may be wrong."
            )

    return fields, gps, notes


def reverse_search_links(sha256: str) -> dict[str, str]:
    """Where to take an image next. Constructed URLs only - nothing is uploaded.

    These need a publicly reachable image URL to be useful; for a local file you
    upload it yourself on the page. Listed because remembering which engines are
    worth trying, and that they differ enormously by region, is half the work.
    """
    return {
        "google_lens": "https://lens.google.com/upload",
        "yandex": "https://yandex.com/images/search?rpt=imageview",
        "tineye": "https://tineye.com/",
        "bing": "https://www.bing.com/visualsearch",
    }


def examine(path: str | Path, audit=None) -> MediaReport:
    path = Path(path)
    report = MediaReport(path=str(path))

    if not path.is_file():
        report.notes.append("File does not exist.")
        return report

    data = path.read_bytes()
    report.bytes = len(data)
    report.sha256 = hashlib.sha256(data).hexdigest()
    # MD5 too: still the join key for most existing hash sets and takedown
    # databases, weak collision resistance notwithstanding.
    report.md5 = hashlib.md5(data).hexdigest()   # noqa: S324
    report.detected_type = _sniff(data[:16])
    report.claimed_type = path.suffix.lower().lstrip(".") or None

    if report.detected_type and report.claimed_type:
        allowed = EXTENSIONS.get(report.detected_type)
        if allowed is None:
            # mp4/mov detection carries the brand in the string.
            if report.detected_type.startswith("mp4/mov"):
                allowed = EXTENSIONS["mp4"]
        if allowed is not None and report.claimed_type not in allowed:
            report.notes.append(
                f"Extension says .{report.claimed_type} but the contents are "
                f"{report.detected_type}. Renamed, mislabelled, or disguised."
            )

    if report.detected_type == "image/jpeg":
        report.exif, report.gps, notes = read_exif(data)
        report.notes.extend(notes)
        report.reverse_search = reverse_search_links(report.sha256)
    elif report.detected_type == "image/png":
        report.reverse_search = reverse_search_links(report.sha256)
        report.notes.append("PNG: no EXIF by convention; screenshots are usually PNG.")

    if audit is not None:
        audit.record("osint.examine", file=path.name, sha256=report.sha256,
                     type=report.detected_type, had_gps=bool(report.gps))
    return report
