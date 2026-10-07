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
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np


def _arg(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


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
    count = len(slices)

    spacing = (0.8, 0.8, 1.0)
    try:
        pixel_spacing = slices[0].PixelSpacing
        spacing = (float(pixel_spacing[1]), float(pixel_spacing[0]), float(slices[0].SliceThickness))
    except Exception:
        pass

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

    affine = np.diag([spacing[0], spacing[1], spacing[2], 1.0])
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
