"""Known-good datasets the app can pull in for someone to try.

Deliberately empty of anything public, because almost nothing suitable is
licensed for it. The obvious candidates — the openMVG image sets that every
structure-from-motion tutorial uses — are **CC BY-NC 4.0**, and "Chateau de
Sceaux" is separately all-rights-reserved. A commercial product that ships a
curated list of them is not merely linking: it is recommending, and it is the
recommendation that carries the exposure. The same is true of most academic
photogrammetry benchmarks; non-commercial research use is the norm in that
field, not the exception.

So the catalogue points at a bucket we control, which also settles the other
half of the problem: a demo that depends on someone else's hosting breaks when
they move it, and a samples button that 404s is worse than no button.

To publish one, add an entry with a direct link to a `.zip` or `.tar.gz` and the
licence it is offered under. Keep `licence` honest — it is shown beside the
download, and it is the field a reviewer will read.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

#: Where the demo bucket lives. Set BONE_VIEWER_SAMPLE_BASE to point somewhere
#: else; unset means the catalogue below is returned with relative URLs, which
#: the app will refuse to fetch rather than guess at.
SAMPLE_BASE = os.environ.get("BONE_VIEWER_SAMPLE_BASE", "").rstrip("/")


@dataclass
class Sample:
    id: str
    title: str
    description: str
    #: Path within the bucket, or a full URL.
    path: str
    licence: str
    #: Rough download size, so nobody starts a 2 GiB fetch on a hunch.
    size_mb: int | None = None
    #: What the set is for, so the modal can group them.
    kind: str = "photographs"
    notes: str | None = None
    tags: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        if self.path.startswith(("http://", "https://")):
            return self.path
        return f"{SAMPLE_BASE}/{self.path.lstrip('/')}" if SAMPLE_BASE else ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "url": self.url,
            "licence": self.licence,
            "size_mb": self.size_mb,
            "kind": self.kind,
            "notes": self.notes,
            "tags": self.tags,
            # Told to the client rather than left to be discovered: a button
            # that fails on click reads as broken, when the real answer is that
            # the deployment has no bucket configured yet.
            "available": bool(self.url),
        }


#: Add entries here as the bucket fills up. See the module docstring.
SAMPLES: list[Sample] = []


def catalogue() -> dict:
    return {
        "configured": bool(SAMPLE_BASE),
        "base": SAMPLE_BASE or None,
        "samples": [sample.as_dict() for sample in SAMPLES],
    }
