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
from .routers import artifacts, exports, health, jobs, segmenter, sources
from .sources import SourceStore
from .storage import LocalStorage


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.storage = LocalStorage(settings.data_dir)
    # The source store comes first: the pipeline reads volumes out of it.
    app.state.source_store = SourceStore(app.state.storage)
    app.state.registry = BackendRegistry(
        storage=app.state.storage,
        recordings_dir=settings.recordings_dir,
        preference=settings.backend,
        source_store=app.state.source_store,
    )
    app.state.job_store = JobStore(
        app.state.registry, data_dir=settings.data_dir, storage=app.state.storage
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

    return app


app = create_app()
