"""Health and capability discovery."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from ..config import Settings
from ..deps import get_config, get_registry
from ..pipeline import BackendRegistry

router = APIRouter(tags=["health"])


@router.get("/health")
def health(registry: Annotated[BackendRegistry, Depends(get_registry)]) -> dict:
    return {"ok": True, "backends": registry.health_all()}


@router.get("/capabilities")
def capabilities(
    registry: Annotated[BackendRegistry, Depends(get_registry)],
    settings: Annotated[Settings, Depends(get_config)],
) -> dict:
    """Which backends are registered, and what each advertises."""
    return {
        "backends": [c.model_dump(mode="json") for c in registry.available()],
        "server_backend_registered": registry.has_server_backend(),
        "diagnostic_ops_enabled": settings.enable_diagnostic_tasks,
    }
