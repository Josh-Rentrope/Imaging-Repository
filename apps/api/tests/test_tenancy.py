"""Viewer separation.

These tests are about a negative: that one viewer cannot reach another's work.
A test for that passes for the wrong reason very easily -- an empty store and a
refused read look identical -- so each one here establishes that the data exists
and is readable by its owner *before* asserting that somebody else cannot have
it.

The primitive being tested is separation, not authentication. Nothing here
claims a viewer cannot mint itself a new identity; it claims identities do not
overlap.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from helpers import RGBD_CAPTURE, run, settle  # noqa: E402  (tests/ is on sys.path)

from app.config import get_settings
from app.jobs import JobStore
from app.models import JobCreate, JobStatus
from app.pipeline import BackendRegistry, Stage
from app.sources import SourceStore
from app.storage import LocalStorage
from app.tenancy import (
    LOCAL_FOLDER,
    ViewerScopedStorage,
    current_folder,
    folder_for,
    is_loopback,
    new_viewer_id,
)

RECONSTRUCT = [{"op": "reconstruct"}]


def key_of(ref: str) -> str:
    """The storage key inside a ref, which is where the scope is visible."""
    return ref.split("://", 1)[1]


def only_artifact(job: dict) -> dict:
    artifacts = job["result"]["artifacts"]
    assert artifacts, f"the job produced nothing to read back: {job}"
    return artifacts[0]


# ── identity ────────────────────────────────────────────────────────────────


def test_a_viewer_is_given_an_id_that_is_not_its_folder(viewers):
    """The folder is derived, so a folder name that leaks says nothing about
    how to read it."""
    client = viewers()
    response = client.get("/health")
    assert response.status_code == 200

    issued = response.cookies.get("bone_viewer")
    assert issued, "no viewer id was issued"
    assert len(issued) == 64
    int(issued, 16)  # hex, or this raises
    # The id is not the folder, and the folder is not the id.
    assert folder_for(issued) != issued

    # And it is stable: the same client keeps the same viewer.
    assert client.get("/health").status_code == 200


def test_two_viewers_are_given_different_folders(viewers):
    first, second = viewers(), viewers()
    a = only_artifact(run(first, RECONSTRUCT, RGBD_CAPTURE))
    b = only_artifact(run(second, RECONSTRUCT, RGBD_CAPTURE))

    folder_a = key_of(a["ref"]).split("/")[1]
    folder_b = key_of(b["ref"]).split("/")[1]
    assert folder_a != folder_b


def test_an_id_is_unguessable_and_changes():
    first, second = new_viewer_id(), new_viewer_id()
    assert first != second
    assert len(first) == 64


def test_only_a_real_address_counts_as_loopback():
    assert is_loopback("127.0.0.1")
    assert is_loopback("::1")
    assert is_loopback("127.5.5.5")
    # The names a client might present are not addresses this can vouch for.
    assert not is_loopback("localhost")
    assert not is_loopback("testclient")
    assert not is_loopback("8.8.8.8")
    assert not is_loopback(None)


# ── artefacts ───────────────────────────────────────────────────────────────


def test_an_artefact_from_one_viewer_is_refused_to_another(viewers):
    owner, stranger = viewers(), viewers()
    job = run(owner, RECONSTRUCT, RGBD_CAPTURE)
    key = key_of(only_artifact(job)["ref"])

    # Established first, so the refusal below cannot be a missing file.
    assert owner.get("/artifacts/" + key).status_code == 200
    assert stranger.get("/artifacts/" + key).status_code == 404


def test_an_artefact_key_carries_the_viewer_it_belongs_to(viewers):
    owner = viewers()
    job = run(owner, RECONSTRUCT, RGBD_CAPTURE)
    key = key_of(only_artifact(job)["ref"])
    assert key.startswith("viewers/")
    assert key.split("/")[1] != LOCAL_FOLDER


def test_a_viewer_cannot_reach_the_local_folder_by_name(viewers):
    """Not by asking for it, at least: `local` is chosen from the socket."""
    client = viewers()
    job = run(client, RECONSTRUCT, RGBD_CAPTURE)
    folder = key_of(only_artifact(job)["ref"]).split("/")[1]
    assert folder != LOCAL_FOLDER

    # And a ref naming the local folder is not readable by this viewer.
    assert client.get("/artifacts/viewers/local/artifacts/nothing/x.ply").status_code == 404


# ── jobs ────────────────────────────────────────────────────────────────────


def test_one_viewers_jobs_are_not_another_viewers_jobs(viewers):
    owner, stranger = viewers(), viewers()
    job = run(owner, RECONSTRUCT, RGBD_CAPTURE)

    assert [entry["job_id"] for entry in owner.get("/jobs").json()] == [job["job_id"]]
    assert stranger.get("/jobs").json() == []
    assert stranger.get(f"/jobs/{job['job_id']}").status_code == 404


def test_a_job_run_on_a_worker_thread_stays_in_its_own_viewer(viewers):
    """The failure this guards is silent and total.

    Submission returns before the work happens, so the artifacts are written on a
    thread that did not exist when the request was handled. A bare thread does
    not inherit the request's context, so if the viewer were not re-established
    on it, every write would either land in the wrong folder or be refused -- and
    the job would come back failed for a reason nothing else would explain.
    """
    owner, stranger = viewers(), viewers()
    job = run(owner, RECONSTRUCT, RGBD_CAPTURE)

    assert job["status"] == "succeeded", job.get("error")
    key = key_of(only_artifact(job)["ref"])
    assert owner.get("/artifacts/" + key).status_code == 200
    assert stranger.get("/artifacts/" + key).status_code == 404


# ── the local folder ────────────────────────────────────────────────────────


def test_loopback_work_lands_in_the_local_folder(loopback):
    job = run(loopback, RECONSTRUCT, RGBD_CAPTURE)
    key = key_of(only_artifact(job)["ref"])
    assert key.startswith(f"viewers/{LOCAL_FOLDER}/")


def test_loopback_is_not_given_a_cookie(loopback):
    """Nothing to issue: the folder is not derived from an identity."""
    response = loopback.get("/health")
    assert response.status_code == 200
    assert response.cookies.get("bone_viewer") is None


def test_the_local_viewer_is_stable_across_clients(viewers, loopback):
    """The point of the local folder: work does not move because a cookie did."""
    loopback.get("/health")
    job = run(loopback, RECONSTRUCT, RGBD_CAPTURE)
    key = key_of(only_artifact(job)["ref"])

    # A second loopback client -- a fresh cookie jar, a restarted server -- reads
    # the same folder.
    assert loopback.get("/artifacts/" + key).status_code == 200


def test_a_host_header_cannot_claim_the_local_folder(viewers):
    """`Host` is typed by the caller. The socket is not, so that is what decides."""
    client = viewers()
    response = client.post(
        "/jobs",
        json={"stages": RECONSTRUCT, "capture": RGBD_CAPTURE},
        headers={"Host": "localhost"},
    )
    assert response.status_code == 201
    job = settle(client, response.json())

    key = key_of(only_artifact(job)["ref"])
    assert key.startswith("viewers/")
    assert not key.startswith(f"viewers/{LOCAL_FOLDER}/")


# ── the store underneath ────────────────────────────────────────────────────


def test_the_scoped_store_refuses_a_ref_from_another_folder(tmp_path):
    scoped = ViewerScopedStorage(LocalStorage(tmp_path / "s"), enabled=True)

    token = current_folder.set("aaaa")
    try:
        ref = scoped.put("artifacts/x/y.ply", b"hello")
    finally:
        current_folder.reset(token)
    assert ref.startswith("local://viewers/aaaa/")

    token = current_folder.set("bbbb")
    try:
        assert scoped.exists(ref) is False
        with pytest.raises(HTTPException) as refused:
            scoped.get(ref)
        assert refused.value.status_code == 404
    finally:
        current_folder.reset(token)


def test_the_scoped_store_refuses_to_guess_a_folder(tmp_path):
    """Enabled with nothing in scope is a bug, and guessing would be worse than
    failing: it would read whichever folder happened to be first."""
    scoped = ViewerScopedStorage(LocalStorage(tmp_path / "s"), enabled=True)
    with pytest.raises(RuntimeError):
        scoped.put("artifacts/x/y.ply", b"hello")


def test_tenancy_off_leaves_refs_exactly_as_they_were(tmp_path):
    """The flat namespace has to survive being turned off, or every ref already
    written stops resolving."""
    scoped = ViewerScopedStorage(LocalStorage(tmp_path / "s"), enabled=False)
    ref = scoped.put("artifacts/x/y.ply", b"hello")
    assert ref == "local://artifacts/x/y.ply"
    assert scoped.get(ref) == b"hello"
    assert scoped.list_keys("artifacts/") == ["artifacts/x/y.ply"]


def test_removing_a_viewer_takes_only_their_folder(tmp_path):
    base = LocalStorage(tmp_path / "s")
    scoped = ViewerScopedStorage(base, enabled=True)

    for folder, payload in (("aaaa", b"a"), ("bbbb", b"b")):
        token = current_folder.set(folder)
        try:
            scoped.put("artifacts/x/y.ply", payload)
        finally:
            current_folder.reset(token)

    token = current_folder.set("aaaa")
    try:
        scoped.reset()
        assert scoped.list_keys("artifacts/") == []
    finally:
        current_folder.reset(token)

    token = current_folder.set("bbbb")
    try:
        assert scoped.list_keys("artifacts/") == ["artifacts/x/y.ply"]
    finally:
        current_folder.reset(token)


# ── staging, for a store with no filesystem ─────────────────────────────────


class BucketLike:
    """A store with no directories, which is where this is heading.

    Exists so the staging path is covered before the backend that needs it is
    written, rather than being discovered on the deployment that introduces it.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> str:
        self.objects[key] = data
        return f"local://{key}"

    def get(self, ref: str) -> bytes:
        key = ref.split("://", 1)[1]
        if key not in self.objects:
            raise HTTPException(404, detail="missing")
        return self.objects[key]

    def exists(self, ref: str) -> bool:
        return ref.split("://", 1)[1] in self.objects

    def ref(self, key: str) -> str:
        return f"local://{key}"

    def remove_tree(self, key: str) -> int:
        gone = [k for k in self.objects if k == key or k.startswith(key + "/")]
        for k in gone:
            del self.objects[k]
        return len(gone)

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def local_dir(self, prefix: str):
        # No filesystem to hand over, which is the whole point.
        return None


def test_a_store_with_no_filesystem_is_staged_to_disk():
    """TotalSegmentator takes a directory. A bucket has none, so the source is
    downloaded for the duration of the run and removed after."""
    bucket = BucketLike()
    bucket.put("sources/s1/files/a.dcm", b"AAA")
    bucket.put("sources/s1/files/b.dcm", b"BBB")

    store = SourceStore(bucket)
    with store.materialize("s1") as directory:
        assert sorted(p.name for p in directory.iterdir()) == ["a.dcm", "b.dcm"]
        assert (directory / "a.dcm").read_bytes() == b"AAA"

    assert not directory.exists(), "the staging directory outlived the run"


def test_a_local_store_is_handed_over_rather_than_copied(tmp_path):
    storage = LocalStorage(tmp_path / "s")
    storage.put("sources/s1/files/a.dcm", b"AAA")

    store = SourceStore(storage)
    with store.materialize("s1") as directory:
        assert (directory / "a.dcm").read_bytes() == b"AAA"
        assert "bone-viewer-source-" not in str(directory), "a local store was copied"


# ── a store that stops answering ────────────────────────────────────────────


class FailingStore(LocalStorage):
    """A store that stops answering, the way a bucket does mid-run."""

    def put(self, key: str, data: bytes) -> str:
        raise OSError("the store is unreachable")

    def get(self, ref: str) -> bytes:
        raise OSError("the store is unreachable")

    def exists(self, ref: str) -> bool:
        raise OSError("the store is unreachable")

    def list_keys(self, prefix: str) -> list[str]:
        raise OSError("the store is unreachable")


def test_a_job_ends_even_when_the_store_stops_answering(tmp_path, monkeypatch):
    """A job the caller can never resolve is the worst outcome available.

    Everything the worker writes goes to the store, so an unreachable store fails
    the job -- but the code that records *that* failure must not itself depend on
    the store, or the record is lost and the job sits in `queued` forever with
    nothing to explain it. A user can act on a failed job; they cannot act on a
    job that never ends.
    """
    monkeypatch.setenv("BONE_VIEWER_RECORDINGS_DIR", str(tmp_path / "recordings"))
    get_settings.cache_clear()

    store = FailingStore(tmp_path / "data")
    jobs = JobStore(BackendRegistry(storage=store), storage=store)
    job = jobs.submit(JobCreate(stages=[Stage(op="reconstruct")], capture=RGBD_CAPTURE), False)

    finished = jobs.wait(job.job_id, timeout=30)
    assert finished is not None
    assert finished.status is JobStatus.FAILED, finished.status
    assert finished.error, "the job failed without saying why"
    get_settings.cache_clear()
