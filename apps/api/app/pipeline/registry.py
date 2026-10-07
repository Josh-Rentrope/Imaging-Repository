"""Backend selection.

A job is routed to a backend that advertises every operation it needs. Adding a
backend -- a local GPU appliance, a tenant-specific deployment -- is an entry in
`_load` rather than a change to the pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ..storage import LocalStorage
from .fixture import FixtureBackend
from .interfaces import (
    BackendId,
    Capabilities,
    InferenceBackend,
    OpNotSupported,
)
from .ondevice import OnDeviceBackend
from .server import load_server_backend


class BackendRegistry:
    def __init__(
        self,
        storage: LocalStorage,
        recordings_dir: Path | None = None,
        preference: str = "auto",
    ) -> None:
        self._storage = storage
        self._recordings_dir = recordings_dir
        self._preference = preference
        self._backends: dict[str, InferenceBackend] = {}
        self._load()

    def _load(self) -> None:
        self._backends[BackendId.FIXTURE] = FixtureBackend(self._storage, self._recordings_dir)

        ondevice = OnDeviceBackend()
        if ondevice.capabilities().ops:
            self._backends[BackendId.ONDEVICE] = ondevice

        core = load_server_backend()
        if core is not None:
            self._backends[BackendId.SERVER] = core

    def available(self) -> list[Capabilities]:
        return [backend.capabilities() for backend in self._backends.values()]

    def health_all(self) -> list[dict]:
        return [
            {"name": name, **backend.health().model_dump(mode="json")}
            for name, backend in self._backends.items()
        ]

    def has_server_backend(self) -> bool:
        return BackendId.SERVER in self._backends

    def get(self, name: str) -> InferenceBackend:
        if name not in self._backends:
            raise KeyError(name)
        return self._backends[name]

    def resolve(self, ops: Sequence[str], preferred: str | None = None) -> InferenceBackend:
        """Return a backend advertising every requested operation."""
        wanted = list(dict.fromkeys(ops))
        for name in self._order(preferred):
            backend = self._backends.get(name)
            if backend is None:
                continue
            advertised = set(backend.capabilities().ops)
            if all(op in advertised for op in wanted):
                return backend

        unsupported = [
            op
            for op in wanted
            if not any(op in set(b.capabilities().ops) for b in self._backends.values())
        ]
        raise OpNotSupported(unsupported[0] if unsupported else wanted[0])

    def _order(self, preferred: str | None) -> list[str]:
        if preferred is not None:
            return [preferred, BackendId.SERVER, BackendId.FIXTURE]
        if self._preference == "fixture":
            return [BackendId.FIXTURE]
        if self.has_server_backend():
            return [BackendId.SERVER, BackendId.FIXTURE]
        return [BackendId.FIXTURE]
