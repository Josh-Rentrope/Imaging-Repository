"""The inference seam.

A job is an ordered list of stages. A backend is anything that can execute them;
the router picks one that advertises every stage the job needs.

Operation names are an open vocabulary, not a closed enum. The names in `Op` are
the canonical spelling every backend is expected to implement, but a backend may
advertise operations this module has never heard of, and requests may name them.
The wire type is `str` for exactly that reason.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class Op(StrEnum):
    RECTIFY = "rectify"
    RECONSTRUCT = "reconstruct"
    SEGMENT = "segment"
    MEASURE = "measure"
    ISOLATE_VOLUME = "isolate_volume"
    ISO_SURFACE = "iso_surface"
    DETECT_CARIES = "detect_caries"
    #: Poses for a set of photographs, from the device or from structure-from-
    #: motion. Separate from `reconstruct` because it is a different kind of
    #: answer — camera geometry, not tissue — and because on a phone it is
    #: usually already known, in which case this stage is skipped rather than run.
    ESTIMATE_POSES = "estimate_poses"
    #: Teeth from a single panoramic radiograph.
    PX2TOOTH = "px2tooth"


class BackendId(StrEnum):
    SERVER = "server"
    FIXTURE = "fixture"
    ONDEVICE = "ondevice"


class Modality(StrEnum):
    RGBD = "rgbd"
    RGB = "rgb"
    RADIOGRAPH = "radiograph"


#: Operations that carry a diagnostic claim and stay gated behind entitlement.
DIAGNOSTIC_OPS: frozenset[str] = frozenset({Op.DETECT_CARIES})


class Stage(BaseModel):
    """One step of a job. `op` is a free string; see the module docstring."""

    op: str
    params: dict[str, Any] = Field(default_factory=dict)


class Capabilities(BaseModel):
    backend: str
    ops: list[str]
    modalities: list[str]
    max_resolution: int | None = None
    supports_dense_output: bool = False
    notes: str | None = None


class Health(BaseModel):
    ok: bool
    backend: str
    detail: str | None = None


class InferenceRequest(BaseModel):
    stages: list[Stage]
    capture: dict[str, Any] | None = None


class InferenceResult(BaseModel):
    """A ResultEnvelope (contracts/results.schema.json), kept as a loose dict.

    The envelope is versioned JSON with its own schema; mirroring it as strict
    models here would create a second source of truth that drifts.
    """

    envelope: dict[str, Any]


@runtime_checkable
class InferenceBackend(Protocol):
    def capabilities(self) -> Capabilities: ...

    def run(self, request: InferenceRequest) -> InferenceResult: ...

    def health(self) -> Health: ...


class OpNotSupported(Exception):
    """Raised when no backend advertises an operation a job asked for."""

    def __init__(self, op: str) -> None:
        super().__init__(f"no backend advertises operation {op!r}")
        self.op = op
