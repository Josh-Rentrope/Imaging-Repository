"""Object storage.

Two concerns, deliberately kept apart:

* a **backend** knows how to keep bytes -- `LocalStorage` on disk today, an
  S3-compatible bucket when this is deployed;
* a **scope** decides which namespace inside that backend a caller may touch
  (`app/tenancy.py`).

Refs are opaque (`local://<key>`) and carry the scope that minted them, so a ref
handed to one viewer cannot be read by another. Nothing outside this module
resolves a filesystem path, which is what lets the backend be swapped without
touching the routes.

`local://` names the backend, not the tenant. The scope rides in the key, so the
scheme does not change when the bytes move into a bucket.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

from fastapi import HTTPException, status

SCHEME = "local://"

#: Where a write lands before it is moved into place. Deliberately not under any
#: prefix the application lists.
INFLIGHT = ".inflight"


def key_from_ref(ref: str) -> str:
    if not ref.startswith(SCHEME):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported storage ref {ref!r}; this build only serves {SCHEME!r}",
        )
    return ref[len(SCHEME):]


def _bad_key() -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid storage key")


def check_key(key: str) -> str:
    """Validate an object key.

    A key is caller-influenced in exactly one place: the artifact route takes a
    ref out of the URL. So this is the boundary that keeps a key inside the
    namespace it was minted for, and it lives here rather than in each backend
    because a bucket has no directories to escape and would otherwise be the one
    configuration where the rule silently went missing.
    """
    if not key or key.startswith("/") or key.endswith("/") or "\\" in key:
        raise _bad_key()
    if any(part in ("", ".", "..") for part in key.split("/")):
        raise _bad_key()
    return key


def check_prefix(prefix: str) -> str:
    """Like `check_key`, but a prefix may name a directory (trailing slash)."""
    if prefix.startswith("/") or "\\" in prefix:
        raise _bad_key()
    for part in prefix.rstrip("/").split("/"):
        if part in (".", ".."):
            raise _bad_key()
    return prefix


def join(prefix: str, key: str) -> str:
    """Prefix a key, keeping exactly one separator between the two halves."""
    if not prefix:
        return key
    return f"{prefix.rstrip('/')}/{key}"


@runtime_checkable
class Storage(Protocol):
    """What the pipeline needs from a store of bytes.

    Deliberately small. `list_keys` and `local_dir` exist for the two callers
    that cannot pretend the store is a dictionary: the source and job indexes
    list what is there, and the segmenter is handed a directory.
    """

    def put(self, key: str, data: bytes) -> str: ...

    def get(self, ref: str) -> bytes: ...

    def exists(self, ref: str) -> bool: ...

    def ref(self, key: str) -> str: ...

    def remove_tree(self, key: str) -> int: ...

    def list_keys(self, prefix: str) -> list[str]: ...

    def local_dir(self, prefix: str) -> Path | None: ...


class LocalStorage:
    """A directory on this machine. The default, and what the tests run against."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    # -- refs ---------------------------------------------------------------

    def ref(self, key: str) -> str:
        return f"{SCHEME}{check_key(key)}"

    def _path(self, key: str) -> Path:
        # Resolve and confirm containment. `check_key` already refuses the
        # traversals it can see; this also catches a symlink pointing out of the
        # tree, which no amount of string inspection would.
        candidate = (self.root / key).resolve()
        root = self.root.resolve()
        if not candidate.is_relative_to(root):
            raise _bad_key()
        return candidate

    # -- io -----------------------------------------------------------------

    def put(self, key: str, data: bytes) -> str:
        path = self._path(check_key(key))
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written elsewhere and moved into place, so a crash mid-write leaves the
        # previous object intact rather than a half-file wearing its name. The
        # staging directory sits outside every key callers list, so a write that
        # never completed is invisible rather than something to filter out.
        staging = self._path(INFLIGHT) / uuid.uuid4().hex
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(data)
        os.replace(staging, path)
        return self.ref(key)

    def get(self, ref: str) -> bytes:
        path = self._path(key_from_ref(ref))
        if not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"missing artefact {ref!r}")
        return path.read_bytes()

    def exists(self, ref: str) -> bool:
        try:
            return self._path(key_from_ref(ref)).is_file()
        except HTTPException:
            return False

    def remove_tree(self, key: str) -> int:
        """Delete everything under a key. Returns how many files went.

        A deletion that leaves the bytes behind is not a deletion — it is a
        hidden file that reappears the moment something indexes the directory.
        """
        path = self._path(check_key(key))
        if not path.exists():
            return 0
        if path.is_dir():
            count = sum(1 for entry in path.rglob("*") if entry.is_file())
            shutil.rmtree(path, ignore_errors=True)
            return count
        path.unlink(missing_ok=True)
        return 1

    def list_keys(self, prefix: str) -> list[str]:
        base = self._path(check_prefix(prefix))
        if not base.is_dir():
            return []
        root = self.root.resolve()
        return sorted(
            path.relative_to(root).as_posix()
            for path in base.rglob("*")
            if path.is_file()
        )

    def local_dir(self, prefix: str) -> Path | None:
        """The real directory behind a prefix, when there is one.

        Lets a directory be handed to an external tool without copying it. A
        backend with no filesystem returns None, and the caller stages instead.
        """
        path = self._path(check_prefix(prefix))
        return path if path.is_dir() else None

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())

    def reset(self) -> None:
        """Wipe everything. Dev convenience only; never wired to a route."""
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
