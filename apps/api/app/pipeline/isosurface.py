"""Iso-surface extraction from a stored volume.

Marching cubes over the assembled volume at a density threshold — the classic
way to turn a CBCT into a bone surface. The volume is stored as
(nz, ny, nx) float32 flattened C-order, so reshaping it back needs those same
dimensions, and marching cubes returns vertices in the array's own axis order
(z, y, x). Reordering them into (x, y, z) is a transposition, which mirrors
handedness, so the triangle winding has to be reversed with it or every normal
points inward.

The transform from index space to world is the header's own affine, direction
included — not spacing and translation alone. A grid whose reader flipped an
axis needs that flip, and dropping it mirrors the whole surface about the
origin: a mask lands hundreds of millimetres from the scan it was segmented
from, which reads as a mysterious offset rather than as a missing sign.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from skimage import measure


def world_transform(
    header: dict[str, Any],
) -> tuple[np.ndarray, np.ndarray]:
    """The 3x3 basis and translation taking array index (x, y, z) to world.

    `direction` is the header's own rotation, row-major, as unit direction
    cosines — the affine's columns scaled out. It defaults to the identity, so a
    header that predates it still resolves to plain spacing.
    """
    spacing = np.array([float(v) for v in header["spacing"]], dtype=np.float64)
    origin = np.array(
        [float(v) for v in header.get("origin", (0.0, 0.0, 0.0))], dtype=np.float64
    )

    raw = header.get("direction")
    if raw is None:
        return np.diag(spacing), origin

    rotation = np.array([float(v) for v in raw], dtype=np.float64).reshape(3, 3)
    return rotation @ np.diag(spacing), origin


def bounds_pair(low: np.ndarray, high: np.ndarray) -> list[float]:
    """Interleave a min/max pair into the `bounds` convention.

    `[xmin, xmax, ymin, ymax, zmin, zmax]`, matching `geometry.bounds` on the
    result. Two orderings of the same six numbers is a bug that reads as correct
    at every glance, so there is one ordering and it is made in one place.
    """
    return [
        round(float(v), 3)
        for axis in range(3)
        for v in (low[axis], high[axis])
    ]


def world_bounds(header: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """World-space bounding box of a voxel grid, over the eight grid corners.

    A bounding box rather than the origin, because an axis flip puts the origin
    at the *opposite corner of the same physical box*: two headers can disagree
    about the origin while describing identical geometry. Comparing origins
    therefore reports a discrepancy that is not there, and can agree while the
    boxes do not — so the box is what gets compared.
    """
    dims = [int(v) for v in header["dims"]]
    basis, origin = world_transform(header)

    corners = np.array(
        [
            [i, j, k]
            for i in (0, dims[0] - 1)
            for j in (0, dims[1] - 1)
            for k in (0, dims[2] - 1)
        ],
        dtype=np.float64,
    )
    world = corners @ basis.T + origin
    return world.min(axis=0), world.max(axis=0)


def placement(mask: dict[str, Any], volume: dict[str, Any]) -> dict[str, Any]:
    """How a segmentation's world box sits relative to the volume it came from.

    This exists to be checked *every run* rather than noticed by eye. The failure
    it catches is a mask transformed differently from the volume it was computed
    on, which renders somewhere else entirely — and nothing in the mask's own
    numbers can reveal that, because from the mask's point of view its geometry
    is self-consistent. Only a comparison can.

    TotalSegmentator resamples to its own grid, so the two boxes never match
    exactly, but they cover the same field of view: a `overlap_pct` far below 100
    means one of them is not where it claims to be.
    """
    mask_low, mask_high = world_bounds(mask)
    volume_low, volume_high = world_bounds(volume)

    # Overlap per axis, which is also what makes the number diagnosable: three
    # axes overlapping says the boxes agree, one axis not says a single flip.
    low = np.maximum(mask_low, volume_low)
    high = np.minimum(mask_high, volume_high)
    extent = np.maximum(high - low, 0.0)

    mask_size = np.maximum(mask_high - mask_low, 1e-9)
    # The fraction of the mask's box that lands inside the volume's box, by
    # volume. Not a per-axis average, which would call a mask that is entirely
    # outside in one axis "two thirds correct".
    overlap_pct = float(100.0 * np.prod(extent / mask_size))

    centre_offset = (mask_low + mask_high) / 2 - (volume_low + volume_high) / 2

    return {
        "mask_bounds": bounds_pair(mask_low, mask_high),
        "volume_bounds": bounds_pair(volume_low, volume_high),
        "overlap_pct": round(overlap_pct, 2),
        "centre_offset_mm": [round(float(v), 3) for v in centre_offset],
        "agrees": bool(overlap_pct >= 75.0),
    }


def _orient(faces: np.ndarray, basis: np.ndarray) -> np.ndarray:
    """Winding for a surface going from array order (z, y, x) into world.

    Two things can mirror it: the transposition into (x, y, z) always does, and
    the basis does whenever its determinant is negative. Two mirrors cancel, so
    the winding is reversed only when they disagree — otherwise every normal
    points inward and the surface is lit from the wrong side.
    """
    return faces[:, ::-1] if np.linalg.det(basis) > 0 else faces


def cluster(vertices: np.ndarray, faces: np.ndarray, cell: float) -> tuple[np.ndarray, np.ndarray]:
    """Collapse a surface onto a grid, one vertex per occupied cell.

    Marching cubes emits a vertex per voxel crossing, which on a 0.7 mm grid
    over a whole torso is millions of them — more than the screen can show and
    far more than a click can scan. Snapping to a coarser grid removes that
    detail without removing anything visible, and unlike subsampling the input
    mask it cannot make a thin structure disappear: a rib one voxel wide still
    has a cell, it just has fewer vertices in it.

    The cost is sharp features and exact volume — both already approximate on a
    marching-cubes surface. Set `cell` to 0 to leave the surface alone.
    """
    if cell <= 0 or len(vertices) == 0 or len(faces) == 0:
        return vertices, faces

    origin = vertices.min(axis=0)
    keys = np.floor((vertices - origin) / cell).astype(np.int64)

    # Folded into one integer per vertex rather than `np.unique(axis=0)`: the
    # row-wise version compares void views and is several times slower on the
    # sizes this has to handle.
    span = keys.max(axis=0) + 1
    flat = (keys[:, 0] * span[1] + keys[:, 1]) * span[2] + keys[:, 2]
    _unique, inverse = np.unique(flat, return_inverse=True)
    inverse = inverse.reshape(-1)

    count = int(inverse.max()) + 1
    totals = np.zeros((count, 3), dtype=np.float64)
    np.add.at(totals, inverse, vertices.astype(np.float64))
    sizes = np.bincount(inverse, minlength=count).astype(np.float64)
    merged = (totals / sizes[:, None]).astype(np.float32)

    # A triangle whose corners landed in the same cell has no area left, and one
    # with two corners sharing a cell is a sliver; both go.
    remapped = inverse[faces]
    keep = (
        (remapped[:, 0] != remapped[:, 1])
        & (remapped[:, 1] != remapped[:, 2])
        & (remapped[:, 0] != remapped[:, 2])
    )
    return merged, remapped[keep].astype(np.int32)


@dataclass
class Surface:
    vertices: np.ndarray  # (N, 3) float32, world space (x, y, z)
    faces: np.ndarray  # (M, 3) int32
    threshold: float
    stride: int
    #: Counts before clustering, so the cost of simplifying is visible.
    vertices_before: int = 0
    triangles_before: int = 0
    cell_mm: float = 0.0


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
    vertices_before: int = 0
    triangles_before: int = 0
    cell_mm: float = 0.0


class SurfaceError(Exception):
    """Extraction could not run; the message is user-facing."""


def extract(
    blob: bytes,
    header: dict[str, Any],
    threshold: float,
    stride: int = 1,
    cell: float = 0.0,
) -> Surface:
    dims = [int(v) for v in header["dims"]]  # [nx, ny, nz]
    basis, origin = world_transform(header)

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

    # Index space, in voxel units. The affine below is what turns these into
    # millimetres, and it carries the spacing itself — passing the spacing here
    # as well would apply it twice and stretch the surface non-uniformly.
    verts, faces, _normals, _values = measure.marching_cubes(
        volume,
        level=threshold,
        spacing=(stride, stride, stride),
    )

    if len(verts) == 0:
        raise SurfaceError(f"no surface found at {threshold:g}")

    # (z, y, x) -> (x, y, z), then through the header's own affine so the mesh
    # overlays the scan it came from.
    reordered = np.column_stack((verts[:, 2], verts[:, 1], verts[:, 0]))
    world = (reordered @ basis.T + origin).astype(np.float32)
    oriented = _orient(faces, basis).astype(np.int32)

    simplified, thinned = cluster(world, oriented, cell)
    return Surface(
        vertices=simplified,
        faces=thinned,
        threshold=float(threshold),
        stride=stride,
        vertices_before=int(len(world)),
        triangles_before=int(len(oriented)),
        cell_mm=float(cell) if cell > 0 else 0.0,
    )


def extract_labelled(
    blob: bytes,
    header: dict[str, Any],
    keep: set[int] | None = None,
    stride: int = 1,
    cell: float = 0.0,
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
    basis, origin = world_transform(header)
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

    # Marching cubes works in index space, and the affine is applied once at the
    # end, so this is a pure voxel step — not the world spacing.
    step_zyx = np.array([stride, stride, stride], dtype=np.float64)

    pieces: list[tuple[np.ndarray, np.ndarray, int]] = []
    counts: dict[int, int] = {}
    before_vertices = 0
    before_triangles = 0
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

        # Undo the window and the padding, swap into (x, y, z), then into world
        # through the header's own affine.
        local = verts + (lo - 1) * step_zyx
        local = np.column_stack((local[:, 2], local[:, 1], local[:, 0]))
        local = (local @ basis.T + origin).astype(np.float32)
        oriented = _orient(faces, basis).astype(np.int32)

        # Clustered per label, before merging. Every survivor of a piece is that
        # piece's label, so the per-vertex labels stay exact — clustering the
        # merged mesh instead would mix neighbours at the seams.
        simplified, thinned = cluster(local, oriented, cell)
        before_vertices += len(local)
        before_triangles += len(oriented)
        if len(simplified) == 0:
            continue

        pieces.append((simplified, thinned, label_id))
        counts[label_id] = int(len(simplified))

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
        vertices_before=before_vertices,
        triangles_before=before_triangles,
        cell_mm=float(cell) if cell > 0 else 0.0,
    )
