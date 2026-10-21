"""Assemble a DICOM series into a single volume.

Stacks the slices in physical order, applies rescale slope/intercept, and emits
a raw little-endian blob with a JSON sidecar describing the geometry. The viewer
loads both and builds a vtkImageData from them.

Raw plus sidecar rather than a written .vti: the appended-data header in VTI is
fiddly to get right and this is two files the client already knows how to fetch.
Convert to .vti if ParaView or Slicer interop is wanted later.

Slices are ordered by their position along the slice normal, not by filename or
InstanceNumber. Scanner exports disagree about both, and a volume stacked in the
wrong order renders as a plausible-looking but anatomically wrong object.
"""

from __future__ import annotations

import io
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pydicom
from pydicom.errors import InvalidDicomError

#: Voxel ceiling. A 512x512x400 CBCT is ~105M voxels / 210 MB as int16, which is
#: too much to hold per request. Above this the volume is strided down.
MAX_VOXELS = 20_000_000

#: Positions closer than this are the same slice acquired twice. See `_dedupe`.
SAME_SLICE_MM = 1e-3

_DTYPE_NAMES = {np.dtype("int16"): "int16", np.dtype("uint16"): "uint16", np.dtype("int8"): "int8", np.dtype("uint8"): "uint8", np.dtype("float32"): "float32"}

#: Spacing tags, best first. Only `PixelSpacing` is the patient's; `ImagerPixelSpacing`
#: is measured at the detector, so it carries the projection's magnification.
SPACING_TAGS = ("PixelSpacing", "ImagerPixelSpacing", "NominalScannedPixelSpacing")


@dataclass
class Volume:
    array: np.ndarray  # (nz, ny, nx), x fastest when flattened
    spacing: tuple[float, float, float]  # mm, (sx, sy, sz)
    origin: tuple[float, float, float]
    #: The three voxel-axis directions concatenated — x, then y, then z — each a
    #: unit direction cosine. They are the *columns* of the index-to-world
    #: rotation, which is how vtk.js reads them and how `world_transform` does.
    #: Calling this "row-major" is what made the renderer and the surface
    #: extractor disagree about the same nine numbers.
    direction: tuple[float, ...]
    window_center: float | None
    window_width: float | None
    stride: int = 1

    @property
    def dtype_name(self) -> str:
        return _DTYPE_NAMES.get(self.array.dtype, "int16")


@dataclass
class VolumeResult:
    volume: Volume | None = None
    reason: str | None = None
    #: A stack, a set of flat images, or nothing drawable. Only "none" is a failure.
    presentation: Literal["volume", "plane", "none"] = "none"
    #: How many images a "plane" series holds. Zero when it is a stack.
    planes: int = 0
    #: Which tag the pixel spacing came from, or None when there is none.
    spacing_source: str | None = None
    #: Slices dropped because their position was already occupied.
    dropped: int = 0
    #: False when the series has positions but they are not one acquisition.
    coherent: bool = True
    #: The measurements behind that verdict.
    coherence: dict[str, Any] | None = None


def _read(data: bytes) -> pydicom.Dataset | None:
    try:
        return pydicom.dcmread(io.BytesIO(data), force=False)
    except (InvalidDicomError, Exception):
        return None


def _normal(dataset: pydicom.Dataset) -> np.ndarray:
    orientation = getattr(dataset, "ImageOrientationPatient", None)
    if orientation and len(orientation) == 6:
        row = np.array([float(v) for v in orientation[:3]])
        col = np.array([float(v) for v in orientation[3:]])
        return np.cross(row, col)
    return np.array([0.0, 0.0, 1.0])


def _sort_key(dataset: pydicom.Dataset, normal: np.ndarray) -> float:
    position = getattr(dataset, "ImagePositionPatient", None)
    if position and len(position) == 3:
        return float(np.dot(np.array([float(v) for v in position]), normal))
    # No geometry: fall back to instance number, then to zero so the sort is stable.
    return float(getattr(dataset, "InstanceNumber", 0) or 0)


def _spacing_2d(dataset: pydicom.Dataset) -> tuple[float, float, str] | None:
    """Pixel spacing in mm as (x, y) and the tag it came from.

    Projections use `ImagerPixelSpacing` and ultrasound uses the region's
    `PhysicalDeltaX`, so asking only for `PixelSpacing` loses both.
    """
    for tag in SPACING_TAGS:
        value = getattr(dataset, tag, None)
        if value is not None and len(value) == 2:
            # DICOM order is (row spacing, column spacing) -> (y, x).
            return float(value[1]), float(value[0]), tag

    regions = getattr(dataset, "SequenceOfUltrasoundRegions", None)
    if regions:
        for region in regions:
            delta_x = getattr(region, "PhysicalDeltaX", None)
            delta_y = getattr(region, "PhysicalDeltaY", None)
            if delta_x and delta_y:
                return abs(float(delta_x)), abs(float(delta_y)), "UltrasoundRegion"

    return None


def _frames_in(dataset: pydicom.Dataset) -> int:
    """How many pixel frames this one file holds, or 1 if that cannot be told."""
    try:
        pixels = dataset.pixel_array
    except Exception:
        return 1
    return int(pixels.shape[0]) if pixels.ndim == 3 else 1


def _positions(datasets: Sequence[pydicom.Dataset], normal: np.ndarray) -> list[float]:
    return [_sort_key(dataset, normal) for dataset in datasets]


def _is_stack(datasets: Sequence[pydicom.Dataset]) -> bool:
    """Is this a stack of slices, or a set of flat images?

    Only explicit positions prove a third axis exists; frames in one file with no
    positions advance in time, not space. Declared slice spacing counts too
    (tomosynthesis), ordered by instance number.
    """
    if not datasets:
        return False

    normal = _normal(datasets[0])
    located = [d for d in datasets if getattr(d, "ImagePositionPatient", None)]
    if located:
        distinct = len({round(p, 3) for p in _positions(located, normal)})
        if distinct >= 2:
            return True
        if len(datasets) == 1:
            # One slice, but we know where it is.
            return False

    if len(datasets) > 1:
        declared = getattr(datasets[0], "SpacingBetweenSlices", None) or getattr(
            datasets[0], "SliceThickness", None
        )
        if declared:
            return True

    return False


def _dedupe(
    datasets: list[pydicom.Dataset], normal: np.ndarray
) -> tuple[list[pydicom.Dataset], int]:
    """Keep one dataset per slice position.

    One slice can be acquired several times (per echo, per b-value); stacking them
    all silently makes the volume deeper than the anatomy. First one wins.
    """
    seen: dict[float, pydicom.Dataset] = {}
    for dataset in datasets:
        key = round(_sort_key(dataset, normal), 3)
        seen.setdefault(key, dataset)
    if len(seen) == len(datasets):
        return datasets, 0
    return [seen[key] for key in sorted(seen)], len(datasets) - len(seen)


def _pixels(dataset: pydicom.Dataset) -> np.ndarray | None:
    try:
        pixels = dataset.pixel_array
    except Exception:
        return None

    slope = float(getattr(dataset, "RescaleSlope", 1) or 1)
    intercept = float(getattr(dataset, "RescaleIntercept", 0) or 0)
    if slope != 1 or intercept != 0:
        pixels = pixels.astype(np.float32) * slope + intercept
    return pixels


def _read_header(data: bytes) -> pydicom.Dataset | None:
    """One file's header, pixels undecoded -- listing reads this per file."""
    try:
        return pydicom.dcmread(io.BytesIO(data), force=False, stop_before_pixels=True)
    except (InvalidDicomError, Exception):
        return None


def plane_frames(data: bytes) -> int:
    """How many images one file holds. 0 when it cannot be read at all."""
    dataset = _read_header(data)
    if dataset is None:
        return 0
    return max(1, int(getattr(dataset, "NumberOfFrames", 1) or 1))


def plane_geometry(data: bytes) -> dict[str, Any] | None:
    """Where one flat image sits in space.

    Per file, because a set of projections shares nothing. `spacing` is [x, y] to
    match the volume header and vtk.js -- not DICOM's [row, column] -- and is None
    when the file declares none, so an unknown scale is never measured against.
    """
    dataset = _read_header(data)
    if dataset is None:
        return None

    spacing = _spacing_2d(dataset)
    orientation = getattr(dataset, "ImageOrientationPatient", None)
    if orientation is not None and len(orientation) == 6:
        row = [float(v) for v in orientation[:3]]
        col = [float(v) for v in orientation[3:]]
        normal = np.cross(np.array(row), np.array(col)).tolist()
        direction = [*row, *col, *(float(v) for v in normal)]
    else:
        # Radiographs often carry no orientation; identity means "as the pixels came".
        direction = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]

    position = getattr(dataset, "ImagePositionPatient", None)
    return {
        "rows": int(getattr(dataset, "Rows", 0) or 0),
        "columns": int(getattr(dataset, "Columns", 0) or 0),
        "spacing": [spacing[0], spacing[1]] if spacing else None,
        "spacing_source": spacing[2] if spacing else None,
        "origin": [float(v) for v in position] if position else [0.0, 0.0, 0.0],
        "direction": direction,
        "window_center": _first_float(dataset, "WindowCenter"),
        "window_width": _first_float(dataset, "WindowWidth"),
    }


def read_plane(data: bytes, frame: int = 0) -> tuple[np.ndarray, dict[str, Any]] | None:
    """One frame's pixels and geometry, for drawing it as a flat image."""
    dataset = _read(data)
    if dataset is None or "PixelData" not in dataset:
        return None
    pixels = _pixels(dataset)
    if pixels is None:
        return None
    if pixels.ndim == 3:
        if not 0 <= frame < pixels.shape[0]:
            return None
        pixels = pixels[frame]
    elif frame != 0:
        return None
    geometry = plane_geometry(data)
    if geometry is None:
        return None
    return pixels.astype(np.float32), geometry


def build(files: Sequence[bytes]) -> VolumeResult:
    """Assemble a series, or say what it is instead. Flat images report "plane"."""
    datasets: list[pydicom.Dataset] = []
    for data in files:
        dataset = _read(data)
        if dataset is not None and "PixelData" in dataset:
            datasets.append(dataset)

    if not datasets:
        return VolumeResult(reason="no readable pixel data in this series")

    # Compute the slice normal before sorting. list.sort() detaches its backing
    # array for the duration of the sort, so indexing `datasets` from inside the
    # key function raises IndexError rather than reading the first element.
    normal = _normal(datasets[0])

    is_stack = _is_stack(datasets)
    if not is_stack:
        return _planar(datasets)

    datasets.sort(key=lambda d: _sort_key(d, normal))
    datasets, dropped = _dedupe(datasets, normal)

    # Positions alone do not make one acquisition; unrelated slices have them too.
    coherence = _coherence(datasets, normal)
    if not coherence["coherent"]:
        flat = _planar(datasets)
        flat.reason = "; ".join(coherence["reasons"])
        flat.coherent = False
        flat.coherence = coherence
        flat.dropped = dropped
        return flat

    first = datasets[0]
    spacing = _spacing_2d(first)
    if spacing is None:
        return VolumeResult(
            reason=(
                "no pixel spacing in this series (looked for "
                + ", ".join(SPACING_TAGS)
                + ")"
            )
        )
    sx, sy, spacing_source = spacing

    frames: list[np.ndarray] = []
    for dataset in datasets:
        pixels = _pixels(dataset)
        if pixels is None:
            transfer = getattr(getattr(dataset, "file_meta", None), "TransferSyntaxUID", "unknown")
            return VolumeResult(
                reason=f"pixel data could not be decoded (transfer syntax {transfer})"
            )
        # Multi-frame files (some CBCT exports) carry the whole stack in one file.
        if pixels.ndim == 3:
            frames.extend(pixels[index] for index in range(pixels.shape[0]))
        else:
            frames.append(pixels)

    shapes = {frame.shape for frame in frames}
    if len(shapes) != 1:
        return VolumeResult(reason=f"inconsistent slice dimensions across the series ({len(shapes)} distinct)")

    array = np.stack(frames).astype(np.float32)

    # Slice spacing from geometry where possible; nominal thickness otherwise.
    if len(datasets) > 1:
        positions = [_sort_key(d, normal) for d in datasets]
        deltas = np.diff(positions)
        positive = deltas[np.abs(deltas) > 1e-4]
        sz = float(np.median(positive)) if positive.size else 1.0
    else:
        sz = float(getattr(first, "SpacingBetweenSlices", 0) or getattr(first, "SliceThickness", 0) or 1.0)
    sz = abs(sz) or 1.0

    origin = tuple(float(v) for v in getattr(first, "ImagePositionPatient", (0.0, 0.0, 0.0)))
    orientation = getattr(first, "ImageOrientationPatient", None)
    if orientation and len(orientation) == 6:
        row = [float(v) for v in orientation[:3]]
        col = [float(v) for v in orientation[3:]]
        normal = np.cross(np.array(row), np.array(col)).tolist()
        direction = tuple([*row, *col, *[float(v) for v in normal]])
    else:
        direction = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)

    stride = 1
    while array.size / (stride**3) > MAX_VOXELS:
        stride += 1
    if stride > 1:
        array = array[::stride, ::stride, ::stride]

    return VolumeResult(
        volume=Volume(
            array=array,
            spacing=(sx * stride, sy * stride, sz * stride),
            origin=origin,
            direction=direction,
            window_center=_first_float(first, "WindowCenter"),
            window_width=_first_float(first, "WindowWidth"),
            stride=stride,
        ),
        presentation="volume",
        spacing_source=spacing_source,
        dropped=dropped,
        coherence=coherence,
    )


#: How far a gap may stray from the median before it counts as irregular.
GAP_TOLERANCE = 0.25

#: Fraction of gaps allowed to stray before it is not one stack.
IRREGULAR_FRACTION_LIMIT = 0.10

#: ...but at least this many, since one missing slice in ten is already 11%.
IRREGULAR_SLACK = 1

#: How far a slice's orientation may differ from the first.
ORIENTATION_TOLERANCE_DEG = 5.0


def _angle_between(a: np.ndarray, b: np.ndarray) -> float:
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator < 1e-9:
        return 0.0
    cosine = float(np.clip(np.dot(a, b) / denominator, -1.0, 1.0))
    return float(np.degrees(np.arccos(cosine)))


def _coherence(datasets: Sequence[pydicom.Dataset], normal: np.ndarray) -> dict[str, Any]:
    """Are these slices one regular stack, or a grab bag?

    Two signals: gaps that do not step evenly, and slices at different angles.
    A stray gap or two is forgiven so one missing slice does not condemn a study.
    """
    positions = [_sort_key(dataset, normal) for dataset in datasets]
    gaps = np.diff(positions)
    positive = gaps[np.abs(gaps) > 1e-4]

    details: dict[str, Any] = {
        "slices": len(datasets),
        "gap_mm": None,
        "irregular_fraction": 0.0,
        "stray_gaps": 0,
        "orientation_spread_deg": 0.0,
    }
    reasons: list[str] = []

    if positive.size:
        median = float(np.median(positive))
        details["gap_mm"] = round(median, 3)
        if median > 0:
            strayed = np.abs(positive - median) / median
            stray = int(np.count_nonzero(strayed > GAP_TOLERANCE))
            allowed = max(
                IRREGULAR_SLACK,
                int(np.ceil(IRREGULAR_FRACTION_LIMIT * positive.size)),
            )
            details["stray_gaps"] = stray
            details["irregular_fraction"] = round(stray / positive.size, 3)
            if stray > allowed:
                reasons.append(
                    f"{stray} of the {positive.size} gaps between slices are not "
                    f"~{median:.1f} mm, so these are not evenly spaced slices of "
                    "one acquisition"
                )

    # Compare the whole frame: a slice rotated about its own normal has the same
    # normal and a different pixel grid.
    frames = [
        (
            np.asarray(_normal(dataset), dtype=float),
            np.asarray(dataset.ImageOrientationPatient[:3], dtype=float),
        )
        for dataset in datasets
        if getattr(dataset, "ImageOrientationPatient", None)
    ]
    if frames:
        first_normal, first_row = frames[0]
        spread = max(
            max(_angle_between(first_normal, normal), _angle_between(first_row, row))
            for normal, row in frames
        )
        details["orientation_spread_deg"] = round(spread, 2)
        if spread > ORIENTATION_TOLERANCE_DEG:
            reasons.append(
                f"the slices are not all at the same angle (up to {spread:.0f}deg apart), "
                "so they are different acquisitions"
            )

    return {**details, "coherent": not reasons, "reasons": reasons}


def _planar(datasets: Sequence[pydicom.Dataset]) -> VolumeResult:
    """A set of flat images, not a stack. Per-image geometry comes from the route."""
    planes = sum(_frames_in(dataset) for dataset in datasets)
    multiframe = any(_frames_in(d) > 1 for d in datasets)
    spacing = _spacing_2d(datasets[0])
    if multiframe:
        reason = (
            f"{planes} frames in {len(datasets)} file(s) with no slice position: "
            "these advance in time, not through the patient"
        )
    else:
        reason = f"{planes} separate 2-D images, not a stack"
    return VolumeResult(
        reason=reason,
        presentation="plane",
        planes=planes,
        spacing_source=spacing[2] if spacing else None,
    )


def _first_float(dataset: pydicom.Dataset, name: str) -> float | None:
    value = getattr(dataset, name, None)
    if value is None:
        return None
    if isinstance(value, (list, tuple)) or hasattr(value, "__iter__"):
        try:
            value = list(value)[0]
        except (IndexError, TypeError):
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def serialise(volume: Volume) -> tuple[bytes, dict[str, Any]]:
    """Return (binary blob, header).

    Flattened C-order from (nz, ny, nx) puts x fastest, which is what
    vtkImageData expects.
    """
    array = volume.array
    stored: np.ndarray = array.astype(np.float32)

    header = {
        "dims": [int(array.shape[2]), int(array.shape[1]), int(array.shape[0])],
        "spacing": [float(v) for v in volume.spacing],
        "origin": [float(v) for v in volume.origin],
        "direction": [float(v) for v in volume.direction],
        "dtype": "float32",
        "byte_length": int(stored.nbytes),
        "stride": volume.stride,
        "value_range": [float(np.min(stored)), float(np.max(stored))],
        "window_center": volume.window_center,
        "window_width": volume.window_width,
    }
    return stored.tobytes(order="C"), header


def serialise_json(header: dict[str, Any]) -> bytes:
    return json.dumps(header, indent=2).encode("utf-8")
