"""What a deployed process can tell you about itself.

Written after a container failed to start and the logs could not answer the two
questions that mattered: *what configuration was this process running with*, and
*what was it doing when it stopped*. Three things were missing, and each is a
separate way for a deployment to be undiagnosable:

* **A startup report.** A container that comes up with the wrong store, the wrong
  bucket or a segmenter that silently did not install looks identical in the log
  to one that came up correctly -- which is to say, silent. One line at startup,
  carrying the effective configuration, makes every restart self-describing and
  makes a restart itself visible.

* **A per-request line carrying the things that are not in the URL.** The default
  access log says which path was hit; it cannot say *which viewer* it served or
  how long the store took. Those are the two facts you need to tell "Spaces is
  slow" apart from "this viewer has a lot of data".

* **Failures that are recorded rather than swallowed.** Suppressing an error so a
  request can still succeed is often right; suppressing it *silently* is not,
  because the success then hides a store that is not being written to.

Nothing here exposes a credential. The viewer is logged as the folder it maps to,
which is a hash and is the same thing that already appears in artefact refs.
"""

from __future__ import annotations

import logging
import platform
import sys
import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .config import Settings
from .storage import Storage
from .tenancy import current_folder

logger = logging.getLogger("bone_viewer")

#: Set by the access middleware so a request line can report it without the
#: middleware having to run inside the viewer's scope.
_VIEWER_UNKNOWN = "-"


def configure(settings: Settings) -> None:
    """Logging, once, at startup."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    # The enriched request line below replaces this one. Two lines per request
    # would be noise, and the default one is the weaker of the two: it has no
    # viewer, no duration and no outcome beyond the status code.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for noisy in ("botocore", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def store_description(settings: Settings) -> str:
    """Where bytes go, without saying how to get at them."""
    if settings.storage_backend == "spaces":
        return f"spaces bucket={settings.spaces_bucket} region={settings.spaces_region}"
    return f"local dir={settings.data_dir}"


def segmenter_status() -> str:
    """Whether the segmenter can be run at all.

    Answered by resolving the binaries rather than by running one, so it is
    cheap. The class list is a subprocess and belongs behind `/segmenter/classes`
    where it is asked for rather than paid for on every start.
    """
    try:
        from .pipeline import totalseg
    except Exception as exc:  # pragma: no cover - import failure is the answer
        return f"unavailable ({type(exc).__name__}: {exc})"

    try:
        segmenter = totalseg.resolve_binary("")
    except Exception as exc:
        # `SegUnavailable` carries a message written for a human, which is
        # exactly what belongs in a startup log.
        return f"unavailable ({exc})"

    binary = " ".join(segmenter)
    try:
        info = totalseg.info_binary("")
    except Exception:
        info = None
    return f"available ({binary})" + (f" info={' '.join(info)}" if info else " info=NONE")


def startup_report(settings: Settings, storage: Storage, routes: int) -> str:
    """The effective configuration, as one multi-line log entry.

    ASCII only. Logs are read through consoles, log collectors and ticket
    attachments, and a startup banner that raises `UnicodeEncodeError` on a
    cp1252 console is a banner that fails on exactly the machine someone is
    trying to diagnose. This was written with box-drawing rules first and they
    did precisely that.
    """
    base = getattr(storage, "_base", storage)
    lines = [
        "",
        "=== bone-viewer-api " + "=" * 46,
        f"  version      {settings.version}   python {platform.python_version()}",
        f"  store        {type(base).__name__}  {store_description(settings)}",
        f"  viewers      {'on' if settings.multi_tenant else 'OFF (single namespace)'}"
        f"  cookie={settings.viewer_cookie_name}"
        f" secure={settings.viewer_cookie_secure}"
        f" loopback_is_local={settings.loopback_is_local_viewer}",
        f"  cors         {settings.cors_origins}",
        f"  gates        remote_fetch={settings.enable_remote_fetch}"
        f" diagnostic_ops={settings.enable_diagnostic_tasks}",
        f"  recordings   {settings.recordings_dir}",
        f"  segmenter    {segmenter_status()}",
        f"  routes       {routes}",
        "=" * 65,
    ]
    return "\n".join(lines)


# ── the store, probed on demand ─────────────────────────────────────────────


def store_probe(storage: Storage) -> dict:
    """Write, read and remove one small object.

    A health check that only asks the process whether it is alive answers a
    question nobody has. This asks the thing that actually fails: whether the
    bytes can be put somewhere and got back. The object is removed again, so
    probing repeatedly does not accumulate.

    Deliberately NOT wired into `/health`. App Platform polls that on a
    thirty-second timer with a ten-second timeout, and a health check that
    depends on a bucket round trip would let a slow Spaces turn into a container
    the platform decides is unhealthy and replaces -- turning a performance
    problem into an outage.
    """
    key = "diagnostics/probe.txt"
    started = time.perf_counter()
    try:
        ref = storage.put(key, b"bone-viewer-probe")
        got = storage.get(ref)
        storage.remove_tree(key)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    return {
        "ok": got == b"bone-viewer-probe",
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
    }


# ── one line per request ────────────────────────────────────────────────────


class AccessLogMiddleware(BaseHTTPMiddleware):
    """A request line carrying the viewer and how long the request took.

    The viewer is taken from the ambient folder rather than from the cookie, so
    the log names a hash and never the credential that reads it -- while still
    being enough to join requests to a viewer and to the refs in the store.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Logged with the viewer and the path, then re-raised: the exception
            # handler still owns the response, but a request that died inside the
            # stack should not be the one thing missing from the log.
            logger.exception(
                "%s %s FAILED after %.0fms viewer=%s",
                request.method,
                request.url.path,
                (time.perf_counter() - started) * 1000,
                current_folder.get() or _VIEWER_UNKNOWN,
            )
            raise
        elapsed = (time.perf_counter() - started) * 1000
        logger.info(
            "%s %s %s %.0fms viewer=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed,
            (current_folder.get() or _VIEWER_UNKNOWN)[:8],
        )
        return response
