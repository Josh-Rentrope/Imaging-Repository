"""TotalSegmentator, driven as an external command.

Deliberately NOT a Python dependency. TotalSegmentator pulls PyTorch and several
gigabytes of weights, and the whole point of the shell/core split is that models
live behind the `InferenceBackend` seam rather than inside this repo. So we shell
out to whatever the operator has installed and fail with an actionable message
when nothing is there.

It takes a directory of DICOM slices directly, which is exactly the shape
`SourceStore` already keeps them in — no conversion step.

Reference: https://github.com/wasserth/totalsegmentator (Apache-2.0 for the
`total` and `teeth` tasks).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

INSTALL_HINT = (
    "TotalSegmentator is not installed. Install it with "
    "`uv pip install TotalSegmentator` (or `pip install TotalSegmentator`), or point "
    "BONE_VIEWER_TOTALSEGMENTATOR at the executable. Weights are downloaded on first run."
)

#: Wall-clock ceiling. A CPU run on a large chest CT is minutes, not seconds.
TIMEOUT_SECONDS = 60 * 30


class SegUnavailable(Exception):
    """No usable TotalSegmentator. The message is user-facing and actionable."""


class SegFailed(Exception):
    """TotalSegmentator ran and did not produce a usable result."""


#: NIfTI writes its world frame in RAS; DICOM's patient frame — and so every
#: coordinate we store from `ImagePositionPatient` — is LPS. The two differ by
#: negating x and y, and a mask carried across without that swap is mirrored
#: about the origin: a rib lands on the far side of the scanner. Four-by-four
#: because it multiplies the whole affine, translation included.
RAS_TO_LPS = np.diag([-1.0, -1.0, 1.0, 1.0])


@dataclass
class Segmentation:
    """A multi-label mask, kept in its own geometry."""

    labels: np.ndarray  # (nz, ny, nx) int32, 0 = background
    dims: tuple[int, int, int]  # (nx, ny, nz)
    spacing: tuple[float, float, float]
    origin: tuple[float, float, float]
    legend: dict[int, str]  # label id -> class name
    #: Row-major rotation of the index-to-world affine, unit cosines. Identity
    #: for an axis-aligned grid; a negated axis is a flip the reader applied.
    direction: tuple[float, ...] = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    volumes_mm3: dict[str, float] = field(default_factory=dict)
    task: str = "total"


def resolve_binary(configured: str = "") -> list[str]:
    """Return the argv prefix to invoke, or raise with an install hint.

    Three routes, in order: an explicit setting, an executable on PATH, a
    `python -m` fallback for an install that did not put a script on PATH.
    """
    if configured:
        path = Path(configured)
        if path.is_file():
            # A .py segmenter needs the interpreter in front of it; everything
            # else is invoked directly.
            return [sys.executable, str(path)] if path.suffix == ".py" else [str(path)]
        found = shutil.which(configured)
        if found:
            return [found]
        raise SegUnavailable(f"BONE_VIEWER_TOTALSEGMENTATOR is set to {configured!r} but that is not executable. {INSTALL_HINT}")

    for name in ("TotalSegmentator", "totalsegmentator"):
        found = shutil.which(name)
        if found:
            return [found]

    # An install that is importable but did not expose a script.
    try:
        probe = subprocess.run(
            [sys.executable, "-c", "import totalsegmentator"],
            capture_output=True,
            timeout=60,
        )
        if probe.returncode == 0:
            return [sys.executable, "-m", "totalsegmentator"]
    except (subprocess.SubprocessError, OSError):
        pass

    raise SegUnavailable(INSTALL_HINT)


def info_binary(segmenter: str = "") -> list[str] | None:
    """The `totalseg_info` companion, or None.

    It answers instantly, needs no GPU and downloads no weights, and is the
    documented way to get a task's class list. Preferred over digging the class
    names out of the NIfTI extended header, which would need `xmltodict` as well.

    Resolved from the SAME install as the segmenter. Consulting PATH regardless
    would let a second install answer for the first, which is not a cosmetic
    mismatch: the class list it returns then names the mask's label ids after
    someone else's anatomy, and every label a user clicks would be wrong. A
    segmenter named explicitly is therefore followed by its own companion — as a
    sibling executable, or the same file when it is a script answering our CLI.
    """
    configured = os.environ.get("BONE_VIEWER_TOTALSEG_INFO", "")
    if configured:
        path = Path(configured)
        if path.is_file():
            return [sys.executable, str(path)] if path.suffix == ".py" else [str(path)]
        found = shutil.which(configured)
        return [found] if found else None

    if segmenter:
        path = Path(segmenter)
        if path.is_file():
            if path.suffix == ".py":
                return [sys.executable, str(path)]
            for name in ("totalseg_info", "totalseg_info.exe", "totalsegmentator_info"):
                beside = path.parent / name
                if beside.is_file():
                    return [str(beside)]
        found = shutil.which(segmenter)
        if found:
            beside = Path(found).parent
            for name in ("totalseg_info", "totalseg_info.exe", "totalsegmentator_info"):
                candidate = beside / name
                if candidate.is_file():
                    return [str(candidate)]

    for name in ("totalseg_info", "totalsegmentator_info"):
        found = shutil.which(name)
        if found:
            return [found]
    return None


#: Keyed on the configuration as well as the task. A bare task key would serve
#: one install's names for another's — and in tests, one stub's for the next.
_NAMES_CACHE: dict[tuple[str, str, str], dict[int, str]] = {}


def class_names(task: str = "total") -> dict[int, str]:
    """Label id -> class name for a task. Empty when it cannot be resolved."""
    segmenter = os.environ.get("BONE_VIEWER_TOTALSEGMENTATOR", "")
    info = os.environ.get("BONE_VIEWER_TOTALSEG_INFO", "")

    key = (segmenter, info, task)
    cached = _NAMES_CACHE.get(key)
    if cached is not None:
        return cached

    argv = info_binary(segmenter)
    if argv is None:
        return {}

    try:
        completed = subprocess.run(
            [*argv, "--classes", "-ta", task],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (subprocess.SubprocessError, OSError):
        return {}

    if completed.returncode != 0:
        return {}

    names: dict[int, str] = {}
    for line in completed.stdout.splitlines():
        # Tolerates "1: spleen", "1  spleen" and "1	spleen".
        head, _, rest = line.strip().partition(":")
        if not rest:
            head, _, rest = line.strip().partition(" ")
        head = head.strip().lstrip("-").strip()
        name = rest.strip()
        if head.isdigit() and name:
            names[int(head)] = name

    _NAMES_CACHE[key] = names
    return names


def run(
    dicom_dir: Path,
    classes: list[str] | None = None,
    fast: bool = True,
    task: str = "total",
) -> Segmentation:
    """Segment a directory of DICOM slices and return the multi-label mask."""
    argv = resolve_binary(os.environ.get("BONE_VIEWER_TOTALSEGMENTATOR", ""))

    if not dicom_dir.is_dir():
        raise SegFailed(f"no DICOM directory at {dicom_dir}")

    with tempfile.TemporaryDirectory(prefix="totalseg-") as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

        # With `--ml`, `-o` is the output FILE, not a directory — the tool's own
        # help says so, and it takes `output.parent` as the directory for
        # everything written alongside the mask, statistics included. Handing it
        # a directory makes it write a NIfTI to a file with no extension, one
        # level away from where anything looks for it, so the run succeeds and
        # the result is lost.
        output_file = out_dir / "seg.nii.gz"

        command = [
            *argv,
            "-i",
            str(dicom_dir),
            "-o",
            str(output_file),
            "-ta",
            task,
            # One combined multi-label file instead of one file per class.
            "-ml",
            "--quiet",
        ]
        if fast:
            command.append("-f")
        if classes:
            command += ["--roi_subset", *classes]
        # Last, because it takes an optional value: anywhere earlier and
        # argparse could read the following argument as that value.
        command.append("-s")

        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired as exc:
            raise SegFailed(
                f"TotalSegmentator timed out after {TIMEOUT_SECONDS // 60} minutes"
            ) from exc
        except OSError as exc:
            raise SegUnavailable(f"could not run TotalSegmentator: {exc}. {INSTALL_HINT}") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-600:]
            raise SegFailed(f"TotalSegmentator exited {completed.returncode}: {detail}")

        mask_path = _find_multilabel(out_dir, output_file)
        if mask_path is None:
            produced = sorted(p.name for p in Path(tmp).rglob("*") if p.is_file())[:12]
            raise SegFailed(
                "TotalSegmentator ran but produced no multi-label output file. "
                f"Wrote instead: {produced or 'nothing'}"
            )

        return _read(mask_path, out_dir, task)


def _find_multilabel(out_dir: Path, expected: Path | None = None) -> Path | None:
    """Locate the combined mask.

    `expected` is the path we asked for and the correct answer on a version that
    takes `-o` as a file. The directory searches cover a version that takes it as
    a directory, including one that would nest a directory of masks inside it.
    """
    if expected is not None and expected.is_file():
        return expected
    if not out_dir.is_dir():
        return None
    for pattern in ("*.nii.gz", "*.nii", "*/*.nii.gz"):
        found = sorted(out_dir.glob(pattern))
        if found:
            return found[0]
    return None


def _read(mask_path: Path, out_dir: Path, task: str = "total") -> Segmentation:
    import nibabel as nib

    image = nib.load(str(mask_path))
    data = np.asarray(image.dataobj)

    # NIfTI is (x, y, z); our convention everywhere else is (nz, ny, nx).
    if data.ndim != 3:
        raise SegFailed(f"expected a 3D mask, got shape {data.shape}")
    labels = np.ascontiguousarray(np.transpose(data, (2, 1, 0))).astype(np.int32)

    # The affine arrives in NIfTI's RAS frame; everything we store is in the
    # DICOM patient frame, so the swap happens here, once, at the boundary.
    affine = RAS_TO_LPS @ image.affine
    linear = affine[:3, :3]

    spacing = tuple(float(np.linalg.norm(linear[:, axis])) for axis in range(3))
    origin = tuple(float(v) for v in affine[:3, 3])

    # Unit direction cosines: the affine's columns with the spacing divided out.
    # A negative entry is a flip, and it has to survive into the header or the
    # mask is placed by its translation alone.
    safe = np.array([s if s else 1.0 for s in spacing])
    direction = tuple(float(v) for v in (linear / safe).reshape(-1))

    # The task's own class list: `teeth` and `total` number their labels
    # differently, so asking for the wrong one renames every structure.
    legend = class_names(task) or _legend(mask_path, out_dir)
    volumes = _statistics(out_dir, labels, legend, spacing)

    return Segmentation(
        labels=labels,
        dims=(int(labels.shape[2]), int(labels.shape[1]), int(labels.shape[0])),
        spacing=(spacing[0], spacing[1], spacing[2]),
        origin=(origin[0], origin[1], origin[2]),
        direction=direction,
        legend=legend,
        volumes_mm3=volumes,
        task=task,
    )


def _legend(mask_path: Path, out_dir: Path) -> dict[int, str]:
    """Class names for each label id.

    TotalSegmentator writes them into the NIfTI's extended header, which needs
    `xmltodict`. Rather than add that, try the sidecar files it also writes and
    fall back to bare ids — a missing legend degrades the labels to numbers, it
    does not break the segmentation.
    """
    for name in ("label_map.json", "class_map.json", "labels.json"):
        candidate = out_dir / name
        if candidate.is_file():
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
                return {int(k): str(v) for k, v in raw.items()}
            except (ValueError, TypeError):
                continue

    try:  # their own loader, if xmltodict happens to be present
        from totalsegmentator.nifti_ext_header import load_multilabel_nifti

        _img, mapping = load_multilabel_nifti(str(mask_path))
        return {int(k): str(v) for k, v in mapping.items()}
    except Exception:
        return {}


def _statistics(
    out_dir: Path,
    labels: np.ndarray,
    legend: dict[int, str],
    spacing: tuple[float, float, float],
) -> dict[str, float]:
    """Per-class volume in mm^3. Prefer TotalSegmentator's own numbers over ours."""
    for name in ("statistics.json", "stats.json"):
        candidate = out_dir / name
        if not candidate.is_file():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except ValueError:
            continue
        volumes: dict[str, float] = {}
        for key, entry in raw.items():
            volume = entry.get("volume") if isinstance(entry, dict) else None
            if volume is not None:
                volumes[str(key)] = float(volume)
        if volumes:
            return volumes

    # Fall back to counting voxels — multiplied out, because the caller reports
    # these as mm^3 and a bare voxel count would understate the volume by the
    # voxel size while looking entirely plausible.
    voxel_mm3 = float(spacing[0]) * float(spacing[1]) * float(spacing[2])
    counts: dict[str, float] = {}
    for label_id in np.unique(labels):
        if label_id == 0:
            continue
        name = legend.get(int(label_id), str(int(label_id)))
        counts[name] = float((labels == label_id).sum()) * voxel_mm3
    return counts


def serialise(seg: Segmentation) -> tuple[bytes, dict]:
    """Blob plus header, matching the volume's storage shape."""
    header = {
        "dims": list(seg.dims),
        "spacing": [float(v) for v in seg.spacing],
        "origin": [float(v) for v in seg.origin],
        "direction": [float(v) for v in seg.direction],
        "dtype": "int32",
        "byte_length": int(seg.labels.nbytes),
        "labels": sorted(int(v) for v in np.unique(seg.labels) if v != 0),
        "legend": {str(k): v for k, v in seg.legend.items()},
        "task": seg.task,
    }
    return seg.labels.tobytes(order="C"), header
