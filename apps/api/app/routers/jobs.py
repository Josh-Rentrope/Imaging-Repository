"""Job submission and polling."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from ..config import Settings
from ..deps import get_config, get_job_store
from ..jobs import JobStore
from ..models import Job, JobCreate

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("", response_model=Job, status_code=status.HTTP_201_CREATED)
def submit(
    request: JobCreate,
    store: Annotated[JobStore, Depends(get_job_store)],
    settings: Annotated[Settings, Depends(get_config)],
) -> Job:
    return store.submit(request, allow_diagnostic_server=settings.enable_diagnostic_tasks)


@router.get("", response_model=list[Job])
def list_jobs(
    store: Annotated[JobStore, Depends(get_job_store)],
    limit: int = 50,
    source_id: str | None = None,
) -> list[Job]:
    """Recent jobs, newest first, optionally only those on one source.

    This is how a reloaded page gets its results back without re-running
    anything: the artifacts are already on disk and the job that describes them
    is already recorded.
    """
    return store.list(limit=limit, source_id=source_id)


@router.get("/{job_id}", response_model=Job)
def get_job(job_id: str, store: Annotated[JobStore, Depends(get_job_store)]) -> Job:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no job {job_id!r}")
    return job
