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
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import numpy as np
import pydicom
from pydicom.errors import InvalidDicomError

#: Voxel ceiling. A 512x512x400 CBCT is ~105M voxels / 210 MB as int16, which is
#: too much to hold per request. Above this the volume is strided down.
MAX_VOXELS = 20_000_000

_DTYPE_NAMES = {np.dtype("int16"): "int16", np.dtype("uint16"): "uint16", np.dtype("int8"): "int8", np.dtype("uint8"): "uint8", np.dtype("float32"): "float32"}


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


def _spacing_2d(dataset: pydicom.Dataset) -> tuple[float, float] | None:
    pixel_spacing = getattr(dataset, "PixelSpacing", None)
    if pixel_spacing and len(pixel_spacing) == 2:
        # DICOM order is (row spacing, column spacing) -> (y, x).
        return float(pixel_spacing[1]), float(pixel_spacing[0])
    return None


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


def build(files: Sequence[bytes]) -> VolumeResult:
    """Stack a series into a volume, or explain why it could not be done."""
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
    datasets.sort(key=lambda d: _sort_key(d, normal))

    first = datasets[0]
    spacing_2d = _spacing_2d(first)
    if spacing_2d is None:
        return VolumeResult(reason="PixelSpacing missing; volume geometry unknown")
    sx, sy = spacing_2d

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
        )
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
