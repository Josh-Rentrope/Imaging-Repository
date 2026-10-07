"""What this deployment is doing, on demand.

Answers in one request the questions a log has to be read to answer: which store
is in use, whether it can actually be written to and read back, whether the
segmenter resolves, and how much is loaded.

Kept separate from `/health` on purpose. App Platform polls `/health` every
thirty seconds with a ten-second timeout, so anything expensive put there turns a
slow dependency into a container the platform decides is unhealthy and replaces —
a performance problem escalated into an outage. `/health` stays a statement about
the process; a bucket round trip belongs here, where a person asks for it.

Nothing in the response is a credential. The bucket name and region are operationally
useful and not secret; the keys are never read here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from ..config import Settings
from ..deps import get_config, get_job_store, get_registry, get_source_store, get_storage
from ..jobs import JobStore
from ..observability import segmenter_status, store_description, store_probe
from ..pipeline import BackendRegistry
from ..sources import SourceStore
from ..storage import Storage
from ..tenancy import current_folder

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


@router.get("")
def diagnostics(
    settings: Annotated[Settings, Depends(get_config)],
    storage: Annotated[Storage, Depends(get_storage)],
    source_store: Annotated[SourceStore, Depends(get_source_store)],
    job_store: Annotated[JobStore, Depends(get_job_store)],
    registry: Annotated[BackendRegistry, Depends(get_registry)],
) -> dict:
    """Configuration, a live store probe, and what is loaded."""
    return {
        "version": settings.version,
        "configuration": {
            "storage": describe(storage, settings),
            "viewers": {
                "multi_tenant": settings.multi_tenant,
                # The folder, not the cookie. It is a hash, and it is already
                # what appears in artefact refs, so it is the join key between a
                # request and the objects it addressed.
                "folder": current_folder.get() or None,
                "cookie_secure": settings.viewer_cookie_secure,
            },
            "cors_origins": settings.cors_origin_list,
            "gates": {
                "remote_fetch": settings.enable_remote_fetch,
                "diagnostic_ops": settings.enable_diagnostic_tasks,
            },
            "log_level": settings.log_level,
            "recordings_dir": str(settings.recordings_dir),
        },
        # The one part of this that is a measurement rather than a report.
        "store": store_probe(storage),
        "segmenter": {
            "status": segmenter_status(),
            "classes_available": segmenter_class_count(),
        },
        "loaded": {
            "sources": len(source_store.list()),
            "jobs": len(job_store.list(limit=10_000)),
            "backends": [c.backend for c in registry.available()],
            "server_backend_registered": registry.has_server_backend(),
        },
    }


def describe(storage: Storage, settings: Settings) -> str:
    """The backend actually in use, unwrapped from any viewer scope."""
    base = getattr(storage, "_base", storage)
    return f"{type(base).__name__}  {store_description(settings)}"


def segmenter_class_count() -> int:
    """How many structures the segmenter can name. Zero means it cannot.

    Guarded because a diagnostics endpoint that raises is a diagnostics endpoint
    that cannot be used on the deployment where something is wrong.
    """
    try:
        from ..pipeline import totalseg

        return len(totalseg.class_names("total"))
    except Exception:
        return 0
