"""What a segmenter can find, without running it.

The class list is what the label picker offers. Being able to ask for it on its
own is the point: the alternative is learning the names by running a full
segmentation and reading them back out of the result, which costs minutes to
answer a question the info companion answers in under a second.

It reports availability separately from the names because the two fail
differently — no segmenter means no segmentation at all, whereas a segmenter
without its info companion still segments and just cannot name what it found.
"""

from __future__ import annotations

import os

from fastapi import APIRouter

from ..pipeline import totalseg

router = APIRouter(prefix="/segmenter", tags=["segmenter"])


@router.get("/classes")
def classes(task: str = "total") -> dict:
    try:
        totalseg.resolve_binary(os.environ.get("BONE_VIEWER_TOTALSEGMENTATOR", ""))
        available, detail = True, None
    except totalseg.SegUnavailable as exc:
        available, detail = False, str(exc)

    names = totalseg.class_names(task) if available else {}
    if available and not names and detail is None:
        detail = (
            "The segmenter is installed but its class list could not be read, so "
            "structures will be offered as numbers."
        )

    return {
        "task": task,
        "available": available,
        "detail": detail,
        "classes": [{"id": key, "name": name} for key, name in sorted(names.items())],
    }
