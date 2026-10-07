"""FastAPI application.

Run it:
    uv run uvicorn app.main:app --reload --port 8787
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings, get_settings
from .jobs import JobStore
from .observability import AccessLogMiddleware, configure, logger, startup_report
from .pipeline import BackendRegistry
from .pipeline import solvers as solver_registry
from .routers import (
    artifacts,
    diagnostics,
    exports,
    health,
    jobs,
    samples,
    segmenter,
    solvers,
    sources,
)
from .sources import SourceStore
from .spaces import SpacesStorage
from .storage import LocalStorage, Storage
from .tenancy import ViewerMiddleware, ViewerScopedStorage


def build_storage(settings: Settings) -> Storage:
    """The backend named by configuration.

    An unknown name is refused at startup rather than falling back, because the
    fallback would be a store that silently accepts writes and loses them the
    moment the container is replaced. A deployment that says `spaces` and gets a
    container filesystem is worse than one that does not start.
    """
    if settings.storage_backend == "local":
        return LocalStorage(settings.data_dir)

    if settings.storage_backend == "spaces":
        if not settings.spaces_bucket:
            raise RuntimeError(
                "BONE_VIEWER_STORAGE=spaces needs BONE_VIEWER_SPACES_BUCKET. "
                "Without it there is nowhere to put anything."
            )
        return SpacesStorage(
            bucket=settings.spaces_bucket,
            region=settings.spaces_region,
            access_key=settings.spaces_key_id,
            secret_key=settings.spaces_secret,
            endpoint=settings.spaces_endpoint or None,
        )

    raise RuntimeError(
        f"unknown BONE_VIEWER_STORAGE {settings.storage_backend!r}; "
        "expected 'local' or 'spaces'"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure(settings)
    # Discover camera-pose solvers published by the core distribution. Absent is
    # the normal case for a checkout of this repo, and is not an error.
    solver_registry.load_registered()
    app.state.settings = settings
    # One store for the process, scoped to whichever viewer is in play at the
    # moment of each call. Every holder below therefore stays viewer-agnostic.
    app.state.storage = ViewerScopedStorage(
        build_storage(settings), enabled=settings.multi_tenant
    )
    # The source store comes first: the pipeline reads volumes out of it.
    app.state.source_store = SourceStore(app.state.storage)
    app.state.registry = BackendRegistry(
        storage=app.state.storage,
        recordings_dir=settings.recordings_dir,
        preference=settings.backend,
        source_store=app.state.source_store,
    )
    app.state.job_store = JobStore(
        app.state.registry,
        storage=app.state.storage,
        # With tenancy on there is no viewer at startup, so there is no folder
        # that could be read back safely. Each viewer's history is loaded on
        # their first request instead.
        preload=not settings.multi_tenant,
    )
    # Last, so it reports the app fully wired rather than half-built. Every
    # container start logs this, which is also how a restart becomes visible: a
    # second banner in the log is a second container.
    logger.info(
        startup_report(settings, app.state.storage, routes=len(app.routes))
    )
    yield


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Bone Viewer API",
        version=settings.version,
        summary="Orthodontic reconstruction and radiograph platform (working name)",
        lifespan=lifespan,
    )

    # Middleware is applied inside-out, so the one added *last* ends up
    # outermost. Added in this order the stack is, outermost first:
    #
    #     CORS  ->  viewer  ->  access log  ->  routes
    #
    # which is the order each one needs. CORS outermost so a preflight is answered
    # without touching a viewer, and so a failure inside the viewer still comes
    # back with CORS headers rather than surfacing in the browser as an opaque
    # cross-origin error. The access log *inside* the viewer, because the one
    # thing it adds over the default log is which viewer a request served, and
    # that is only in scope once the viewer has run.
    #
    # Credentials are allowed only when specific origins are named, because `*`
    # and credentials are contradictory: a browser refuses a wildcard origin on a
    # credentialed request. Emitting both would produce a configuration that
    # looks right, answers the preflight, and has the browser silently discard the
    # viewer cookie on the way back -- and the cookie is the whole of the
    # separation, so that failure is total and invisible. Same-origin requests do
    # not consult CORS at all, which is why the local dev proxy is unaffected
    # either way.
    named_origins = "*" not in settings.cors_origin_list

    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(ViewerMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=named_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(diagnostics.router)
    app.include_router(segmenter.router)
    app.include_router(jobs.router)
    app.include_router(sources.router)
    app.include_router(artifacts.router)
    app.include_router(exports.router)
    app.include_router(samples.router)
    app.include_router(solvers.router)

    return app


app = create_app()
