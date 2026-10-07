"""The samples catalogue, and importing from a remote URL."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..deps import get_source_store
from ..fetch import RemoteError, fetch_dataset, origin_of
from ..samples import catalogue
from ..sources import SourceStore, to_summary

router = APIRouter(tags=["samples"])


@router.get("/samples")
def list_samples() -> dict:
    """What the app can pull in for someone to try. Empty until a bucket is set."""
    return catalogue()


class RemoteRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    workspace_id: str
    set_id: str
    name: str | None = None
    #: "dicom" or "images". Unset means decide from the contents.
    kind: str | None = None


@router.post("/sources/remote", response_model=dict, status_code=status.HTTP_201_CREATED)
def import_remote(
    request: RemoteRequest,
    store: Annotated[SourceStore, Depends(get_source_store)],
) -> dict:
    """Download a URL and add what it contains as a normal source.

    The archive is unpacked server-side, so the client never has to hold it. That
    is also why the checks in `fetch.py` matter: this endpoint makes the server
    fetch and unpack something a user named.
    """
    try:
        files = fetch_dataset(request.url)
    except RemoteError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    kind = request.kind if request.kind in ("dicom", "images") else _sniff(files)
    fallback = request.name or origin_of(request.url).rsplit("/", 1)[-1] or "Remote dataset"

    try:
        if kind == "dicom":
            record = store.create_dicom(files, request.workspace_id, request.set_id, fallback)
            if not record.renderable and record.render_reason:
                # A .zip of photographs is not a failed import; it is the other
                # kind of source, and saying which is more useful than reporting
                # that the volume could not be assembled.
                record = store.create_images(files, request.workspace_id, request.set_id, fallback)
        else:
            record = store.create_images(files, request.workspace_id, request.set_id, fallback)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"that dataset could not be read: {exc}",
        ) from exc

    summary = to_summary(record)
    return {
        **summary.model_dump(mode="json"),
        "imported_from": origin_of(request.url),
        "file_count": len(files),
    }


def _sniff(files: list[tuple[str, bytes]]) -> str:
    """DICOM or photographs, decided by looking rather than by the file name.

    An extension is a claim; a DICOM preamble is a fact. Only a handful of files
    are examined — one hit is enough, and a whole-directory scan on a 4000-file
    archive is work for nothing.
    """
    from ..dicom import parse_file

    for _name, payload in files[:24]:
        if payload[:4] == b"DICM" or parse_file(payload) is not None:
            return "dicom"
    return "images"
