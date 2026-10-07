"""On-device execution.

Advertises no operations, so the router never selects it. It is registered so a
client can probe for local capability and so registering a real on-device
runtime later is a constructor change.
"""

from __future__ import annotations

from .interfaces import (
    BackendId,
    Capabilities,
    Health,
    InferenceRequest,
    InferenceResult,
    Modality,
    OpNotSupported,
)


class OnDeviceBackend:
    def capabilities(self) -> Capabilities:
        return Capabilities(
            backend=BackendId.ONDEVICE,
            ops=[],
            modalities=[Modality.RGBD, Modality.RGB],
        )

    def run(self, request: InferenceRequest) -> InferenceResult:
        raise OpNotSupported(request.stages[0].op if request.stages else "<empty job>")

    def health(self) -> Health:
        return Health(ok=True, backend=BackendId.ONDEVICE, detail="no local runtime")
