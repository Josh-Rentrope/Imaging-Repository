"""Development object store.

A local-filesystem stand-in for object storage. Same interface shape a real S3
/ Azure Blob adapter would have, so swapping it is a constructor change.

Refs are opaque (`local://<key>`) and are what the contracts carry. Nothing in
the pipeline resolves a filesystem path directly -- that indirection is what lets
tenant-scoped buckets and signed URLs drop in later without touching the routes.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import HTTPException, status

SCHEME = "local://"


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    # -- refs ---------------------------------------------------------------

    def ref(self, key: str) -> str:
        return f"{SCHEME}{key}"

    def _key_from_ref(self, ref: str) -> str:
        if not ref.startswith(SCHEME):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"unsupported storage ref {ref!r}; this build only serves {SCHEME!r}",
            )
        return ref[len(SCHEME):]

    def _path(self, key: str) -> Path:
        # Resolve and confirm containment: a key is caller-influenced, and this is
        # the one place a traversal would land.
        candidate = (self.root / key).resolve()
        root = self.root.resolve()
        if not candidate.is_relative_to(root):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid storage key")
        return candidate

    # -- io -----------------------------------------------------------------

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.ref(key)

    def get(self, ref: str) -> bytes:
        path = self._path(self._key_from_ref(ref))
        if not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"missing artefact {ref!r}")
        return path.read_bytes()

    def path_for(self, ref: str) -> Path:
        path = self._path(self._key_from_ref(ref))
        if not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"missing artefact {ref!r}")
        return path

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())

    def reset(self) -> None:
        """Wipe everything. Dev convenience only; never wired to a route."""
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
