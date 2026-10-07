"""The samples catalogue, and importing from a remote URL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..config import Settings
from ..deps import get_config, get_source_store
from ..fetch import RemoteError, fetch_dataset, origin_of
from ..samples import catalogue
from ..sources import SourceStore, to_summary

router = APIRouter(tags=["samples"])

#: Files examined when deciding what a dataset is. One hit is enough, and
#: scanning a 400-file archive to answer a question the first few settle is work
#: for nothing.
SNIFF_FILES = 24

#: Magic bytes for the formats a photogrammetry set is likely to hold.
IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"II*\x00", "TIFF"),
    (b"MM\x00*", "TIFF"),
    (b"RIFF", "WebP"),
)

#: A Part 10 DICOM file starts with 128 bytes of padding and then `DICM`. The
#: offset is the whole point of the preamble, and reading it from offset zero
#: matches nothing a real file actually contains.
DICOM_MAGIC = b"DICM"
DICOM_PREAMBLE_AT = 128


@dataclass
class Detection:
    """What the contents look like, and how sure that is.

    `confident` is reported rather than hidden because the answer decides how
    every file is read, and a guess presented as a fact is how a usable dataset
    gets refused. When it is false the client is expected to ask.
    """

    kind: str
    reason: str
    confident: bool


@router.get("/samples")
def list_samples() -> dict:
    """What the app can pull in for someone to try."""
    return catalogue()


class RemoteRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    workspace_id: str
    set_id: str
    name: str | None = None
    #: "dicom" or "images". Unset means decide from the contents.
    kind: str | None = None


@router.post("/sources/remote", response_model=dict, status_code=status.HTTP_201_CREATED)
def import_remote(
    request: RemoteRequest,
    store: Annotated[SourceStore, Depends(get_source_store)],
    settings: Annotated[Settings, Depends(get_config)],
) -> dict:
    """Download a URL and add what it contains as a normal source.

    The archive is unpacked server-side, so the client never has to hold it. That
    is also why the checks in `fetch.py` matter: this endpoint makes the server
    fetch and unpack something a user named.

    Refused outright when the server is not configured for it. This is the one
    route that turns a caller's string into network activity performed from
    inside whatever network the server sits in, and on a public hostname there is
    no operator to be trusted -- so the deployment turns it off and says so,
    rather than relying on the checks in `fetch.py` being complete.
    """
    if not settings.enable_remote_fetch:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=(
                "This server will not fetch remote URLs. It requires "
                "BONE_VIEWER_ENABLE_REMOTE_FETCH, which is off here because the API "
                "is reachable from outside a trusted network. Upload the files "
                "instead, or turn the setting on somewhere private."
            ),
        )

    try:
        files = fetch_dataset(request.url)
    except RemoteError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    detection = detect(files)
    asked = request.kind if request.kind in ("dicom", "images") else None
    kind = asked or detection.kind
    fallback = request.name or origin_of(request.url).rsplit("/", 1)[-1] or "Remote dataset"

    try:
        if kind == "dicom":
            record = store.create_dicom(files, request.workspace_id, request.set_id, fallback)
            if record.headerless:
                # Not one file carried a DICOM header, so this really is the
                # other kind of source and reading it as DICOM was wrong. The
                # test is `headerless` and deliberately not `renderable`: a
                # three-view chest X-ray and a mammogram are DICOM with no
                # volume to assemble, and treating that as a misdetection threw
                # away their headers and their modality to make them photographs.
                #
                # The DICOM record was persisted by `create_dicom` before we
                # could look at it, so it is removed here. Leaving it would put a
                # source in the list that no response ever mentioned — one import
                # producing two entries, the wrong-looking one being the one
                # nobody was told about.
                store.delete(record.source_id)
                record = store.create_images(files, request.workspace_id, request.set_id, fallback)
                kind = "images"
        else:
            record = store.create_images(files, request.workspace_id, request.set_id, fallback)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"that dataset could not be read: {exc}",
        ) from exc

    summary = to_summary(record)
    return {
        **summary.model_dump(mode="json"),
        "imported_from": origin_of(request.url),
        "file_count": len(files),
        # What was found and what was done with it are two different things: a
        # caller can override the detection, and DICOM that cannot be assembled
        # falls through to images. Reporting only one of them would hide both.
        "detected": detection.kind,
        "detected_reason": detection.reason,
        "detected_confident": detection.confident,
        "kind_used": kind,
        "kind_overridden": asked is not None and asked != detection.kind,
    }


def detect(files: list[tuple[str, bytes]]) -> Detection:
    """DICOM or photographs, decided by looking rather than by the file name.

    An extension is a claim; a DICOM preamble is a fact. Both possibilities are
    counted over the first few files, and the answer is only reported as
    confident when one is clearly ahead — a mixed archive, or one where nothing
    is recognised at all, comes back unsure so the importer can say which it is.

    Two DICOM checks, because there are two kinds of file. A Part 10 file
    announces itself in its preamble; the older headerless kind carries no
    marker at all and has to be recognised by its tags. Reading the preamble from
    offset zero — which is where this started, and matches nothing a real file
    contains — left `parse_file` carrying the whole load, so headerless files
    were found and properly-formed ones without parseable tags were not.
    """
    from ..dicom import parse_file

    dicom = 0
    image = 0
    image_format = "image"

    for _name, payload in files[:SNIFF_FILES]:
        if (
            payload[DICOM_PREAMBLE_AT : DICOM_PREAMBLE_AT + 4] == DICOM_MAGIC
            or parse_file(payload) is not None
        ):
            dicom += 1
            continue
        for magic, label in IMAGE_MAGIC:
            if payload.startswith(magic):
                image += 1
                image_format = label
                break

    examined = min(len(files), SNIFF_FILES)

    if dicom and not image:
        return Detection("dicom", f"{dicom} of the first {examined} files are DICOM", True)
    if image and not dicom:
        return Detection(
            "images", f"{image} of the first {examined} files are {image_format}", True
        )
    if dicom and image:
        # Both present. The more common one wins, but this is a dataset that
        # would be misread either way, so it is not claimed as certain.
        lead = "dicom" if dicom > image else "images"
        return Detection(
            lead,
            f"the archive holds both DICOM ({dicom}) and images ({image}); "
            f"assuming {lead}",
            False,
        )
    return Detection(
        "images",
        f"none of the first {examined} files is DICOM or a recognised image format",
        False,
    )
