"""Sources: upload, list, rename, delete, and volume access.

One surface for both DICOM series and image sets. The client supplies
`workspace_id` and `set_id` as form fields on upload and query parameters on
list, which is what makes uploads persist across reloads without a session.
"""

from __future__ import annotations

import io
from typing import Annotated

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response

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


#: Media types the browser will render directly for an uploaded photo.
_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


@router.get("/{source_id}/images")
def list_images(
    source_id: str, store: Annotated[SourceStore, Depends(get_source_store)]
) -> dict:
    """What the Image View can show for this source.

    A DICOM series is listed as axial slices of the assembled volume rather than
    as files on disk. The volume is the thing the 3D view draws and it is already
    in geometric order and windowed, so the two views cannot disagree about what
    slice 40 is — which they could if this re-read and re-sorted the files.

    The cost is resolution: the volume is assembled at its working stride, so
    these are coarser than the original pixels.
    """
    record = store.get(source_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no source {source_id!r}")

    if record.kind == "dicom":
        volume = store.volume_payload(record)
        meta = record.volume_meta or {}
        dims = meta.get("dims") or []
        if volume is None or len(dims) != 3:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=record.render_reason or "this series has no assembled volume",
            )
        return {
            "source_id": source_id,
            "kind": "dicom",
            "axis": "axial",
            "count": int(dims[2]),
            "window": {
                "center": meta.get("window_center"),
                "width": meta.get("window_width"),
            },
            "slice_spacing_mm": (meta.get("spacing") or [None, None, None])[2],
            "images": [
                {"index": index, "name": f"slice {index + 1}"} for index in range(int(dims[2]))
            ],
        }

    return {
        "source_id": source_id,
        "kind": "images",
        "count": len(record.instances),
        "images": [
            {
                "index": index,
                "name": instance.file_name,
                "bytes": instance.bytes,
                "media_type": _MEDIA_TYPES.get(
                    "." + instance.file_name.rsplit(".", 1)[-1].lower()
                    if "." in instance.file_name
                    else "",
                    "application/octet-stream",
                ),
            }
            for index, instance in enumerate(record.instances)
        ],
    }


@router.get("/{source_id}/images/{index}")
def get_image(
    source_id: str,
    index: int,
    store: Annotated[SourceStore, Depends(get_source_store)],
    level: float | None = None,
    window: float | None = None,
) -> Response:
    """One image: a windowed PNG for a DICOM slice, the original bytes for a photo.

    `level` and `window` override the series defaults, so the Image View can
    offer the same contrast control a clinical viewer would.
    """
    record = store.get(source_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no source {source_id!r}")

    if record.kind != "dicom":
        if not 0 <= index < len(record.instances):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no image {index}")
        instance = record.instances[index]
        media = _MEDIA_TYPES.get(
            "." + instance.file_name.rsplit(".", 1)[-1].lower()
            if "." in instance.file_name
            else "",
            "application/octet-stream",
        )
        return Response(
            content=store.storage.get(instance.ref),
            media_type=media,
            headers={"Cache-Control": "private, max-age=3600"},
        )

    payload = store.volume_payload(record)
    header = payload.header if payload else {}
    dims = [int(v) for v in header.get("dims", [])]
    if len(dims) != 3:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="no assembled volume")
    nz = dims[2]
    if not 0 <= index < nz:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no slice {index} of {nz}")

    nx, ny, _ = dims
    # One plane, not the volume. The blob is a C-ordered (nz, ny, nx) float32
    # array, so a plane is a contiguous run of bytes and can be asked for on its
    # own -- which it has to be, because the store is now a bucket: reading the
    # whole object to return one slice meant downloading the entire study's
    # volume, tens of megabytes, for every thumbnail in the strip.
    plane_bytes = nx * ny * 4
    plane = np.frombuffer(
        store.storage.get_range(payload.bin_ref, index * plane_bytes, plane_bytes),
        dtype="<f4",
        count=nx * ny,
    ).reshape(ny, nx)

    centre, width = _window(record, plane, level, window)
    low = centre - width / 2
    span = width if width else 1.0
    scaled = np.clip((plane - low) / span, 0.0, 1.0)

    from PIL import Image

    buffer = io.BytesIO()
    # Flipped vertically: an axial slice's first row is the patient's posterior
    # in DICOM's convention, and an image is drawn top-down.
    Image.fromarray((scaled[::-1] * 255).astype(np.uint8), mode="L").save(
        buffer, format="PNG", optimize=True
    )
    return Response(
        content=buffer.getvalue(),
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=3600",
            # Already post-stride: the stored spacing is the spacing of the
            # volume as stored, so multiplying by stride again reports slices as
            # twice as far apart as they are.
            "X-Slice-Spacing-MM": str(header.get("spacing", [0, 0, 0])[2]),
        },
    )


def _window(record, plane: np.ndarray, level: float | None, width: float | None) -> tuple[float, float]:
    """Window centre and width, from the request, the series, or the slice."""
    meta = record.volume_meta or {}
    centre = level if level is not None else meta.get("window_center")
    span = width if width is not None else meta.get("window_width")
    if centre is not None and span:
        return float(centre), float(span)

    low = float(np.percentile(plane, 1))
    high = float(np.percentile(plane, 99))
    if high <= low:
        high = low + 1.0
    return (low + high) / 2, high - low


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
