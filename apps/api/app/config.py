from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

API_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseModel):
    app_name: str = "bone-viewer-api"
    version: str = "0.0.1"

    data_dir: Path = API_DIR / ".data"

    #: Optional stage recordings, replayed in place of generated output.
    recordings_dir: Path = API_DIR / "recordings"

    backend: str = "auto"

    cors_origins: str = "*"

    enable_diagnostic_tasks: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings(
        data_dir=Path(os.environ.get("BONE_VIEWER_DATA_DIR", API_DIR / ".data")),
        recordings_dir=Path(os.environ.get("BONE_VIEWER_RECORDINGS_DIR", API_DIR / "recordings")),
        backend=os.environ.get("BONE_VIEWER_BACKEND", "auto"),
        cors_origins=os.environ.get("BONE_VIEWER_CORS_ORIGINS", "*"),
        enable_diagnostic_tasks=_truthy(os.environ.get("BONE_VIEWER_ENABLE_DIAGNOSTIC", "")),
    )


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}
