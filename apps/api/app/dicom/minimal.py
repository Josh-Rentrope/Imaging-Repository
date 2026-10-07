"""Minimal DICOM header reader -- NO DEPENDENCIES, deliberately narrow.

Scope, stated honestly:
  HANDLES   Explicit and Implicit VR, little endian, uncompressed, Part 10 files
            with a `DICM` preamble.
  IGNORES   Big endian, deflated datasets, encapsulated/compressed pixel data,
            and sequences (we skip over them rather than descending).

Implicit VR is here because leaving it out was losing whole collections. It
stores no VR field — element, then a 4-byte length, then the value — and it is
the encoding of the LIDC-IDRI chest CT studies, among a great deal else. Files
like that parsed as `None`, which downstream read as "not DICOM at all" and
turned a 251-slice CT into a set of photographs. The VRs for the handful of tags
read here are known from the standard, so a dictionary entry is enough; we do not
need to decode the rest of the file.

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
TAG_TRANSFER_SYNTAX = (0x0002, 0x0010)

#: VRs that carry a 4-byte length plus 2 reserved bytes after the VR.
_LONG_VRS = {b"OB", b"OW", b"OF", b"SQ", b"UT", b"UN"}

_PREAMBLE = b"DICM"

#: Transfer syntaxes, by the UIDs that decide how the dataset is encoded.
_IMPLICIT_VR_LE = "1.2.840.10008.1.2"
_EXPLICIT_VR_LE = "1.2.840.10008.1.2.1"
_EXPLICIT_VR_BE = "1.2.840.10008.1.2.2"  # retired; refused rather than guessed
_DEFLATED = "1.2.840.10008.1.2.1.99"  # the dataset itself is compressed

#: What the standard says these tags are. Implicit VR stores no VR, so a file
#: that does not say must be read from the dictionary — and getting this wrong
#: is silent: Rows decoded as ASCII yields a number rather than an error.
_IMPLICIT_VR: dict[tuple[int, int], bytes] = {
    TAG_MODALITY: b"CS",
    TAG_SERIES_DESCRIPTION: b"LO",
    TAG_SOP_INSTANCE_UID: b"UI",
    TAG_STUDY_UID: b"UI",
    TAG_SERIES_UID: b"UI",
    TAG_INSTANCE_NUMBER: b"IS",
    TAG_ROWS: b"US",
    TAG_COLUMNS: b"US",
}


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
        # The File Meta Information group is always Explicit VR Little Endian,
        # whatever the dataset turns out to be, and it names that encoding.
        while pos + 8 <= len(data):
            if int.from_bytes(data[pos : pos + 2], "little") != 0x0002:
                break
            step = _read_element(data, pos, explicit=True)
            if step is None:
                break
            tag, vr, raw, pos = step
            _absorb(header, tag, vr, raw)

        if header.transfer_syntax in (_EXPLICIT_VR_BE, _DEFLATED):
            # Big endian is retired and deflated data is compressed. Both could
            # be decoded and neither is worth guessing at: a header read with the
            # wrong byte order is wrong in a way that still looks like an answer.
            return None

        # An absent transfer syntax means the meta group was unreadable. A Part
        # 10 file with nothing else to go on is implicit VR by default.
        explicit = header.transfer_syntax != _IMPLICIT_VR_LE

        while pos + 8 <= len(data):
            step = _read_element(data, pos, explicit)
            if step is None:
                # A sequence, a truncated element, or bytes that are not an
                # element at all. Stop rather than produce a plausible-looking
                # wrong answer from whatever follows.
                break
            tag, vr, raw, pos = step
            _absorb(header, tag, vr, raw)
    except (IndexError, ValueError):
        pass

    if header.series_uid is None and header.modality is None:
        return None
    return header


def _read_element(
    data: bytes, pos: int, explicit: bool
) -> tuple[tuple[int, int], bytes, bytes, int] | None:
    """One element: (tag, vr, value, position after it), or None to stop.

    The only difference between the two encodings is whether a VR sits between
    the tag and the length. Everything after that is the same, so both are read
    here rather than in two near-identical walks.
    """
    if pos + 8 > len(data):
        return None

    tag = (
        int.from_bytes(data[pos : pos + 2], "little"),
        int.from_bytes(data[pos + 2 : pos + 4], "little"),
    )

    if explicit:
        vr = data[pos + 4 : pos + 6]
        if not _looks_like_vr(vr):
            return None
        if vr in _LONG_VRS:
            if pos + 12 > len(data):
                return None
            length = int.from_bytes(data[pos + 8 : pos + 12], "little")
            value_start = pos + 12
        else:
            length = int.from_bytes(data[pos + 6 : pos + 8], "little")
            value_start = pos + 8
    else:
        vr = _IMPLICIT_VR.get(tag, b"UN")
        length = int.from_bytes(data[pos + 4 : pos + 8], "little")
        value_start = pos + 8

    if length == 0xFFFFFFFF:
        # Undefined length: a sequence, whose contents we do not descend into.
        return None

    value_end = value_start + length
    if value_end > len(data):
        return None

    return tag, vr, data[value_start:value_end], value_end


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
