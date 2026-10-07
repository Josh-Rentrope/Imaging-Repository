"""DICOM ingest.

Uploads are grouped into series by SeriesInstanceUID. Files whose headers cannot
be parsed are kept in a `headerless` series rather than rejected -- a header this
parser does not handle is not the same as a bad file.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from ..deps import get_storage
from ..dicom import parse_file
from ..models import DicomInstance, DicomSeries, DicomSeriesSummary
from ..storage import LocalStorage

router = APIRouter(prefix="/dicom", tags=["dicom"])

MAX_FILES = 2000
MAX_TOTAL_BYTES = 2 * 1024**3


@router.post("/series", response_model=list[DicomSeries], status_code=status.HTTP_201_CREATED)
async def upload_series(
    storage: Annotated[LocalStorage, Depends(get_storage)],
    files: Annotated[list[UploadFile], File(description="DICOM instances, any order")],
) -> list[DicomSeries]:
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="no files uploaded")
    if len(files) > MAX_FILES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{len(files)} files exceeds the {MAX_FILES}-file limit for one upload",
        )

    groups: dict[str, DicomSeries] = {}
    total = 0

    for upload in files:
        data = await upload.read()
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"upload exceeds {MAX_TOTAL_BYTES // 1024**3} GiB",
            )

        name = upload.filename or "unnamed.dcm"
        header = parse_file(data)
        key = (header.series_uid if header and header.series_uid else None) or "__headerless__"

        series = groups.get(key)
        if series is None:
            series = DicomSeries(
                series_id=(header.series_uid if header and header.series_uid else storage.new_id()),
                study_id=(header.study_uid if header and header.study_uid else "") or "",
                description=header.series_description if header else None,
                modality=header.modality if header else None,
                headerless=header is None,
            )
            groups[key] = series

        instance_key = f"dicom/{series.series_id}/{name}"
        storage.put(instance_key, data)
        series.instances.append(
            DicomInstance(
                sop_instance_uid=(header.sop_instance_uid if header else None) or "",
                file_name=name,
                ref=storage.ref(instance_key),
                bytes=len(data),
                rows=header.rows if header else None,
                columns=header.columns if header else None,
            )
        )

    for series in groups.values():
        series.instance_count = len(series.instances)
        series.bytes_total = sum(item.bytes for item in series.instances)
        # Lexical by filename, which is correct for the zero-padded names every
        # scanner writes and is all we carry until InstanceNumber is modelled.
        series.instances.sort(key=lambda item: item.file_name)
        _write_index(storage, series)

    return list(groups.values())


@router.get("/series", response_model=list[DicomSeriesSummary])
def list_series(storage: Annotated[LocalStorage, Depends(get_storage)]) -> list[DicomSeriesSummary]:
    index_dir = storage.root / "dicom"
    if not index_dir.is_dir():
        return []

    summaries = []
    for index_file in sorted(index_dir.glob("*/index.json")):
        series = DicomSeries.model_validate_json(index_file.read_text(encoding="utf-8"))
        summaries.append(
            DicomSeriesSummary(
                series_id=series.series_id,
                description=series.description,
                modality=series.modality,
                instance_count=series.instance_count,
                bytes_total=series.bytes_total,
                headerless=series.headerless,
            )
        )
    return summaries


@router.get("/series/{series_id}", response_model=DicomSeries)
def get_series(
    series_id: str, storage: Annotated[LocalStorage, Depends(get_storage)]
) -> DicomSeries:
    index_file = storage.root / "dicom" / series_id / "index.json"
    if not index_file.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no series {series_id!r}")
    return DicomSeries.model_validate_json(index_file.read_text(encoding="utf-8"))


def _write_index(storage: LocalStorage, series: DicomSeries) -> None:
    storage.put(
        f"dicom/{series.series_id}/index.json",
        json.dumps(series.model_dump(mode="json"), indent=2).encode("utf-8"),
    )
