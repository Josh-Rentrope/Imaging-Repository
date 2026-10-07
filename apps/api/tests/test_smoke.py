"""Smoke tests.

Concentrated on behaviour that fails quietly rather than loudly: scale handling,
the diagnostic gate, and error paths that could otherwise return a plausible
wrong answer.
"""

from __future__ import annotations

import io
import json
import struct
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydicom.dataset import Dataset, FileMetaDataset
from pathlib import Path

from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from app.dicom import parse_file
from app.main import create_app

RGBD_CAPTURE = {
    "capture_id": "11111111-1111-1111-1111-111111111111",
    "modality": "rgbd",
    "depth": [{"frame_id": 0, "depth_ref": "depth/0.bin", "scale_factor": 0.001}],
}

RGB_NO_FIDUCIAL = {
    "capture_id": "22222222-2222-2222-2222-222222222222",
    "modality": "rgb",
}

FULL_PIPELINE = [
    {"op": "rectify"},
    {"op": "reconstruct"},
    {"op": "segment"},
    {"op": "measure"},
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BONE_VIEWER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BONE_VIEWER_RECORDINGS_DIR", str(tmp_path / "recordings"))
    monkeypatch.delenv("BONE_VIEWER_ENABLE_DIAGNOSTIC", raising=False)
    from app.config import get_settings

    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


# ── pipeline ────────────────────────────────────────────────────────────────


def run(client: TestClient, stages: list[dict], capture: dict | None = None, **extra) -> dict:
    response = client.post("/jobs", json={"stages": stages, "capture": capture, **extra})
    assert response.status_code == 201, response.text
    return response.json()


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
    job = response.json()
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
    job = client.post(
        "/jobs",
        json={"stages": [{"op": "iso_surface", "params": {"threshold": 500}}],
              "capture": {"source_id": source["source_id"]}},
    ).json()
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
    job = client.post(
        "/jobs",
        json={"stages": [{"op": "iso_surface", "params": {"threshold": 99999}}],
              "capture": {"source_id": source["source_id"]}},
    ).json()
    assert job["status"] == "failed"
    assert "outside the volume" in (job["error"] or "")


def test_iso_surface_without_a_source_fails_cleanly(client):
    job = client.post("/jobs", json={"stages": [{"op": "iso_surface"}], "capture": {}}).json()
    assert job["status"] == "failed"
    assert "source_id" in (job["error"] or "")


def test_iso_surface_rejects_a_single_slice_source(client):
    """A 1-voxel-thick volume has no interior to extract a surface from.

    skimage's own error for this says nothing about slice counts, so the guard
    has to name the real problem.
    """
    series_uid = generate_uid()
    source = upload_dicom(client, [("only.dcm", make_slice(0, series_uid, block=True))]).json()
    job = client.post(
        "/jobs",
        json={"stages": [{"op": "iso_surface", "params": {"threshold": 500}}],
              "capture": {"source_id": source["source_id"]}},
    ).json()
    assert job["status"] == "failed"
    assert "at least 2 along every axis" in (job["error"] or "")


def test_iso_surface_names_a_missing_source(client):
    job = client.post(
        "/jobs",
        json={"stages": [{"op": "iso_surface", "params": {"threshold": 500}}],
              "capture": {"source_id": "11111111-2222-3333-4444-555555555555"}},
    ).json()
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
    return client.post(
        "/jobs",
        json={
            "stages": [{"op": "segment", "params": params}],
            "capture": {"source_id": source_id},
        },
    ).json()


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
    job = client.post(
        "/jobs",
        json={"stages": [{"op": "segment"}], "capture": {"source_id": source["source_id"]}},
    ).json()
    assert job["status"] == "succeeded", job["error"]
    assert job["result"]["segmentation"]["instances"]


# ── labelled surfaces (segment -> iso_surface in one job) ───────────────────


def run_labelled_surface(client, monkeypatch, **params):
    use_stub_segmenter(monkeypatch)
    source = upload_blocky_series(client, slices=9, rows=24, cols=24)
    return client.post(
        "/jobs",
        json={
            "stages": [{"op": "segment"}, {"op": "iso_surface", "params": params}],
            "capture": {"source_id": source["source_id"]},
        },
    ).json()


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


def test_unknown_label_names_the_available_ones(client, monkeypatch):
    job = run_labelled_surface(client, monkeypatch, label="rib_right_9")
    assert job["status"] == "failed"
    error = job["error"] or ""
    assert "rib_right_9" in error and "rib_left_4" in error
