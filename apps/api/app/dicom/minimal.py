"""Minimal DICOM header reader -- NO DEPENDENCIES, deliberately narrow.

Scope, stated honestly:
  HANDLES   Explicit VR, little endian (the overwhelmingly common case for
            CT/CBCT exports), uncompressed, Part 10 files with a `DICM` preamble.
  IGNORES   Implicit VR, big endian, deflated datasets, encapsulated/compressed
            pixel data, and sequences (we skip over them rather than descending).

It reads *metadata only*. It does not explicitly decode pixels:
That will first be done through Cornerstone3D in the browser

Replace with pydicom when pixel data or compressed transfer syntaxes are needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Tags we care about, as (group, element).
TAG_MODALITY = (0x0008, 0x0060)
TAG_SERIES_DESCRIPTION = (0x0008, 0x103E)
TAG_SOP_INSTANCE_UID = (0x0008, 0x0018)
TAG_STUDY_UID = (0x0020, 0x000D)
TAG_SERIES_UID = (0x0020, 0x000E)
TAG_INSTANCE_NUMBER = (0x0020, 0x0013)
TAG_ROWS = (0x0028, 0x0010)
TAG_COLUMNS = (0x0028, 0x0011)

#: VRs that carry a 4-byte length plus 2 reserved bytes after the VR.
_LONG_VRS = {b"OB", b"OW", b"OF", b"SQ", b"UT", b"UN"}
_UID_VRS = {b"UI"}

_PREAMBLE = b"DICM"


@dataclass
class DicomHeader:
    modality: str | None = None
    series_description: str | None = None
    study_uid: str | None = None
    series_uid: str | None = None
    sop_instance_uid: str | None = None
    instance_number: int | None = None
    rows: int | None = None
    columns: int | None = None
    transfer_syntax: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


def has_preamble(data: bytes) -> bool:
    return len(data) >= 132 and data[128:132] == _PREAMBLE


def parse_file(data: bytes) -> DicomHeader | None:
    """Parse a Part 10 DICOM file's metadata. Returns None if out of scope."""
    if not has_preamble(data):
        return None

    header = DicomHeader()
    pos = 132

    try:
        while pos + 8 <= len(data):
            group = int.from_bytes(data[pos : pos + 2], "little")
            element = int.from_bytes(data[pos + 2 : pos + 4], "little")
            vr = data[pos + 4 : pos + 6]

            if not _looks_like_vr(vr):
                # Implicit VR, or we have walked off the rails. Bail rather than
                # produce a plausible-looking wrong answer.
                return header if header.series_uid or header.modality else None

            if vr in _LONG_VRS:
                if pos + 12 > len(data):
                    break
                length = int.from_bytes(data[pos + 8 : pos + 12], "little")
                value_start = pos + 12
            else:
                length = int.from_bytes(data[pos + 6 : pos + 8], "little")
                value_start = pos + 8

            if length == 0xFFFFFFFF:  # undefined length (sequence) -- skip out
                return header if header.series_uid or header.modality else None

            value_end = value_start + length
            if value_end > len(data):
                break

            _absorb(header, (group, element), vr, data[value_start:value_end])
            pos = value_end
    except (IndexError, ValueError):
        return header if header.series_uid or header.modality else None

    if header.series_uid is None and header.modality is None:
        return None
    return header


def _looks_like_vr(vr: bytes) -> bool:
    return len(vr) == 2 and vr.isalpha() and vr.isupper()


def _absorb(header: DicomHeader, tag: tuple[int, int], vr: bytes, raw: bytes) -> None:
    if tag == TAG_MODALITY:
        header.modality = _text(raw) or None
    elif tag == TAG_SERIES_DESCRIPTION:
        header.series_description = _text(raw) or None
    elif tag == TAG_STUDY_UID:
        header.study_uid = _text(raw) or None
    elif tag == TAG_SERIES_UID:
        header.series_uid = _text(raw) or None
    elif tag == TAG_SOP_INSTANCE_UID:
        header.sop_instance_uid = _text(raw) or None
    elif tag == TAG_INSTANCE_NUMBER:
        # IS: integer stored as ASCII.
        header.instance_number = _as_int(_text(raw))
    elif tag == TAG_ROWS:
        header.rows = _as_number(raw, vr)
    elif tag == TAG_COLUMNS:
        header.columns = _as_number(raw, vr)
    elif tag == (0x0002, 0x0010):
        header.transfer_syntax = _text(raw) or None


def _text(raw: bytes) -> str:
    return raw.decode("ascii", errors="replace").strip("\x00 ")


def _as_number(raw: bytes, vr: bytes) -> int | None:
    """Decode a numeric VR.

    These carry BINARY values, not ASCII. Rows/Columns are `US` -- a 2-byte
    little-endian unsigned short -- and decoding them as text yields garbage
    rather than an exception, so the failure is silent. Handle the VR properly
    or return None.
    """
    try:
        if vr == b"US":
            return int.from_bytes(raw[:2], "little", signed=False)
        if vr == b"UL":
            return int.from_bytes(raw[:4], "little", signed=False)
        if vr == b"SS":
            return int.from_bytes(raw[:2], "little", signed=True)
        if vr == b"SL":
            return int.from_bytes(raw[:4], "little", signed=True)
    except (IndexError, ValueError):
        return None
    # Some producers write numeric VRs as ASCII anyway; accept that too.
    return _as_int(_text(raw))


def _as_int(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None
