"""Iso-surface extraction from a stored volume.

Marching cubes over the assembled volume at a density threshold — the classic
way to turn a CBCT into a bone surface. The volume is stored as
(nz, ny, nx) float32 flattened C-order, so reshaping it back needs those same
dimensions, and marching cubes returns vertices in the array's own axis order
(z, y, x). Reordering them into (x, y, z) is a transposition, which mirrors
handedness, so the triangle winding has to be reversed with it or every normal
points inward.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from skimage import measure


@dataclass
class Surface:
    vertices: np.ndarray  # (N, 3) float32, world space (x, y, z)
    faces: np.ndarray  # (M, 3) int32
    threshold: float
    stride: int


class SurfaceError(Exception):
    """Extraction could not run; the message is user-facing."""


def extract(blob: bytes, header: dict[str, Any], threshold: float, stride: int = 1) -> Surface:
    dims = [int(v) for v in header["dims"]]  # [nx, ny, nz]
    spacing = [float(v) for v in header["spacing"]]  # (sx, sy, sz)
    origin = [float(v) for v in header.get("origin", (0.0, 0.0, 0.0))]

    nx, ny, nz = dims
    expected = nx * ny * nz * 4
    if len(blob) != expected:
        raise SurfaceError(f"volume payload is {len(blob)} bytes, expected {expected}")

    # frombuffer hands back a read-only view over the bytes, and marching cubes
    # assigns to `.shape` internally, so it needs an owned writable array.
    # `np.ascontiguousarray` will NOT do: it is a no-op for an already-contiguous
    # array, so it hands the same read-only view straight back.
    volume = np.frombuffer(blob, dtype="<f4").reshape(nz, ny, nx)

    stride = max(1, int(stride))
    if stride > 1:
        volume = volume[::stride, ::stride, ::stride]
    volume = np.array(volume, dtype=np.float32, copy=True)

    vmin = float(volume.min())
    vmax = float(volume.max())
    if not (vmin <= threshold <= vmax):
        raise SurfaceError(
            f"threshold {threshold:g} is outside the volume's range ({vmin:g} … {vmax:g})"
        )

    # Spacing follows the array's axis order, which is (z, y, x) here.
    verts, faces, _normals, _values = measure.marching_cubes(
        volume,
        level=threshold,
        spacing=(spacing[2] * stride, spacing[1] * stride, spacing[0] * stride),
    )

    if len(verts) == 0:
        raise SurfaceError(f"no surface found at {threshold:g}")

    # (z, y, x) -> (x, y, z), then translate into the volume's world position so
    # the mesh overlays the scan it came from.
    reordered = np.column_stack((verts[:, 2], verts[:, 1], verts[:, 0]))
    reordered += np.array(origin, dtype=np.float64)
    faces = faces[:, ::-1]

    return Surface(
        vertices=reordered.astype(np.float32),
        faces=faces.astype(np.int32),
        threshold=float(threshold),
        stride=stride,
    )
