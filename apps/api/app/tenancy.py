"""Which viewer is asking, and which folder they may read.

A viewer is identified by a random id kept in a cookie. The id is deliberately
not the folder name: the folder is `sha256(id)`, so a name that leaks -- into a
ref, a log, a screenshot of a URL -- cannot be turned back into the cookie that
reads it. Requests arriving over loopback skip the cookie entirely and use the
`local` viewer, so a development machine keeps one stable folder instead of
minting a new one for every browser profile and every test client.

What this is: **separation**. Two viewers cannot read each other's artefacts
through the API, because a ref is only served to the folder that minted it, and a
ref naming any other folder fails closed.

What this is not: **authorisation**. There is no account behind a cookie and
nothing to revoke; an id that leaks is an id that works, and a client can mint
itself a fresh one at will. That is a reasonable amount of structure for a viewer
with no sign-in, and it is not a substitute for one. It should not be relied on to
keep real patient data apart until identities are server-issued.

The `local` viewer is not reachable from off the machine: it is chosen from the
socket peer, never from a header, because `Host` is caller-controlled and
trusting it would let anyone send `Host: localhost` and land in the shared folder.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .config import Settings, get_settings
from .storage import Storage, check_key, check_prefix, join, key_from_ref

#: Top-level folder every viewer lives under, so the root of the store stays
#: legible and a whole deployment can be recognised at a glance.
VIEWERS_ROOT = "viewers"

#: The folder loopback requests use. Not a hash, because it is not a secret and
#: being obviously "the local one" is the point.
LOCAL_FOLDER = "local"

#: 32 bytes of entropy, hex, is 64 characters and not worth guessing.
_ID_BYTES = 32
_ID_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")

#: The viewer the current request or job belongs to.
#:
#: Ambient rather than threaded through every call because one store instance
#: serves every request and every job thread, and the alternative is a `viewer`
#: parameter on most of the codebase. The one place it does not travel by itself
#: is a job worker -- see the note in `JobStore._execute`.
current_folder: ContextVar[str | None] = ContextVar("bone_viewer_folder", default=None)


def folder_for(viewer_id: str) -> str:
    """The folder a viewer id reads and writes.

    A hash so the cookie never appears in a path, and so a leaked folder name
    says nothing about how to address it.
    """
    return hashlib.sha256(viewer_id.encode("ascii")).hexdigest()


def new_viewer_id() -> str:
    return secrets.token_hex(_ID_BYTES)


def is_valid_viewer_id(value: str | None) -> bool:
    return bool(value) and _ID_PATTERN.match(value or "") is not None


def is_loopback(host: str | None) -> bool:
    """Whether a connection came from this machine.

    From the socket, never from a header. `request.client.host` is what the
    kernel says; `Host` is whatever the caller typed.
    """
    if not host:
        return False
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Resolution:
    #: Present only when a cookie should be set, i.e. a viewer was just minted.
    viewer_id: str | None
    #: The folder in scope. None when tenancy is off.
    folder: str | None

    @property
    def issue_cookie(self) -> bool:
        return self.viewer_id is not None


def resolve(request: Request, settings: Settings) -> Resolution:
    if not settings.multi_tenant:
        return Resolution(None, None)

    peer = request.client.host if request.client else None
    if settings.loopback_is_local_viewer and is_loopback(peer):
        return Resolution(None, LOCAL_FOLDER)

    presented = request.cookies.get(settings.viewer_cookie_name)
    if is_valid_viewer_id(presented):
        assert presented is not None
        return Resolution(None, folder_for(presented))

    viewer_id = new_viewer_id()
    return Resolution(viewer_id, folder_for(viewer_id))


class ViewerMiddleware(BaseHTTPMiddleware):
    """Put the viewer in scope for the request and hand out the cookie.

    Middleware rather than a dependency for two reasons: the cookie has to be set
    on responses the routes build themselves (a `Response` returned from a route
    never passes back through a dependency), and a context variable written in a
    dependency does not reliably reach the endpoint, because sync endpoints run in
    a worker thread against a copy of the context taken at call time.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        settings = get_settings()
        resolution = resolve(request, settings)
        token = current_folder.set(resolution.folder)
        try:
            response = await call_next(request)
        finally:
            current_folder.reset(token)

        if resolution.issue_cookie and resolution.viewer_id:
            response.set_cookie(
                settings.viewer_cookie_name,
                resolution.viewer_id,
                max_age=settings.viewer_cookie_max_age,
                httponly=True,
                samesite="lax",
                secure=settings.viewer_cookie_secure,
                path="/",
            )
        return response


class ViewerScopedStorage:
    """A backend seen through the ambient viewer's folder.

    One instance lives for the process and addresses whichever folder is in scope
    at the moment of the call, because the same store serves every request and
    every job thread.

    A ref carries the folder it was minted under, so a record saved by one viewer
    is only resolvable by that viewer. A ref from anywhere else fails closed
    rather than being served.
    """

    def __init__(self, base: Storage, *, enabled: bool) -> None:
        self._base = base
        self.enabled = enabled

    def _prefix(self) -> str:
        if not self.enabled:
            return ""
        folder = current_folder.get()
        if not folder:
            # Enabled, but nothing is in scope: a call that escaped both the
            # middleware and a job thread. Refusing is the only safe answer,
            # because guessing a folder would read someone else's data.
            raise RuntimeError(
                "no viewer in scope; storage was reached outside a request or a job"
            )
        return f"{VIEWERS_ROOT}/{folder}"

    # -- refs ---------------------------------------------------------------

    def ref(self, key: str) -> str:
        return self._base.ref(join(self._prefix(), check_key(key)))

    # -- io -----------------------------------------------------------------

    def put(self, key: str, data: bytes) -> str:
        return self._base.put(join(self._prefix(), check_key(key)), data)

    def _admitted(self, ref: str) -> str:
        """Return the ref if the caller owns it, else refuse.

        Validated before the scope check so a traversal is refused as a bad key
        rather than reported as somebody else's missing artefact.
        """
        inner = key_from_ref(ref)
        check_key(inner)
        if not inner.startswith(self._prefix()):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"missing artefact {ref!r}")
        return ref

    def get(self, ref: str) -> bytes:
        return self._base.get(self._admitted(ref))

    def get_range(self, ref: str, start: int, length: int) -> bytes:
        return self._base.get_range(self._admitted(ref), start, length)

    def exists(self, ref: str) -> bool:
        try:
            return self._base.exists(self._admitted(ref))
        except HTTPException:
            return False

    def remove_tree(self, key: str) -> int:
        return self._base.remove_tree(join(self._prefix(), check_key(key)))

    def list_keys(self, prefix: str) -> list[str]:
        outer = self._prefix()
        whole = join(outer, check_prefix(prefix))
        found = self._base.list_keys(whole)
        # Strip the scope so callers see the same keys whichever backend and
        # whichever viewer is in play.
        cut = len(outer) + 1 if outer else 0
        return [key[cut:] for key in found]

    def local_dir(self, prefix: str) -> Path | None:
        return self._base.local_dir(join(self._prefix(), check_prefix(prefix)))

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())

    def reset(self) -> None:
        """Wipe this viewer only.

        `LocalStorage.reset` takes the whole root, which in a shared store is
        everyone's data rather than this caller's.
        """
        prefix = self._prefix()
        if not prefix:
            self._base.reset()
            return
        self._base.remove_tree(prefix)
        # `remove_tree` on a bucket deletes objects; on disk it leaves the empty
        # directory behind, which is harmless and keeps the next write cheap.
