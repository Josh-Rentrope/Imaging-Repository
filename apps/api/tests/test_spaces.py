"""The Spaces backend.

Exercised against a fake client rather than a real bucket: the point is the
*shape* of the thing -- that a store with no directories still satisfies what the
pipeline asks of it, and that its failures arrive in the vocabulary the rest of
the codebase already guards against. Whether DigitalOcean answers correctly is
not something a unit test can establish, and pretending otherwise would be a test
that passes because the fake agrees with the code.

The one that matters most is `test_a_pipeline_run_completes_against_a_bucket`:
everything else here tests a method, and that one tests the deployment.
"""

from __future__ import annotations

import io

import pytest
from fastapi import HTTPException
from helpers import RGBD_CAPTURE  # noqa: E402  (tests/ is on sys.path)

from app.config import Settings
from app.jobs import JobStore
from app.main import build_storage
from app.models import JobCreate, JobStatus
from app.pipeline import BackendRegistry, Stage
from app.sources import SourceStore
from app.spaces import SpacesStorage


def _error(code: str, status_code: int) -> Exception:
    """What botocore raises, imitated far enough to be recognised."""
    error = Exception(f"{code}")
    error.response = {  # type: ignore[attr-defined]
        "Error": {"Code": code},
        "ResponseMetadata": {"HTTPStatusCode": status_code},
    }
    return error


class FakeS3:
    """The parts of the S3 API this backend uses, and no more."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.page_size = 1000
        self.calls: list[str] = []
        self.last_range: str | None = None

    def put_object(self, Bucket: str, Key: str, Body: bytes) -> dict:
        self.calls.append("put_object")
        self.objects[Key] = Body if isinstance(Body, bytes) else bytes(Body)
        return {}

    def get_object(self, Bucket: str, Key: str, Range: str | None = None) -> dict:
        self.calls.append("get_object")
        if Key not in self.objects:
            raise _error("NoSuchKey", 404)
        body = self.objects[Key]
        if Range:
            self.last_range = Range
            start, _, end = Range.removeprefix("bytes=").partition("-")
            body = body[int(start) : int(end) + 1]
        return {"Body": io.BytesIO(body)}

    def head_object(self, Bucket: str, Key: str) -> dict:
        self.calls.append("head_object")
        if Key not in self.objects:
            raise _error("NoSuchKey", 404)
        return {"ContentLength": len(self.objects[Key])}

    def list_objects_v2(self, Bucket: str, Prefix: str, ContinuationToken=None, **kwargs) -> dict:
        self.calls.append("list_objects_v2")
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + self.page_size]
        truncated = len(keys) > start + self.page_size
        listing: dict = {"Contents": [{"Key": key} for key in page], "IsTruncated": truncated}
        if truncated:
            listing["NextContinuationToken"] = str(start + self.page_size)
        return listing

    def delete_objects(self, Bucket: str, Delete: dict) -> dict:
        self.calls.append("delete_objects")
        for entry in Delete["Objects"]:
            self.objects.pop(entry["Key"], None)
        return {}


def bucket(**kwargs) -> tuple[SpacesStorage, FakeS3]:
    fake = FakeS3()
    return SpacesStorage(bucket="imaging", client=fake, **kwargs), fake


# ── the basics ──────────────────────────────────────────────────────────────


def test_a_round_trip_works_the_same_as_a_directory():
    store, fake = bucket()
    ref = store.put("sources/s1/index.json", b"{}")

    # The ref is the same shape the local store mints, because the scheme names
    # the store a deployment uses rather than the backend behind it.
    assert ref == "local://sources/s1/index.json"
    assert store.exists(ref) is True
    assert store.get(ref) == b"{}"
    assert fake.objects == {"sources/s1/index.json": b"{}"}


def test_a_missing_object_is_a_404_not_a_crash():
    """A caller asking for something that is not there is a 404. It is only an
    error if it arrives as anything else, which is how the route would end up
    answering 500 on a deleted artifact."""
    store, _ = bucket()
    with pytest.raises(HTTPException) as missing:
        store.get("local://artifacts/nope/x.ply")
    assert missing.value.status_code == 404
    assert store.exists("local://artifacts/nope/x.ply") is False


def test_a_store_failure_becomes_an_oserror():
    """The vocabulary matters.

    Callers guard against `OSError` because that is what a failed file operation
    raises. If a bucket spelled its failures differently, those guards would exist
    on one backend and silently not on the other -- and the guard that runs during
    startup is the difference between an empty API and one that will not boot.
    """
    store, fake = bucket()

    def denied(*args, **kwargs):
        raise _error("AccessDenied", 403)

    fake.put_object = denied  # type: ignore[method-assign]
    with pytest.raises(OSError) as failed:
        store.put("artifacts/x/y.ply", b"hello")
    assert "AccessDenied" in str(failed.value)


def test_traversal_is_refused_before_the_bucket_sees_it():
    """A bucket has no directories to escape, so the check has to be made on the
    key itself or it would not be made at all."""
    store, _ = bucket()
    with pytest.raises(HTTPException):
        store.put("../outside.json", b"x")
    with pytest.raises(HTTPException):
        store.get("local://../../etc/passwd")


# ── listing ─────────────────────────────────────────────────────────────────


def test_listing_follows_every_page():
    """A bucket truncates at a thousand keys. A listing that stopped at the first
    page would report a partial store as if it were the whole one."""
    store, fake = bucket()
    fake.page_size = 2
    for index in range(5):
        store.put(f"artifacts/r/mesh-{index}.ply", b"x")

    assert store.list_keys("artifacts/") == [
        f"artifacts/r/mesh-{index}.ply" for index in range(5)
    ]
    assert fake.calls.count("list_objects_v2") == 3


def test_listing_something_absent_is_empty_rather_than_an_error():
    store, _ = bucket()
    assert store.list_keys("sources/") == []


# ── removal ─────────────────────────────────────────────────────────────────


def test_removing_a_prefix_takes_the_subtree_and_leaves_the_rest():
    store, _ = bucket()
    store.put("artifacts/a/one.ply", b"1")
    store.put("artifacts/a/two.ply", b"2")
    store.put("artifacts/b/three.ply", b"3")

    assert store.remove_tree("artifacts/a") == 2
    assert store.list_keys("artifacts/") == ["artifacts/b/three.ply"]


def test_removing_a_single_object_works_too():
    """Deleting a job removes `jobs/<id>.json`, which is an object and not a
    prefix -- a bucket makes no distinction, so both cases have to be handled."""
    store, _ = bucket()
    store.put("jobs/j1.json", b"{}")
    store.put("jobs/j2.json", b"{}")

    assert store.remove_tree("jobs/j1.json") == 1
    assert store.list_keys("jobs/") == ["jobs/j2.json"]


def test_removing_something_absent_is_zero():
    store, _ = bucket()
    assert store.remove_tree("artifacts/nothing") == 0


def test_the_bucket_refuses_to_be_emptied():
    """`LocalStorage.reset` deletes a dev directory that is trivially recreated.
    The same call against a bucket is not the same act."""
    store, _ = bucket()
    with pytest.raises(RuntimeError):
        store.reset()


# ── ranges: how one image plane is fetched from a volume ────────────────────


def test_a_range_asks_the_bucket_for_only_that_range():
    """The whole point: the object is addressed, not read.

    Before this, serving one image plane downloaded the entire volume -- tens of
    megabytes over the network for each thumbnail, which is what saturated the
    request path into gateway timeouts.
    """
    store, fake = bucket()
    blob = bytes(range(256)) * 4
    store.put("artifacts/r/volume.bin", blob)

    got = store.get_range("local://artifacts/r/volume.bin", 100, 50)

    assert got == blob[100:150]
    assert fake.last_range == "bytes=100-149", fake.last_range


def test_a_range_past_the_end_is_refused_rather_than_short():
    """A short read means the stored volume is truncated. Returning fewer bytes
    than asked for would reshape into a wrong-sized plane and silently draw the
    wrong image."""
    store, _ = bucket()
    store.put("artifacts/r/volume.bin", b"0123456789")

    with pytest.raises(HTTPException) as short:
        store.get_range("local://artifacts/r/volume.bin", 8, 50)
    assert short.value.status_code == 416


def test_the_plane_offset_matches_slicing_the_whole_array():
    """The arithmetic that decides *which* plane comes back.

    An off-by-one here is not a performance bug, it is a wrong image: the viewer
    would show a different slice than the one asked for, and nothing about the
    response would look wrong. So the range read is checked against the answer
    the old whole-object read gave.
    """
    import numpy as np

    nz, ny, nx = 4, 3, 5
    volume = np.arange(nz * ny * nx, dtype="<f4").reshape(nz, ny, nx)
    blob = volume.tobytes("C")

    store, _ = bucket()
    store.put("artifacts/r/volume.bin", blob)
    ref = "local://artifacts/r/volume.bin"
    plane_bytes = nx * ny * 4

    for index in range(nz):
        by_range = np.frombuffer(
            store.get_range(ref, index * plane_bytes, plane_bytes), dtype="<f4", count=nx * ny
        ).reshape(ny, nx)
        assert np.array_equal(by_range, volume[index]), f"slice {index} came back wrong"


def test_a_local_store_reads_a_range_too():
    """Both backends have to answer the same question, or the slice endpoint
    works on one deployment and not another."""
    import tempfile
    from pathlib import Path

    from app.storage import LocalStorage

    with tempfile.TemporaryDirectory() as tmp:
        storage = LocalStorage(Path(tmp) / "s")
        blob = bytes(range(256)) * 4
        storage.put("artifacts/r/volume.bin", blob)

        assert storage.get_range("local://artifacts/r/volume.bin", 10, 20) == blob[10:30]
        with pytest.raises(HTTPException):
            storage.get_range("local://artifacts/r/volume.bin", 2000, 10)


def test_a_range_outside_the_viewers_own_folder_is_refused(tmp_path):
    """Scoping applies to ranges as much as to whole objects, or it would be a
    way round it."""
    from app.storage import LocalStorage
    from app.tenancy import ViewerScopedStorage, current_folder

    scoped = ViewerScopedStorage(LocalStorage(tmp_path / "s"), enabled=True)
    token = current_folder.set("aaaa")
    try:
        ref = scoped.put("artifacts/x/volume.bin", b"0123456789")
    finally:
        current_folder.reset(token)

    token = current_folder.set("bbbb")
    try:
        with pytest.raises(HTTPException) as refused:
            scoped.get_range(ref, 0, 4)
        assert refused.value.status_code == 404
    finally:
        current_folder.reset(token)


# ── staging, and the run that proves it ─────────────────────────────────────


def test_materialize_stages_a_source_onto_disk():
    store, _ = bucket()
    store.put("sources/s1/files/a.dcm", b"AAA")
    store.put("sources/s1/files/b.dcm", b"BBB")

    sources = SourceStore(store)
    with sources.materialize("s1") as directory:
        assert sorted(path.name for path in directory.iterdir()) == ["a.dcm", "b.dcm"]
        assert (directory / "b.dcm").read_bytes() == b"BBB"
    assert not directory.exists()


def test_a_pipeline_run_completes_against_a_bucket():
    """The deployment, in one test.

    A job reads and writes through the store, and the segmenter path needs a real
    directory, so a run only completes if every part of the abstraction holds at
    once. This is the thing that would otherwise be discovered on the deploy.
    """
    store, fake = bucket()
    jobs = JobStore(BackendRegistry(storage=store), storage=store)

    job = jobs.submit(
        JobCreate(stages=[Stage(op="reconstruct")], capture=RGBD_CAPTURE), False
    )
    finished = jobs.wait(job.job_id, timeout=60)

    assert finished.status is JobStatus.SUCCEEDED, finished.error
    mesh = finished.result["artifacts"][0]
    assert store.get(mesh["ref"])[:4] == b"ply\n"

    # And the whole run landed in the bucket, under the keys it should have.
    keys = store.list_keys("artifacts/")
    assert any(key.endswith("/arch.ply") for key in keys), keys
    assert store.list_keys("jobs/") == [f"jobs/{job.job_id}.json"]


# ── wiring ──────────────────────────────────────────────────────────────────


def test_the_backend_is_chosen_by_configuration():
    chosen = build_storage(
        Settings(
            storage_backend="spaces",
            spaces_bucket="imaging",
            spaces_key_id="key",
            spaces_secret="secret",
        )
    )
    assert isinstance(chosen, SpacesStorage)
    assert chosen.bucket == "imaging"
    assert chosen.region == "nyc3", "the region has to reach the endpoint"


def test_the_local_backend_is_the_default():
    """A deployment that says nothing gets the directory it always had, so the
    local development story does not depend on remembering a new variable."""
    assert isinstance(build_storage(Settings()), object)
    assert type(build_storage(Settings())).__name__ == "LocalStorage"


def test_asking_for_spaces_without_a_bucket_is_refused_at_startup():
    """Refused rather than fallen back: a container filesystem that accepts writes
    and loses them on the next deploy is worse than one that does not start."""
    with pytest.raises(RuntimeError) as refused:
        build_storage(Settings(storage_backend="spaces"))
    assert "BONE_VIEWER_SPACES_BUCKET" in str(refused.value)


def test_an_unknown_backend_is_refused_at_startup():
    with pytest.raises(RuntimeError) as refused:
        build_storage(Settings(storage_backend="azure"))
    assert "azure" in str(refused.value)
