"""Camera-pose solvers this deployment knows about, and which it can run.

Reported rather than offered blindly: a client that lists COLMAP on a machine
without COLMAP is offering a button that fails, and the failure arrives after
someone has uploaded a few hundred photographs.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..pipeline import solvers

router = APIRouter(tags=["solvers"])


@router.get("/solvers")
def list_solvers() -> dict:
    """What can estimate camera poses here, and what each one accepts."""
    rows = solvers.survey()
    return {
        "solvers": rows,
        "available": sorted(row["name"] for row in rows if row["available"]),
        # Solvers that were published to this deployment and would not load.
        # Separate from the survey because it answers a different question: that
        # one says what can run, this says what was *meant* to and did not --
        # which is the difference between a deployment that never had the private
        # core and one where it is installed and broken.
        "load_failures": solvers.load_failures(),
        "discovery_error": solvers.discovery_error(),
        # The input shape is the thing a caller actually has to match against:
        # an unordered folder cannot be handed to a sequence solver.
        "shapes": [
            {
                "name": shape,
                "description": description,
            }
            for shape, description in (
                ("unordered_images", "A set of photographs in no particular order."),
                ("image_sequence", "Frames in capture order, consecutive frames overlapping."),
                ("device_poses", "The capture already carries a camera position per frame."),
            )
        ],
    }
