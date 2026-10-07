"""Packaging results for use somewhere else.

An export is two things: the geometry, in a format the recipient's software
reads, and a record of how it was produced. The second is the point. Geometry
that arrives without its provenance is a mesh of unknown scale, unknown units
and unknown algorithm — which for anything clinical is worse than no file at
all, because it looks usable.

FBX is deliberately absent. It is a proprietary binary format that needs the
Autodesk SDK to write; a hand-rolled approximation would produce files that
open in some tools and not others, which is the worst of both.
"""

from __future__ import annotations

import io
import json
import struct
import zipfile
from datetime import UTC, datetime
from typing import Any

import numpy as np

FORMATS = ("stl", "obj", "ply")

#: What each format is good for. Used in the UI, deliberately NOT copied into
#: the manifest: the manifest states what was made, not what to think of it.
FORMAT_NOTES = {
    "stl": "Meshes only, no colour. The safest bet for CAD and printing.",
    "obj": "Meshes with a separate material file, so colour travels too.",
    "ply": "Exactly what the viewer loaded, vertex labels included.",
}


def to_stl(vertices: np.ndarray, faces: np.ndarray) -> bytes:
    """Binary STL, the format every mesh tool reads.

    No colour and no units: STL has a header field but no convention for either,
    so the units live in the manifest and nowhere else.
    """
    triangles = vertices[faces]
    edge1 = triangles[:, 1] - triangles[:, 0]
    edge2 = triangles[:, 2] - triangles[:, 0]
    normals = np.cross(edge1, edge2)

    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    # A degenerate triangle has no normal; zero is the conventional answer and
    # readers recompute from the winding anyway.
    normals = np.divide(normals, lengths, out=np.zeros_like(normals), where=lengths > 0)

    record = np.zeros(
        len(faces), dtype=[("normal", "<f4", 3), ("points", "<f4", (3, 3)), ("attr", "<u2")]
    )
    record["normal"] = normals.astype("<f4")
    record["points"] = triangles.astype("<f4")
    record["attr"] = 0

    header = b"Bone Viewer export".ljust(80, b"\0")
    return header + struct.pack("<I", len(faces)) + record.tobytes()


def to_obj(vertices: np.ndarray, faces: np.ndarray, name: str) -> tuple[bytes, bytes]:
    """Wavefront OBJ, plus a material file so the mesh arrives as something.

    Indices are one-based here, which is the single most common way to produce
    an OBJ that loads as an empty scene.
    """
    lines = [
        "# Bone Viewer export",
        f"# {name}",
        f"# {len(vertices)} vertices, {len(faces)} triangles",
        f"mtllib {path_safe(name)}.mtl",
        f"o {path_safe(name)}",
    ]
    lines.extend(f"v {x:.6f} {y:.6f} {z:.6f}" for x, y, z in vertices)
    lines.append(f"usemtl {path_safe(name)}")
    lines.extend(f"f {a + 1} {b + 1} {c + 1}" for a, b, c in faces)

    material = (
        f"newmtl {path_safe(name)}\n"
        "Ka 0.6 0.6 0.6\n"
        "Kd 0.75 0.75 0.72\n"
        "Ks 0.1 0.1 0.1\n"
        "Ns 20\n"
        "d 1\n"
        "illum 2\n"
    )
    return "\n".join(lines).encode("utf-8"), material.encode("utf-8")


def path_safe(name: str) -> str:
    """A name that survives a filesystem and does not escape the archive."""
    cleaned = "".join(c if c.isalnum() or c in "-_." else "-" for c in name).strip("-")
    return (cleaned or "mesh")[:64]


def build_manifest(
    *,
    application: str,
    application_version: str,
    source: dict[str, Any],
    envelopes: list[dict[str, Any]],
    workflows: dict[str, str | None],
    files: list[dict[str, Any]],
    fmt: str,
) -> dict[str, Any]:
    """How the exported data came to exist.

    Assembled from the result envelopes rather than restated, so a machine can
    trace any exported file back to the operation, the tool and the parameters
    that produced it. The alternative — a document written by hand alongside the
    export — drifts from the data the first time a parameter changes.

    Facts only. Anything that reads as advice belongs in the interface, where it
    can be read once and updated; putting it here means every archive ever made
    carries whatever we believed on the day it was written, and a consumer
    parsing this is looking for values, not commentary.
    """
    return {
        "manifest_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "application": {"name": application, "version": application_version},
        "mesh_format": fmt,
        "source": source,
        "runs": [
            {
                "result_id": envelope.get("result_id"),
                # Which pipeline was chosen, not just which operations ran: the
                # same two stages mean different things under different
                # workflows, and the ops alone do not say which was intended.
                "workflow": workflows.get(str(envelope.get("result_id"))),
                "ops": envelope.get("ops", []),
                # The registry key of the weights that produced the labels and
                # geometry. A label id means nothing without it: id 5 is one
                # structure under `total` and another under `teeth`.
                "model_version": envelope.get("model_version"),
                "scale": envelope.get("scale"),
                "geometry": envelope.get("geometry"),
                "measurements": envelope.get("measurements", []),
                "warnings": envelope.get("warnings", []),
                "segmentation": _segmentation(envelope.get("segmentation")),
                "stages": _stages((envelope.get("provenance") or {}).get("stages", [])),
                "produced_at": (envelope.get("provenance") or {}).get("started_at"),
                "backend": (envelope.get("provenance") or {}).get("backend"),
            }
            for envelope in envelopes
        ],
        "files": files,
    }


def _stages(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stage records without their prose.

    The `notes` field is worth having in the interface — it is the sort of thing
    that belongs on a hover — but in an interchange document it is text a
    consumer has to skip past, and it dates: a note written today rides along in
    every archive made from now on, still asserting what we believed then.
    """
    return [{k: v for k, v in stage.items() if k != "notes"} for stage in stages]


def _segmentation(segmentation: dict[str, Any] | None) -> dict[str, Any] | None:
    """Class names and volumes, without the mask reference.

    The mask itself is not in the archive, so its ref would point at nothing.
    """
    if not segmentation:
        return None
    return {
        "kind": segmentation.get("kind"),
        "classes": segmentation.get("classes", []),
        "flags": segmentation.get("flags", []),
    }


def label_summary(header: dict[str, Any]) -> dict[str, Any]:
    """The legend and tallies from a vertex-label sidecar header.

    Included whether or not the array itself is in the archive: the names are
    what make the exported mesh interpretable, and they are small.
    """
    return {
        "count": header.get("count"),
        "legend": header.get("legend", {}),
        "counts": header.get("counts", {}),
    }


def build_zip(entries: list[tuple[str, bytes]]) -> bytes:
    """A zip in memory. Timestamps are fixed so the same input gives the same bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    return buffer.getvalue()


def encode_mesh(
    vertices: np.ndarray, faces: np.ndarray, fmt: str, name: str
) -> list[tuple[str, bytes]]:
    """One mesh in the requested format, as (filename, bytes) pairs."""
    stem = path_safe(name)
    if fmt == "stl":
        return [(f"{stem}.stl", to_stl(vertices, faces))]
    if fmt == "obj":
        obj, material = to_obj(vertices, faces, name)
        return [(f"{stem}.obj", obj), (f"{stem}.mtl", material)]
    from .pipeline.ply import write_ply

    return [(f"{stem}.ply", write_ply(vertices, faces))]


def as_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, indent=2, default=str).encode("utf-8")
