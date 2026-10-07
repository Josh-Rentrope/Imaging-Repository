"""Known-good datasets the app can pull in for someone to try.

**The licensing problem this file used to describe is mostly solved, and the
reasoning is worth keeping because it is why the answer is this source.**

Almost nothing in the photogrammetry world is usable here. The openMVG image
sets that every structure-from-motion tutorial uses are **CC BY-NC 4.0**, and
"Chateau de Sceaux" is separately all-rights-reserved. A product that ships a
curated list is not merely linking — it is recommending, and the recommendation
is what carries the exposure. Non-commercial research use is the norm in that
field, not the exception.

What changed: NCI Imaging Data Commons publishes de-identified clinical studies
under **CC BY 3.0/4.0**, and Saga IT redistributes a curated 28-study subset of
exactly that, with the non-commercial collections excluded by policy. CC BY
permits commercial use, so these are usable — **with attribution, which is a
condition of the licence and not a courtesy.** That is why every entry carries
its source collection, the collection's URL and its DOI, and why the modal shows
them beside the download rather than burying them in a manifest. A licence line
reading "CC BY 4.0" on its own does not satisfy the licence it names.

The data lives in `sample_catalogue.py`, generated from the page's own JSON-LD
so a licence cannot end up attached to the wrong study. This module is the
policy: what a `Sample` is, and what counts as available.

`BONE_VIEWER_SAMPLE_BASE` still exists and is still the answer for a deployment
that wants its own datasets — a demo that depends on someone else's hosting
breaks when they move it. Entries there may give a path relative to the bucket
instead of a full URL.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .sample_catalogue import ENTRIES

#: Where a deployment's own demo bucket lives. Unset means only the shipped
#: catalogue is offered.
SAMPLE_BASE = os.environ.get("BONE_VIEWER_SAMPLE_BASE", "").rstrip("/")


def _size_mb(size: int | None) -> float | None:
    """Megabytes to one decimal. Rounded to whole numbers, four of these read
    as `0 MB`, which looks like a broken entry rather than a small study."""
    return None if size is None else round(size / 1_000_000, 1)


@dataclass
class Sample:
    id: str
    title: str
    description: str
    #: Path within the bucket, or a full URL.
    path: str
    licence: str
    #: Exact download size, from the catalogue rather than estimated.
    size: int | None = None
    #: What the set is, so the modal can group and filter it.
    kind: str = "dicom"
    modality: str | None = None
    anatomy: str | None = None
    #: CC BY requires attribution: who it came from, where, and how to cite it.
    collection: str | None = None
    source: str | None = None
    doi: str | None = None
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
            "size_mb": _size_mb(self.size),
            "bytes": self.size,
            "kind": self.kind,
            "modality": self.modality,
            "anatomy": self.anatomy,
            "collection": self.collection,
            "source": self.source,
            "doi": self.doi,
            "notes": self.notes,
            "tags": self.tags,
            # Told to the client rather than left to be discovered: a button that
            # fails on click reads as broken, when the real answer is that the
            # deployment has no bucket configured for a relative path.
            "available": bool(self.url),
        }


def _shipped() -> list[Sample]:
    return [
        Sample(
            id=entry["id"],
            title=entry["title"],
            description=entry["description"],
            path=entry["url"],
            licence=entry["licence"],
            size=entry.get("bytes"),
            kind="dicom",
            modality=entry.get("modality") or None,
            anatomy=entry.get("anatomy") or None,
            collection=entry.get("collection") or None,
            source=entry.get("source") or None,
            doi=entry.get("doi") or None,
            tags=[t for t in (entry.get("modality"), entry.get("anatomy")) if t],
        )
        for entry in ENTRIES
    ]


#: A deployment's own datasets, on top of the shipped ones. Add `Sample`s whose
#: `path` is relative to `BONE_VIEWER_SAMPLE_BASE`.
OWN: list[Sample] = []


def samples() -> list[Sample]:
    return _shipped() + OWN


def catalogue() -> dict:
    """Everything the modal shows, with the modalities present in it."""
    rows = [sample.as_dict() for sample in samples()]
    return {
        # True when this deployment points at its own bucket as well.
        "configured": bool(SAMPLE_BASE),
        "base": SAMPLE_BASE or None,
        "samples": rows,
        # Derived rather than declared, so a modality cannot be listed that has
        # no studies behind it.
        "modalities": sorted({row["modality"] for row in rows if row["modality"]}),
    }
