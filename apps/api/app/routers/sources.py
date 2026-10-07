"""Sources: upload, list, rename, delete, and volume access.

One surface for both DICOM series and image sets. The client supplies
`workspace_id` and `set_id` as form fields on upload and query parameters on
list, which is what makes uploads persist across reloads without a session.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from ..deps import get_source_store
from ..sources import (
    MAX_FILES,
    MAX_TOTAL_BYTES,
    SourceRecord,
    SourceStore,
    SourceSummary,
    VolumePayload,
    to_summary,
)

router = APIRouter(prefix="/sources", tags=["sources"])


async def _read_uploads(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="no files uploaded")
    if len(files) > MAX_FILES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{len(files)} files exceeds the {MAX_FILES}-file limit",
        )

    payloads: list[tuple[str, bytes]] = []
    total = 0
    for upload in files:
        data = await upload.read()
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"upload exceeds {MAX_TOTAL_BYTES // 1024**3} GiB",
            )
        payloads.append((upload.filename or "unnamed", data))
    return payloads


@router.post("/dicom", response_model=SourceSummary, status_code=status.HTTP_201_CREATED)
async def upload_dicom(
    store: Annotated[SourceStore, Depends(get_source_store)],
    files: Annotated[list[UploadFile], File()],
    workspace_id: Annotated[str, Form()] = "",
    set_id: Annotated[str, Form()] = "",
    name: Annotated[str | None, Form()] = None,
) -> SourceSummary:
    record = store.create_dicom(await _read_uploads(files), workspace_id, set_id, name)
    return to_summary(record)


@router.post("/images", response_model=SourceSummary, status_code=status.HTTP_201_CREATED)
async def upload_images(
    store: Annotated[SourceStore, Depends(get_source_store)],
    files: Annotated[list[UploadFile], File()],
    workspace_id: Annotated[str, Form()] = "",
    set_id: Annotated[str, Form()] = "",
    name: Annotated[str | None, Form()] = None,
) -> SourceSummary:
    record = store.create_images(await _read_uploads(files), workspace_id, set_id, name)
    return to_summary(record)


@router.get("", response_model=list[SourceSummary])
def list_sources(
    store: Annotated[SourceStore, Depends(get_source_store)],
    workspace_id: str | None = None,
    set_id: str | None = None,
) -> list[SourceSummary]:
    return [to_summary(record) for record in store.list(workspace_id, set_id)]


@router.get("/{source_id}", response_model=SourceRecord)
def get_source(source_id: str, store: Annotated[SourceStore, Depends(get_source_store)]) -> SourceRecord:
    record = store.get(source_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no source {source_id!r}")
    return record


@router.patch("/{source_id}", response_model=SourceSummary)
def rename_source(
    source_id: str,
    store: Annotated[SourceStore, Depends(get_source_store)],
    name: Annotated[str, Form()] = "",
) -> SourceSummary:
    record = store.get(source_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no source {source_id!r}")

    cleaned = name.strip()
    if not cleaned:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="name must not be empty")

    record.name = cleaned
    return to_summary(store.save(record))


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(source_id: str, store: Annotated[SourceStore, Depends(get_source_store)]) -> None:
    if not store.delete(source_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no source {source_id!r}")


@router.get("/{source_id}/volume", response_model=VolumePayload)
def get_volume(source_id: str, store: Annotated[SourceStore, Depends(get_source_store)]) -> VolumePayload:
    record = store.get(source_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no source {source_id!r}")

    payload = store.volume_payload(record)
    if payload is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=record.render_reason or "no volume for this source",
        )
    return payload
