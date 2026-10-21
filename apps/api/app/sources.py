"""Source records and their storage.

A *source* is one thing the user dropped into a working set: a DICOM series or
a set of images. Both live under `sources/<source_id>/` with an `index.json`
describing them, so listing is a key scan and deleting is a subtree removal.

Sources are scoped by `workspace_id` and `set_id`, which the client supplies.
Those are opaque client-generated strings. `workspace_id` in particular is *not*
a security boundary -- the client makes it up -- so it groups work but does not
keep anyone apart. What keeps viewers apart is the storage scope in
`app/tenancy.py`, which is applied underneath these keys and is not something the
client can name.

Everything here goes through the `Storage` seam rather than touching a directory,
because the deployed store is a bucket: `directory()` used to hand a real path to
the segmenter, and `materialize()` replaces it.
"""

from __future__ import annotations

import json
import logging
import tempfile
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .dicom import parse_file
from .dicom.volume import build as build_volume
from .dicom.volume import plane_frames, plane_geometry, serialise, serialise_json
from .storage import Storage

MAX_FILES = 4000
MAX_TOTAL_BYTES = 3 * 1024**3

#: Bumped when the analysis changes what it concludes; older records are re-analysed
#: on first read, so studies imported before a fix get it without being re-uploaded.
ANALYSIS_VERSION = 2

logger = logging.getLogger("bone_viewer.sources")


class InstanceRecord(BaseModel):
    file_name: str
    ref: str
    bytes: int
    sop_instance_uid: str = ""
    rows: int | None = None
    columns: int | None = None


class SourceRecord(BaseModel):
    source_id: str
    kind: Literal["dicom", "images"]
    name: str
    #: The name as discovered from the upload, kept so a rename is reversible.
    original_name: str | None = None
    workspace_id: str = ""
    set_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    modality: str | None = None
    instance_count: int = 0
    bytes_total: int = 0
    headerless: bool = False

    renderable: bool = False
    render_reason: str | None = None
    #: A stack, a set of flat images, or nothing drawable; only "none" is a failure.
    presentation: Literal["volume", "plane", "none"] = "none"
    #: Images in a "plane" source.
    plane_count: int = 0
    #: Tag the pixel spacing came from; None means the scale is unknown.
    spacing_source: str | None = None
    #: Slices discarded because their position was already occupied.
    dropped_slices: int = 0
    #: False when the series has positions but they are not one acquisition.
    coherent: bool = True
    #: The measurements behind that verdict.
    coherence: dict[str, Any] | None = None
    #: 0 means written before this field existed; see `ANALYSIS_VERSION`.
    analysis_version: int = 0
    volume_header_ref: str | None = None
    volume_bin_ref: str | None = None
    volume_meta: dict[str, Any] | None = None

    instances: list[InstanceRecord] = Field(default_factory=list)


class SourceSummary(BaseModel):
    source_id: str
    kind: Literal["dicom", "images"]
    name: str
    original_name: str | None
    workspace_id: str
    set_id: str
    created_at: datetime
    modality: str | None
    instance_count: int
    bytes_total: int
    headerless: bool
    renderable: bool
    render_reason: str | None
    presentation: Literal["volume", "plane", "none"]
    plane_count: int
    spacing_source: str | None
    dropped_slices: int
    coherent: bool
    coherence: dict[str, Any] | None


class VolumePayload(BaseModel):
    header_ref: str
    bin_ref: str
    header: dict[str, Any]


def to_summary(record: SourceRecord) -> SourceSummary:
    return SourceSummary(
        source_id=record.source_id,
        kind=record.kind,
        name=record.name,
        original_name=record.original_name,
        workspace_id=record.workspace_id,
        set_id=record.set_id,
        created_at=record.created_at,
        modality=record.modality,
        instance_count=record.instance_count,
        bytes_total=record.bytes_total,
        headerless=record.headerless,
        renderable=record.renderable,
        render_reason=record.render_reason,
        presentation=record.presentation,
        plane_count=record.plane_count,
        spacing_source=record.spacing_source,
        dropped_slices=record.dropped_slices,
        coherent=record.coherent,
        coherence=record.coherence,
    )


class SourceStore:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    # -- keys ---------------------------------------------------------------

    @staticmethod
    def _index_key(source_id: str) -> str:
        return f"sources/{source_id}/index.json"

    @staticmethod
    def _files_prefix(source_id: str) -> str:
        return f"sources/{source_id}/files/"

    def _stored(self, key: str) -> SourceRecord | None:
        ref = self.storage.ref(key)
        if not self.storage.exists(ref):
            return None
        return SourceRecord.model_validate_json(self.storage.get(ref))

    # -- reads --------------------------------------------------------------

    def get(self, source_id: str) -> SourceRecord | None:
        return self._refreshed(self._stored(self._index_key(source_id)))

    def reanalyse(self, record: SourceRecord) -> SourceRecord:
        """Re-run the analysis against the stored files, rebuilding the blob so the
        geometry cannot disagree with a verdict that changed."""
        if record.kind == "dicom":
            payloads = [self.storage.get(instance.ref) for instance in record.instances]
            self._attach_volume(record, payloads)
        # Stamped even when not drawable: "not a volume" is an answer, and
        # re-asking on every read would make a slow study a slow page for ever.
        record.analysis_version = ANALYSIS_VERSION
        return self.save(record)

    def _refreshed(self, record: SourceRecord | None) -> SourceRecord | None:
        """Re-analyse a record written by an older version. Reading therefore writes,
        once per record, so studies nobody has opened since a fix still get it."""
        if record is None or record.analysis_version >= ANALYSIS_VERSION:
            return record
        try:
            return self.reanalyse(record)
        except Exception:
            # Logged, not swallowed: a record that always fails is retried on every
            # load, and the only other symptom is that its verdict never changes.
            logger.warning(
                "could not re-analyse source %s; leaving it as it was",
                record.source_id,
                exc_info=True,
            )
            return record

    def list(
        self, workspace_id: str | None = None, set_id: str | None = None
    ) -> list[SourceRecord]:
        records: list[SourceRecord] = []
        for key in self.storage.list_keys("sources/"):
            if not key.endswith("/index.json"):
                continue
            try:
                record = self._stored(key)
            except ValueError:
                # A record that will not parse is skipped rather than fatal: one
                # bad write must not hide every other source.
                continue
            if record is None:
                continue
            # Before the filters, so a stale record is re-analysed once regardless
            # of which view asks.
            record = self._refreshed(record)
            if record is None:
                continue
            if workspace_id is not None and record.workspace_id != workspace_id:
                continue
            if set_id is not None and record.set_id != set_id:
                continue
            records.append(record)

        records.sort(key=lambda r: r.created_at)
        return records

    @contextmanager
    def materialize(self, source_id: str) -> Iterator[Path]:
        """A directory holding this source's uploaded files.

        The segmenter is an external command that takes a directory on disk, so
        something has to put real files somewhere. On a local store that is
        already true and the folder is handed over as-is. On a bucket there is no
        folder to hand over, so the objects are pulled into a temporary directory
        for the duration of the call and removed with it -- and because that
        happens per run, a source can be far larger than the container's disk
        without the store having to keep a copy of everything.
        """
        prefix = self._files_prefix(source_id)
        direct = self.storage.local_dir(prefix)
        if direct is not None:
            yield direct
            return

        with tempfile.TemporaryDirectory(prefix="bone-viewer-source-") as staging:
            staged = Path(staging)
            for key in self.storage.list_keys(prefix):
                name = key.rsplit("/", 1)[-1]
                staged.joinpath(name).write_bytes(self.storage.get(self.storage.ref(key)))
            yield staged

    # -- writes -------------------------------------------------------------

    def save(self, record: SourceRecord) -> SourceRecord:
        self.storage.put(
            self._index_key(record.source_id),
            json.dumps(record.model_dump(mode="json"), indent=2).encode("utf-8"),
        )
        return record

    def delete(self, source_id: str) -> bool:
        if not self.storage.exists(self.storage.ref(self._index_key(source_id))):
            return False
        self.storage.remove_tree(f"sources/{source_id}")
        return True

    # -- ingestion ----------------------------------------------------------

    def create_dicom(
        self, files: Sequence[Path], workspace_id: str, set_id: str, name: str | None
    ) -> SourceRecord:
        """One DICOM series from staged files.

        `files` are paths, not payloads. The grouping below needs every file's
        header before it can choose, and reading them all to get that would put a
        study's worth of pixels in memory to read a few hundred bytes each.
        """
        source_id = str(uuid.uuid4())

        # Group by SeriesInstanceUID; anything unparseable shares a bucket.
        groups: dict[str, list[Path]] = {}
        header_of: dict[Path, Any] = {}
        for path in files:
            header = parse_file(path.read_bytes())
            header_of[path] = header
            key = (header.series_uid if header and header.series_uid else None) or "__headerless__"
            groups.setdefault(key, []).append(path)

        # Use the largest group: dragging in a mixed folder should pick the study,
        # not the first stray file that happened to sort first.
        _, chosen = max(groups.items(), key=lambda item: len(item[1]))
        entries = [(path, header_of[path]) for path in chosen]

        record = SourceRecord(
            source_id=source_id,
            kind="dicom",
            name=name or (_describe(entries) or f"Series {source_id[:8]}"),
            original_name=_describe(entries),
            workspace_id=workspace_id,
            set_id=set_id,
            modality=next((h.modality for _, h in entries if h and h.modality), None),
            headerless=all(h is None for _, h in entries),
        )

        for path, header in entries:
            data = path.read_bytes()
            key = f"sources/{source_id}/files/{path.name}"
            self.storage.put(key, data)
            record.instances.append(
                InstanceRecord(
                    file_name=path.name,
                    ref=self.storage.ref(key),
                    bytes=len(data),
                    sop_instance_uid=(header.sop_instance_uid if header else None) or "",
                    rows=header.rows if header else None,
                    columns=header.columns if header else None,
                )
            )

        record.instance_count = len(record.instances)
        record.bytes_total = sum(i.bytes for i in record.instances)
        record.instances.sort(key=lambda i: i.file_name)
        self._attach_volume(record, [path.read_bytes() for path, _ in entries])
        record.analysis_version = ANALYSIS_VERSION
        return self.save(record)

    def create_images(
        self, files: Sequence[Path], workspace_id: str, set_id: str, name: str | None
    ) -> SourceRecord:
        """One image set from staged files.

        The staged names carry the order, and ordering is sorted by name
        everywhere it is used. A phone capture numbers its frames in capture
        order, a camera names them in shot order, and a zip keeps the names it
        was built with -- so all three reconstruct in the order they were taken
        without anything having to record an order separately.
        """
        source_id = str(uuid.uuid4())

        fallback = f"Image set {source_id[:8]}"
        record = SourceRecord(
            source_id=source_id,
            kind="images",
            name=name or fallback,
            original_name=fallback,
            workspace_id=workspace_id,
            set_id=set_id,
            # Reconstruction is not implemented, so an image set has nothing to
            # display yet.
            renderable=False,
            render_reason="reconstruction not implemented",
        )

        for path in files:
            # One file at a time. This is the loop a phone capture goes through,
            # and holding the set rather than the file is what made it a memory
            # problem.
            data = path.read_bytes()
            key = f"sources/{source_id}/files/{path.name}"
            self.storage.put(key, data)
            record.instances.append(
                InstanceRecord(file_name=path.name, ref=self.storage.ref(key), bytes=len(data))
            )

        record.instance_count = len(record.instances)
        record.bytes_total = sum(i.bytes for i in record.instances)
        record.instances.sort(key=lambda i: i.file_name)
        # Stamped current so a re-read does not run a DICOM analysis over photos.
        record.analysis_version = ANALYSIS_VERSION
        return self.save(record)

    def _attach_volume(self, record: SourceRecord, payloads: list[bytes]) -> None:
        result = build_volume(payloads)

        # The shape comes from the assembler, not from whether a blob came out:
        # a set of projections has no blob and is still drawable.
        record.presentation = result.presentation
        record.plane_count = result.planes
        record.spacing_source = result.spacing_source
        record.dropped_slices = result.dropped
        record.coherent = result.coherent
        record.coherence = result.coherence
        record.renderable = result.presentation != "none"

        if result.volume is None:
            # A refusal or a description of what the series is; shown as a tooltip.
            record.render_reason = result.reason
            return

        blob, header = serialise(result.volume)
        volume = result.volume

        record.volume_header_ref = self.storage.put(
            f"sources/{record.source_id}/volume.json", serialise_json(header)
        )
        record.volume_bin_ref = self.storage.put(
            f"sources/{record.source_id}/volume.bin", blob
        )
        record.volume_meta = {
            "dims": header["dims"],
            "spacing": header["spacing"],
            "value_range": header["value_range"],
            "window_center": volume.window_center,
            "window_width": volume.window_width,
            "stride": volume.stride,
        }
        record.render_reason = None

    def volume_payload(self, record: SourceRecord) -> VolumePayload | None:
        if not (record.volume_header_ref and record.volume_bin_ref):
            return None
        raw = self.storage.get(record.volume_header_ref)
        return VolumePayload(
            header_ref=record.volume_header_ref,
            bin_ref=record.volume_bin_ref,
            header=json.loads(raw.decode("utf-8")),
        )

    # -- flat images --------------------------------------------------------

    def plane_images(self, record: SourceRecord) -> list[dict[str, Any]]:
        """Every image in a plane source, with where it sits. Indexes span frames,
        and the geometry is per file because projections need share nothing."""
        images: list[dict[str, Any]] = []
        for instance in record.instances:
            data = self.storage.get(instance.ref)
            frames = plane_frames(data)
            geometry = plane_geometry(data) or {}
            for frame in range(frames):
                images.append(
                    {
                        "index": len(images),
                        "name": instance.file_name
                        if frames == 1
                        else f"{instance.file_name} [{frame + 1}/{frames}]",
                        "frame": frame,
                        **geometry,
                    }
                )
        return images

    def plane_at(self, record: SourceRecord, index: int) -> tuple[bytes, int] | None:
        """The file and frame an index names; stops early, so image 3 opens 4 files."""
        if index < 0:
            return None
        remaining = index
        for instance in record.instances:
            data = self.storage.get(instance.ref)
            frames = plane_frames(data)
            if remaining < frames:
                return data, remaining
            remaining -= frames
        return None


def _describe(entries: list[tuple[Path, Any]]) -> str | None:
    for _, header in entries:
        if header and header.series_description:
            return header.series_description
    return None
