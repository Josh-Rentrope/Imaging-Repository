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


@dataclass
class LabelledSurface:
    """One surface carrying a label per vertex.

    Per-vertex rather than per-triangle because a triangle straddles the boundary
    between two structures anyway, and one value per vertex is a third of the data.
    Every vertex carries a real label: the meshes are extracted per label, so the
    label is known rather than inferred.
    """

    vertices: np.ndarray  # (N, 3) float32, world space (x, y, z)
    faces: np.ndarray  # (M, 3) int32
    vertex_labels: np.ndarray  # (N,) int32
    legend: dict[int, str]
    counts: dict[int, int]  # label -> vertex count
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

    # Marching cubes needs a voxel to have neighbours on every side. A single
    # slice assembles into a 1-voxel-thick "volume" that has no interior, and
    # skimage's own error for it says nothing about the slice count.
    if min(nx, ny, nz) < 2:
        raise SurfaceError(
            f"volume is {nx}x{ny}x{nz} voxels and needs at least 2 along every axis; "
            f"this source has {nz} slice(s), so there is nothing to extract a surface from"
        )

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


def extract_labelled(
    blob: bytes,
    header: dict[str, Any],
    keep: set[int] | None = None,
    stride: int = 1,
) -> LabelledSurface:
    """Surface every structure in a multi-label mask, tagging each vertex.

    Runs on the MASK's own geometry rather than the volume's: the mask carries
    its own dims, spacing and origin, and the assembled volume may have been
    strided down, so the two grids need not agree.

    One marching-cubes pass PER LABEL, not one over the union. The union looks
    cheaper but silently loses structures: two labels in contact share no
    boundary with the background, so the union's surface has no geometry there
    and neither label can be seen or clicked. It also leaves the label to be
    recovered by sampling back into the mask, which is guesswork at exactly the
    boundary the surface represents. Extracting per label makes the label exact
    and gives touching structures a visible seam between them.

    Each pass is confined to that label's bounding box, so a mask with a hundred
    labels costs a hundred small extractions rather than a hundred over the
    whole volume.
    """
    dims = [int(v) for v in header["dims"]]  # [nx, ny, nz]
    spacing = [float(v) for v in header["spacing"]]
    origin = [float(v) for v in header.get("origin", (0.0, 0.0, 0.0))]
    nx, ny, nz = dims

    expected = nx * ny * nz * 4
    if len(blob) != expected:
        raise SurfaceError(f"label mask is {len(blob)} bytes, expected {expected}")

    labels = np.array(
        np.frombuffer(blob, dtype="<i4").reshape(nz, ny, nx), dtype=np.int32, copy=True
    )
    if min(nx, ny, nz) < 2:
        raise SurfaceError(f"mask is {nx}x{ny}x{nz} voxels; needs at least 2 along every axis")

    stride = max(1, int(stride))
    if stride > 1:
        labels = labels[::stride, ::stride, ::stride]
        labels = np.ascontiguousarray(labels)

    present = {int(v) for v in np.unique(labels) if int(v) != 0}
    if keep is not None:
        present &= keep
    if not present:
        raise SurfaceError("the mask has no voxels for the requested labels")

    # Spacing follows the array's axis order, which is (z, y, x) here.
    step_zyx = np.array(
        [spacing[2] * stride, spacing[1] * stride, spacing[0] * stride], dtype=np.float64
    )
    origin_xyz = np.array(origin, dtype=np.float64)

    pieces: list[tuple[np.ndarray, np.ndarray, int]] = []
    counts: dict[int, int] = {}
    for label_id in sorted(present):
        mask = labels == label_id
        occupied = np.argwhere(mask)
        lo = np.maximum(occupied.min(axis=0) - 1, 0)
        hi = np.minimum(occupied.max(axis=0) + 2, labels.shape)
        window = mask[lo[0] : hi[0], lo[1] : hi[1], lo[2] : hi[2]]

        # A structure clipped by the scan edge has no boundary there, and the
        # surface would come out open with a hole through it. One voxel of true
        # zero padding closes it flush with the edge, which is where the tissue
        # actually stops as far as the data knows.
        window = np.pad(window, 1).astype(np.float32)
        verts, faces, _normals, _values = measure.marching_cubes(
            window, level=0.5, spacing=tuple(step_zyx)
        )
        if len(verts) == 0:
            continue

        # Undo the window and the padding, then swap into (x, y, z). The winding
        # reverses with the transposition or every normal points inward.
        local = verts + (lo - 1) * step_zyx
        local = np.column_stack((local[:, 2], local[:, 1], local[:, 0])) + origin_xyz
        pieces.append((local, faces[:, ::-1], label_id))
        counts[label_id] = int(len(local))

    if not pieces:
        raise SurfaceError("the mask has labels but no extractable boundary")

    vertices = np.concatenate([p[0] for p in pieces]).astype(np.float32)
    vertex_labels = np.concatenate(
        [np.full(len(p[0]), p[2], dtype=np.int32) for p in pieces]
    )

    # Face indices are per-piece, so each has to be shifted into the merged array.
    offset = 0
    face_blocks = []
    for local, faces, _label_id in pieces:
        face_blocks.append(faces + offset)
        offset += len(local)
    merged_faces = np.concatenate(face_blocks).astype(np.int32)

    legend = {int(k): str(v) for k, v in (header.get("legend") or {}).items()}
    return LabelledSurface(
        vertices=vertices,
        faces=merged_faces,
        vertex_labels=vertex_labels,
        legend={k: v for k, v in legend.items() if k in present},
        counts=counts,
        stride=stride,
    )
