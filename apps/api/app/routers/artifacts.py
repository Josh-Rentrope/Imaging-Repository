"""Artefact serving.

Artefacts are addressed by opaque ref, never by path, so tenant-scoped buckets
and signed URLs can be swapped in behind this route.

Not authenticated yet: every read is unscoped.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from ..deps import get_storage
from ..storage import LocalStorage

router = APIRouter(prefix="/artifacts", tags=["artifacts"])

_MEDIA_TYPES = {
    ".ply": "application/octet-stream",
    ".stl": "model/stl",
    ".obj": "text/plain",
    ".vtk": "text/plain",
    ".vti": "application/octet-stream",
    ".json": "application/json",
    ".png": "image/png",
}


@router.get("/{ref:path}")
def fetch(ref: str, storage: Annotated[LocalStorage, Depends(get_storage)]) -> FileResponse:
    # Callers pass the ref with its scheme; strip the single leading slash the
    # path converter leaves behind when it is absent.
    full_ref = ref if "://" in ref else f"local://{ref}"
    try:
        path = storage.path_for(full_ref)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        filename=path.name,
    )
