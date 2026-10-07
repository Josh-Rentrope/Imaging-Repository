"""Source records and their storage.

A *source* is one thing the user dropped into a working set: a DICOM series or
a set of images. Both live under `sources/<source_id>/` with an `index.json`
describing them, so listing is a directory scan and deleting is a directory
removal.

Sources are scoped by `workspace_id` and `set_id`, which the client supplies.
Those are opaque client-generated strings today; when a real tenant model lands
they become server-issued. Storing them now means persistence works without
waiting for auth.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .dicom import parse_file
from .dicom.volume import build as build_volume
from .dicom.volume import serialise, serialise_json
from .storage import LocalStorage

MAX_FILES = 4000
MAX_TOTAL_BYTES = 3 * 1024**3

#: Names that identify a zip rather than a DICOM part-10 file.
_ZIP_SUFFIXES = (".zip",)


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
    )


def expand_uploads(files: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """Flatten any zips. Scanner exports arrive zipped as often as not."""
    expanded: list[tuple[str, bytes]] = []
    for name, data in files:
        if not name.lower().endswith(_ZIP_SUFFIXES):
            expanded.append((name, data))
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    # Keep the basename: archives often nest a folder, and the
                    # path adds nothing since sources are already isolated.
                    expanded.append((Path(info.filename).name, archive.read(info)))
        except zipfile.BadZipFile:
            expanded.append((name, data))
    return expanded


class SourceStore:
    def __init__(self, storage: LocalStorage) -> None:
        self.storage = storage

    # -- paths --------------------------------------------------------------

    def _dir(self, source_id: str) -> Path:
        return self.storage.root / "sources" / source_id

    def _index(self, source_id: str) -> Path:
        return self._dir(source_id) / "index.json"

    # -- reads --------------------------------------------------------------

    def get(self, source_id: str) -> SourceRecord | None:
        index = self._index(source_id)
        if not index.is_file():
            return None
        return SourceRecord.model_validate_json(index.read_text(encoding="utf-8"))

    def list(
        self, workspace_id: str | None = None, set_id: str | None = None
    ) -> list[SourceRecord]:
        root = self.storage.root / "sources"
        if not root.is_dir():
            return []

        records: list[SourceRecord] = []
        for index in sorted(root.glob("*/index.json")):
            try:
                record = SourceRecord.model_validate_json(index.read_text(encoding="utf-8"))
            except Exception:
                continue
            if workspace_id is not None and record.workspace_id != workspace_id:
                continue
            if set_id is not None and record.set_id != set_id:
                continue
            records.append(record)

        records.sort(key=lambda r: r.created_at)
        return records

    # -- writes -------------------------------------------------------------

    def save(self, record: SourceRecord) -> SourceRecord:
        self._dir(record.source_id).mkdir(parents=True, exist_ok=True)
        self.storage.put(
            f"sources/{record.source_id}/index.json",
            json.dumps(record.model_dump(mode="json"), indent=2).encode("utf-8"),
        )
        return record

    def delete(self, source_id: str) -> bool:
        directory = self._dir(source_id)
        if not directory.is_dir():
            return False
        # Storage root is trusted here; the id came from a validated record.
        import shutil

        shutil.rmtree(directory, ignore_errors=True)
        return True

    # -- ingestion ----------------------------------------------------------

    def create_dicom(
        self, files: list[tuple[str, bytes]], workspace_id: str, set_id: str, name: str | None
    ) -> SourceRecord:
        source_id = str(uuid.uuid4())
        expanded = expand_uploads(files)

        # Group by SeriesInstanceUID; anything unparseable shares a bucket.
        groups: dict[str, list[tuple[str, bytes, Any]]] = {}
        for file_name, data in expanded:
            header = parse_file(data)
            key = (header.series_uid if header and header.series_uid else None) or "__headerless__"
            groups.setdefault(key, []).append((file_name, data, header))

        # Use the largest group: dragging in a mixed folder should pick the study,
        # not the first stray file that happened to sort first.
        _, entries = max(groups.items(), key=lambda item: len(item[1]))

        record = SourceRecord(
            source_id=source_id,
            kind="dicom",
            name=name or (_describe(entries) or f"Series {source_id[:8]}"),
            original_name=_describe(entries),
            workspace_id=workspace_id,
            set_id=set_id,
            modality=next((h.modality for _, _, h in entries if h and h.modality), None),
            headerless=all(h is None for _, _, h in entries),
        )

        for file_name, data, header in entries:
            key = f"sources/{source_id}/files/{file_name}"
            self.storage.put(key, data)
            record.instances.append(
                InstanceRecord(
                    file_name=file_name,
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
        self._attach_volume(record, [data for _, data, _ in entries])
        return self.save(record)

    def create_images(
        self, files: list[tuple[str, bytes]], workspace_id: str, set_id: str, name: str | None
    ) -> SourceRecord:
        source_id = str(uuid.uuid4())
        expanded = expand_uploads(files)

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

        for file_name, data in expanded:
            key = f"sources/{source_id}/files/{file_name}"
            self.storage.put(key, data)
            record.instances.append(
                InstanceRecord(file_name=file_name, ref=self.storage.ref(key), bytes=len(data))
            )

        record.instance_count = len(record.instances)
        record.bytes_total = sum(i.bytes for i in record.instances)
        record.instances.sort(key=lambda i: i.file_name)
        return self.save(record)

    def _attach_volume(self, record: SourceRecord, payloads: list[bytes]) -> None:
        result = build_volume(payloads)
        if result.volume is None:
            record.renderable = False
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
        record.renderable = True
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


def _describe(entries: list[tuple[str, bytes, Any]]) -> str | None:
    for _, _, header in entries:
        if header and header.series_description:
            return header.series_description
    return None
