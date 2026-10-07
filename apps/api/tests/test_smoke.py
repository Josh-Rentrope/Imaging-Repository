"""Smoke tests.

Concentrated on behaviour that fails quietly rather than loudly: scale handling,
the diagnostic gate, and error paths that could otherwise return a plausible
wrong answer.
"""

from __future__ import annotations

import io
import json
import struct
import time
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydicom.dataset import Dataset, FileMetaDataset
from pathlib import Path

from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from app.dicom import parse_file
from app.main import create_app

from helpers import (  # noqa: E402  (tests/ is on sys.path under pytest)
    FULL_PIPELINE,
    RGB_NO_FIDUCIAL,
    RGBD_CAPTURE,
    run,
    settle,
    submit,
)

# ── pipeline ────────────────────────────────────────────────────────────────

def test_a_finished_job_outlives_the_process_that_ran_it(tmp_path, monkeypatch):
    """The record is on disk, so a reload does not cost the run.

    This is the whole point of persisting jobs: the dev server restarts on every
    edit, and the artifacts are on disk either way — without the job that
    describes them, a minute-long segmentation becomes an orphaned file.
    """
    monkeypatch.setenv("BONE_VIEWER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BONE_VIEWER_RECORDINGS_DIR", str(tmp_path / "recordings"))
    from app.config import get_settings

    capture = {**RGBD_CAPTURE, "source_id": "source-under-test"}
    get_settings.cache_clear()
    with TestClient(create_app()) as first:
        job = run(first, [{"op": "reconstruct"}], capture)
        assert job["status"] == "succeeded"
    get_settings.cache_clear()

    # A second app over the same data directory: exactly what a restart does.
    with TestClient(create_app()) as second:
        listed = second.get("/jobs?source_id=source-under-test").json()

    assert [entry["job_id"] for entry in listed] == [job["job_id"]]
    assert listed[0]["result"]["ops"] == ["reconstruct"]


def test_listing_jobs_can_be_scoped_to_one_source(client):
    """Otherwise a reloaded page shows every run the server has ever done."""
    run(client, [{"op": "reconstruct"}], {**RGBD_CAPTURE, "source_id": "source-a"})
    run(client, [{"op": "reconstruct"}], {**RGBD_CAPTURE, "source_id": "source-b"})

    scoped = client.get("/jobs?source_id=source-a").json()
    assert len(scoped) == 1
    assert scoped[0]["source_id"] == "source-a"


def test_a_rejection_is_reported_on_the_submitting_request(client):
    """Diagnostic gating is decided before any work starts.

    It has to come back on the POST rather than only via polling, because the
    caller's mistake is the request itself.
    """
    job = submit(client, [{"op": "detect_caries"}])
    assert job["status"] == "rejected"
    assert job["finished_at"] is not None


def test_health_and_capabilities(client):
    names = {b["name"] for b in client.get("/health").json()["backends"]}
    assert "fixture" in names

    caps = client.get("/capabilities").json()
    fixture = next(b for b in caps["backends"] if b["backend"] == "fixture")
    assert set(fixture["ops"]) >= {"rectify", "reconstruct", "segment", "measure"}


def test_multi_stage_job_returns_one_combined_envelope(client):
    job = run(client, FULL_PIPELINE, RGBD_CAPTURE)
    assert job["status"] == "succeeded"

    result = job["result"]
    assert result["ops"] == ["rectify", "reconstruct", "segment", "measure"]
    assert result["segmentation"] is not None
    assert any(a["kind"] == "mesh" for a in result["artifacts"])
    assert result["measurements"]


def test_rgbd_reconstruction_is_metric(client):
    result = run(client, FULL_PIPELINE, RGBD_CAPTURE)["result"]
    assert result["scale"] == {"verified": True, "source": "depth_sensor"}
    mesh = next(a for a in result["artifacts"] if a["kind"] == "mesh")
    assert mesh["units"] == "mm"


def test_rgb_without_fiducial_refuses_to_measure(client):
    """RGB-only geometry is scale-ambiguous.

    With no fiducial it must report unverified scale, label the geometry
    arbitrary, and derive no measurements.
    """
    result = run(client, FULL_PIPELINE, RGB_NO_FIDUCIAL)["result"]

    assert result["scale"]["verified"] is False
    assert result["measurements"] == []
    mesh = next(a for a in result["artifacts"] if a["kind"] == "mesh")
    assert mesh["units"] == "arbitrary"


def test_diagnostic_op_is_gated(client):
    assert run(client, [{"op": "detect_caries"}])["status"] == "rejected"


def test_diagnostic_op_needs_server_switch_too(client):
    assert run(client, [{"op": "detect_caries"}], allow_diagnostic=True)["status"] == "rejected"


def test_unknown_op_fails_without_a_result(client):
    job = run(client, [{"op": "does_not_exist"}])
    assert job["status"] == "failed"
    assert job["result"] is None


def test_segmentation_contains_abstentions(client):
    seg = run(client, [{"op": "segment"}], RGBD_CAPTURE)["result"]["segmentation"]
    assert seg["unassigned_region_pct"] > 0
    assert any(instance["fdi"] is None for instance in seg["instances"])


def test_mesh_artifact_is_a_valid_ply(client):
    result = run(client, [{"op": "reconstruct"}], RGBD_CAPTURE)["result"]
    ref = next(a for a in result["artifacts"] if a["kind"] == "mesh")["ref"]
    response = client.get("/artifacts/" + ref.split("://", 1)[1])
    assert response.status_code == 200

    lines = response.content.decode("ascii").splitlines()
    header_end = lines.index("end_header")
    vertex_count = int(next(ln for ln in lines if ln.startswith("element vertex")).split()[-1])
    face_count = int(next(ln for ln in lines if ln.startswith("element face")).split()[-1])

    vertices = [ln.split() for ln in lines[header_end + 1 : header_end + 1 + vertex_count]]
    faces = [ln.split() for ln in lines[header_end + 1 + vertex_count :]]

    assert len(vertices) == vertex_count and all(len(v) == 3 for v in vertices)
    assert len(faces) == face_count and all(len(f) == 4 and f[0] == "3" for f in faces)
    indices = [int(x) for f in faces for x in f[1:]]
    assert 0 <= min(indices) and max(indices) < vertex_count


def test_artifact_errors(client):
    assert client.get("/artifacts/does/not/exist.ply").status_code == 404
    assert client.get("/artifacts/../../pyproject.toml").status_code in (400, 404)


def test_empty_stage_list_is_rejected(client):
    assert client.post("/jobs", json={"stages": []}).status_code == 422


# ── DICOM header parser ─────────────────────────────────────────────────────


def _dicom_bytes(tags: list[tuple[int, int, bytes, bytes]]) -> bytes:
    """Build a minimal explicit-VR little-endian Part 10 file with no pixel data."""
    out = bytearray(b"\x00" * 128 + b"DICM")
    for group, element, vr, value in tags:
        out += struct.pack("<HH", group, element) + vr
        if vr in (b"OB", b"OW", b"SQ", b"UN", b"UT"):
            out += b"\x00\x00" + struct.pack("<I", len(value))
        else:
            out += struct.pack("<H", len(value))
        out += value
    return bytes(out)


def test_dicom_parser_reads_core_tags():
    data = _dicom_bytes(
        [
            (0x0008, 0x0060, b"CS", b"CT"),
            (0x0008, 0x103E, b"LO", b"Mandible CBCT"),
            (0x0020, 0x000E, b"UI", b"1.2.840.113619.2.55.3\x00"),
            (0x0028, 0x0010, b"US", struct.pack("<H", 512)),
            (0x0028, 0x0011, b"US", struct.pack("<H", 512)),
        ]
    )
    header = parse_file(data)
    assert header is not None
    assert header.modality == "CT"
    assert header.series_description == "Mandible CBCT"
    assert header.rows == 512
    assert header.columns == 512


def test_dicom_parser_rejects_non_dicom():
    assert parse_file(b"this is a jpeg, not a dicom file") is None


# ── sources ─────────────────────────────────────────────────────────────────


def make_slice(
    index: int,
    series_uid: str,
    rows: int = 8,
    cols: int = 8,
    spacing_mm: float = 1.5,
    block: bool = False,
) -> bytes:
    """A real CT slice with pixel data, so the volume path is genuinely exercised."""
    dataset = Dataset()
    dataset.file_meta = FileMetaDataset()
    dataset.file_meta.MediaStorageSOPClassUID = CTImageStorage
    dataset.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    dataset.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    dataset.SOPClassUID = CTImageStorage
    dataset.SOPInstanceUID = dataset.file_meta.MediaStorageSOPInstanceUID
    dataset.StudyInstanceUID = generate_uid()
    dataset.SeriesInstanceUID = series_uid
    dataset.Modality = "CT"
    dataset.SeriesDescription = "Test CBCT"

    dataset.InstanceNumber = index + 1
    dataset.ImagePositionPatient = [0.0, 0.0, index * spacing_mm]
    dataset.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    dataset.PixelSpacing = [0.4, 0.4]
    dataset.SliceThickness = spacing_mm

    dataset.Rows = rows
    dataset.Columns = cols
    dataset.BitsAllocated = 16
    dataset.BitsStored = 16
    dataset.HighBit = 15
    dataset.PixelRepresentation = 0
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.RescaleSlope = 1
    dataset.RescaleIntercept = -1024

    pixels = np.full((rows, cols), 100 + index * 10, dtype=np.uint16)
    if block:
        pixels[rows // 4: -rows // 4, cols // 4: -cols // 4] = 3000
    dataset.PixelData = pixels.tobytes()

    buffer = io.BytesIO()
    dataset.save_as(buffer, enforce_file_format=True)
    return buffer.getvalue()


def upload_dicom(client: TestClient, payloads: list[tuple[str, bytes]], **form):
    return client.post(
        "/sources/dicom",
        files=[("files", (name, data, "application/dicom")) for name, data in payloads],
        data={"workspace_id": "ws-1", "set_id": "set-1", **form},
    )


def test_series_uploads_and_builds_a_volume(client):
    series_uid = generate_uid()
    payloads = [(f"slice{i:03d}.dcm", make_slice(i, series_uid)) for i in range(3)]

    response = upload_dicom(client, payloads)
    assert response.status_code == 201, response.text
    source = response.json()

    assert source["kind"] == "dicom"
    assert source["name"] == "Test CBCT"
    assert source["instance_count"] == 3
    assert source["renderable"] is True, source["render_reason"]

    volume = client.get(f"/sources/{source['source_id']}/volume").json()
    header = volume["header"]

    assert header["dims"] == [8, 8, 3]
    assert header["spacing"][0] == pytest.approx(0.4)
    assert header["spacing"][2] == pytest.approx(1.5)
    assert header["dtype"] == "float32"
    # 8 x 8 x 3 voxels at 4 bytes.
    assert header["byte_length"] == 8 * 8 * 3 * 4

    blob = client.get("/artifacts/" + volume["bin_ref"].split("://", 1)[1])
    assert blob.status_code == 200
    assert len(blob.content) == header["byte_length"]


def test_volume_respects_slice_order_not_filename(client):
    """Slices named out of order must still stack by ImagePositionPatient."""
    series_uid = generate_uid()
    payloads = [
        ("c.dcm", make_slice(2, series_uid)),
        ("a.dcm", make_slice(0, series_uid)),
        ("b.dcm", make_slice(1, series_uid)),
    ]
    source = upload_dicom(client, payloads).json()
    volume = client.get(f"/sources/{source['source_id']}/volume").json()

    blob = client.get("/artifacts/" + volume["bin_ref"].split("://", 1)[1]).content
    values = np.frombuffer(blob, dtype="<f4")

    # Stored value encodes the slice index (100, 110, 120), then the intercept of
    # -1024 is applied. Ascending values prove the geometry sort won, not the
    # filenames, which run c -> a -> b.
    assert values[0] == pytest.approx(-924.0)
    assert values[-1] == pytest.approx(-904.0)
    assert np.all(np.diff(values) >= 0)


def test_rescale_intercept_is_applied(client):
    series_uid = generate_uid()
    source = upload_dicom(client, [("s.dcm", make_slice(0, series_uid, rows=2, cols=2))]).json()
    volume = client.get(f"/sources/{source['source_id']}/volume").json()

    blob = client.get("/artifacts/" + volume["bin_ref"].split("://", 1)[1]).content
    values = np.frombuffer(blob, dtype="<f4")
    # Stored 100, intercept -1024.
    assert values[0] == pytest.approx(-924.0)


def test_zipped_series_is_expanded(client):
    series_uid = generate_uid()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for i in range(3):
            archive.writestr(f"export/slice{i:03d}.dcm", make_slice(i, series_uid))

    source = upload_dicom(client, [("export.zip", buffer.getvalue())]).json()
    assert source["instance_count"] == 3
    assert source["renderable"] is True


def test_image_set_uploads_but_is_not_renderable(client):
    response = client.post(
        "/sources/images",
        files=[("files", ("a.jpg", b"\xff\xd8\xff\xe0jpeg", "image/jpeg"))],
        data={"workspace_id": "ws-1", "set_id": "set-1"},
    )
    assert response.status_code == 201
    source = response.json()
    assert source["kind"] == "images"
    assert source["renderable"] is False
    assert source["render_reason"]


def test_sources_are_scoped_by_workspace_and_set(client):
    series_uid = generate_uid()
    upload_dicom(client, [("s.dcm", make_slice(0, series_uid))])

    assert len(client.get("/sources", params={"workspace_id": "ws-1", "set_id": "set-1"}).json()) == 1
    assert client.get("/sources", params={"workspace_id": "ws-1", "set_id": "other"}).json() == []
    assert client.get("/sources", params={"workspace_id": "nope"}).json() == []


def test_rename_preserves_the_original_name(client):
    series_uid = generate_uid()
    source = upload_dicom(client, [("s.dcm", make_slice(0, series_uid))]).json()
    assert source["original_name"] == "Test CBCT"

    renamed = client.patch(f"/sources/{source['source_id']}", data={"name": "Patient A"}).json()
    assert renamed["name"] == "Patient A"
    assert renamed["original_name"] == "Test CBCT"

    assert client.patch(f"/sources/{source['source_id']}", data={"name": "  "}).status_code == 400


def test_delete_removes_the_source_and_its_files(client):
    series_uid = generate_uid()
    source = upload_dicom(client, [("s.dcm", make_slice(0, series_uid))]).json()
    source_id = source["source_id"]

    assert client.get(f"/sources/{source_id}/volume").status_code == 200
    assert client.delete(f"/sources/{source_id}").status_code == 204
    assert client.get(f"/sources/{source_id}").status_code == 404
    assert client.get("/sources").json() == []


def test_headerless_upload_is_kept_and_flagged(client):
    response = client.post(
        "/sources/dicom",
        files=[("files", ("scan.png", b"\x89PNG\r\n\x1a\nnot-a-dicom", "image/png"))],
        data={"workspace_id": "ws-1", "set_id": "set-1"},
    )
    assert response.status_code == 201
    source = response.json()
    assert source["headerless"] is True
    assert source["instance_count"] == 1
    assert source["renderable"] is False


# ── iso-surface ─────────────────────────────────────────────────────────────


def upload_blocky_series(client, slices: int = 6, rows: int = 24, cols: int = 24) -> dict:
    series_uid = generate_uid()
    payloads = [
        (f"s{i:03d}.dcm", make_slice(i, series_uid, rows=rows, cols=cols, spacing_mm=1.0, block=True))
        for i in range(slices)
    ]
    return upload_dicom(client, payloads).json()


def test_iso_surface_extracts_a_mesh_from_a_volume(client):
    source = upload_blocky_series(client)

    response = client.post(
        "/jobs",
        json={
            "stages": [{"op": "iso_surface", "params": {"threshold": 500}}],
            "capture": {"capture_id": source["source_id"], "modality": "radiograph",
                        "source_id": source["source_id"]},
        },
    )
    assert response.status_code == 201, response.text
    job = settle(client, response.json())
    assert job["status"] == "succeeded", job["error"]

    result = job["result"]
    assert result["ops"] == ["iso_surface"]
    mesh = next(a for a in result["artifacts"] if a["kind"] == "mesh")
    assert mesh["format"] == "ply" and mesh["units"] == "mm"
    assert mesh["triangles"] > 0 and mesh["vertices"] > 0
    assert result["geometry"]["threshold"] == 500

    # The surface must sit inside the volume's world extent, not at the origin.
    xmin, xmax, ymin, ymax, zmin, zmax = result["geometry"]["bounds"]
    assert xmax > xmin and ymax > ymin and zmax > zmin
    assert zmin == pytest.approx(0.0, abs=1.5) and zmax == pytest.approx(5.0, abs=1.5)


def test_iso_surface_ply_is_binary_and_parses(client):
    source = upload_blocky_series(client)
    job = settle(client, client.post(
        "/jobs",
        json={"stages": [{"op": "iso_surface", "params": {"threshold": 500}}],
              "capture": {"source_id": source["source_id"]}},
    ).json())
    mesh = next(a for a in job["result"]["artifacts"] if a["kind"] == "mesh")

    body = client.get("/artifacts/" + mesh["ref"].split("://", 1)[1]).content
    marker = b"end_header" + bytes([10])
    header_end = body.index(marker) + len(marker)
    assert b"format binary_little_endian 1.0" in body[:200]

    header_lines = body[:header_end].decode("ascii").splitlines()
    vertex_count = int(next(l for l in header_lines if l.startswith("element vertex")).split()[-1])
    face_count = int(next(l for l in header_lines if l.startswith("element face")).split()[-1])
    assert vertex_count == mesh["vertices"] and face_count == mesh["triangles"]

    # 12 bytes a vertex, 13 bytes a face (uchar count + three int32).
    assert len(body) - header_end == vertex_count * 12 + face_count * 13

    verts = np.frombuffer(body, dtype="<f4", count=vertex_count * 3, offset=header_end)
    verts = verts.reshape(vertex_count, 3)
    assert np.isfinite(verts).all()


def test_iso_surface_rejects_a_threshold_outside_the_range(client):
    source = upload_blocky_series(client)
    job = settle(client, client.post(
        "/jobs",
        json={"stages": [{"op": "iso_surface", "params": {"threshold": 99999}}],
              "capture": {"source_id": source["source_id"]}},
    ).json())
    assert job["status"] == "failed"
    assert "outside the volume" in (job["error"] or "")


def test_iso_surface_without_a_source_fails_cleanly(client):
    job = settle(client, client.post(
        "/jobs", json={"stages": [{"op": "iso_surface"}], "capture": {}}
    ).json())
    assert job["status"] == "failed"
    assert "source_id" in (job["error"] or "")


def test_iso_surface_rejects_a_single_slice_source(client):
    """A 1-voxel-thick volume has no interior to extract a surface from.

    skimage's own error for this says nothing about slice counts, so the guard
    has to name the real problem.
    """
    series_uid = generate_uid()
    source = upload_dicom(client, [("only.dcm", make_slice(0, series_uid, block=True))]).json()
    job = settle(client, client.post(
        "/jobs",
        json={"stages": [{"op": "iso_surface", "params": {"threshold": 500}}],
              "capture": {"source_id": source["source_id"]}},
    ).json())
    assert job["status"] == "failed"
    assert "at least 2 along every axis" in (job["error"] or "")


def test_iso_surface_names_a_missing_source(client):
    job = settle(client, client.post(
        "/jobs",
        json={"stages": [{"op": "iso_surface", "params": {"threshold": 500}}],
              "capture": {"source_id": "11111111-2222-3333-4444-555555555555"}},
    ).json())
    assert job["status"] == "failed"
    error = job["error"] or ""
    assert "no source" in error and "Re-upload" in error


# ── TotalSegmentator plumbing ───────────────────────────────────────────────
#
# Driven through tests/stub_totalseg.py so command construction, NIfTI axis
# handling, legend parsing and mask storage are exercised without a PyTorch
# install. The real binary is never required by the suite.

STUB_SEGMENTER = Path(__file__).parent / "stub_totalseg.py"


def use_stub_segmenter(monkeypatch) -> None:
    """Point both the segmenter and its info companion at the stub."""
    monkeypatch.setenv("BONE_VIEWER_TOTALSEGMENTATOR", str(STUB_SEGMENTER))
    monkeypatch.setenv("BONE_VIEWER_TOTALSEG_INFO", str(STUB_SEGMENTER))


def run_segment(client, source_id: str, **params):
    return settle(client, client.post(
        "/jobs",
        json={
            "stages": [{"op": "segment", "params": params}],
            "capture": {"source_id": source_id},
        },
    ).json())


def test_segment_without_a_segmenter_explains_how_to_install(client, monkeypatch):
    # Point at a path that does not exist rather than relying on TotalSegmentator
    # being absent. Whether it is installed is a property of the machine, and a
    # test that only passes on a bare machine tests nothing.
    monkeypatch.setenv("BONE_VIEWER_TOTALSEGMENTATOR", str(Path("does") / "not" / "exist"))
    monkeypatch.delenv("BONE_VIEWER_TOTALSEG_INFO", raising=False)
    series_uid = generate_uid()
    source = upload_dicom(client, [("s.dcm", make_slice(0, series_uid, block=True))]).json()

    job = run_segment(client, source["source_id"])
    assert job["status"] == "failed"
    error = job["error"] or ""
    assert "TotalSegmentator" in error and "install" in error.lower()


def test_segment_produces_a_mask_and_a_legend(client, monkeypatch):
    use_stub_segmenter(monkeypatch)
    source = upload_blocky_series(client, slices=6, rows=24, cols=24)

    job = run_segment(client, source["source_id"])
    assert job["status"] == "succeeded", job["error"]

    seg = job["result"]["segmentation"]
    assert seg["kind"] == "volumetric"
    assert [c["name"] for c in seg["classes"]] == ["rib_left_4", "rib_left_5"]
    assert all(c["volume"] > 0 for c in seg["classes"])

    mask = next(a for a in job["result"]["artifacts"] if a["kind"] == "mask")
    header = json.loads(
        client.get("/artifacts/" + seg["mask_header_ref"].split("://", 1)[1]).content
    )
    assert header["labels"] == [1, 2]
    assert header["legend"]["1"] == "rib_left_4"

    blob = client.get("/artifacts/" + mask["ref"].split("://", 1)[1]).content
    assert len(blob) == header["byte_length"] == 24 * 24 * 6 * 4


def test_segment_mask_keeps_its_axis_order(client, monkeypatch):
    """The stub splits the volume along z, so a transposed mask shows up here.

    NIfTI is (x, y, z) and we store (nz, ny, nx); getting that wrong is silent
    and would put every label in the wrong place.
    """
    use_stub_segmenter(monkeypatch)
    source = upload_blocky_series(client, slices=6, rows=24, cols=24)
    job = run_segment(client, source["source_id"])

    seg = job["result"]["segmentation"]
    header = json.loads(
        client.get("/artifacts/" + seg["mask_header_ref"].split("://", 1)[1]).content
    )
    blob = client.get("/artifacts/" + seg["mask_ref"].split("://", 1)[1]).content

    # Storage is (nz, ny, nx); taking the dims from the header rather than
    # assuming them is the point, since a wrong reshape of the right element
    # count fails silently.
    nz, ny, nx = header["dims"][2], header["dims"][1], header["dims"][0]
    mask = np.frombuffer(blob, dtype="<i4").reshape(nz, ny, nx)

    # Label 1 occupies the low-z third, label 2 the middle third.
    assert mask[0, 0, 0] == 1
    assert mask[2, 0, 0] == 2
    assert (mask == 1).sum() == 24 * 24 * 2
    assert (mask == 2).sum() == 24 * 24 * 2


def test_image_set_segment_stays_synthetic(client):
    """No volume means no volumetric segmenter; the dental path is unchanged."""
    response = client.post(
        "/sources/images",
        files=[("files", ("a.jpg", b"\xff\xd8\xff\xe0jpeg", "image/jpeg"))],
        data={"workspace_id": "ws-1", "set_id": "set-1"},
    )
    source = response.json()
    job = settle(client, client.post(
        "/jobs",
        json={"stages": [{"op": "segment"}], "capture": {"source_id": source["source_id"]}},
    ).json())
    assert job["status"] == "succeeded", job["error"]
    assert job["result"]["segmentation"]["instances"]


# ── labelled surfaces (segment -> iso_surface in one job) ───────────────────


def run_labelled_surface(client, monkeypatch, **params):
    use_stub_segmenter(monkeypatch)
    source = upload_blocky_series(client, slices=9, rows=24, cols=24)
    return settle(client, client.post(
        "/jobs",
        json={
            "stages": [{"op": "segment"}, {"op": "iso_surface", "params": params}],
            "capture": {"source_id": source["source_id"]},
        },
    ).json())


def test_chained_stages_produce_a_surface_with_labels(client, monkeypatch):
    job = run_labelled_surface(client, monkeypatch)
    assert job["status"] == "succeeded", job["error"]
    result = job["result"]
    assert result["ops"] == ["segment", "iso_surface"]

    mesh = next(a for a in result["artifacts"] if a["kind"] == "mesh")
    labels = next(a for a in result["artifacts"] if a["kind"] == "labels")

    # One int32 per vertex, and the counts must agree with the mesh.
    assert labels["vertices"] == mesh["vertices"]
    blob = client.get("/artifacts/" + labels["ref"].split("://", 1)[1]).content
    assert len(blob) == mesh["vertices"] * 4

    header = json.loads(
        client.get("/artifacts/" + labels["header_ref"].split("://", 1)[1]).content
    )
    assert header["kind"] == "vertex"
    assert header["legend"] == {"1": "rib_left_4", "2": "rib_left_5"}


def test_every_vertex_carries_a_known_label(client, monkeypatch):
    """A surface extracted from a mask must not have unlabelled vertices.

    The stub's two labels sit flush against each other, which is the case that
    catches the obvious implementation: extracting the boundary of the union
    gives label 1 no geometry at all, because it never touches background, so it
    can neither be seen nor clicked. Every label with voxels must come back.
    """
    job = run_labelled_surface(client, monkeypatch)
    mesh = next(a for a in job["result"]["artifacts"] if a["kind"] == "mesh")
    labels = next(a for a in job["result"]["artifacts"] if a["kind"] == "labels")

    values = np.frombuffer(
        client.get("/artifacts/" + labels["ref"].split("://", 1)[1]).content, dtype="<i4"
    )
    assert len(values) == mesh["vertices"]
    assert set(np.unique(values).tolist()) == {1, 2}


def test_an_anisotropic_volume_is_scaled_once():
    """Spacing must be applied exactly once.

    Marching cubes is run in voxel units and the affine carries the spacing, so
    passing it in both places squares it — a 1x1x4 mm grid comes out 4 mm thick
    and 16 mm tall. Anisotropy is what makes it visible; uniform spacing just
    looks uniformly too big.
    """
    from app.pipeline.isosurface import extract

    volume = np.zeros((6, 8, 10), dtype="<f4")  # (nz, ny, nx)
    volume[2:4] = 1.0

    header = {
        "dims": [10, 8, 6],
        "spacing": [1.0, 1.0, 4.0],
        "origin": [0.0, 0.0, 0.0],
    }
    surface = extract(volume.tobytes(), header, threshold=0.5)

    # The bloc covers voxel z 2 and 3, so the surface sits on the boundaries
    # either side of it: z index 1.5 and 3.5, at 4 mm each — 6 and 14. Applying
    # the spacing twice would put them at 24 and 56.
    assert surface.vertices[:, 2].min() == pytest.approx(6.0, abs=0.01)
    assert surface.vertices[:, 2].max() == pytest.approx(14.0, abs=0.01)
    # x and y are unit spacing, so those stay at the array's own extent.
    assert surface.vertices[:, 0].max() <= 10.5
    assert surface.vertices[:, 1].max() <= 8.5


def test_a_flipped_axis_is_honoured_not_dropped():
    """A mask whose reader negated an axis must be placed with that negation.

    Using the affine's translation alone silently mirrors the whole mask about
    the origin. It still looks like a plausible mesh, in the wrong half of the
    volume — which is exactly how it presents in the viewport.
    """
    from app.pipeline.isosurface import extract_labelled

    # Array order is (z, y, x). This labels only the last slab along x.
    labels = np.zeros((4, 4, 4), dtype=np.int32)
    labels[:, :, 3] = 1

    header = {
        "dims": [4, 4, 4],
        "spacing": [1.0, 1.0, 1.0],
        "origin": [3.0, 0.0, 0.0],
        # x direction negated: index 3 sits at world x = 3 - 3 = 0.
        "direction": [-1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        "legend": {"1": "only"},
    }

    surface = extract_labelled(labels.tobytes(), header)
    assert surface.vertices[:, 0].max() < 1.0
    # Ignoring the direction would put the same slab out at x = 6.
    assert surface.vertices[:, 0].min() > -1.0


def test_the_volume_fallback_is_in_mm3(tmp_path):
    """A bare voxel count reported as mm^3 is wrong by the voxel size.

    It looks entirely plausible, which is the problem: a 0.5x0.5x2.0 mm CT would
    understate every volume eightfold and nothing downstream could tell.
    """
    from app.pipeline.totalseg import _statistics

    labels = np.zeros((4, 4, 4), dtype=np.int32)
    labels[:2] = 1  # 2 * 4 * 4 = 32 voxels

    volumes = _statistics(tmp_path, labels, {1: "rib_left_4"}, (0.5, 0.5, 2.0))
    assert volumes == {"rib_left_4": 32 * 0.5 * 0.5 * 2.0}


def test_the_legend_comes_from_the_configured_segmenter(client, monkeypatch):
    """Configuring only the segmenter must not source class names from PATH.

    A machine with a real TotalSegmentator installed would otherwise answer for
    a stand-in's mask and rename every label after the wrong anatomy — the mask
    and its legend would both look valid and disagree.
    """
    use_stub_segmenter(monkeypatch)
    monkeypatch.delenv("BONE_VIEWER_TOTALSEG_INFO")

    job = run_segment(client, upload_blocky_series(client)["source_id"])
    assert job["status"] == "succeeded", job["error"]

    legend = json.loads(
        client.get(
            "/artifacts/"
            + next(
                a for a in job["result"]["artifacts"] if a["kind"] == "mask"
            )["header_ref"].split("://", 1)[1]
        ).content
    )["legend"]
    assert legend == {"1": "rib_left_4", "2": "rib_left_5"}


def test_the_task_selects_the_class_list(client, monkeypatch):
    """`teeth` and `total` number their labels differently.

    Asking the info companion for the wrong task's list still returns names, so
    a missing `-ta` would rename structures rather than fail.
    """
    use_stub_segmenter(monkeypatch)

    job = run_segment(client, upload_blocky_series(client)["source_id"], task="teeth")
    assert job["status"] == "succeeded", job["error"]

    header = json.loads(
        client.get(
            "/artifacts/"
            + next(
                a for a in job["result"]["artifacts"] if a["kind"] == "mask"
            )["header_ref"].split("://", 1)[1]
        ).content
    )
    assert header["task"] == "teeth"
    assert header["legend"] == {"1": "upper_tooth_11", "2": "upper_tooth_12"}


def test_touching_labels_meet_at_the_same_plane(client, monkeypatch):
    """Adjacent labels must share a seam, not overlap or leave a gap.

    Each label is extracted in its own bounding box, so the two passes reach the
    shared boundary from opposite sides — one clamped at the array edge, one with
    a margin. If the window offset is off by a voxel the surfaces separate or
    interpenetrate, and the seam is the only place that shows it.
    """
    first = run_labelled_surface(client, monkeypatch, label="rib_left_4")
    second = run_labelled_surface(client, monkeypatch, label="rib_left_5")
    assert first["status"] == "succeeded", first["error"]
    assert second["status"] == "succeeded", second["error"]

    # bounds are [xmin, xmax, ymin, ymax, zmin, zmax]; the stub's labels are
    # stacked along z, which is the axis they touch on.
    below = first["result"]["geometry"]["bounds"]
    above = second["result"]["geometry"]["bounds"]
    assert below[5] == pytest.approx(above[4], abs=1e-4)
    assert above[5] > below[5]


def test_label_param_restricts_the_surface(client, monkeypatch):
    job = run_labelled_surface(client, monkeypatch, label="rib_left_4")
    assert job["status"] == "succeeded", job["error"]

    labels = next(a for a in job["result"]["artifacts"] if a["kind"] == "labels")
    header = json.loads(
        client.get("/artifacts/" + labels["header_ref"].split("://", 1)[1]).content
    )
    assert set(header["legend"].keys()) == {"1"}

    values = np.frombuffer(
        client.get("/artifacts/" + labels["ref"].split("://", 1)[1]).content, dtype="<i4"
    )
    assert set(np.unique(values).tolist()) == {1}


def test_several_labels_can_be_extracted_at_once(client, monkeypatch):
    """Multi-select rides on the same extraction — one mesh, several labels."""
    job = run_labelled_surface(
        client, monkeypatch, label=["rib_left_4", "rib_left_5"]
    )
    assert job["status"] == "succeeded", job["error"]

    labels = next(a for a in job["result"]["artifacts"] if a["kind"] == "labels")
    header = json.loads(
        client.get("/artifacts/" + labels["header_ref"].split("://", 1)[1]).content
    )
    assert set(header["legend"].keys()) == {"1", "2"}

    values = np.frombuffer(
        client.get("/artifacts/" + labels["ref"].split("://", 1)[1]).content, dtype="<i4"
    )
    assert set(np.unique(values).tolist()) == {1, 2}


def test_an_empty_label_list_means_every_label(client, monkeypatch):
    """An empty filter is no filter, which is what the form sends by default."""
    job = run_labelled_surface(client, monkeypatch, label=[])
    assert job["status"] == "succeeded", job["error"]

    labels = next(a for a in job["result"]["artifacts"] if a["kind"] == "labels")
    header = json.loads(
        client.get("/artifacts/" + labels["header_ref"].split("://", 1)[1]).content
    )
    assert set(header["legend"].keys()) == {"1", "2"}


def test_unknown_label_names_the_available_ones(client, monkeypatch):
    job = run_labelled_surface(client, monkeypatch, label="rib_right_9")
    assert job["status"] == "failed"
    error = job["error"] or ""
    assert "rib_right_9" in error and "rib_left_4" in error


# ── simplifying surfaces ───────────────────────────────────────────────────


def test_clustering_reduces_a_surface_without_losing_it():
    """Every cell that held vertices still holds one afterwards.

    A decimation that drops a whole structure is worse than no decimation, and
    it would be invisible in a bounding box or a triangle count.
    """
    from app.pipeline.isosurface import cluster

    # A unit grid of vertices, so cells are predictable.
    grid = np.stack(
        np.meshgrid(np.arange(6), np.arange(6), np.arange(6), indexing="ij"), axis=-1
    ).reshape(-1, 3).astype(np.float32)

    # A cube per grid point's lower corner, giving a connected surface.
    faces = []
    for i in range(5):
        for j in range(5):
            for k in range(5):
                a = (i * 6 + j) * 6 + k
                faces.append((a, a + 1, a + 6))
    faces = np.array(faces, dtype=np.int32)

    merged, thinned = cluster(grid, faces, cell=2.0)
    assert 0 < len(merged) < len(grid)
    assert len(thinned) > 0
    # Nothing may reference a vertex that no longer exists.
    assert thinned.max() < len(merged)


def test_clustering_is_off_by_default_and_leaves_the_surface_alone():
    from app.pipeline.isosurface import cluster

    verts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
    faces = np.array([[0, 1, 2]], dtype=np.int32)
    same_v, same_f = cluster(verts, faces, cell=0.0)
    assert same_v is verts and same_f is faces


def test_a_simplified_surface_reports_what_it_cost(client, monkeypatch):
    """The reduction is a trade, so the numbers that describe it are in the result."""
    job = run_labelled_surface(client, monkeypatch, simplify=4.0)
    assert job["status"] == "succeeded", job["error"]

    geometry = job["result"]["geometry"]
    assert geometry["simplify_mm"] == 4.0
    assert geometry["vertices_before"] > 0
    assert geometry["triangles_before"] > 0

    mesh = next(a for a in job["result"]["artifacts"] if a["kind"] == "mesh")
    assert mesh["vertices"] <= geometry["vertices_before"]
    assert mesh["triangles"] <= geometry["triangles_before"]


def test_simplifying_keeps_every_vertex_labelled(client, monkeypatch):
    """Clustering happens before the per-label meshes are merged.

    Merged first, a cluster spanning two touching labels would average their
    vertices and the seam would take whichever label won — silently mislabelling
    exactly the boundary a click is most likely to land on.
    """
    job = run_labelled_surface(client, monkeypatch, simplify=3.0)
    assert job["status"] == "succeeded", job["error"]

    mesh = next(a for a in job["result"]["artifacts"] if a["kind"] == "mesh")
    labels = next(a for a in job["result"]["artifacts"] if a["kind"] == "labels")
    values = np.frombuffer(
        client.get("/artifacts/" + labels["ref"].split("://", 1)[1]).content, dtype="<i4"
    )
    assert len(values) == mesh["vertices"]
    assert set(np.unique(values).tolist()) == {1, 2}


# ── the Image View ─────────────────────────────────────────────────────────


def test_dicom_slices_are_listed_and_rendered(client):
    """Slices come off the assembled volume, so the two views agree.

    Listing the files instead would need its own sort, and any disagreement
    about order between the slice strip and the 3D volume is a clinical bug.
    """
    source = upload_blocky_series(client, slices=6, rows=24, cols=24)

    listing = client.get(f"/sources/{source['source_id']}/images").json()
    assert listing["kind"] == "dicom"
    assert listing["count"] == 6
    assert [entry["index"] for entry in listing["images"]] == [0, 1, 2, 3, 4, 5]

    response = client.get(f"/sources/{source['source_id']}/images/2")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_slice_index_outside_the_volume_is_refused(client):
    source = upload_blocky_series(client, slices=6, rows=24, cols=24)
    assert client.get(f"/sources/{source['source_id']}/images/6").status_code == 404
    assert client.get(f"/sources/{source['source_id']}/images/-1").status_code == 404


def test_an_uploaded_photo_is_served_as_itself(client):
    """A photo is not a slice: it is shown as captured, not re-encoded."""
    jpeg = b"\xff\xd8\xff\xe0" + b"jpeg payload" * 4
    source = client.post(
        "/sources/images",
        files=[("files", ("anterior.jpg", jpeg, "image/jpeg"))],
        data={"workspace_id": "ws-1", "set_id": "set-1"},
    ).json()

    listing = client.get(f"/sources/{source['source_id']}/images").json()
    assert listing["kind"] == "images"
    assert listing["count"] == 1
    assert listing["images"][0]["media_type"] == "image/jpeg"

    response = client.get(f"/sources/{source['source_id']}/images/0")
    assert response.status_code == 200
    assert response.content == jpeg


def test_the_image_window_can_be_overridden(client):
    """Contrast is the one thing a viewer must be able to change."""
    source = upload_blocky_series(client, slices=4, rows=24, cols=24)
    wide = client.get(f"/sources/{source['source_id']}/images/1?level=0&window=4000")
    narrow = client.get(f"/sources/{source['source_id']}/images/1?level=500&window=50")
    assert wide.status_code == narrow.status_code == 200
    # A different window must actually change the pixels, not just the header.
    assert wide.content != narrow.content


def test_the_slice_spacing_is_not_scaled_twice(client):
    """The listing and the slice's own header must agree.

    The stored volume spacing is already the spacing *after* subsampling, so
    applying the stride again reports every slice as twice as far from its
    neighbour as it is — and a viewer that draws a scale bar would be wrong.
    """
    source = upload_blocky_series(client, slices=6, rows=24, cols=24)

    listing = client.get(f"/sources/{source['source_id']}/images").json()
    header = client.get(f"/sources/{source['source_id']}/images/1").headers[
        "x-slice-spacing-mm"
    ]
    assert float(header) == pytest.approx(listing["slice_spacing_mm"])


# ── the two workflows that are not a CT volume ─────────────────────────────


def test_poses_come_from_the_device_when_it_has_them(client):
    """A phone that tracks its own motion must not pay for structure-from-motion.

    Which source applies is a property of the capture, not a parameter: the
    frame either carries a pose or it does not.
    """
    capture = {
        **RGBD_CAPTURE,
        "frames": [{"frame_id": 0, "pose": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]}],
    }
    result = run(client, [{"op": "estimate_poses"}], capture)["result"]
    assert result["geometry"]["pose_source"] == "device"
    assert result["geometry"]["poses_solved"] == 0
    assert result["geometry"]["scale_known"] is True


def test_poses_are_solved_when_the_capture_has_none(client):
    """Ordinary photographs are not refused for lacking a tracker.

    What they get is not a trajectory, though. Nothing in this checkout can
    solve one, so the result says `placeholder` and warns — see
    test_photos_with_no_solver_say_placeholder_rather_than_claiming_a_solve.
    The earlier version of this test asserted `sfm` and `poses_solved: N`,
    which was the fixture describing work no solver had done.
    """
    result = run(client, [{"op": "estimate_poses"}], RGB_NO_FIDUCIAL)["result"]
    assert result["geometry"]["pose_source"] == "placeholder"
    assert result["geometry"]["poses_solved"] == 0
    assert result["geometry"]["scale_known"] is False
    assert any("No camera-pose solver is installed" in w for w in result["warnings"])


def test_any_number_of_views_is_accepted(client):
    """The five-view convention is a constraint of other people's pipelines.

    Nothing here may require a particular count: a phone captures a video, and a
    clinician uploads however many photographs they took.
    """
    for frames in (1, 3, 5, 17):
        capture = {
            **RGB_NO_FIDUCIAL,
            "frames": [{"frame_id": i} for i in range(frames)],
        }
        result = run(client, [{"op": "estimate_poses"}], capture)["result"]
        assert result["geometry"]["frames"] == frames


def test_a_panoramic_reconstruction_says_what_it_cannot_know(client):
    """One flat projection has no buccolingual extent to observe.

    Reporting a depth that was never measured is the failure mode that matters
    here, so the surface is produced and the limitation is stated with it.
    """
    job = run(client, [{"op": "px2tooth"}], {**RGB_NO_FIDUCIAL, "modality": "radiograph"})
    assert job["status"] == "succeeded", job["error"]

    result = job["result"]
    assert result["ops"] == ["px2tooth"]
    mesh = next(a for a in result["artifacts"] if a["kind"] == "mesh")
    # No pixel spacing, so no millimetres to claim.
    assert mesh["units"] == "arbitrary"
    assert any("buccolingual" in w for w in result["warnings"])


def test_the_fixture_advertises_the_new_stages(client):
    caps = client.get("/capabilities").json()
    fixture = next(b for b in caps["backends"] if b["backend"] == "fixture")
    assert {"estimate_poses", "px2tooth"} <= set(fixture["ops"])


# ── exporting ──────────────────────────────────────────────────────────────


def _export(client, result_ids, **body):
    return client.post(
        "/exports", json={"result_ids": result_ids, "format": "stl", **body}
    )


def test_an_export_carries_the_geometry_and_how_it_was_made(client):
    """The manifest is the point of the exercise, not a courtesy.

    A mesh separated from the record of how it was produced is a shape of
    unknown scale made by an unknown process, and it looks perfectly usable.
    """
    job = run(client, FULL_PIPELINE, RGBD_CAPTURE)
    response = _export(client, [job["job_id"]])
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = archive.namelist()
    assert any(name.endswith(".stl") for name in names)
    assert "manifest.json" in names

    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["manifest_version"] == "1.0"
    assert manifest["runs"][0]["result_id"] == job["result"]["result_id"]

    stages = manifest["runs"][0]["stages"]
    assert [stage["op"] for stage in stages] == ["rectify", "reconstruct", "segment", "measure"]
    # Every stage has to name what ran, or the record answers nothing.
    assert all(stage["tool"] and stage["algorithm"] for stage in stages)
    assert manifest["runs"][0]["scale"]["verified"] is True


def test_exported_stl_is_readable_and_keeps_the_mesh(client):
    """A binary STL is a header, a count, and 50 bytes per triangle.

    Getting the record layout wrong produces a file that is exactly the right
    size and opens as garbage, so the count and the size are checked together.
    """
    source = upload_blocky_series(client)
    job = run(
        client,
        [{"op": "iso_surface", "params": {"threshold": 500}}],
        {"source_id": source["source_id"]},
    )

    archive = zipfile.ZipFile(io.BytesIO(_export(client, [job["job_id"]]).content))
    name = next(n for n in archive.namelist() if n.endswith(".stl"))
    blob = archive.read(name)

    assert len(blob) >= 84
    triangles = struct.unpack("<I", blob[80:84])[0]
    assert len(blob) == 84 + triangles * 50
    assert triangles > 0

    # Positions are finite and sit inside the volume's own bounds.
    verts = np.frombuffer(blob[84:], dtype=np.uint8).reshape(triangles, 50)
    points = verts[:, 12:48].copy().view("<f4").reshape(-1, 3)
    assert np.isfinite(points).all()
    bounds = job["result"]["geometry"]["bounds"]
    assert points[:, 0].min() >= bounds[0] - 1 and points[:, 0].max() <= bounds[1] + 1


def test_obj_is_one_based_indexed(client):
    """Zero-based indices load as an empty scene, which is the usual failure."""
    job = run(client, FULL_RECONSTRUCT := [{"op": "reconstruct"}], RGBD_CAPTURE)
    archive = zipfile.ZipFile(
        io.BytesIO(_export(client, [job["job_id"]], format="obj").content)
    )
    obj = archive.read(next(n for n in archive.namelist() if n.endswith(".obj"))).decode()

    faces = [ln for ln in obj.splitlines() if ln.startswith("f ")]
    assert faces, "no faces in the OBJ"
    assert min(int(v) for ln in faces for v in ln.split()[1:]) >= 1

    vertices = [ln for ln in obj.splitlines() if ln.startswith("v ")]
    highest = max(int(v) for ln in faces for v in ln.split()[1:])
    assert highest <= len(vertices)


def test_labels_travel_with_the_mesh_when_asked_for(client, monkeypatch):
    """Re-encoding preserves vertex order, so the sidecar still lines up."""
    job = run_labelled_surface(client, monkeypatch, simplify=0)
    archive = zipfile.ZipFile(
        io.BytesIO(_export(client, [job["job_id"]], include_labels=True).content)
    )
    names = archive.namelist()
    assert any(n.endswith("labels.bin") for n in names)
    assert any(n.endswith("labels.json") for n in names)

    manifest = json.loads(archive.read("manifest.json"))
    mesh = next(f for f in manifest["files"] if f["name"].endswith(".stl"))

    # The label file is described on the mesh it belongs to, not in an entry of
    # its own — an array of ints with no mesh is an array of ints.
    assert mesh["label_file"]["bin"] in names
    assert len(archive.read(mesh["label_file"]["bin"])) == mesh["vertices"] * 4

    # And the names travel even without the array, because they are what make
    # the mesh interpretable.
    assert mesh["labels"]["legend"] == {"1": "rib_left_4", "2": "rib_left_5"}
    assert set(mesh["labels"]["counts"]) == {"1", "2"}


def test_an_export_refuses_what_it_cannot_describe(client):
    """Better a clear refusal than an archive with no manifest in it."""
    assert _export(client, ["no-such-result"]).status_code == 404
    job = submit(client, [{"op": "does_not_exist"}])
    assert settle(client, job)["status"] == "failed"
    assert _export(client, [job["job_id"]]).status_code == 409

    good = run(client, [{"op": "reconstruct"}], RGBD_CAPTURE)
    assert _export(client, [good["job_id"]], format="fbx").status_code == 400


def test_the_manifest_states_the_model_the_labels_came_from(client, monkeypatch):
    """A label id means nothing on its own.

    Id 5 is one structure under the `total` task and a different one under
    `teeth`, so the legend is only interpretable alongside the model that
    produced it.
    """
    job = run_labelled_surface(client, monkeypatch)
    archive = zipfile.ZipFile(io.BytesIO(_export(client, [job["job_id"]]).content))
    manifest = json.loads(archive.read("manifest.json"))

    run_entry = manifest["runs"][0]
    assert run_entry["model_version"] == job["result"]["model_version"]
    assert run_entry["segmentation"]["kind"] == "volumetric"
    assert {c["name"] for c in run_entry["segmentation"]["classes"]} == {
        "rib_left_4",
        "rib_left_5",
    }
    assert manifest["source"]["source_id"] is not None


def test_the_manifest_carries_no_advice(client):
    """It describes what was made, not what to think of it.

    Guidance belongs in the interface, where it is read once and can be
    updated. In the manifest it rides along in every archive ever produced,
    still asserting whatever we believed on the day it was written.
    """
    job = run(client, [{"op": "reconstruct"}], RGB_NO_FIDUCIAL)
    archive = zipfile.ZipFile(io.BytesIO(_export(client, [job["job_id"]]).content))
    manifest = json.loads(archive.read("manifest.json"))

    def keys_of(node) -> list[str]:
        if isinstance(node, dict):
            return [k for k in node] + [k for v in node.values() for k in keys_of(v)]
        if isinstance(node, list):
            return [k for item in node for k in keys_of(item)]
        return []

    keys = keys_of(manifest)
    assert "units_note" not in keys
    # A prose field is where advice gets back in, so there is deliberately none.
    assert "notes" not in keys

    # The fact stays; the sentence about what it implies does not.
    assert manifest["runs"][0]["scale"] == {"verified": False, "source": "unknown"}


def test_the_workflow_travels_with_the_result(client):
    """Two stages under different workflows are different pipelines.

    The ops alone do not say which was intended, so the manifest has to carry
    the choice — and it has to survive the round trip through the store.
    """
    job = run(client, [{"op": "reconstruct"}], RGBD_CAPTURE, workflow="Photographs")
    assert job["workflow"] == "Photographs"
    assert client.get(f"/jobs/{job['job_id']}").json()["workflow"] == "Photographs"

    archive = zipfile.ZipFile(io.BytesIO(_export(client, [job["job_id"]]).content))
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["runs"][0]["workflow"] == "Photographs"


def test_every_stage_describes_itself(client):
    """The hover shows one sentence per step, so every stage must have one."""
    from app.pipeline.fixture import _IMPLEMENTATIONS
    from app.pipeline.interfaces import Op

    for op in Op:
        entry = _IMPLEMENTATIONS.get(op)
        assert entry is not None, f"{op} has no implementation record"
        assert entry.get("description"), f"{op} has no description"
        assert entry["description"].endswith("."), f"{op}'s description is not a sentence"


# ── importing from a URL ───────────────────────────────────────────────────
#
# The endpoint takes a string from a user and makes the server act on it, so
# these test the attacks rather than the happy path. A regression here is not a
# broken feature, it is a hole.


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/anything",          # the server itself
        "http://localhost/anything",          # the same, by name
        "http://169.254.169.254/latest/meta-data/",  # cloud instance metadata
        "http://10.1.2.3/x",                  # private network
        "http://192.168.0.1/x",
        "http://[::1]/x",                     # loopback, v6
        "http://0.0.0.0/x",
        "file:///etc/passwd",                 # the server's own disk
        "ftp://example.com/x",
        "gopher://example.com/x",
    ],
)
def test_internal_addresses_are_refused(url):
    """A URL is an instruction, not a location.

    Left alone, the server fetches these with its own credentials and its own
    network position, and the caller learns the answers.
    """
    from app.fetch import RemoteError, download

    with pytest.raises(RemoteError):
        download(url)


def test_a_public_host_that_resolves_inward_is_refused(monkeypatch):
    """One public record must not launder a private one.

    A name with both is the standard way past a check that only looks at the
    first answer.
    """
    from app import fetch

    monkeypatch.setattr(
        fetch.socket,
        "getaddrinfo",
        lambda *a, **k: [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ],
    )
    with pytest.raises(fetch.RemoteError, match="not a public address"):
        fetch.download("http://looks-fine.example/x")


def test_archive_paths_cannot_escape_their_folder(tmp_path):
    """`../../` in an entry name writes wherever the extractor is allowed to."""
    from app.fetch import RemoteError, _extract_zip

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../../etc/passwd", "root:x:0:0")
        archive.writestr("normal/slice.dcm", "fine")

    with pytest.raises(RemoteError):
        _extract_zip(buffer.getvalue())


def test_absolute_paths_are_refused():
    from app.fetch import RemoteError, _extract_zip

    for name in ("/etc/shadow", r"C:\Windows\system.ini", r"\\server\share\x"):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(name, "x")
        with pytest.raises(RemoteError):
            _extract_zip(buffer.getvalue())


def test_a_decompression_bomb_is_refused():
    """42 KB of zeros expands to gigabytes. The ratio is the tell."""
    from app.fetch import RemoteError, _extract_zip

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bomb.bin", b"\0" * (80 * 1024 * 1024))

    blob = buffer.getvalue()
    assert len(blob) < 200_000, "the test's own premise: the archive is small"
    with pytest.raises(RemoteError, match="decompression bomb"):
        _extract_zip(blob)


def test_a_symlink_in_a_zip_is_refused():
    """Stored as a member whose content is the target, and followed on write."""
    from app.fetch import RemoteError, _extract_zip

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo("link")
        # 0o120000 is S_IFLNK in the high bits of the external attributes.
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "/etc/passwd")

    with pytest.raises(RemoteError, match="symbolic link"):
        _extract_zip(buffer.getvalue())


def test_a_symlink_in_a_tar_is_refused(tmp_path):
    """`filter="data"` would strip it; refusing loudly is better than silently
    dropping a file the archive's author meant to be there."""
    import tarfile

    from app.fetch import RemoteError, _extract_tar

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("escape")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)

    with pytest.raises(RemoteError, match="link or device"):
        _extract_tar(buffer.getvalue())


def test_a_normal_archive_is_detected_by_its_contents_not_its_name():
    """The name is a claim; the magic bytes are a fact."""
    from app.fetch import _looks_like_archive

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.dcm", "x")
    assert _looks_like_archive(buffer.getvalue()) == "zip"

    # A JPEG is not an archive, whatever it is called.
    assert _looks_like_archive(b"\xff\xd8\xff\xe0" + b"rest") is None


def test_a_rar_says_what_is_missing_rather_than_failing_obscurely():
    from app.fetch import RemoteError, fetch_dataset

    blob = b"Rar!\x1a\x07\x00" + b"\0" * 64
    monkey = __import__("app.fetch", fromlist=["download"])
    original = monkey.download
    monkey.download = lambda url: blob
    try:
        with pytest.raises(RemoteError, match="unrar"):
            fetch_dataset("http://example.com/x.rar")
    finally:
        monkey.download = original


def test_the_samples_catalogue_carries_the_attribution_cc_by_requires(client):
    """The licence permits commercial use *with attribution*.

    So the credit is a condition of shipping these, not a courtesy: an entry
    offering "CC BY 4.0" without saying who to credit does not satisfy the
    licence it names, and a reviewer has no way to tell which study it came from.
    """
    body = client.get("/samples").json()
    samples = body["samples"]

    assert len(samples) >= 20, "the shipped catalogue should not be near-empty"
    for sample in samples:
        assert sample["available"], sample["id"]
        assert sample["url"].startswith("https://"), sample["id"]
        assert sample["licence"].startswith("CC BY"), sample["id"]
        assert sample["collection"], sample["id"]
        assert sample["source"], sample["id"]
        assert sample["modality"], sample["id"]
        # Sizes are read from the catalogue, so none should be missing — and a
        # real one must not round to zero, which reads as a broken entry.
        assert sample["bytes"] and sample["size_mb"] > 0, sample["id"]

    assert body["modalities"] == ["CT", "DX", "MG", "MR", "NM", "PT", "US"]
    # No bucket is set in tests, and the shipped catalogue does not need one.
    assert body["configured"] is False


def test_the_catalogue_refuses_a_non_commercial_licence():
    """The generator is where a CC BY-NC study would have to get past.

    Checked as a unit because it is the one place the mistake is cheap to stop:
    once written into `sample_catalogue.py`, an NC study ships as usable.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "tools" / "refresh_sample_catalogue.py"
    spec = importlib.util.spec_from_file_location("refresh", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    entry = {
        "@id": "https://example.com/samples#nc-study",
        "name": "A study that may not be used commercially",
        "license": "https://creativecommons.org/licenses/by-nc/4.0/",
        "keywords": "CT, Chest",
        "distribution": [
            {
                "@type": "DataDownload",
                "encodingFormat": "application/zip",
                "contentUrl": "https://example.com/x.zip",
                "contentSize": "100",
            }
        ],
    }
    with pytest.raises(SystemExit, match="not an allowed CC BY variant"):
        module.build([entry])


def test_importing_from_a_url_adds_a_normal_source(client, monkeypatch):
    """The end result is an ordinary source: nothing downstream special-cases it."""
    from app import fetch

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("photo-1.jpg", b"\xff\xd8\xff\xe0" + b"a" * 32)
        archive.writestr("nested/photo-2.jpg", b"\xff\xd8\xff\xe0" + b"b" * 32)

    monkeypatch.setattr(fetch, "download", lambda url: buffer.getvalue())

    response = client.post(
        "/sources/remote",
        json={
            "url": "https://example.com/demo-set.tar.gz",
            "workspace_id": "ws-1",
            "set_id": "set-1",
            "name": "Demo set",
        },
    )
    assert response.status_code == 201, response.text
    created = response.json()

    assert created["kind"] == "images"
    assert created["name"] == "Demo set"
    assert created["imported_from"] == "https://example.com/demo-set.tar.gz"
    assert created["file_count"] == 2

    listed = client.get("/sources?workspace_id=ws-1&set_id=set-1").json()
    assert [entry["source_id"] for entry in listed] == [created["source_id"]]


def test_a_refused_url_is_a_client_error_with_a_reason(client, monkeypatch):
    from app import fetch

    def refuse(url):
        raise fetch.RemoteError("that address is not public")

    monkeypatch.setattr(fetch, "download", refuse)
    response = client.post(
        "/sources/remote",
        json={"url": "http://10.0.0.1/x", "workspace_id": "w", "set_id": "s"},
    )
    assert response.status_code == 400
    assert "not public" in response.json()["detail"]


def test_the_remote_import_can_be_turned_off(tmp_path, monkeypatch):
    """The route that makes the server act on a caller's URL, refused outright.

    Built as its own app rather than reusing the fixture, because the setting is
    read when the app starts: an app already running when the flag changed would
    still be the one being tested.
    """
    monkeypatch.setenv("BONE_VIEWER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BONE_VIEWER_RECORDINGS_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("BONE_VIEWER_MULTI_TENANT", "0")
    monkeypatch.setenv("BONE_VIEWER_ENABLE_REMOTE_FETCH", "0")
    from app.config import get_settings

    get_settings.cache_clear()

    from app import fetch

    def never(url):
        raise AssertionError("the gate let a fetch through")

    monkeypatch.setattr(fetch, "download", never)

    with TestClient(create_app()) as gated:
        response = gated.post(
            "/sources/remote",
            json={
                # The classic SSRF target, which is the point of the gate.
                "url": "http://169.254.169.254/latest/meta-data/",
                "workspace_id": "w",
                "set_id": "s",
            },
        )

    assert response.status_code == 403, response.text
    assert "BONE_VIEWER_ENABLE_REMOTE_FETCH" in response.json()["detail"]
    get_settings.cache_clear()


# ── telling DICOM from photographs ──────────────────────────────────────────


def test_detection_reports_what_it_saw_not_just_the_answer():
    """The reason is the difference between a fact and a guess."""
    from app.routers.samples import detect

    dicom = detect([(f"{i}.dcm", b"\x00" * 128 + b"DICM" + b"\x00" * 64) for i in range(3)])
    assert dicom.kind == "dicom"
    assert dicom.confident is True
    assert "3 of the first 3" in dicom.reason

    photos = detect([(f"{i}.jpg", b"\xff\xd8\xff\xe0" + b"x" * 40) for i in range(4)])
    assert photos.kind == "images"
    assert photos.confident is True
    assert "JPEG" in photos.reason


def test_a_mixed_archive_is_reported_as_unsure():
    """This is the case the manual override exists for.

    Claiming certainty about a set that is genuinely both would silently pick
    one reading and mis-handle the other half of the files.
    """
    from app.routers.samples import detect

    files = [
        ("a.dcm", b"\x00" * 128 + b"DICM" + b"\x00" * 64),
        ("b.jpg", b"\xff\xd8\xff\xe0" + b"x" * 40),
        ("c.jpg", b"\xff\xd8\xff\xe0" + b"y" * 40),
    ]
    found = detect(files)
    assert found.kind == "images"  # the more common of the two
    assert found.confident is False
    assert "both" in found.reason


def test_an_unrecognisable_archive_says_so(client, monkeypatch):
    """Nothing matched, so the answer is a guess and is labelled as one."""
    from app import fetch
    from app.routers.samples import detect

    found = detect([("notes.txt", b"just some text, no header at all")])
    assert found.kind == "images"
    assert found.confident is False
    assert "none of the first 1 files is DICOM" in found.reason

    # And the same verdict travels back through the endpoint.
    monkeypatch.setattr(fetch, "download", lambda url: b"just some text")
    response = client.post(
        "/sources/remote",
        json={"url": "https://example.com/mystery.zip", "workspace_id": "w", "set_id": "s"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["detected"] == "images"
    assert body["detected_confident"] is False
    assert body["kind_overridden"] is False


def test_an_explicit_kind_beats_detection_and_is_recorded_as_such(client, monkeypatch):
    """The person importing knows what they put in the archive."""
    from app import fetch

    payload = b"\x00" * 128 + b"DICM" + b"\x00" * 64
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("scan.dcm", payload)
    monkeypatch.setattr(fetch, "download", lambda url: buffer.getvalue())

    response = client.post(
        "/sources/remote",
        json={
            "url": "https://example.com/scan.zip",
            "workspace_id": "w",
            "set_id": "s",
            # Detection would say dicom; the caller says photographs. The caller
            # wins, and the response says the two disagreed.
            "kind": "images",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["detected"] == "dicom"
    assert body["kind_used"] == "images"
    assert body["kind_overridden"] is True
    assert body["imported_from"] == "https://example.com/scan.zip"


def test_dicom_that_will_not_assemble_stays_dicom(client, monkeypatch):
    """A three-view chest X-ray is DICOM and has no volume to assemble.

    The fallback to images keys off `headerless` — whether any file parsed as
    DICOM — and not off `renderable`. Keying it off `renderable` meant every
    single-frame study the catalogue offers was quietly reclassified as
    photographs, losing the DICOM header and the modality on the way.
    """
    from app import fetch

    # A real header, not just a preamble: `headerless` is about whether the
    # files *parse*, and a preamble with no tags parses as nothing.
    payload = _dicom_bytes(
        [
            (0x0008, 0x0060, b"CS", b"DX"),
            (0x0028, 0x0010, b"US", struct.pack("<H", 512)),
            (0x0028, 0x0011, b"US", struct.pack("<H", 512)),
        ]
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("pa.dcm", payload)
    monkeypatch.setattr(fetch, "download", lambda url: buffer.getvalue())

    response = client.post(
        "/sources/remote",
        json={"url": "https://example.com/one.zip", "workspace_id": "w", "set_id": "s"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["detected"] == "dicom"
    assert body["kind"] == "dicom"
    assert body["kind_used"] == "dicom"
    assert body["kind_overridden"] is False
    # The header survived, which is the thing the old fallback destroyed.
    assert body["modality"] == "DX"
    assert body["headerless"] is False


def test_headers_that_are_not_dicom_at_all_fall_through_to_images(client, monkeypatch):
    """The case the fallback is actually for: the detection was simply wrong.

    Nothing here is DICOM, so reading it as DICOM got nowhere — and the honest
    answer is that this is the other kind of source rather than an error.
    """
    from app import fetch

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("photo.jpg", b"\xff\xd8\xff\xe0" + b"x" * 64)
    monkeypatch.setattr(fetch, "download", lambda url: buffer.getvalue())

    response = client.post(
        "/sources/remote",
        json={
            "url": "https://example.com/photos.zip",
            "workspace_id": "w",
            "set_id": "s",
            # Detection would say images; the caller insists on DICOM, and is
            # wrong. The fallback corrects it rather than failing the import.
            "kind": "dicom",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["detected"] == "images"
    assert body["kind_used"] == "images"
    assert body["kind_overridden"] is True


def _implicit_vr_bytes(tags: list[tuple[int, int, bytes]]) -> bytes:
    """A minimal Part 10 file whose dataset is Implicit VR Little Endian."""
    out = bytearray(b"\x00" * 128 + b"DICM")

    def element(group: int, elem: int, vr: bytes, value: bytes) -> None:
        out.extend(struct.pack("<HH", group, elem) + vr)
        if vr in (b"OB", b"OW", b"SQ", b"UN", b"UT"):
            out.extend(b"\x00\x00" + struct.pack("<I", len(value)))
        else:
            out.extend(struct.pack("<H", len(value)))
        out.extend(value)

    # The meta group is explicit VR whatever the dataset is, and names the
    # transfer syntax the dataset uses.
    element(0x0002, 0x0010, b"UI", b"1.2.840.10008.1.2\x00")

    # From here: no VR, just element then a 4-byte length.
    for group, elem, value in tags:
        out.extend(struct.pack("<HH", group, elem) + struct.pack("<I", len(value)) + value)
    return bytes(out)


def test_implicit_vr_files_are_read_rather_than_mistaken_for_photographs():
    """The encoding that was silently costing whole collections.

    Implicit VR stores no VR field, and the reader used to bail on it — so every
    file came back None, which the importer read as "not DICOM at all". A real
    251-slice chest CT became a folder of photographs. The LIDC-IDRI studies are
    encoded this way, which is how it was found.
    """
    data = _implicit_vr_bytes(
        [
            (0x0008, 0x0060, b"CT"),
            (0x0020, 0x000E, b"1.2.3.4.5"),
            (0x0028, 0x0010, struct.pack("<H", 512)),
            (0x0028, 0x0011, struct.pack("<H", 512)),
        ]
    )
    header = parse_file(data)

    assert header is not None, "an implicit-VR file must not read as nothing"
    assert header.modality == "CT"
    assert header.series_uid == "1.2.3.4.5"
    # Rows and Columns are binary US, not ASCII. Read as text they would still
    # produce a number, which is why this is asserted rather than assumed.
    assert header.rows == 512
    assert header.columns == 512
    assert header.transfer_syntax == "1.2.840.10008.1.2"


def test_an_unreadable_transfer_syntax_is_refused_not_guessed():
    """Big endian and deflated are refused rather than read with the wrong rules.

    A header decoded with the wrong byte order is wrong in a way that still looks
    like a header, which is worse than no header at all.
    """
    for uid in (b"1.2.840.10008.1.2.2\x00", b"1.2.840.10008.1.2.1.99\x00"):
        out = bytearray(b"\x00" * 128 + b"DICM")
        out.extend(struct.pack("<HH", 0x0002, 0x0010) + b"UI" + struct.pack("<H", len(uid)) + uid)
        out.extend(struct.pack("<HH", 0x0008, 0x0060) + struct.pack("<I", 2) + b"CT")
        assert parse_file(bytes(out)) is None, uid


def test_an_import_that_falls_back_leaves_no_orphan_source(client, monkeypatch):
    """One import, one source.

    `create_dicom` persists the record before the fallback can inspect it, so the
    abandoned attempt has to be removed — otherwise the list shows two sources
    for one import and the extra one is the one nobody was told about.
    """
    from app import fetch

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("photo.jpg", b"\xff\xd8\xff\xe0" + b"x" * 64)
    monkeypatch.setattr(fetch, "download", lambda url: buffer.getvalue())

    response = client.post(
        "/sources/remote",
        json={
            "url": "https://example.com/photos.zip",
            "workspace_id": "w",
            "set_id": "s",
            "kind": "dicom",  # wrong on purpose, so the fallback runs
        },
    )
    assert response.status_code == 201, response.text

    listed = client.get("/sources?workspace_id=w&set_id=s").json()
    assert len(listed) == 1, [entry["name"] for entry in listed]
    assert listed[0]["source_id"] == response.json()["source_id"]


# ── does the segmentation land on the volume it came from? ─────────────────


def _grid(dims, spacing, origin, direction=None) -> dict:
    grid = {"dims": dims, "spacing": spacing, "origin": origin}
    if direction is not None:
        grid["direction"] = direction
    return grid


def test_a_mask_on_the_same_field_of_view_agrees():
    """The normal case: the segmenter resamples, but to the same extent."""
    from app.pipeline.isosurface import placement

    volume = _grid([256, 256, 51], [1.3671875, 1.3671875, 10.0], [-179.658, -325.658, -754.3])
    mask = _grid(
        [512, 512, 101],
        [0.68359375, 0.68359375, 5.0],
        [-179.658203125, 23.658203125, -754.2999877929688],
        # The RAS/LPS swap flips y, which puts the origin at the opposite corner
        # of the *same* box. Origins disagree wildly; the geometry does not.
        [1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, 1.0],
    )

    report = placement(mask, volume)
    assert report["agrees"] is True
    assert report["overlap_pct"] > 99
    # The centres are nearly the same point, which is the thing that would move
    # if the mask were mirrored rather than merely re-origined.
    assert max(abs(v) for v in report["centre_offset_mm"]) < 2.0


def test_a_mirrored_mask_is_caught():
    """The failure the check exists for: right shape, wrong place.

    Every number inside the mask is self-consistent here — same dims, same
    spacing, a plausible origin. Nothing about the mask alone says it is wrong.
    Only comparing its world box to the volume's does.
    """
    from app.pipeline.isosurface import placement

    volume = _grid([256, 256, 51], [1.3671875, 1.3671875, 10.0], [-179.658, -325.658, -754.3])
    # Same size and spacing, translated far away along x.
    mask = _grid([512, 512, 101], [0.68359375, 0.68359375, 5.0], [900.0, -325.0, -754.3])

    report = placement(mask, volume)
    assert report["agrees"] is False
    assert report["overlap_pct"] == 0.0
    assert abs(report["centre_offset_mm"][0]) > 500


def test_a_single_flipped_axis_is_caught_and_is_diagnosable():
    """One axis disagreeing is the signature of a flip, not of a translation."""
    from app.pipeline.isosurface import placement

    volume = _grid([100, 100, 100], [1.0, 1.0, 1.0], [-50.0, -50.0, -50.0])
    # z negated: the box now runs the other way from the same origin.
    flipped = _grid(
        [100, 100, 100],
        [1.0, 1.0, 1.0],
        [-50.0, -50.0, -50.0],
        [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, -1.0],
    )

    report = placement(flipped, volume)
    assert report["agrees"] is False
    assert report["overlap_pct"] == 0.0
    # Bounds are [xmin, xmax, ymin, ymax, zmin, zmax], the same convention as
    # `geometry.bounds`. x and y still line up; z does not — which is what
    # points at the axis responsible.
    assert report["mask_bounds"][0:4] == report["volume_bounds"][0:4]
    assert report["mask_bounds"][4:6] != report["volume_bounds"][4:6]


def test_the_segment_result_records_where_the_mask_landed(client, monkeypatch):
    """On the result, next to the geometry, on every run.

    Not a log line and not a manual check: when this is wrong the result looks
    entirely well-formed, so the comparison has to be part of the result.
    """
    use_stub_segmenter(monkeypatch)
    source = upload_blocky_series(client, slices=6)
    job = run_segment(client, source["source_id"])

    assert job["status"] == "succeeded", job["error"]
    geometry = job["result"]["geometry"]
    report = geometry["mask_placement"]

    assert report["agrees"] is True
    assert report["overlap_pct"] > 90
    assert len(report["mask_bounds"]) == 6
    assert len(report["volume_bounds"]) == 6
    # Bounds are [xmin, xmax, ymin, ymax, zmin, zmax], the same convention
    # `geometry.bounds` uses. The stub writes a mask on the volume's own grid,
    # so the two boxes should coincide rather than merely overlap.
    for axis, (mask_low, mask_high, vol_low, vol_high) in enumerate(
        zip(
            report["mask_bounds"][0::2],
            report["mask_bounds"][1::2],
            report["volume_bounds"][0::2],
            report["volume_bounds"][1::2],
            strict=True,
        )
    ):
        assert abs(mask_low - vol_low) < 1.0, axis
        assert abs(mask_high - vol_high) < 1.0, axis

    assert not any("disagree about where space is" in w for w in job["result"]["warnings"])


def test_a_dicom_segment_envelope_matches_the_contract(client, monkeypatch):
    """The volumetric path is the one that writes mask_placement.

    The contract test in test_solvers.py runs captures with no source, which
    take the synthetic path — so it never saw a `segment` result carrying the
    placement check, and the schema's `additionalProperties: false` would not
    have caught the field being undeclared.
    """
    import jsonschema

    use_stub_segmenter(monkeypatch)
    source = upload_blocky_series(client, slices=6)
    job = run_segment(client, source["source_id"])
    assert job["status"] == "succeeded", job["error"]

    schema = json.loads(
        (Path(__file__).resolve().parents[3] / "contracts" / "results.schema.json").read_text(
            encoding="utf-8"
        )
    )
    errors = sorted(
        jsonschema.Draft202012Validator(schema).iter_errors(job["result"]),
        key=lambda error: list(error.path),
    )
    assert not errors, "; ".join(
        f"{'/'.join(str(p) for p in e.path)}: {e.message}" for e in errors
    )
    assert job["result"]["geometry"]["mask_placement"] is not None
