"""Export selected results as a self-describing archive.

The geometry and its provenance travel together, in one file, because they are
only useful together: a mesh separated from the record of how it was made is a
shape of unknown scale produced by an unknown process.
"""

from __future__ import annotations

import json
from typing import Annotated

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..config import Settings
from ..deps import get_config, get_job_store, get_source_store
from ..export import (
    FORMATS,
    as_json,
    build_manifest,
    build_zip,
    encode_mesh,
    label_summary,
    path_safe,
)
from ..jobs import JobStore
from ..pipeline.ply import read_ply
from ..sources import SourceStore

router = APIRouter(prefix="/exports", tags=["exports"])


class ExportRequest(BaseModel):
    result_ids: list[str] = Field(min_length=1)
    format: str = "stl"
    #: Per-vertex label arrays, which stay aligned with the mesh because the
    #: converters preserve vertex order.
    include_labels: bool = False
    #: One archive for everything, or a file per result. A single archive is
    #: easier to hand over; separate files are easier to load.
    single_archive: bool = True


@router.post("")
def export(
    request: ExportRequest,
    jobs: Annotated[JobStore, Depends(get_job_store)],
    sources: Annotated[SourceStore, Depends(get_source_store)],
    settings: Annotated[Settings, Depends(get_config)],
) -> Response:
    fmt = request.format.lower()
    if fmt not in FORMATS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported format {request.format!r}; expected one of {', '.join(FORMATS)}",
        )

    envelopes = []
    workflows: dict[str, str | None] = {}
    for result_id in request.result_ids:
        job = jobs.get(result_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no result {result_id!r}")
        if job.result is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"result {result_id!r} has no output to export ({job.status})",
            )
        envelopes.append(job.result)
        workflows[str(job.result.get("result_id"))] = job.workflow

    entries: list[tuple[str, bytes]] = []
    files: list[dict] = []

    for envelope in envelopes:
        result_id = str(envelope.get("result_id"))
        short = result_id[:8]
        ops = "-".join(envelope.get("ops") or ["result"])

        for index, artifact in enumerate(envelope.get("artifacts") or []):
            if artifact.get("kind") != "mesh" or artifact.get("format") != "ply":
                continue

            try:
                vertices, faces = read_ply(_read(sources, artifact["ref"]))
            except (ValueError, KeyError, OSError) as exc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{artifact['ref']} could not be read as a mesh: {exc}",
                ) from exc

            name = f"{short}-{ops}-{index}" if len(envelopes) > 1 or index else f"{short}-{ops}"

            # Resolved before the loop so the manifest can describe the labels
            # whether or not the array travels with them — the names are what
            # make the mesh interpretable.
            labels = next(
                (
                    a
                    for a in envelope.get("artifacts") or []
                    if a.get("kind") == "labels" and a.get("header_ref")
                ),
                None,
            )
            legend = None
            if labels is not None:
                try:
                    legend = label_summary(
                        json.loads(_read(sources, labels["header_ref"]).decode("utf-8"))
                    )
                except (ValueError, KeyError, OSError):
                    legend = None

            written = encode_mesh(vertices, faces, fmt, name)
            for file_name, payload in written:
                entries.append((file_name, payload))
                files.append(
                    {
                        "name": file_name,
                        "result_id": result_id,
                        "artifact_ref": artifact["ref"],
                        "vertices": int(len(vertices)),
                        "triangles": int(len(faces)),
                        "units": artifact.get("units"),
                        "labels": legend,
                    }
                )

            if request.include_labels and labels is not None and legend is not None:
                # Beside the mesh and named after it, not in a subfolder: a
                # label array means nothing except in relation to one specific
                # mesh, and the pairing should survive being unpacked elsewhere.
                stem = f"{path_safe(name)}.labels"
                entries.append((f"{stem}.bin", _read(sources, labels["ref"])))
                entries.append((f"{stem}.json", _read(sources, labels["header_ref"])))
                # The name of the binary file is recorded on the mesh it belongs
                # to, rather than in an entry of its own that says only "this is
                # a label array" with no mesh to be a label array of.
                files[-1]["label_file"] = {
                    "bin": f"{stem}.bin",
                    "json": f"{stem}.json",
                    "dtype": "int32",
                    "per": "vertex",
                }

    if not entries:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="none of the selected results produced a mesh to export",
        )

    source_id = (jobs.get(request.result_ids[0]).source_id if jobs.get(request.result_ids[0]) else None)
    record = sources.get(source_id) if source_id else None

    manifest = build_manifest(
        application="Bone Viewer",
        application_version=settings.version,
        source={
            "source_id": source_id,
            "kind": record.kind if record else None,
            "name": record.name if record else None,
            "modality": record.modality if record else None,
            "instance_count": record.instance_count if record else None,
        },
        envelopes=envelopes,
        workflows=workflows,
        files=files,
        fmt=fmt,
    )
    entries.append(("manifest.json", as_json(manifest)))

    archive = build_zip(entries)
    stamp = manifest["generated_at"][:19].replace(":", "").replace("-", "")
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="bone-viewer-{stamp}.zip"',
            "X-File-Count": str(len(entries)),
        },
    )


def _read(sources: SourceStore, ref: str) -> bytes:
    return sources.storage.get(ref)
