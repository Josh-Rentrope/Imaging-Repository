"""API request/response models.

The contract envelopes (CaptureBundle, ResultEnvelope) are validated against
contracts/*.schema.json rather than duplicated here. These are the transport
shapes around them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .pipeline import Stage


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"  # refused before execution, e.g. by entitlement


class JobCreate(BaseModel):
    stages: list[Stage] = Field(min_length=1)
    capture: dict[str, Any] | None = None
    backend: str | None = Field(
        default=None,
        description="Force a backend by id. Leave unset to let the registry negotiate.",
    )
    allow_diagnostic: bool = Field(
        default=False,
        description="Required for diagnostic operations, in addition to the server setting.",
    )


class Job(BaseModel):
    job_id: str
    status: JobStatus
    ops: list[str]
    backend: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)


class DicomInstance(BaseModel):
    #: DICOM SOP Instance UID (0008,0018), as read from the file. May be empty
    #: for a file whose header could not be parsed.
    sop_instance_uid: str = ""
    file_name: str
    #: Storage ref. Callers fetch by this rather than constructing a path.
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
    #: True when no file in the upload carried a parseable header.
    headerless: bool = False


class DicomSeriesSummary(BaseModel):
    series_id: str
    description: str | None = None
    modality: str | None = None
    instance_count: int
    bytes_total: int
    headerless: bool
