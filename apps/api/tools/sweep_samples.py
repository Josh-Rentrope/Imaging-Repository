"""Import every catalogued study and report what the server does with it.

Runs through the real ASGI stack via TestClient; only the download is served from
a disk cache. Uses a temp data dir, never `.data`. Exit code 1 if any study fails.

    uv run python tools/sweep_samples.py [--modality MG DX] [--pipeline] [--json out.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import traceback
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Runs as a script, so the API root has to be importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.fetch as fetch_module  # noqa: E402
from app.sample_catalogue import ENTRIES  # noqa: E402

DEFAULT_CACHE = Path(__file__).resolve().parents[1] / ".cache" / "samples"
WORKSPACE_ID = "sweep-workspace"
SET_ID = "sweep-set"


@dataclass
class Outcome:
    """One study, as the server saw it."""

    sample_id: str
    title: str
    modality: str
    bytes: int
    ok: bool = False
    status: int | None = None
    seconds: float = 0.0

    # What the import decided.
    detected: str | None = None
    detected_reason: str | None = None
    detected_confident: bool | None = None
    kind_used: str | None = None
    source_id: str | None = None
    instance_count: int | None = None
    source_modality: str | None = None
    headerless: bool | None = None
    renderable: bool | None = None
    render_reason: str | None = None
    presentation: str | None = None
    plane_count: int | None = None
    spacing_source: str | None = None
    dropped_slices: int | None = None

    # The volume, if one came out of it.
    dims: list[int] | None = None
    spacing: list[float] | None = None
    slices: int | None = None
    problems: list[str] = field(default_factory=list)

    # Set when --pipeline ran the default job for this source.
    job_op: str | None = None
    job_status: str | None = None
    job_error: str | None = None
    job_seconds: float | None = None


def cached_download(cache: Path, fetch: bool):
    """`app.fetch.download` behind a disk cache. Patched on the module because
    `fetch_dataset` resolves it there, so the router's code path is untouched."""

    def download(url: str, on_progress=None) -> bytes:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        path = cache / f"{key}.bin"
        if path.exists():
            blob = path.read_bytes()
            if on_progress:
                on_progress(len(blob), len(blob))
            return blob

        blob = fetch(url, on_progress=on_progress)
        cache.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        return blob

    return download


def import_one(client, entry: dict[str, Any]) -> Outcome:
    """POST one study to the real import route and read back what it produced."""
    out = Outcome(
        sample_id=entry["id"],
        title=entry.get("title") or entry["id"],
        modality=entry.get("modality") or "?",
        bytes=entry.get("bytes") or 0,
    )

    started = time.monotonic()
    try:
        response = client.post(
            "/sources/remote",
            json={
                "url": entry["url"],
                "workspace_id": WORKSPACE_ID,
                "set_id": SET_ID,
                "name": entry.get("title") or entry["id"],
            },
        )
    except Exception:
        # A crash inside the route is a finding, not a reason to end the sweep.
        out.seconds = time.monotonic() - started
        out.problems.append("raised: " + traceback.format_exc(limit=3).strip().splitlines()[-1])
        return out
    out.seconds = time.monotonic() - started
    out.status = response.status_code

    if response.status_code != 201:
        detail = ""
        try:
            detail = response.json().get("detail") or ""
        except Exception:
            detail = response.text[:400]
        out.problems.append(str(detail) or f"HTTP {response.status_code}")
        return out

    body = response.json()
    out.ok = True
    out.source_id = body.get("source_id")
    out.detected = body.get("detected")
    out.detected_reason = body.get("detected_reason")
    out.detected_confident = body.get("detected_confident")
    out.kind_used = body.get("kind_used")
    out.instance_count = body.get("instance_count")
    out.source_modality = body.get("modality")
    out.headerless = body.get("headerless")
    out.renderable = body.get("renderable")
    out.render_reason = body.get("render_reason")
    out.presentation = body.get("presentation")
    out.plane_count = body.get("plane_count")
    out.spacing_source = body.get("spacing_source")
    out.dropped_slices = body.get("dropped_slices")

    # A detection that disagrees with the decision is a finding, not an error.
    if body.get("detected") != body.get("kind_used"):
        out.problems.append(
            f"detected {body.get('detected')} but imported as {body.get('kind_used')}"
        )

    if out.kind_used == "dicom" and out.presentation == "none":
        out.problems.append(f"nothing to draw: {out.render_reason}")

    # A flat source is fine; one with no scale is worth saying out loud.
    if out.presentation == "plane" and out.spacing_source is None:
        out.problems.append("flat images with no pixel spacing: scale is unknown")

    if out.dropped_slices:
        out.problems.append(
            f"{out.dropped_slices} slices sat at a position another slice already "
            "occupied and were dropped"
        )

    try:
        volume = client.get(f"/sources/{out.source_id}/volume")
    except Exception:
        out.problems.append("volume request raised")
        return out

    if volume.status_code == 200:
        header = volume.json().get("header") or {}
        dims = header.get("dims")
        if isinstance(dims, list) and len(dims) == 3:
            out.dims = dims
            out.slices = dims[2]
        spacing = header.get("spacing")
        if isinstance(spacing, list):
            out.spacing = [round(float(v), 4) for v in spacing]
    elif volume.status_code == 409:
        pass  # expected without a volume; the reason is already recorded above
    else:
        out.problems.append(f"volume HTTP {volume.status_code}")

    return out


def run_default_job(client, out: Outcome) -> None:
    """Run the op this shape calls for: `segment` for a stack, `px2tooth` for flat
    images. Slow -- a real segmenter pass is most of a minute per CT."""
    if not out.source_id:
        return
    op = "segment" if out.presentation == "volume" else "px2tooth"
    started = time.monotonic()
    try:
        response = client.post(
            "/jobs",
            json={
                "stages": [{"op": op, "params": {}}],
                "capture": {"source_id": out.source_id},
                "workflow": "sweep",
            },
        )
    except Exception:
        out.job_op = op
        out.job_status = "raised"
        out.job_error = traceback.format_exc(limit=3).strip().splitlines()[-1]
        return

    if response.status_code not in (200, 201, 202):
        out.job_op = op
        out.job_status = f"HTTP {response.status_code}"
        try:
            detail = response.json().get("detail")
        except Exception:
            detail = response.text[:200]
        # A pydantic rejection is a list of per-field errors; str() is unreadable.
        if isinstance(detail, list):
            detail = "; ".join(
                f"{'.'.join(str(part) for part in item.get('loc', []))}: {item.get('msg')}"
                for item in detail
                if isinstance(item, dict)
            )
        out.job_error = str(detail)
        return

    job = response.json()
    job_id = job.get("job_id") or job.get("id")
    out.job_op = op
    out.job_status = job.get("status")

    deadline = time.monotonic() + 600
    while job_id and time.monotonic() < deadline:
        current = client.get(f"/jobs/{job_id}")
        if current.status_code != 200:
            out.job_status = f"poll HTTP {current.status_code}"
            break
        job = current.json()
        out.job_status = job.get("status")
        if out.job_status in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.5)

    out.job_seconds = time.monotonic() - started
    if out.job_status != "succeeded":
        out.job_error = job.get("error") or out.job_error or "(no error text)"


def shorten(text: str | None, width: int) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "…"


def report(outcomes: list[Outcome], pipeline: bool) -> None:
    print()
    head = (
        f"{'modality':<8} {'MB':>7}  {'status':>6}  {'shown as':<8} "
        f"{'n':>5}  {'shape':<16} {'spacing':<22} {'flags'}"
    )
    print(head)
    print("-" * len(head))
    for o in outcomes:
        dims = "x".join(str(v) for v in o.dims) if o.dims else ""
        shape = dims or (f"{o.plane_count} planes" if o.plane_count else "")
        flags = []
        if o.headerless:
            flags.append("headerless")
        if o.presentation == "none":
            flags.append("NOTHING TO DRAW")
        if o.detected and o.kind_used and o.detected != o.kind_used:
            flags.append("overridden")
        if o.dropped_slices:
            flags.append(f"dropped {o.dropped_slices}")
        if o.presentation == "plane" and o.spacing_source is None:
            flags.append("no scale")
        print(
            f"{o.modality:<8} {o.bytes / 1e6:7.1f}  "
            f"{(o.status if o.status else '-'):>6}  "
            f"{str(o.presentation or '-'):<8} "
            f"{(o.instance_count if o.instance_count is not None else '-'):>5}  "
            f"{shape:<16} {str(o.spacing_source or '-'):<22} {','.join(flags)}"
        )
        for problem in o.problems:
            # ASCII only: box drawing raises UnicodeEncodeError on a cp1252 console.
            print(f"         `- {shorten(problem, 150)}")

    if pipeline:
        print()
        print(f"{'study':<40} {'op':<10} {'status':<10} {'s':>6}  error")
        print("-" * 96)
        for o in outcomes:
            if not o.job_op:
                continue
            print(
                f"{shorten(o.title, 40):<40} {o.job_op:<10} "
                f"{str(o.job_status):<10} {(o.job_seconds or 0):6.1f}  "
                f"{shorten(o.job_error, 60)}"
            )


def summarise(outcomes: list[Outcome]) -> int:
    failed = [o for o in outcomes if not o.ok]
    suspicious = [o for o in outcomes if o.ok and o.problems]
    jobs = [o for o in outcomes if o.job_op]
    jobs_failed = [o for o in jobs if o.job_status != "succeeded"]
    undrawable = [o for o in outcomes if o.ok and o.presentation == "none"]
    volumes = [o for o in outcomes if o.presentation == "volume"]
    planes = [o for o in outcomes if o.presentation == "plane"]

    print()
    print(f"{len(outcomes)} studies, {len(outcomes) - len(failed)} imported")
    print(f"  {len(volumes)} assembled as volumes")
    print(f"  {len(planes)} offered as flat images")
    # Counted separately from `failed`: imports cleanly, draws nothing, no error.
    print(f"  {len(undrawable)} with NOTHING TO DRAW")
    print(f"  {len(failed)} failed to import")
    print(f"  {len(suspicious)} imported with a caveat")
    if jobs:
        print(f"  {len(jobs_failed)} of {len(jobs)} jobs did not succeed")

    if undrawable:
        print()
        print("IMPORTED BUT UNVIEWABLE")
        for o in undrawable:
            print(f"  {o.modality:<4} {o.sample_id:<30} {shorten(o.render_reason, 90)}")

    if failed:
        print()
        print("FAILED")
        for o in failed:
            print(f"  {o.modality:<4} {o.sample_id:<30} {shorten(o.problems[0], 110)}")

    by_modality = Counter(o.modality for o in failed)
    if len(by_modality) == 1 and len(failed) > 1:
        only = next(iter(by_modality))
        print(f"  (every failure is {only} -- suspect the modality, not the study)")

    return 1 if (failed or jobs_failed) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--id", action="append", default=[], help="only this sample id")
    parser.add_argument("--modality", nargs="*", default=[], help="only these modalities")
    parser.add_argument("--pipeline", action="store_true", help="also run the default job")
    parser.add_argument("--json", type=Path, default=None, help="write results here")
    parser.add_argument("--keep-data", action="store_true", help="keep the temp data dir")
    args = parser.parse_args()

    entries = list(ENTRIES)
    if args.id:
        wanted = set(args.id)
        entries = [e for e in entries if e["id"] in wanted]
    if args.modality:
        wanted = {m.upper() for m in args.modality}
        entries = [e for e in entries if (e.get("modality") or "").upper() in wanted]
    if not entries:
        print("no studies selected", file=sys.stderr)
        return 2

    # Settings are cached at first use, so the env must be right before any import.
    data_dir = Path(tempfile.mkdtemp(prefix="bone-viewer-sweep-"))
    os.environ["BONE_VIEWER_DATA_DIR"] = str(data_dir)
    os.environ["BONE_VIEWER_RECORDINGS_DIR"] = str(data_dir / "recordings")
    os.environ["BONE_VIEWER_MULTI_TENANT"] = "0"
    os.environ["BONE_VIEWER_ENABLE_REMOTE_FETCH"] = "1"
    os.environ.setdefault("BONE_VIEWER_LOG_LEVEL", "WARNING")

    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()

    real_download = fetch_module.download
    fetch_module.download = cached_download(args.cache, real_download)

    print(f"{len(entries)} studies, cache {args.cache}, data {data_dir}")
    outcomes: list[Outcome] = []
    try:
        with TestClient(create_app()) as client:
            for index, entry in enumerate(entries, 1):
                print(
                    f"[{index:>2}/{len(entries)}] {entry['id']} …",
                    end="\r",
                    flush=True,
                )
                outcome = import_one(client, entry)
                if args.pipeline and outcome.ok:
                    run_default_job(client, outcome)
                outcomes.append(outcome)
    finally:
        fetch_module.download = real_download
        if not args.keep_data:
            import shutil

            shutil.rmtree(data_dir, ignore_errors=True)

    report(outcomes, args.pipeline)
    code = summarise(outcomes)

    if args.json:
        args.json.write_text(
            json.dumps([asdict(o) for o in outcomes], indent=2), encoding="utf-8"
        )
        print(f"\nwrote {args.json}")

    return code


if __name__ == "__main__":
    raise SystemExit(main())
