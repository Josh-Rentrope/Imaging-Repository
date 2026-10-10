"""Build a throwaway private-core wheel, to test the WITH_PRIVATE_CORE build path.

Publishes both entry points and does nothing useful, so the install and discovery
can be exercised before the private repo exists. Never ship it: a stub in an image
would make /solvers list COLMAP and /capabilities claim a server backend.

Assembled by hand rather than with a build backend -- entry-point discovery is
three files in a *.dist-info directory, and `entry_points.txt` is the mechanism.

    cd apps/api && uv run python tools/make_stub_core.py
"""

from __future__ import annotations

import base64
import hashlib
import zipfile
from pathlib import Path

DIST = "bone_viewer_core"
VERSION = "0.0.0"
VENDOR = Path(__file__).resolve().parents[1] / "vendor"

#: Written out, not derived from the app: a foreign package's view of the
#: contract, which is the thing under test.
ENTRY_POINTS = """\
[bone_viewer.solvers]
colmap = bone_viewer_core.solvers:ColmapSolver

[bone_viewer.backends]
server = bone_viewer_core.backend:ServerBackend
"""

#: Both import the public contract types rather than duck-typing them -- this
#: also verifies they are importable from outside the source tree, which is why
#: the Dockerfile installs the API project into the venv.
SOLVERS = '''\
"""A stand-in pose solver. Solves nothing; see tools/make_stub_core.py."""

from app.pipeline.solvers import InputShape, SolverInfo


class ColmapSolver:
    """Reports itself unavailable, so nothing is ever routed to it."""

    info = SolverInfo(
        name="colmap",
        tool="COLMAP (stub)",
        description="A stand-in that solves nothing.",
        algorithm="none",
        accepts=frozenset({InputShape.UNORDERED}),
        licence="BSD-3-Clause",
    )

    def available(self):
        return False, "stub core: packaging test only, no solver installed here"

    def solve(self, frames, params):
        raise RuntimeError("stub core: this wheel exists to test packaging")
'''

BACKEND = '''\
"""A stand-in server backend. Advertises nothing; see tools/make_stub_core.py."""

from app.pipeline.interfaces import Capabilities, Health


class ServerBackend:
    """Registers but advertises no ops, so nothing can be routed to it."""

    def capabilities(self):
        return Capabilities(
            backend="server",
            ops=[],
            modalities=[],
            notes="stub core: packaging test only",
        )

    def run(self, request):
        raise RuntimeError("stub core: this wheel exists to test packaging")

    def health(self):
        return Health(ok=False, backend="server", detail="stub core")
'''

METADATA = f"""\
Metadata-Version: 2.1
Name: bone-viewer-core
Version: {VERSION}
Summary: Throwaway stub built by tools/make_stub_core.py. Never shipped.
"""

WHEEL = """\
Wheel-Version: 1.0
Generator: make_stub_core.py
Root-Is-Purelib: true
Tag: py3-none-any
"""


def build(out_dir: Path = VENDOR) -> Path:
    """Write the stub wheel into `out_dir` and return its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = out_dir / f"{DIST}-{VERSION}-py3-none-any.whl"
    dist_info = f"{DIST}-{VERSION}.dist-info"

    files = {
        f"{DIST}/__init__.py": '"""Stub private core. See tools/make_stub_core.py."""\n',
        f"{DIST}/solvers.py": SOLVERS,
        f"{DIST}/backend.py": BACKEND,
        f"{dist_info}/METADATA": METADATA,
        f"{dist_info}/WHEEL": WHEEL,
        f"{dist_info}/entry_points.txt": ENTRY_POINTS,
    }

    # RECORD lists each member with its hash and size, and itself with neither.
    record = [
        f"{name},{_digest(text)},{len(text.encode('utf-8'))}"
        for name, text in files.items()
    ]
    record.append(f"{dist_info}/RECORD,,")
    files[f"{dist_info}/RECORD"] = "\n".join(record) + "\n"

    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text)

    return wheel_path


def _digest(text: str) -> str:
    """`sha256=` + url-safe base64, unpadded, as the wheel spec writes it."""
    raw = hashlib.sha256(text.encode("utf-8")).digest()
    return "sha256=" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def main() -> None:
    path = build()
    print(f"wrote {path}")
    print("  build with: docker build --build-arg WITH_PRIVATE_CORE=1 -t bone-viewer-api apps/api")
    print("  and remove it again afterwards -- a stub must not reach a real deployment")


if __name__ == "__main__":
    main()
