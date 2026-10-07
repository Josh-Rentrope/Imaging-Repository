"""Job store.

Durable, because the alternative is worse than it sounds. The dev server runs
under `--reload`, so every backend edit restarts the process — an in-memory
store would drop the result of a segmentation that took minutes, and the
artifacts would still be sitting on disk with nothing left to describe them.

Each job is one JSON file beside the artifacts it points at. The in-memory dict
stays the working set; the files are the record.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .models import Job, JobCreate, JobStatus
from .storage import LocalStorage
from .pipeline import (
    DIAGNOSTIC_OPS,
    BackendRegistry,
    InferenceRequest,
    OpNotSupported,
)

#: Enough history to survive a working session without growing without bound.
MAX_JOBS = 500


class JobStore:
    def __init__(
        self,
        registry: BackendRegistry,
        data_dir: Path | None = None,
        max_jobs: int = MAX_JOBS,
        storage: LocalStorage | None = None,
    ) -> None:
        self._registry = registry
        # Held so deleting a job can delete what it produced, rather than
        # leaving the bytes behind for the next directory walk to find.
        self._storage = storage
        self._jobs: dict[str, Job] = {}
        self._max_jobs = max_jobs
        # Workers mutate jobs while request threads read them.
        self._lock = threading.RLock()
        self._dir = (data_dir / "jobs") if data_dir is not None else None
        if self._dir is not None:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._load()

    def list(self, limit: int = 50, source_id: str | None = None) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        if source_id is not None:
            jobs = [job for job in jobs if _source_of(job) == source_id]
        ordered = sorted(jobs, key=lambda j: j.created_at, reverse=True)
        return ordered[:limit]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def wait(self, job_id: str, timeout: float | None = None) -> Job | None:
        """Block until a job finishes. Tests and callers that want the old
        synchronous behaviour; the API itself never does this."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            job = self.get(job_id)
            if job is None or job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
                return job
            if deadline is not None and time.monotonic() > deadline:
                return job
            time.sleep(0.01)

    def rename(self, job_id: str, name: str | None) -> Job | None:
        updated = self._replace(job_id, name=name)
        if updated is not None:
            self._persist(updated)
        return updated

    def delete(self, job_id: str) -> Job | None:
        """Forget a job and every artifact it points at.

        Both halves matter. Dropping the record alone leaves the mesh on disk to
        be re-indexed by anything that walks the directory, and dropping the
        files alone leaves a job whose result 404s.
        """
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return None

        if self._dir is not None:
            (self._dir / f"{job_id}.json").unlink(missing_ok=True)
        self._remove_artifacts(job)
        return job

    def _remove_artifacts(self, job: Job) -> None:
        if self._storage is None or job.result is None:
            return
        result_id = job.result.get("result_id")
        if not result_id:
            return
        try:
            self._storage.remove_tree(f"artifacts/{result_id}")
        except (OSError, ValueError):
            # The record is already gone; a file that will not delete is not a
            # reason to fail the request.
            pass

    def _replace(self, job_id: str, **fields) -> Job | None:
        """Update a job under the lock, returning the new state.

        A copy rather than a mutation, so a reader holding the previous object
        sees a whole state and not a half-applied one.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            updated = job.model_copy(update=fields)
            self._jobs[job_id] = updated
            return updated

    # ── durability ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Read back whatever is still on disk.

        A file that will not parse is skipped rather than fatal: one truncated
        write must not cost every other result in the directory.
        """
        assert self._dir is not None
        for path in sorted(self._dir.glob("*.json")):
            try:
                job = Job.model_validate_json(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            self._jobs[job.job_id] = job

    def _persist(self, job: Job) -> None:
        if self._dir is None:
            return
        path = self._dir / f"{job.job_id}.json"
        # Written to a sibling and moved, so a crash mid-write cannot leave a
        # half-file where a job used to be.
        staging = path.with_suffix(".json.tmp")
        try:
            staging.write_text(job.model_dump_json(indent=2), encoding="utf-8")
            staging.replace(path)
        except OSError:
            staging.unlink(missing_ok=True)

    def submit(self, request: JobCreate, allow_diagnostic_server: bool) -> Job:
        """Accept a job and return it immediately, before it has run.

        The work happens on a thread. A segmentation takes minutes, and holding
        the HTTP response open for it means a client cannot see that anything is
        happening, cannot queue the next job deliberately, and loses the run
        entirely if the connection drops. The job's own record is the progress
        channel instead: poll `GET /jobs/{id}` until it is no longer queued.

        Diagnostic operations need the request opt-in AND the server setting, so
        they are refused here rather than on the thread — a rejection should be
        visible in the response that caused it.
        """
        ops = [stage.op for stage in request.stages]
        job = Job(
            job_id=str(uuid.uuid4()),
            status=JobStatus.QUEUED,
            ops=ops,
            backend=request.backend,
            # Kept so a reloaded page can ask which results belong to the source
            # it is showing, rather than every job the server has ever run.
            source_id=(request.capture or {}).get("source_id"),
            workflow=request.workflow,
        )

        if any(op in DIAGNOSTIC_OPS for op in ops) and not (
            request.allow_diagnostic and allow_diagnostic_server
        ):
            job.status = JobStatus.REJECTED
            job.finished_at = datetime.now(UTC)
            job.error = (
                f"{', '.join(sorted(set(ops) & DIAGNOSTIC_OPS))} carries a diagnostic "
                "claim. It requires allow_diagnostic on the request and "
                "BONE_VIEWER_ENABLE_DIAGNOSTIC on the server."
            )
            self._remember(job)
            self._persist(job)
            return job

        self._remember(job)
        worker = threading.Thread(
            target=self._execute,
            args=(job, request),
            name=f"job-{job.job_id[:8]}",
            daemon=True,
        )
        worker.start()
        return job

    def _execute(self, job: Job, request: JobCreate) -> None:
        """Run one job, on its own thread, recording how it ended."""
        try:
            self._replace(job.job_id, status=JobStatus.RUNNING)
            backend = self._registry.resolve(job.ops, request.backend)
            outcome = backend.run(
                InferenceRequest(stages=request.stages, capture=request.capture)
            )
            self._replace(
                job.job_id,
                status=JobStatus.SUCCEEDED,
                backend=backend.capabilities().backend,
                result=outcome.envelope,
                warnings=list(outcome.envelope.get("warnings") or []),
            )
        except OpNotSupported as exc:
            self._replace(job.job_id, status=JobStatus.FAILED, error=str(exc))
        except Exception as exc:  # surface any backend failure to the caller
            self._replace(
                job.job_id, status=JobStatus.FAILED, error=f"{type(exc).__name__}: {exc}"
            )
        finally:
            finished = self._replace(job.job_id, finished_at=datetime.now(UTC))
            if finished is not None:
                self._persist(finished)

    def _remember(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.job_id] = job
            while len(self._jobs) > self._max_jobs:
                oldest = min(self._jobs.values(), key=lambda j: j.created_at)
                self._jobs.pop(oldest.job_id, None)
                if self._dir is not None:
                    (self._dir / f"{oldest.job_id}.json").unlink(missing_ok=True)
                self._remove_artifacts(oldest)


def _source_of(job: Job) -> str | None:
    return getattr(job, "source_id", None)
