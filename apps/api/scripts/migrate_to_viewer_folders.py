"""Move an existing flat store into the `local` viewer's folder. Run once.

Before viewers, everything lived directly under the data directory:

    .data/sources/<id>/...
    .data/artifacts/<id>/...
    .data/jobs/<id>.json

With viewers on, a store addresses `<data>/viewers/<folder>/<same keys>`. So the
move is two changes and not one, and the second is the one that is easy to miss:

1. **The bytes have to move.** A rename within one filesystem, so 3.7 GB is
   instant rather than a copy.

2. **The refs recorded inside the records have to be rewritten.** A source's
   `index.json` and a job's record both store refs like
   `local://sources/<id>/volume.bin`. Those refs are checked against the folder
   they claim, so after the move a ref that still says `local://sources/...`
   resolves to a folder that no longer holds it -- every existing image, volume
   and mesh would 404 while still being listed. Rewriting is by prefix, over any
   string that looks like a ref, so no field has to be enumerated and a field
   added later cannot be forgotten.

Dry run by default. Nothing is written without `--apply`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

API_DIR = Path(__file__).resolve().parent.parent
SCHEME = "local://"
VIEWERS_ROOT = "viewers"
LOCAL_FOLDER = "local"

#: The three trees the store used to keep at its root, and still does, one level
#: down. `jobs` is a directory of files rather than a directory of directories,
#: which the move does not care about.
TREES = ("sources", "artifacts", "jobs")

PREFIX = f"{SCHEME}{VIEWERS_ROOT}/{LOCAL_FOLDER}/"


def count_files(directory: Path) -> int:
    return sum(1 for path in directory.rglob("*") if path.is_file()) if directory.is_dir() else 0


def move_trees(data_dir: Path, target: Path, apply: bool) -> None:
    for name in TREES:
        source = data_dir / name
        destination = target / name
        if not source.exists():
            print(f"  {name}: nothing to move")
            continue
        if destination.exists():
            raise SystemExit(
                f"refusing to merge: {destination} already exists.\n"
                f"Both {source} and {destination} hold data. Decide by hand which "
                f"is real before running this again -- picking one for you could "
                f"discard the other."
            )
        files = count_files(source)
        print(f"  {name}: {files} files -> {destination}")
        if apply:
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))


def rewrite_refs(node: Any) -> tuple[Any, int]:
    """Prefix every ref in a parsed record. Returns the record and how many went.

    Deliberately shape-agnostic: it walks whatever is there. Enumerating the
    fields that hold refs would be a second place to keep in step with the
    models, and the failure mode of forgetting is a 404 on data that looks fine.
    """
    if isinstance(node, str):
        if node.startswith(SCHEME) and not node.startswith(PREFIX):
            return PREFIX + node[len(SCHEME):], 1
        return node, 0
    if isinstance(node, list):
        total = 0
        rewritten = []
        for item in node:
            updated, count = rewrite_refs(item)
            rewritten.append(updated)
            total += count
        return rewritten, total
    if isinstance(node, dict):
        total = 0
        rewritten = {}
        for key, value in node.items():
            updated, count = rewrite_refs(value)
            rewritten[key] = updated
            total += count
        return rewritten, total
    return node, 0


def record_paths(data_dir: Path, target: Path) -> list[Path]:
    """Where the records are now, whether or not the move has happened yet.

    A dry run has not moved anything, so reading only from the target would
    report nothing to do -- which is precisely the wrong thing to report about
    the step that is easy to forget.
    """
    found: list[Path] = []
    for name, pattern in (("sources", "*/index.json"), ("jobs", "*.json")):
        for base in (target / name, data_dir / name):
            if base.is_dir():
                found += sorted(base.glob(pattern))
                break
    return found


def rewrite_records(data_dir: Path, target: Path, apply: bool) -> tuple[int, int]:
    """Rewrite refs in every source index and job record. Idempotent."""
    changed = 0
    refs = 0
    for path in record_paths(data_dir, target):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            # A record that will not parse is reported and left alone. It was
            # already unreadable; rewriting it would only risk the bytes.
            print(f"  ! skipped {path.name}: {exc}")
            continue
        updated, count = rewrite_refs(record)
        if not count:
            continue
        changed += 1
        refs += count
        if apply:
            # Written beside and moved, so a crash cannot truncate a record that
            # was readable a moment ago.
            staging = path.with_suffix(path.suffix + ".migrating")
            staging.write_text(json.dumps(updated, indent=2), encoding="utf-8")
            staging.replace(path)

    print(f"  {changed} records, {refs} refs")
    return changed, refs


def verify(data_dir: Path, target: Path) -> int:
    """Total files across the trees, wherever they now live."""
    total = 0
    for name in TREES:
        total += count_files(data_dir / name)
        total += count_files(target / name)
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=API_DIR / ".data",
        help="the store to migrate (default: apps/api/.data)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write. Without this, nothing is touched.",
    )
    args = parser.parse_args()

    data_dir: Path = args.data_dir
    target = data_dir / VIEWERS_ROOT / LOCAL_FOLDER

    if not data_dir.is_dir():
        print(f"no store at {data_dir}")
        return 1

    before = verify(data_dir, target)
    print(f"store:  {data_dir}")
    print(f"target: {target}")
    print(f"files before: {before}")
    print()
    print("move:")
    move_trees(data_dir, target, args.apply)
    print()
    print("rewrite refs:")
    rewrite_records(data_dir, target, args.apply)
    print()

    after = verify(data_dir, target)
    print(f"files after:  {after}")
    if after != before:
        print(f"MISMATCH: {before} before, {after} after")
        return 1

    if not args.apply:
        print()
        print("dry run -- nothing was written. Re-run with --apply.")
    else:
        print()
        print("done. The `local` viewer now reads this store.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
