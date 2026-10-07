"""A stand-in for TotalSegmentator. TESTS ONLY.

Exists so the ingestion path — command construction, NIfTI axis handling, legend
parsing, mask storage — is genuinely exercised without a multi-gigabyte PyTorch
install. It is not shipped and it is not registered anywhere; a test points
BONE_VIEWER_TOTALSEGMENTATOR at this file.

It answers the same CLI arguments we pass and writes the same output shape: one
combined multi-label NIfTI plus a statistics sidecar.

The two labels it emits are a stand-in for ribs, chosen so the axis handling is
checkable — they are separated along z, so a transposed mask would put them in
the wrong place.

**It builds its NIfTI affine from the DICOM geometry**, as the real tool does:
origin from the first slice's position, axes from ImageOrientationPatient,
spacing from PixelSpacing and the actual slice step. It used to write
`diag(spacing)` with a zero origin, which put the mask at the world origin while
the volume it came from sat at the patient's coordinates — so the two disagreed
about where space was, and nothing noticed, because every assertion about the
mask was about its own shape. A stand-in that gets the geometry wrong can only
ever confirm that the rest of the code agrees with it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

#: DICOM is LPS (left, posterior, superior); NIfTI is RAS (right, anterior,
#: superior). The two differ by a flip in x and y.
LPS_TO_RAS = np.diag([-1.0, -1.0, 1.0, 1.0])


def _arg(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def _geometry(slices: list) -> tuple[tuple[float, float, float], np.ndarray]:
    """Slice spacing and the RAS index-to-world affine, from the DICOM headers.

    Read from the files rather than assumed, because the whole point of a
    stand-in is to exercise the path the real one takes. The slice step comes
    from the positions themselves rather than SliceThickness, which is the
    thickness of the acquisition and not always the distance between the slices
    that were written out — a distinction that puts a volume at the wrong scale
    while every individual number still looks right.
    """
    first = slices[0]

    row_spacing, col_spacing = 1.0, 1.0
    try:
        pixel_spacing = first.PixelSpacing
        row_spacing = float(pixel_spacing[0])
        col_spacing = float(pixel_spacing[1])
    except Exception:
        pass

    orientation = [float(v) for v in getattr(first, "ImageOrientationPatient", [])[:6]]
    row_cosines = np.array(orientation[:3]) if len(orientation) == 6 else np.array([1.0, 0.0, 0.0])
    col_cosines = np.array(orientation[3:]) if len(orientation) == 6 else np.array([0.0, 1.0, 0.0])
    normal = np.cross(row_cosines, col_cosines)
    if not np.any(normal):
        normal = np.array([0.0, 0.0, 1.0])

    def position(dataset) -> np.ndarray:
        values = [float(v) for v in getattr(dataset, "ImagePositionPatient", [])[:3]]
        return np.array(values) if len(values) == 3 else np.zeros(3)

    # Order along the normal, so the third axis follows increasing position
    # rather than the order the directory happened to be read in.
    slices = sorted(slices, key=lambda dataset: float(np.dot(position(dataset), normal)))

    step = float(getattr(first, "SliceThickness", 1.0) or 1.0)
    if len(slices) > 1:
        delta = position(slices[-1]) - position(slices[0])
        if np.linalg.norm(delta) > 0:
            step = float(np.dot(delta, normal) / (len(slices) - 1))

    spacing = (col_spacing, row_spacing, step)

    affine = np.eye(4)
    # Axes in index order: x walks columns, y walks rows, z walks slices.
    affine[:3, 0] = row_cosines * col_spacing
    affine[:3, 1] = col_cosines * row_spacing
    affine[:3, 2] = normal * step
    affine[:3, 3] = position(slices[0])

    return spacing, LPS_TO_RAS @ affine


#: Per task, mirroring how the real tool numbers labels differently per task.
CLASSES = {
    "total": {1: "rib_left_4", 2: "rib_left_5"},
    "teeth": {1: "upper_tooth_11", 2: "upper_tooth_12"},
}


def main(argv: list[str]) -> int:
    task = _arg(argv, "-ta") or "total"
    classes = CLASSES.get(task, CLASSES["total"])

    # `totalseg_info --classes -ta total` answers with the class list.
    if "--classes" in argv:
        for label_id, name in classes.items():
            print(f"{label_id}: {name}")
        return 0

    source = _arg(argv, "-i")
    output = _arg(argv, "-o")
    if not source or not output:
        print("usage: stub_totalseg.py -i <dicom dir> -o <out file>", file=sys.stderr)
        return 2

    import nibabel as nib
    import pydicom

    files = sorted(Path(source).glob("*"))
    slices = []
    for path in files:
        try:
            dataset = pydicom.dcmread(str(path), force=False)
        except Exception:
            continue
        if "PixelData" in dataset:
            slices.append(dataset)
    if not slices:
        print("stub: no readable DICOM with pixel data", file=sys.stderr)
        return 1

    rows = int(slices[0].Rows)
    cols = int(slices[0].Columns)

    spacing, affine = _geometry(slices)
    count = len(slices)

    # (x, y, z), matching NIfTI's axis order.
    mask = np.zeros((cols, rows, count), dtype=np.int16)
    third = max(1, count // 3)
    mask[:, :, :third] = 1
    mask[:, :, third : 2 * third] = 2

    # `-o` is the output FILE when --ml is set, not a directory: the real tool
    # takes its parent as the directory for everything written alongside it.
    # Writing to exactly this path is what makes a regression to the directory
    # convention fail here rather than only against the real binary.
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    nib.save(nib.Nifti1Image(mask, affine), str(output_path))

    # Only with -s, as the real tool does.
    if "-s" in argv or "--statistics" in argv:
        voxel_mm3 = spacing[0] * spacing[1] * spacing[2]
        (output_path.parent / "statistics.json").write_text(
            json.dumps(
                {
                    classes[label_id]: {
                        "volume": float((mask == label_id).sum()) * voxel_mm3,
                        "intensity": 0.0,
                    }
                    for label_id in sorted(classes)
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
