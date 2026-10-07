"""Job store.

Durable, because the alternative is worse than it sounds. The dev server runs
under `--reload`, so every backend edit restarts the process — an in-memory
store would drop the result of a segmentation that took minutes, and the
artifacts would still be sitting on disk with nothing left to describe them.

Each job is one JSON object beside the artifacts it points at. The in-memory dict
stays the working set; the store is the record.

Jobs are held **per viewer**. One viewer's history is loaded the first time they
ask for it and never loaded for a viewer who has not, because with tenancy on,
eagerly reading every job in the store would put one viewer's work into the
process memory serving another — which is the exact thing the separation exists
to prevent.
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime

from fastapi import HTTPException

from .models import Job, JobCreate, JobStatus
from .observability import logger
from .pipeline import (
    DIAGNOSTIC_OPS,
    BackendRegistry,
    InferenceRequest,
    OpNotSupported,
)
from .storage import Storage
from .tenancy import current_folder

#: Enough history to survive a working session without growing without bound.
MAX_JOBS = 500

_JOBS_PREFIX = "jobs/"


def _viewer() -> str:
    """The viewer whose jobs the caller may see.

    Empty when tenancy is off, which is the single shared namespace this store
    had before viewers existed.
    """
    return current_folder.get() or ""


def _job_key(job_id: str) -> str:
    return f"{_JOBS_PREFIX}{job_id}.json"


class JobStore:
    def __init__(
        self,
        registry: BackendRegistry,
        storage: Storage | None = None,
        max_jobs: int = MAX_JOBS,
        preload: bool = True,
    ) -> None:
        self._registry = registry
        # Held so deleting a job can delete what it produced, rather than
        # leaving the bytes behind for the next walk of the store to find.
        self._storage = storage
        self._max_jobs = max_jobs
        # Workers mutate jobs while request threads read them.
        self._lock = threading.RLock()
        # viewer -> job_id -> Job, with the set of viewers already read back.
        self._jobs: dict[str, dict[str, Job]] = {}
        self._loaded: set[str] = set()
        if preload:
            # Only when tenancy is off: at startup there is no viewer in scope,
            # so on a scoped store there is nothing safe to read.
            self._bucket()

    # ── visibility ──────────────────────────────────────────────────────────

    def _bucket(self) -> dict[str, Job]:
        """The current viewer's jobs, read back on first use.

        Takes no argument on purpose. The store underneath is scoped by the
        ambient viewer, so a bucket keyed by anything other than that viewer
        would be a bucket whose keys came from one folder and whose name claimed
        another.
        """
        viewer = _viewer()
        with self._lock:
            if viewer not in self._loaded:
                self._loaded.add(viewer)
                self._read_back(viewer)
            return self._jobs.setdefault(viewer, {})

    def _read_back(self, viewer: str) -> None:
        """Read one viewer's jobs out of the store.

        A record that will not parse is skipped rather than fatal: one truncated
        write must not cost every other result in the folder.

        Called with the lock held, and doing io under it, which is a deliberate
        trade: it happens once per viewer per process, and the alternative is two
        threads racing to be the first to populate the same bucket.
        """
        if self._storage is None:
            return
        bucket = self._jobs.setdefault(viewer, {})
        try:
            keys = self._storage.list_keys(_JOBS_PREFIX)
        except (OSError, ValueError, HTTPException) as exc:
            # A store that cannot be listed is a store with nothing to show, not
            # a reason to refuse to start. This runs during startup, so letting it
            # out would mean an API that will not boot because a bucket was
            # briefly unreachable -- and empty-but-up beats down.
            #
            # Logged, because the visible symptom is otherwise "my results are
            # gone": the viewer's history comes back empty and nothing anywhere
            # says the listing failed rather than found nothing.
            logger.warning(
                "could not list stored jobs for viewer %s -- %s: %s. Reporting no "
                "history, which is indistinguishable from having none.",
                viewer or "(single namespace)",
                type(exc).__name__,
                exc,
            )
            return
        for key in keys:
            try:
                job = Job.model_validate_json(self._storage.get(self._storage.ref(key)))
            except (ValueError, OSError, HTTPException):
                continue
            bucket[job.job_id] = job

    def _live(self) -> dict[str, Job]:
        """The current viewer's jobs, without reading anything back.

        For the paths that already hold the job in hand. Recording an outcome
        must not be able to fail because the store is unreachable, or the code
        responsible for resolving a job is the code that leaves it unresolved --
        and a job stuck in `queued` is far harder to explain than a failed one.
        """
        return self._jobs.setdefault(_viewer(), {})

    # ── reads ───────────────────────────────────────────────────────────────

    def list(self, limit: int = 50, source_id: str | None = None) -> list[Job]:
        with self._lock:
            jobs = list(self._bucket().values())
        if source_id is not None:
            jobs = [job for job in jobs if _source_of(job) == source_id]
        ordered = sorted(jobs, key=lambda j: j.created_at, reverse=True)
        return ordered[:limit]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._bucket().get(job_id)

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

    # ── writes ──────────────────────────────────────────────────────────────

    def rename(self, job_id: str, name: str | None) -> Job | None:
        updated = self._replace(job_id, name=name)
        if updated is not None:
            self._persist(updated)
        return updated

    def delete(self, job_id: str) -> Job | None:
        """Forget a job and every artifact it points at.

        Both halves matter. Dropping the record alone leaves the mesh in the
        store to be re-indexed by anything that walks it, and dropping the files
        alone leaves a job whose result 404s.
        """
        with self._lock:
            job = self._bucket().pop(job_id, None)
        if job is None:
            return None

        if self._storage is not None:
            with suppress(OSError, ValueError, HTTPException):
                self._storage.remove_tree(_job_key(job_id))
                logger.debug("removed record for job %s", job_id)
        self._remove_artifacts(job)
        return job

    def _remove_artifacts(self, job: Job) -> None:
        if self._storage is None or job.result is None:
            return
        result_id = job.result.get("result_id")
        if not result_id:
            return
        # The record is already gone; a file that will not delete is not a reason
        # to fail the request. It is a reason to say so, though: an artifact that
        # will not delete is either a store that is not answering or a key that
        # has drifted, and both are worth knowing about before they fill a bucket.
        try:
            self._storage.remove_tree(f"artifacts/{result_id}")
        except (OSError, ValueError, HTTPException) as exc:
            logger.warning(
                "job %s: could not remove artifacts/%s -- %s: %s",
                job.job_id,
                result_id,
                type(exc).__name__,
                exc,
            )

    def _replace(self, job_id: str, **fields) -> Job | None:
        """Update a job under the lock, returning the new state.

        A copy rather than a mutation, so a reader holding the previous object
        sees a whole state and not a half-applied one.
        """
        with self._lock:
            bucket = self._live()
            job = bucket.get(job_id)
            if job is None:
                return None
            updated = job.model_copy(update=fields)
            bucket[job_id] = updated
            return updated

    # ── durability ──────────────────────────────────────────────────────────

    def _persist(self, job: Job) -> None:
        if self._storage is None:
            return
        # A record that will not persist must not take the job with it: the
        # in-memory copy is still the truth for this process. But the job is then
        # only as durable as the process, and it will not survive the next
        # restart -- which is the one thing persistence exists for, so it is
        # logged rather than swallowed.
        try:
            self._storage.put(
                _job_key(job.job_id), job.model_dump_json(indent=2).encode("utf-8")
            )
        except (OSError, HTTPException) as exc:
            logger.warning(
                "job %s (%s) was NOT persisted to the store -- %s: %s. It exists "
                "only in this process and will be lost on restart.",
                job.job_id,
                job.status,
                type(exc).__name__,
                exc,
            )

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
            args=(job, request, _viewer()),
            name=f"job-{job.job_id[:8]}",
            daemon=True,
        )
        worker.start()
        return job

    def _execute(self, job: Job, request: JobCreate, viewer: str) -> None:
        """Run one job, on its own thread, recording how it ended.

        The viewer is re-established here rather than inherited. A bare
        `threading.Thread` starts with a fresh context, so the context variable
        the request set is *not* visible on this thread — and the work this
        thread does writes artefacts, which must land in the folder that asked
        for them and nowhere else.
        """
        token = current_folder.set(viewer or None)
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
            current_folder.reset(token)

    def _remember(self, job: Job) -> None:
        with self._lock:
            bucket = self._live()
            bucket[job.job_id] = job
            while len(bucket) > self._max_jobs:
                oldest = min(bucket.values(), key=lambda j: j.created_at)
                bucket.pop(oldest.job_id, None)
                if self._storage is not None:
                    with suppress(OSError, ValueError, HTTPException):
                        self._storage.remove_tree(_job_key(oldest.job_id))
                self._remove_artifacts(oldest)


def _source_of(job: Job) -> str | None:
    return getattr(job, "source_id", None)
