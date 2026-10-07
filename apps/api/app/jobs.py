"""Temporary Job store. Not durable across restarts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from .models import Job, JobCreate, JobStatus
from .pipeline import (
    DIAGNOSTIC_OPS,
    BackendRegistry,
    InferenceRequest,
    OpNotSupported,
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
        ops = [stage.op for stage in request.stages]
        job = Job(
            job_id=str(uuid.uuid4()),
            status=JobStatus.QUEUED,
            ops=ops,
            backend=request.backend,
        )
        self._remember(job)

        # Diagnostic operations need the request opt-in AND the server setting.
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
            return job

        try:
            job.status = JobStatus.RUNNING
            backend = self._registry.resolve(ops, request.backend)
            job.backend = backend.capabilities().backend
            outcome = backend.run(
                InferenceRequest(stages=request.stages, capture=request.capture)
            )
            job.status = JobStatus.SUCCEEDED
            job.result = outcome.envelope
            job.warnings = list(outcome.envelope.get("warnings") or [])
        except OpNotSupported as exc:
            job.status = JobStatus.FAILED
            job.error = str(exc)
        except Exception as exc:  # surface any backend failure to the caller
            job.status = JobStatus.FAILED
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = job.finished_at or datetime.now(UTC)

        return job

    def _remember(self, job: Job) -> None:
        self._jobs[job.job_id] = job
        if len(self._jobs) > self._max_jobs:
            oldest = min(self._jobs.values(), key=lambda j: j.created_at)
            self._jobs.pop(oldest.job_id, None)
