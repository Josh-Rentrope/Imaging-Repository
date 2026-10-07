"""Temporary Job store. Not Durable for Restarts/Spin ups/etc"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from .models import Job, JobCreate, JobStatus
from .pipeline import (
    DIAGNOSTIC_TASKS,
    BackendRegistry,
    InferenceRequest,
    TaskNotSupported,
)


class JobStore:
    def __init__(self, registry: BackendRegistry, max_jobs: int = 500) -> None:
        self._registry = registry
        self._jobs: dict[str, Job] = {}
        self._max_jobs = max_jobs

    def list(self, limit: int = 50) -> list[Job]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def submit(self, request: JobCreate, allow_diagnostic_server: bool) -> Job:
        job = Job(
            job_id=self._new_id(),
            status=JobStatus.QUEUED,
            task=request.task,
            backend=request.backend,
        )
        self._remember(job)

        # Two-switch gate for diagnostic tasks: the caller must opt in per request
        # AND the server must be configured for it. Neither alone is enough.
        if request.task in DIAGNOSTIC_TASKS and not (request.allow_diagnostic and allow_diagnostic_server):
            job.status = JobStatus.REJECTED
            job.finished_at = datetime.now(UTC)
            job.error = (
                f"task {request.task.value!r} carries a diagnostic claim and is gated. "
                "It requires allow_diagnostic=true on the request and "
                "BONE_VIEWER_ENABLE_DIAGNOSTIC=1 on the server. See Notes/06."
            )
            return job

        try:
            job.status = JobStatus.RUNNING
            backend = self._registry.resolve(request.task, request.backend)
            job.backend = backend.capabilities().backend
            outcome = backend.run(InferenceRequest(task=request.task, capture=request.capture, params=request.params))
            job.status = JobStatus.SUCCEEDED
            job.result = outcome.envelope
            job.warnings = list(outcome.envelope.get("warnings") or [])
        except TaskNotSupported as exc:
            job.status = JobStatus.FAILED
            job.error = str(exc)
        except NotImplementedError as exc:
            job.status = JobStatus.FAILED
            job.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - surface anything to the client in dev
            job.status = JobStatus.FAILED
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = job.finished_at or datetime.now(UTC)

        return job

    # -- internals ----------------------------------------------------------

    def _remember(self, job: Job) -> None:
        self._jobs[job.job_id] = job
        if len(self._jobs) > self._max_jobs:
            oldest = min(self._jobs.values(), key=lambda j: j.created_at)
            self._jobs.pop(oldest.job_id, None)

    @staticmethod
    def _new_id() -> str:
        return str(uuid.uuid4())
