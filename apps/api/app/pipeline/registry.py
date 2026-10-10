"""Backend selection.

A job is routed to a backend that advertises every operation it needs. Adding a
backend -- a local GPU appliance, a tenant-specific deployment -- is an entry in
`_load` rather than a change to the pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ..sources import SourceStore
from ..storage import Storage
from .fixture import FixtureBackend
from .interfaces import (
    BackendId,
    BackendUnavailable,
    Capabilities,
    InferenceBackend,
    OpNotSupported,
)
from .ondevice import OnDeviceBackend
from .server import BackendLoadError, load_server_backend


class BackendRegistry:
    def __init__(
        self,
        storage: Storage,
        recordings_dir: Path | None = None,
        preference: str = "auto",
        source_store: SourceStore | None = None,
    ) -> None:
        self._storage = storage
        self._recordings_dir = recordings_dir
        self._preference = preference
        self._source_store = source_store
        self._backends: dict[str, InferenceBackend] = {}
        self._server_error: str | None = None
        self._load()

    def _load(self) -> None:
        self._backends[BackendId.FIXTURE] = FixtureBackend(
            self._storage, self._source_store, self._recordings_dir
        )

        ondevice = OnDeviceBackend()
        if ondevice.capabilities().ops:
            self._backends[BackendId.ONDEVICE] = ondevice

        try:
            # Same arguments FixtureBackend gets, so the two are interchangeable.
            core = load_server_backend(
                storage=self._storage,
                source_store=self._source_store,
                recordings_dir=self._recordings_dir,
            )
        except BackendLoadError as exc:
            self._server_error = str(exc)
        else:
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

    def server_backend_error(self) -> str | None:
        """Why the private backend is missing, when it was meant to be here."""
        return self._server_error

    def get(self, name: str) -> InferenceBackend:
        if name not in self._backends:
            raise KeyError(name)
        return self._backends[name]

    def resolve(self, ops: Sequence[str], preferred: str | None = None) -> InferenceBackend:
        """Return a backend advertising every requested operation."""
        wanted = list(dict.fromkeys(ops))

        if preferred is not None:
            return self._named(preferred, wanted)

        for name in self._order():
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

    def _named(self, name: str, wanted: list[str]) -> InferenceBackend:
        """The backend the caller named, or a refusal explaining why not."""
        backend = self._backends.get(name)
        if backend is None:
            detail = self._server_error if name == BackendId.SERVER else None
            raise BackendUnavailable(name, detail)

        advertised = set(backend.capabilities().ops)
        missing = [op for op in wanted if op not in advertised]
        if missing:
            raise OpNotSupported(missing[0])
        return backend

    def _order(self) -> list[str]:
        if self._preference == "fixture":
            return [BackendId.FIXTURE]
        if self.has_server_backend():
            return [BackendId.SERVER, BackendId.FIXTURE]
        return [BackendId.FIXTURE]
