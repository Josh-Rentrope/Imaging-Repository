"""Job submission and polling."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

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


class JobRename(BaseModel):
    #: `None` clears the operator's name and falls back to the derived one.
    name: str | None = None


@router.patch("/{job_id}", response_model=Job)
def rename_job(
    job_id: str,
    body: JobRename,
    store: Annotated[JobStore, Depends(get_job_store)],
) -> Job:
    """Give a result the name the operator calls it.

    Stored on the job rather than kept in the page, so a layer renamed now is
    still called that after a reload, in another tab, or on another machine.
    """
    name = (body.name or "").strip() or None
    updated = store.rename(job_id, name)
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no job {job_id!r}")
    return updated


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: str, store: Annotated[JobStore, Depends(get_job_store)]) -> None:
    """Remove a result and the geometry it produced.

    Both, or the deleted layer comes back: the page re-reads the server's list
    on load, so anything still recorded here reappears as though the delete had
    never happened.
    """
    if store.delete(job_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no job {job_id!r}")
