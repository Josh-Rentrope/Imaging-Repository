"""Loader for the server backend.

The implementation is a separate distribution that publishes an entry point::

    [project.entry-points."bone_viewer.backends"]
    server = "bone_viewer_core.backend:ServerBackend"

Nothing here imports it directly. When the distribution is absent -- the normal
case for a checkout of this repo -- `load_server_backend()` returns None and the
router uses another backend.
"""

from __future__ import annotations

from importlib.metadata import entry_points

ENTRY_POINT_GROUP = "bone_viewer.backends"
ENTRY_POINT_NAME = "server"


def load_server_backend() -> object | None:
    try:
        discovered = entry_points(group=ENTRY_POINT_GROUP)
    except Exception:
        return None

    for entry_point in discovered:
        if entry_point.name != ENTRY_POINT_NAME:
            continue
        try:
            return entry_point.load()()
        except Exception:
            return None
    return None
