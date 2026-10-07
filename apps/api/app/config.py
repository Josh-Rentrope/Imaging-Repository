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

    #: Where bytes live: `local` for a directory on this machine, `spaces` for a
    #: DigitalOcean Spaces bucket. See `app/spaces.py`.
    storage_backend: str = "local"

    spaces_bucket: str = ""
    spaces_region: str = "nyc3"
    #: Derived from the region when unset, which is right for every DO region.
    spaces_endpoint: str = ""
    spaces_key_id: str = ""
    spaces_secret: str = ""

    #: Optional stage recordings, replayed in place of generated output.
    recordings_dir: Path = API_DIR / "recordings"

    backend: str = "auto"

    cors_origins: str = "*"

    enable_diagnostic_tasks: bool = False

    #: Whether `POST /sources/remote` will fetch a URL the caller names.
    #:
    #: On by default because it is what makes local work convenient, and off in
    #: the deployment. `app/fetch.py` is the most dangerous code in the repo: it
    #: makes the server perform network requests and unpack archives on a
    #: stranger's instruction, and its own docstring says the DNS-rebinding gap
    #: must be closed before it is "exposed beyond a trusted operator". A public
    #: hostname is exactly that exposure, so the gate is the honest answer until
    #: the connection is pinned to a vetted address.
    enable_remote_fetch: bool = True

    #: Whether a viewer is resolved per request and given their own folder.
    #:
    #: On by default so that a plain `uvicorn` run and a deployment behave the
    #: same way, and so the `local` folder is where local work lands. Turning it
    #: off reverts to a single unscoped namespace, which is what the tests that
    #: predate tenancy assume.
    multi_tenant: bool = True

    #: The name of the cookie holding a viewer id.
    viewer_cookie_name: str = "bone_viewer"

    #: Two years. The alternative is a user losing their work because a cookie
    #: expired, which is a worse failure than a long-lived one.
    viewer_cookie_max_age: int = 60 * 60 * 24 * 730

    #: Only send the cookie over https. Off by default so a plain-http local run
    #: still works; the deployment sets it.
    viewer_cookie_secure: bool = False

    #: Whether a request from this machine uses the `local` folder instead of a
    #: cookie. Off makes local runs behave exactly like the deployment.
    loopback_is_local_viewer: bool = True

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings(
        data_dir=Path(os.environ.get("BONE_VIEWER_DATA_DIR", API_DIR / ".data")),
        storage_backend=os.environ.get("BONE_VIEWER_STORAGE", "local").strip().lower(),
        spaces_bucket=os.environ.get("BONE_VIEWER_SPACES_BUCKET", ""),
        spaces_region=os.environ.get("BONE_VIEWER_SPACES_REGION", "nyc3"),
        spaces_endpoint=os.environ.get("BONE_VIEWER_SPACES_ENDPOINT", ""),
        spaces_key_id=os.environ.get("BONE_VIEWER_SPACES_KEY", ""),
        spaces_secret=os.environ.get("BONE_VIEWER_SPACES_SECRET", ""),
        recordings_dir=Path(os.environ.get("BONE_VIEWER_RECORDINGS_DIR", API_DIR / "recordings")),
        backend=os.environ.get("BONE_VIEWER_BACKEND", "auto"),
        cors_origins=os.environ.get("BONE_VIEWER_CORS_ORIGINS", "*"),
        enable_diagnostic_tasks=_truthy(os.environ.get("BONE_VIEWER_ENABLE_DIAGNOSTIC", "")),
        enable_remote_fetch=_truthy_default(
            os.environ.get("BONE_VIEWER_ENABLE_REMOTE_FETCH", ""), True
        ),
        multi_tenant=_truthy_default(os.environ.get("BONE_VIEWER_MULTI_TENANT", ""), True),
        viewer_cookie_name=os.environ.get("BONE_VIEWER_COOKIE_NAME", "bone_viewer"),
        viewer_cookie_max_age=int(
            os.environ.get("BONE_VIEWER_COOKIE_MAX_AGE", str(60 * 60 * 24 * 730))
        ),
        viewer_cookie_secure=_truthy(os.environ.get("BONE_VIEWER_COOKIE_SECURE", "")),
        loopback_is_local_viewer=_truthy_default(
            os.environ.get("BONE_VIEWER_LOOPBACK_IS_LOCAL", ""), True
        ),
    )


def _truthy_default(value: str, default: bool) -> bool:
    """A flag that defaults on: unset means the default, not off."""
    if not value.strip():
        return default
    return _truthy(value)


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}
