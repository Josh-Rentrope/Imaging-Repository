"""Packaging rules that keep the private core out of the public repo.

Asserts on `.gitignore` behaviour, not on `.gitignore` text. Uses
`git ls-files --cached --others --exclude-standard` -- "does git track this, or
would it stage it?" -- rather than `git check-ignore -q`, which exits zero for a
path matched by a negation (`!apps/api/vendor/.gitkeep`) and so reads as
"ignored".
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
VENDOR = "apps/api/vendor/"


def _visible(pattern: str) -> list[str]:
    """Files under `pattern` git tracks or would stage.

    Both, not just the untracked: a committed file is out of `--others` and is
    exactly what these tests are looking for.
    """
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", pattern],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


@pytest.fixture(autouse=True)
def require_git():
    if shutil.which("git") is None or not (REPO / ".git").exists():
        pytest.skip("not a git checkout")


def test_a_core_wheel_cannot_be_committed():
    """A wheel here is a build input, not source. `git log` keeps it after deletion."""
    wheels = [name for name in _visible(VENDOR) if name.endswith(".whl")]

    assert not wheels, (
        f"a wheel in {VENDOR} is not ignored ({wheels}). Committing one publishes "
        "the private core in this repository permanently."
    )


def test_the_vendor_directory_survives_a_clone():
    """`COPY vendor` fails on a missing path, so the directory must survive a clone."""
    assert f"{VENDOR}.gitkeep" in _visible(VENDOR), (
        "vendor/.gitkeep is not tracked, so the directory would not exist in a "
        "fresh clone and the Dockerfile's COPY would fail"
    )
