"""Staging: whatever a client sent, as files on disk.

The three ways in -- a folder drop, a zip, a phone capture -- have to arrive at
the store identically, or "all three work" means three code paths that agree by
coincidence. These check the properties that make them identical rather than the
three paths separately.
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image

from app import ingest
from app.ingest import stage_payloads, stage_uploads


class FakeUpload:
    """An `UploadFile` that hands over one chunk per read and counts the reads.

    The count is the point: it is how a test can tell a limit enforced as bytes
    arrive from one enforced after they have all arrived.
    """

    def __init__(self, name: str, chunks: list[bytes]) -> None:
        self.filename = name
        self._chunks = list(chunks)
        self.reads = 0

    async def read(self, size: int = -1) -> bytes:
        self.reads += 1
        return self._chunks.pop(0) if self._chunks else b""


def _stage(uploads, directory: Path) -> list[Path]:
    return asyncio.run(stage_uploads(uploads, directory))


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_loose_files_stage_as_themselves(tmp_path):
    staged = _stage([FakeUpload("a.jpg", [b"one"]), FakeUpload("b.jpg", [b"two"])], tmp_path)

    assert sorted(p.name for p in staged) == ["a.jpg", "b.jpg"]
    assert (tmp_path / "a.jpg").read_bytes() == b"one"


def test_every_staged_thing_is_a_path_not_bytes(tmp_path):
    """The contract that bounds memory. A list of paths costs the same whether the
    capture is six photographs or six hundred; a list of payloads does not."""
    staged = _stage([FakeUpload("a.jpg", [b"x" * 100])], tmp_path)

    assert all(isinstance(p, Path) for p in staged)


def test_a_zip_becomes_its_members(tmp_path):
    data = _zip_bytes({"photo-1.jpg": b"one", "nested/photo-2.jpg": b"two"})

    staged = _stage([FakeUpload("capture.zip", [data])], tmp_path)

    assert sorted(p.name for p in staged) == ["photo-1.jpg", "photo-2.jpg"]


def test_a_folder_drop_is_flattened_the_same_way_a_zip_is(tmp_path):
    """A folder drop sends each file's path relative to the folder. Without
    flattening it, a folder and the same folder zipped differ -- and the folder
    case is the one that would fail outright, because the directory it names is
    not there to move into."""
    staged = _stage([FakeUpload("holiday/IMG_0001.jpg", [b"one"])], tmp_path)

    assert [p.name for p in staged] == ["IMG_0001.jpg"]


def test_a_zip_and_the_same_files_loose_produce_the_same_names(tmp_path):
    """The congruence claim, stated as a test."""
    loose = tmp_path / "loose"
    zipped = tmp_path / "zipped"
    names = {"photo-1.jpg": b"one", "nested/photo-2.jpg": b"two"}

    from_loose = _stage([FakeUpload(name, [data]) for name, data in names.items()], loose)
    from_zip = _stage([FakeUpload("capture.zip", [_zip_bytes(names)])], zipped)

    assert sorted(p.name for p in from_loose) == sorted(p.name for p in from_zip)


def test_duplicate_names_are_kept_apart(tmp_path):
    """The store keys by name, so a second file of the same name would silently
    replace the first. Two directories in one drop can genuinely collide."""
    staged = _stage(
        [FakeUpload("day1/photo.jpg", [b"first"]), FakeUpload("day2/photo.jpg", [b"second"])],
        tmp_path,
    )

    contents = sorted(p.read_bytes() for p in staged)
    assert contents == [b"first", b"second"]
    assert len({p.name for p in staged}) == 2


def test_a_traversal_in_an_entry_name_is_neutralised(tmp_path):
    """Entry names are attacker-chosen.

    Flattening removes the traversal and keeps the file: a name that escapes is
    not the same thing as a file that should be dropped, and dropping it would
    lose a photograph for no reason. `..` reduces to nothing and is the one that
    genuinely is not a file.
    """
    data = _zip_bytes({"../escape.jpg": b"up", "./ok.jpg": b"yes", "..": b"no"})

    staged = _stage([FakeUpload("capture.zip", [data])], tmp_path)

    assert sorted(p.name for p in staged) == ["escape.jpg", "ok.jpg"]
    assert all(p.parent == tmp_path for p in staged)


def test_something_named_like_a_zip_that_is_not_one_is_kept(tmp_path):
    """The extension is a guess and the contents are the fact."""
    staged = _stage([FakeUpload("not-really.zip", [b"just bytes"])], tmp_path)

    assert [p.name for p in staged] == ["not-really.zip"]


def test_an_empty_upload_is_refused(tmp_path):
    with pytest.raises(HTTPException) as raised:
        _stage([], tmp_path)

    assert raised.value.status_code == 400


def test_a_zip_of_nothing_is_refused(tmp_path):
    with pytest.raises(HTTPException) as raised:
        _stage([FakeUpload("empty.zip", [_zip_bytes({})])], tmp_path)

    assert raised.value.status_code == 400


def test_the_total_limit_bites_before_everything_has_been_read(tmp_path, monkeypatch):
    """The difference between refusing an oversized upload and dying on it.

    A limit checked after the last byte has arrived is a limit that runs out of
    memory before it runs out of patience.
    """
    monkeypatch.setattr(ingest, "MAX_TOTAL_BYTES", 4 * 1024)
    chunks = [b"x" * 1024] * 50
    upload = FakeUpload("big.jpg", chunks)

    with pytest.raises(HTTPException) as raised:
        _stage([upload], tmp_path)

    assert raised.value.status_code == 413
    # It stopped early: the remaining chunks were never asked for.
    assert upload.reads < len(chunks)


def test_a_single_oversized_file_is_named(tmp_path, monkeypatch):
    """"upload too large" does not tell anyone which file to look at."""
    monkeypatch.setattr(ingest, "MAX_FILE_BYTES", 2 * 1024)

    with pytest.raises(HTTPException) as raised:
        _stage([FakeUpload("huge.tif", [b"x" * 4096])], tmp_path)

    assert raised.value.status_code == 413
    assert "huge.tif" in raised.value.detail


def test_the_staged_files_outlive_the_call(tmp_path):
    """The store reads them afterwards, so staging must not clean up behind it.
    The caller owns the directory -- which is also what lets a failed conversion
    be the caller's to clean up rather than a silent leak."""
    staged = _stage([FakeUpload("a.jpg", [b"x"])], tmp_path)

    assert staged and all(path.is_file() for path in staged)


# ── formats nothing downstream can read ─────────────────────────────────────


def _heic_bytes() -> bytes:
    """A real HEIC, encoded here rather than committed as a fixture."""
    buffer = io.BytesIO()
    Image.new("RGB", (8, 6), (10, 20, 30)).save(buffer, format="HEIF")
    return buffer.getvalue()


def test_heic_becomes_a_jpeg(tmp_path):
    """An iPhone's originals are HEIC, and only Safari renders one. Storing it
    unconverted produces a source whose every image draws as a blank rectangle."""
    staged = _stage([FakeUpload("IMG_0001.HEIC", [_heic_bytes()])], tmp_path)

    assert [p.name for p in staged] == ["IMG_0001.jpg"]
    assert not list(tmp_path.glob("*.HEIC"))


def test_heic_inside_a_zip_is_converted_too(tmp_path):
    """The zip branch and the loose branch must not diverge here -- that is the
    whole reason the conversion is in staging rather than in either of them."""
    data = _zip_bytes({"nested/IMG_0002.heic": _heic_bytes()})

    staged = _stage([FakeUpload("capture.zip", [data])], tmp_path)

    assert [p.name for p in staged] == ["IMG_0002.jpg"]


def test_a_fetched_heic_is_converted_like_any_other(tmp_path):
    staged = stage_payloads([("IMG_0003.heic", _heic_bytes())], tmp_path)

    assert [p.name for p in staged] == ["IMG_0003.jpg"]


def test_a_converted_frame_does_not_overwrite_an_existing_one(tmp_path):
    """A folder can hold `IMG_1.heic` and `IMG_1.jpg` from different sources, and
    the converted copy must not silently replace the original."""
    staged = _stage(
        [FakeUpload("IMG_1.jpg", [b"already a jpeg"]), FakeUpload("IMG_1.heic", [_heic_bytes()])],
        tmp_path,
    )

    assert sorted(p.name for p in staged) == ["IMG_1-2.jpg", "IMG_1.jpg"]
    assert (tmp_path / "IMG_1.jpg").read_bytes() == b"already a jpeg"


def test_something_named_heic_that_will_not_decode_names_itself(tmp_path):
    with pytest.raises(HTTPException) as raised:
        _stage([FakeUpload("broken.heic", [b"not an image at all"])], tmp_path)

    assert raised.value.status_code == 415
    assert "broken.heic" in raised.value.detail


# ── the remote fetch takes the same route ───────────────────────────────────


def test_payloads_in_memory_are_staged_the_same_way(tmp_path):
    """A fetched dataset and a dropped folder have to reach the store alike."""
    payloads = [("nested/photo-1.jpg", b"one"), ("capture.zip", _zip_bytes({"b.jpg": b"two"}))]

    staged = stage_payloads(payloads, tmp_path)

    # The zip's member is staged; the zip itself is not, because its contents
    # are what it contributed.
    assert sorted(p.name for p in staged) == ["b.jpg", "photo-1.jpg"]


def test_payloads_that_are_not_files_are_refused(tmp_path):
    with pytest.raises(HTTPException):
        stage_payloads([], tmp_path)
