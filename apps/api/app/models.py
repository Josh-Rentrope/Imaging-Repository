"""API request/response models.

Contract envelope models (CaptureBundle, ResultEnvelope) are validated against
contracts/*.schema.json in tests rather than duplicated here -- one source of
truth. These are the transport-level shapes around them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .pipeline import BackendName, Task


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"  # quality gate refused it; not an error


class JobCreate(BaseModel):
    task: Task
    capture: dict[str, Any] | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    backend: BackendName | None = Field(
        default=None,
        description="Force a backend. Server-side is the production default; "
        "leave unset to let the registry negotiate.",
    )
    allow_diagnostic: bool = Field(
        default=False,
        description="Must be true for diagnostic tasks, and the server must also have "
        "BONE_VIEWER_ENABLE_DIAGNOSTIC set. Two switches, deliberately.",
    )


class Job(BaseModel):
    job_id: str
    status: JobStatus
    task: Task
    backend: BackendName | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)


class DicomInstance(BaseModel):
    #: DICOM SOP Instance UID (0008,0018), as read from the file. May be empty
    #: for a headerless upload.
    sop_instance_uid: str = ""
    file_name: str
    #: Storage ref for the stored instance. The viewer fetches by this, never by
    #: constructing a path.
    ref: str = ""
    bytes: int
    rows: int | None = None
    columns: int | None = None


class DicomSeries(BaseModel):
    series_id: str
    study_id: str
    description: str | None = None
    modality: str | None = None
    instances: list[DicomInstance] = Field(default_factory=list)
    instance_count: int = 0
    bytes_total: int = 0
    #: True when the upload carried no parseable DICOM headers and the files were
    #: accepted as an opaque blob. The viewer must say so rather than render blank.
    headerless: bool = False


class DicomSeriesSummary(BaseModel):
    series_id: str
    description: str | None = None
    modality: str | None = None
    instance_count: int
    bytes_total: int
    headerless: bool
