"""Backend that executes pipeline stages without model weights.

Implements the same operations as the server backend and returns the same result
shapes, so the two are interchangeable: routing a job to one or the other is a
deployment decision, not a code path.

Stages run in order against a shared envelope, which is how a multi-stage job
(rectify -> reconstruct -> segment -> measure) returns one combined result.

If `recordings/<op>.json` exists its contents are merged in place of the
generated output for that stage, so a response captured from a real backend can
be replayed through the same code path.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from ..sources import SourceStore
from ..storage import LocalStorage
from .interfaces import (
    BackendId,
    Capabilities,
    Health,
    InferenceRequest,
    InferenceResult,
    Modality,
    Op,
    OpNotSupported,
)
from . import totalseg
from .isosurface import SurfaceError, extract, extract_labelled
from .meshgen import build_dental_arch, to_ply
from .ply import write_ply

#: Cell size for clustering a surface, in millimetres. Absolute rather than a
#: multiple of the voxel size, because the two paths run on very different
#: grids: 2.5 mm is ~9x fewer triangles on a 0.7 mm segmentation mask and ~3x
#: on a 1.4/2.5 mm volume, which puts both in the same workable range. 0 keeps
#: every vertex.
DEFAULT_SIMPLIFY_MM = 2.5

#: FDI numbers per quadrant, mesial to distal.
_UPPER_RIGHT = (18, 17, 16, 15, 14, 13, 12, 11)
_UPPER_LEFT = (21, 22, 23, 24, 25, 26, 27, 28)

_ENVELOPE_VERSION = "1.0"


class FixtureBackend:
    def __init__(
        self,
        storage: LocalStorage,
        source_store: SourceStore | None = None,
        recordings_dir: Path | None = None,
    ) -> None:
        self.storage = storage
        self.source_store = source_store
        self.recordings_dir = recordings_dir
        self._handlers: dict[str, Callable[[dict, dict, dict], None]] = {
            Op.RECTIFY: _rectify,
            Op.RECONSTRUCT: _reconstruct,
            Op.SEGMENT: _segment,
            Op.MEASURE: _measure,
            Op.ISOLATE_VOLUME: _isolate_volume,
            Op.ISO_SURFACE: _iso_surface,
            Op.DETECT_CARIES: _detect_caries,
        }

    def capabilities(self) -> Capabilities:
        return Capabilities(
            backend=BackendId.FIXTURE,
            ops=sorted(self._handlers),
            modalities=[Modality.RGBD, Modality.RGB, Modality.RADIOGRAPH],
            max_resolution=None,
            supports_dense_output=True,
        )

    def health(self) -> Health:
        return Health(ok=True, backend=BackendId.FIXTURE)

    def run(self, request: InferenceRequest) -> InferenceResult:
        capture = request.capture or {}
        started = datetime.now(UTC)

        envelope: dict[str, Any] = {
            "schema_version": _ENVELOPE_VERSION,
            "result_id": self.storage.new_id(),
            "source_capture_id": str(capture.get("capture_id") or self.storage.new_id()),
            "ops": [],
            "model_version": "",
            "scale": _resolve_scale(capture),
            "artifacts": [],
            "segmentation": None,
            "measurements": [],
            "quality": {"gate_passed": True, "rejections": []},
            "warnings": [],
            "provenance": {"backend": BackendId.FIXTURE, "started_at": started.isoformat()},
        }

        context: dict[str, Any] = {
            "capture": capture,
            "storage": self.storage,
            "sources": self.source_store,
        }

        for stage in request.stages:
            handler = self._handlers.get(stage.op)
            if handler is None:
                raise OpNotSupported(stage.op)
            recording = self._recording(stage.op)
            if recording is not None:
                _merge_recording(envelope, recording)
            else:
                handler(envelope, stage.params, context)
            envelope["ops"].append(stage.op)

        envelope["model_version"] = "fixture@" + "+".join(envelope["ops"])
        envelope["provenance"]["duration_ms"] = int(
            (datetime.now(UTC) - started).total_seconds() * 1000
        )
        return InferenceResult(envelope=envelope)

    def _recording(self, op: str) -> dict[str, Any] | None:
        if self.recordings_dir is None:
            return None
        path = self.recordings_dir / f"{op}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


# ── stage handlers ──────────────────────────────────────────────────────────


def _rectify(envelope: dict, params: dict, context: dict) -> None:
    capture = context["capture"]
    frames = capture.get("frames") or []
    depth = capture.get("depth") or []

    coverage = float((capture.get("quality") or {}).get("coverage_pct") or 0.0)
    rejections: list[dict[str, Any]] = []

    if coverage and coverage < 60:
        rejections.append(
            {
                "code": "insufficient_coverage",
                "detail": f"{coverage:.0f}% of the arch was captured",
            }
        )

    envelope["rectification"] = {
        "frames_in": len(frames),
        "frames_kept": len(frames),
        "depth_frames": len(depth),
        "depth_aligned": all(bool(d.get("aligned_to_rgb")) for d in depth) if depth else False,
        "extrinsics_source": "assumed" if depth else None,
    }
    envelope["quality"]["gate_passed"] = not rejections
    envelope["quality"]["rejections"] = rejections
    envelope["quality"]["coverage_pct"] = coverage


def _reconstruct(envelope: dict, params: dict, context: dict) -> None:
    storage: LocalStorage = context["storage"]
    capture = context["capture"]

    verts, faces = build_dental_arch()
    ref = storage.put(
        f"artifacts/{envelope['result_id']}/arch.ply",
        to_ply(verts, faces),
    )

    verified = envelope["scale"]["verified"]
    envelope["artifacts"].append(
        {
            "kind": "mesh",
            "format": "ply",
            "ref": ref,
            "units": "mm" if verified else "arbitrary",
        }
    )

    if not verified:
        envelope["warnings"].append(
            "No depth and no fiducial: the reconstruction has no metric anchor, "
            "so it is reported in arbitrary units and no measurements are derived."
        )
        return

    envelope["measurements"].extend(
        [
            {"name": "arch_width_molar", "value": 54.0, "unit": "mm", "uncertainty": 0.4},
            {"name": "arch_depth", "value": 38.0, "unit": "mm", "uncertainty": 0.3},
        ]
    )

    if capture.get("modality") == Modality.RGBD:
        envelope.setdefault("geometry", {})
        envelope["geometry"]["voxel_size_mm"] = 0.4
        envelope["geometry"]["frames_used"] = len(capture.get("depth") or [])


def _segment(envelope: dict, params: dict, context: dict) -> None:
    """Volumetric segmentation for a DICOM source, synthetic FDI otherwise.

    A CT has structures to find and TotalSegmentator finds them. An RGB capture
    has no volume, so the dental path stays synthetic until a mesh segmenter
    exists (Notes/05, Notes/11).
    """
    capture = context.get("capture") or {}
    sources = context.get("sources")
    source_id = capture.get("source_id")
    record = sources.get(str(source_id)) if (sources is not None and source_id) else None

    if record is not None and record.kind == "dicom":
        _segment_volume(envelope, params, context, sources, record)
        return

    _segment_synthetic(envelope, params)


def _segment_volume(envelope: dict, params: dict, context: dict, sources, record) -> None:
    storage: LocalStorage = context["storage"]
    classes = params.get("classes") or None

    seg = totalseg.run(
        sources.directory(record.source_id),
        classes=classes,
        fast=bool(params.get("fast", True)),
        task=str(params.get("task", "total")),
    )
    blob, header = totalseg.serialise(seg)

    header_ref = storage.put(
        f"artifacts/{envelope['result_id']}/labels.json",
        json.dumps(header, indent=2).encode("utf-8"),
    )
    bin_ref = storage.put(f"artifacts/{envelope['result_id']}/labels.bin", blob)

    envelope["artifacts"].append(
        {
            # "mask", not "labels": the per-vertex label array below is the
            # "labels" artifact, and one kind for both made them ambiguous.
            "kind": "mask",
            "format": "bin",
            "ref": bin_ref,
            "bytes": len(blob),
            "header_ref": header_ref,
        }
    )

    entries = []
    for label_id in header["labels"]:
        name = seg.legend.get(int(label_id), f"label {label_id}")
        entry: dict[str, object] = {"id": int(label_id), "name": name}
        volume = seg.volumes_mm3.get(name)
        if volume is not None:
            entry["volume"] = float(volume)
        entries.append(entry)

    envelope["segmentation"] = {
        "kind": "volumetric",
        "classes": entries,
        "mask_ref": bin_ref,
        "mask_header_ref": header_ref,
        "flags": [],
    }

    # The mask keeps its own geometry. If our assembled volume was strided down
    # they will not match, and per-label extraction has to run on the mask.
    envelope["geometry"] = {
        "mask_dims": list(seg.dims),
        "mask_spacing": [float(v) for v in seg.spacing],
    }


def _segment_synthetic(envelope: dict, params: dict) -> None:
    instances = []
    abstentions = 0
    for index, fdi in enumerate((*_UPPER_RIGHT, *_UPPER_LEFT)):
        t = (index - 6.5) / 6.5
        # 17 is left unlabelled: a second molar in contact with its neighbour.
        confident = fdi != 17
        abstentions += 0 if confident else 1
        instances.append(
            {
                "instance_id": index,
                "fdi": fdi if confident else None,
                "fdi_confidence": round(0.88 + 0.08 * (1 - abs(t)), 3) if confident else None,
                "centroid": [round(t * 27.0, 2), 4.0, round(-38.0 * t * t, 2)],
                "crown_area_mm2": round(38.0 - 6.0 * abs(t), 1),
            }
        )

    envelope["segmentation"] = {
        "arch": params.get("arch", "upper"),
        "unassigned_region_pct": round(100.0 * abstentions / 40.0, 1),
        "instances": instances,
        "flags": ["crowding"] if abstentions else [],
    }


def _measure(envelope: dict, params: dict, context: dict) -> None:
    if not envelope["scale"]["verified"]:
        return
    existing = {m["name"] for m in envelope["measurements"]}
    if "intercanine_width" not in existing:
        envelope["measurements"].append(
            {"name": "intercanine_width", "value": 35.2, "unit": "mm", "uncertainty": 0.3}
        )


def _isolate_volume(envelope: dict, params: dict, context: dict) -> None:
    storage: LocalStorage = context["storage"]
    ref = storage.put(
        f"artifacts/{envelope['result_id']}/volume.vtk",
        _placeholder_volume(32),
    )
    envelope["artifacts"].append({"kind": "volume", "format": "vtk", "ref": ref, "units": "mm"})


def _iso_surface(envelope: dict, params: dict, context: dict) -> None:
    """Surface the data.

    Two routes, chosen by what the job already has: if an earlier `segment`
    stage produced a mask in this same envelope, the surface follows the mask's
    label boundaries and every vertex carries a label. Otherwise it is a plain
    density threshold over the volume, with no labels.

    The first route is why stages share one envelope — chaining `segment` and
    `iso_surface` in one job is what turns a mask into clickable geometry.
    """
    segmentation = envelope.get("segmentation") or {}
    if segmentation.get("kind") == "volumetric" and segmentation.get("mask_ref"):
        _iso_surface_labelled(envelope, params, context, segmentation)
        return

    _iso_surface_threshold(envelope, params, context)


def _iso_surface_labelled(
    envelope: dict, params: dict, context: dict, segmentation: dict
) -> None:
    storage: LocalStorage = context["storage"]

    header = json.loads(storage.get(segmentation["mask_header_ref"]).decode("utf-8"))
    legend = {int(k): str(v) for k, v in (header.get("legend") or {}).items()}

    keep = _resolve_labels(params.get("label"), legend)

    surface = extract_labelled(
        storage.get(segmentation["mask_ref"]),
        header,
        keep=keep,
        stride=int(params.get("stride", 1)),
        cell=_cell_mm(params),
    )

    mesh_ref = storage.put(
        f"artifacts/{envelope['result_id']}/surface.ply",
        write_ply(surface.vertices, surface.faces),
    )
    labels_ref = storage.put(
        f"artifacts/{envelope['result_id']}/vertex-labels.bin",
        surface.vertex_labels.tobytes(order="C"),
    )
    labels_header_ref = storage.put(
        f"artifacts/{envelope['result_id']}/vertex-labels.json",
        json.dumps(
            {
                "kind": "vertex",
                "count": int(surface.vertex_labels.size),
                "legend": {str(k): v for k, v in surface.legend.items()},
                "counts": {str(k): v for k, v in surface.counts.items()},
            },
            indent=2,
        ).encode("utf-8"),
    )

    envelope["artifacts"].append(
        {
            "kind": "mesh",
            "format": "ply",
            "ref": mesh_ref,
            "units": "mm",
            "vertices": int(len(surface.vertices)),
            "triangles": int(len(surface.faces)),
        }
    )
    # Sidecar, not a PLY face property: the vtk.js PLY reader ignores face
    # properties and would drop these without an error.
    envelope["artifacts"].append(
        {
            "kind": "labels",
            "format": "bin",
            "ref": labels_ref,
            "vertices": int(surface.vertex_labels.size),
            "header_ref": labels_header_ref,
        }
    )
    envelope["geometry"] = {
        "stride": surface.stride,
        "labels": sorted(surface.legend),
        "bounds": _bounds(surface.vertices),
        **_simplification(surface),
    }


def _iso_surface_threshold(envelope: dict, params: dict, context: dict) -> None:
    """Density threshold over the volume. No labels — there is nothing to label with."""
    storage: LocalStorage = context["storage"]
    sources: SourceStore | None = context.get("sources")
    source_id = (context.get("capture") or {}).get("source_id")

    if sources is None or not source_id:
        raise SurfaceError("iso_surface needs capture.source_id")

    record = sources.get(str(source_id))
    if record is None:
        # Usually means the source was deleted, or the server restarted against
        # a different data directory while the browser still listed it.
        raise SurfaceError(
            f"no source {source_id!r} in the store. It may have been deleted, or the "
            "server may be running against a different data directory. Re-upload the "
            "series and run again."
        )

    payload = sources.volume_payload(record)
    if payload is None:
        raise SurfaceError(record.render_reason or "this source has no volume to extract from")

    threshold = float(params.get("threshold", 300))
    stride = int(params.get("stride", 1))

    surface = extract(
        storage.get(payload.bin_ref), payload.header, threshold, stride, cell=_cell_mm(params)
    )
    ref = storage.put(
        f"artifacts/{envelope['result_id']}/surface.ply",
        write_ply(surface.vertices, surface.faces),
    )

    envelope["artifacts"].append(
        {
            "kind": "mesh",
            "format": "ply",
            "ref": ref,
            "units": "mm",
            "vertices": int(len(surface.vertices)),
            "triangles": int(len(surface.faces)),
        }
    )
    envelope["geometry"] = {
        "threshold": surface.threshold,
        "stride": surface.stride,
        "bounds": _bounds(surface.vertices),
        **_simplification(surface),
    }


def _resolve_labels(requested: Any, legend: dict[int, str]) -> set[int] | None:
    """Which labels to extract. None means every one of them.

    Accepts a name, a label id, a list of either, or `"all"`. An empty list is
    every label too — an empty filter is no filter, and it is what the form
    sends when nothing has been narrowed down.
    """
    if requested in (None, "", "all"):
        return None

    if isinstance(requested, (list, tuple, set)):
        items = [item for item in requested if str(item).strip()]
        if not items:
            return None
    else:
        items = [requested]

    keep: set[int] = set()
    for item in items:
        text = str(item)
        if text.lstrip("-").isdigit():
            found = {int(text)}
        else:
            found = {label_id for label_id, name in legend.items() if name == text}
        if not found:
            raise SurfaceError(
                f"no label {text!r} in this mask. Available: "
                + ", ".join(sorted(legend.values()))[:400]
            )
        keep |= found
    return keep


def _cell_mm(params: dict) -> float:
    """Clustering cell size for this job. 0 disables it."""
    value = float(params.get("simplify", DEFAULT_SIMPLIFY_MM))
    return max(0.0, value)


def _simplification(surface) -> dict[str, Any]:
    """What simplifying cost, so the trade is visible rather than assumed."""
    if surface.cell_mm <= 0:
        return {}
    return {
        "simplify_mm": surface.cell_mm,
        "vertices_before": surface.vertices_before,
        "triangles_before": surface.triangles_before,
    }


def _bounds(vertices) -> list[float]:
    return [
        float(vertices[:, 0].min()),
        float(vertices[:, 0].max()),
        float(vertices[:, 1].min()),
        float(vertices[:, 1].max()),
        float(vertices[:, 2].min()),
        float(vertices[:, 2].max()),
    ]


def _detect_caries(envelope: dict, params: dict, context: dict) -> None:
    storage: LocalStorage = context["storage"]
    overlay = {
        "findings": [
            {"fdi": 16, "surface": "mesial", "depth": "enamel", "confidence": 0.71},
            {"fdi": 26, "surface": "distal", "depth": "dentine", "confidence": 0.83},
        ]
    }
    ref = storage.put(
        f"artifacts/{envelope['result_id']}/overlay.json",
        json.dumps(overlay, indent=2).encode("utf-8"),
    )
    envelope["artifacts"].append(
        {"kind": "radiograph_overlay", "format": "json", "ref": ref}
    )


# ── helpers ─────────────────────────────────────────────────────────────────


def _resolve_scale(capture: dict) -> dict[str, Any]:
    """Decide what units the result carries.

    Metric scale comes from depth. An RGB-only capture is scale-ambiguous and
    needs a fiducial; without one the geometry has no metric anchor.
    """
    if capture.get("modality") == Modality.RGBD and capture.get("depth"):
        return {"verified": True, "source": "depth_sensor"}
    if (capture.get("quality") or {}).get("scale_reference"):
        return {"verified": True, "source": "fiducial"}
    if capture.get("modality") == Modality.RADIOGRAPH:
        return {"verified": True, "source": "pixel_spacing"}
    return {"verified": False, "source": "unknown"}


def _merge_recording(envelope: dict, recording: dict[str, Any]) -> None:
    for key, value in recording.items():
        if key == "artifacts":
            envelope["artifacts"].extend(value)
        elif key == "measurements":
            envelope["measurements"].extend(value)
        elif key == "warnings":
            envelope["warnings"].extend(value)
        else:
            envelope[key] = value


def _placeholder_volume(n: int) -> bytes:
    """A smooth scalar field on an n^3 grid, as ASCII VTK structured points."""
    centre = (n - 1) / 2
    values = []
    for z in range(n):
        for y in range(n):
            for x in range(n):
                d = math.dist((x, y, z), (centre, centre, centre))
                values.append(f"{max(0.0, 1.0 - d / centre):.4f}")

    header = [
        "# vtk DataFile Version 3.0",
        "structured points",
        "ASCII",
        "DATASET STRUCTURED_POINTS",
        f"DIMENSIONS {n} {n} {n}",
        "ORIGIN 0 0 0",
        "SPACING 1 1 1",
        f"POINT_DATA {n * n * n}",
        "SCALARS density float 1",
        "LOOKUP_TABLE default",
    ]
    body = [" ".join(values[i : i + 12]) for i in range(0, len(values), 12)]
    return ("\n".join(header) + "\n" + "\n".join(body) + "\n").encode("ascii")
