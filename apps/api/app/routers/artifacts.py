"""Artefact serving.

Artefacts are addressed by an opaque ref, never by a path, so the backend behind
this route can be a directory today and a bucket later without the route
changing.

Reads are scoped: a ref is served only to the viewer whose folder minted it, and
a ref naming any other folder is reported as missing rather than fetched. That
scoping is what makes an artefact URL unshareable -- the URL alone is not enough
without the cookie, which is the property refs were made opaque for.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response

from ..deps import get_storage
from ..storage import SCHEME, Storage

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
def fetch(ref: str, storage: Annotated[Storage, Depends(get_storage)]) -> Response:
    # Callers pass the ref with its scheme; strip the single leading slash the
    # path converter leaves behind when it is absent.
    full_ref = ref if "://" in ref else f"{SCHEME}{ref}"
    try:
        data = storage.get(full_ref)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # The name is taken from the key rather than from a path, because on a bucket
    # there is no path. Everything here is a small mesh, mask or header the client
    # asked for whole, so the body is served in one piece.
    name = full_ref.rsplit("/", 1)[-1]
    suffix = f".{name.rsplit('.', 1)[-1].lower()}" if "." in name else ""
    return Response(
        content=data,
        media_type=_MEDIA_TYPES.get(suffix, "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
