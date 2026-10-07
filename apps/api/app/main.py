"""FastAPI application.

Run it:
    uv run uvicorn app.main:app --reload --port 8787
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .jobs import JobStore
from .pipeline import BackendRegistry
from .pipeline import solvers as solver_registry
from .routers import artifacts, exports, health, jobs, samples, segmenter, solvers, sources
from .sources import SourceStore
from .storage import LocalStorage
from .tenancy import ViewerMiddleware, ViewerScopedStorage


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Discover camera-pose solvers published by the core distribution. Absent is
    # the normal case for a checkout of this repo, and is not an error.
    solver_registry.load_registered()
    app.state.settings = settings
    # One store for the process, scoped to whichever viewer is in play at the
    # moment of each call. Every holder below therefore stays viewer-agnostic.
    app.state.storage = ViewerScopedStorage(
        LocalStorage(settings.data_dir), enabled=settings.multi_tenant
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
    # outermost. CORS is added last on purpose: it then wraps the viewer, which
    # means a preflight is answered without touching a viewer, and a failure
    # inside the viewer still comes back with CORS headers instead of surfacing
    # in the browser as an opaque cross-origin error.
    app.add_middleware(ViewerMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(segmenter.router)
    app.include_router(jobs.router)
    app.include_router(sources.router)
    app.include_router(artifacts.router)
    app.include_router(exports.router)
    app.include_router(samples.router)
    app.include_router(solvers.router)

    return app


app = create_app()
