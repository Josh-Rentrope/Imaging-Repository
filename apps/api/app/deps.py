"""Shared dependencies.

Everything is attached to `app.state` at startup and read back through these
accessors, so tests can build an app against a temporary data dir without
monkeypatching module globals.
"""

from __future__ import annotations

from fastapi import Request

from .config import Settings, get_settings
from .jobs import JobStore
from .pipeline import BackendRegistry
from .sources import SourceStore
from .storage import Storage


def get_storage(request: Request) -> Storage:
    """The store, already scoped to the viewer making this request.

    Which folder that is was settled by the middleware before the route ran, so
    nothing below here has to know about viewers at all.
    """
    return request.app.state.storage


def get_registry(request: Request) -> BackendRegistry:
    return request.app.state.registry


def get_job_store(request: Request) -> JobStore:
    return request.app.state.job_store


def get_source_store(request: Request) -> SourceStore:
    return request.app.state.source_store


def get_config(request: Request) -> Settings:
    return request.app.state.settings


__all__ = [
    "get_config",
    "get_job_store",
    "get_registry",
    "get_settings",
    "get_source_store",
    "get_storage",
]
