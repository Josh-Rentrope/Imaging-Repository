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
    #: Which workflow the client ran this as. Recorded rather than inferred: the
    #: same two stages are a different pipeline depending on what they were
    #: chosen for, and a reader of the result cannot tell which from the ops.
    workflow: str | None = None
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
    #: Which source this ran against. Lets a reloaded page ask for its own
    #: results instead of every job the server has ever run.
    source_id: str | None = None
    #: What the operator calls this result. Stored rather than derived, because
    #: a name someone chose for a layer should survive a reload — and the
    #: derived label is still there underneath as `ops`.
    name: str | None = None
    #: The workflow this was run as, as the client named it.
    workflow: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
