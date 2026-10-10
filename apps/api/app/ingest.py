"""Turning whatever a client sent into files on disk.

Four ways in -- a folder drop, a zip, a phone capture, and a URL the server
fetched -- and they have to produce the same thing, because everything
downstream assumes one shape: a flat list of files, one per image or one per
DICOM instance. Keeping that conversion in one place is what makes the four
congruent.

It is a function of its own for a second reason. Reading every upload into a
single list of bytes puts the whole capture in the process at once;  Each upload is copied in chunks and never held whole.
"""

from __future__ import annotations

import shutil
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

import pillow_heif
from fastapi import HTTPException, UploadFile, status
from PIL import Image, ImageOps

from .sources import MAX_FILES, MAX_TOTAL_BYTES

# Pillow does not read HEIC without being told to, and a phone's originals are
# HEIC. Registered once, here, so every path through staging gets it.
pillow_heif.register_heif_opener()

#: How much is read at a time. Large enough that the loop is not the cost, small
#: enough that nothing resembling a whole photograph is ever resident.
_CHUNK = 1 << 20

#: Refused by name rather than by total, so the message says which file.
MAX_FILE_BYTES = 512 * 1024 * 1024

_ZIP_SUFFIXES = (".zip",)

#: Formats no browser renders and no downstream consumer reads, converted on the
#: way in. HEIC is the one that matters: it is what an iPhone writes, so a folder
#: dragged from Photos is full of it, and storing it unconverted produces a
#: source whose every image draws as a blank rectangle.
_TRANSCODE = {".heic", ".heif"}

#: High enough that the re-encode is not the limiting factor. HEIC is already
#: lossy, so this costs a second generation rather than a first.
_JPEG_QUALITY = 92


async def stage_uploads(uploads: Sequence[UploadFile], directory: Path) -> list[Path]:
    """Every upload as a file under `directory`, zips expanded.

    The caller owns `directory` and its lifetime: staging writes into it and
    says nothing about when it should go away, because the files have to outlive
    this call -- the store reads them.
    """
    if not uploads:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="no files uploaded")

    directory.mkdir(parents=True, exist_ok=True)
    incoming = directory / "incoming"
    incoming.mkdir(exist_ok=True)

    budget = _Budget()
    taken: set[str] = set()
    staged: list[Path] = []

    # Zips are expanded into the same directory as everything else, so a zip and
    # a folder of the same photographs produce identical sources -- which is the
    # point of doing this here rather than in the zip's own branch.
    for index, upload in enumerate(uploads):
        name = upload.filename or "unnamed"
        spool = incoming / f"{index:05d}.part"
        await _spool(upload, spool, budget, name)
        staged.extend(_fan_out(spool, name, directory, budget, taken))

    if not staged:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="no files uploaded")
    return _usable(staged, taken)


def _usable(staged: list[Path], taken: set[str]) -> list[Path]:
    """Convert anything nothing downstream can read.

    Applied to every path, whichever way it arrived -- loose, from inside a zip,
    or from the server's own fetch. That is the reason it lives here: a phone
    capture and a folder of iPhone originals are the same problem and should not
    need two solutions.
    """
    return [
        _transcode(path, taken) if path.suffix.lower() in _TRANSCODE else path
        for path in staged
    ]


def _transcode(path: Path, taken: set[str]) -> Path:
    """A JPEG beside the original, for a format nothing can read.

    The rotation is baked in rather than carried as a tag. The copy has no reason
    to keep one, and anything that ignores it draws the photograph on its side.
    """
    target = _unique(path.parent, f"{path.stem}.jpg", taken)
    try:
        with Image.open(path) as image:
            ImageOps.exif_transpose(image).convert("RGB").save(
                target, format="JPEG", quality=_JPEG_QUALITY
            )
    except (OSError, ValueError) as exc:
        # Named, because "a file could not be read" across a folder of four
        # hundred photographs is not something anyone can act on.
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"{path.name} could not be read as an image: {exc}",
        ) from exc

    path.unlink()
    return target


def stage_payloads(payloads: Sequence[tuple[str, bytes]], directory: Path) -> list[Path]:
    """The same staging for bytes already in memory, as the remote fetch has.

    Synchronous because there is nothing to await: the bytes arrived whole. It
    exists so a fetched dataset and an uploaded one reach the store identically.
    """
    if not payloads:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="nothing to import")

    directory.mkdir(parents=True, exist_ok=True)
    incoming = directory / "incoming"
    incoming.mkdir(exist_ok=True)

    budget = _Budget()
    taken: set[str] = set()
    staged: list[Path] = []

    for index, (name, data) in enumerate(payloads):
        spool = incoming / f"{index:05d}.part"
        spool.write_bytes(data)
        budget.spend(len(data))
        staged.extend(_fan_out(spool, name, directory, budget, taken))

    if not staged:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="nothing to import")
    return _usable(staged, taken)


class _Budget:
    """A running total.

    Enforced as bytes arrive rather than once they have all arrived, which is the
    difference between refusing an oversized upload and dying on it.
    """

    def __init__(self) -> None:
        self.bytes = 0

    def spend(self, size: int) -> None:
        self.bytes += size
        if self.bytes > MAX_TOTAL_BYTES:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"upload exceeds {MAX_TOTAL_BYTES // 1024**3} GiB",
            )


async def _spool(upload: UploadFile, target: Path, budget: _Budget, name: str) -> None:
    written = 0
    with target.open("wb") as out:
        while chunk := await upload.read(_CHUNK):
            written += len(chunk)
            if written > MAX_FILE_BYTES:
                # Named as the client sent it. `target` is a numbered spool file,
                # and a message about `00003.part` tells the reader nothing about
                # which of their files is the problem.
                raise HTTPException(
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=f"{name} exceeds {MAX_FILE_BYTES // 1024**2} MB",
                )
            budget.spend(len(chunk))
            out.write(chunk)


def _fan_out(
    source: Path, original_name: str, directory: Path, budget: _Budget, taken: set[str]
) -> list[Path]:
    """One spooled file into the staged set: as itself, or as its zip members."""
    if original_name.lower().endswith(_ZIP_SUFFIXES):
        try:
            with zipfile.ZipFile(source) as archive:
                return _members(archive, directory, budget, taken)
        except zipfile.BadZipFile:
            # Named like a zip and is not one. Kept as itself rather than
            # refused: the extension is a guess and the contents are the fact.
            pass

    # Flattened here too, not only for zip members. A folder drop sends each
    # file's path relative to the folder it came from, so `holiday/IMG_1.jpg` is
    # an ordinary upload name and has to become a file in one flat directory like
    # everything else -- which is what makes a folder, a zip and a capture
    # indistinguishable by the time the store sees them.
    target = _unique(directory, _safe_name(original_name) or "unnamed", taken)
    shutil.move(source, target)
    return [target]


def _members(
    archive: zipfile.ZipFile, directory: Path, budget: _Budget, taken: set[str]
) -> list[Path]:
    """Zip members, written one at a time.

    The archive is the only thing held, and it is already on disk. The budget is
    spent again for each member, so a zip is counted as slightly more than its
    own size -- conservative, and it refuses early rather than late.
    """
    written: list[Path] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = _safe_name(info.filename)
        if name is None:
            continue

        target = _unique(directory, name, taken)
        with archive.open(info) as member, target.open("wb") as out:
            while chunk := member.read(_CHUNK):
                budget.spend(len(chunk))
                out.write(chunk)
        written.append(target)

        if len(written) > MAX_FILES:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"more than {MAX_FILES} files",
            )
    return written


def _safe_name(raw: str) -> str | None:
    """The basename, or None when the entry is not a file.

    Entry names are attacker-chosen. A name that reduces to `.` or `..`, or to
    nothing, would become a storage key the store refuses -- refused here too,
    rather than surfacing as a confusing failure on an otherwise valid upload.
    Nested paths are flattened because a source is already its own directory and
    the path inside the archive carries no information.
    """
    name = PurePosixPath(raw.replace("\\", "/")).name
    return name if name not in ("", ".", "..") else None


def _unique(directory: Path, name: str, taken: set[str]) -> Path:
    """A path no other staged file has.

    Duplicates are real: a folder drop of two directories can hold two files of
    the same name, and the store keys by name, so the second would silently
    replace the first. Suffixed rather than overwritten.
    """
    candidate = directory / name
    if name not in taken:
        taken.add(name)
        return candidate

    stem, suffix = candidate.stem, candidate.suffix
    index = 2
    while f"{stem}-{index}{suffix}" in taken:
        index += 1
    name = f"{stem}-{index}{suffix}"
    taken.add(name)

    if len(taken) > MAX_FILES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"more than {MAX_FILES} files",
        )
    return directory / name
