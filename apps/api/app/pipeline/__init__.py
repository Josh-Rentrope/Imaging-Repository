"""Inference pipeline: the backend seam and its implementations."""

from .interfaces import (
    DIAGNOSTIC_OPS,
    BackendId,
    Capabilities,
    Health,
    InferenceBackend,
    InferenceRequest,
    InferenceResult,
    Modality,
    Op,
    OpNotSupported,
    Stage,
)
from .registry import BackendRegistry

__all__ = [
    "DIAGNOSTIC_OPS",
    "BackendId",
    "BackendRegistry",
    "Capabilities",
    "Health",
    "InferenceBackend",
    "InferenceRequest",
    "InferenceResult",
    "Modality",
    "Op",
    "OpNotSupported",
    "Stage",
]
