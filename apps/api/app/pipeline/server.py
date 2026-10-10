"""Loader for the server backend.

The implementation is a separate distribution that publishes an entry point::

    [project.entry-points."bone_viewer.backends"]
    server = "bone_viewer_core.backend:ServerBackend"

Nothing here imports it directly. When the distribution is absent -- the normal
case for a checkout of this repo -- `load_server_backend()` returns None and the
router uses another backend.

**Absent and broken are not the same, and the difference decides whether a job
succeeds.** Backend selection falls back to the fixture when no server backend is
registered. That is right for a deployment that never had a private core, and
wrong for one where it is installed and will not import: the job then runs against
the fixture and reports numbers, with nothing saying the answer came from a
stand-in. So the reason is kept rather than discarded, and `BackendRegistry` turns
it into a refusal whenever the caller named the backend.
"""

from __future__ import annotations

from importlib.metadata import entry_points

ENTRY_POINT_GROUP = "bone_viewer.backends"
ENTRY_POINT_NAME = "server"


class BackendLoadError(Exception):
    """A distribution published the server backend and it would not build.

    Carries a short reason rather than a traceback. The message comes from the
    private package, which is the only thing that knows what went wrong.
    """


def load_server_backend(**injected: object) -> object | None:
    """The private backend, or None when nothing published one.

    Raises `BackendLoadError` when one *was* published and could not be built.
    The two outcomes are deliberately not collapsed into a single None.

    `injected` goes to the constructor: the same arguments `FixtureBackend`
    receives, so the two are interchangeable. A backend with no store handle
    cannot read the uploaded sources or write what it produced.
    """
    try:
        discovered = entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:
        raise BackendLoadError(
            f"entry points could not be enumerated: {type(exc).__name__}: {exc}"
        ) from exc

    candidates = [
        entry_point for entry_point in discovered if entry_point.name == ENTRY_POINT_NAME
    ]
    if not candidates:
        return None

    failures: list[str] = []
    for entry_point in candidates:
        try:
            return entry_point.load()(**injected)
        except Exception as exc:
            failures.append(f"{entry_point.value}: {type(exc).__name__}: {exc}")

    raise BackendLoadError("; ".join(failures))
