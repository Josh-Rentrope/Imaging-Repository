"""Smoke tests.

Concentrated on behaviour that fails quietly rather than loudly: scale handling,
the diagnostic gate, and error paths that could otherwise return a plausible
wrong answer.
"""

from __future__ import annotations

import struct

import pytest
from fastapi.testclient import TestClient

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


def run(client: TestClient, stages: list[dict], capture: dict | None = None, **extra) -> dict:
    response = client.post(
        "/jobs", json={"stages": stages, "capture": capture, **extra}
    )
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
    assert result["rectification"]["frames_in"] == 0
    assert result["segmentation"] is not None
    assert any(a["kind"] == "mesh" for a in result["artifacts"])
    assert result["measurements"]


def test_stage_order_is_preserved(client):
    job = run(client, [{"op": "segment"}, {"op": "reconstruct"}], RGBD_CAPTURE)
    assert job["result"]["ops"] == ["segment", "reconstruct"]


def test_rgbd_reconstruction_is_metric(client):
    result = run(client, FULL_PIPELINE, RGBD_CAPTURE)["result"]
    assert result["scale"] == {"verified": True, "source": "depth_sensor"}
    mesh = next(a for a in result["artifacts"] if a["kind"] == "mesh")
    assert mesh["units"] == "mm"
    assert result["geometry"]["voxel_size_mm"] > 0


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
    assert result["warnings"]


def test_rgb_with_fiducial_is_metric(client):
    capture = {**RGB_NO_FIDUCIAL, "quality": {"scale_reference": {"kind": "aruco", "size_mm": 20}}}
    result = run(client, FULL_PIPELINE, capture)["result"]
    assert result["scale"] == {"verified": True, "source": "fiducial"}
    assert result["measurements"]


def test_diagnostic_op_is_gated(client):
    job = run(client, [{"op": "detect_caries"}])
    assert job["status"] == "rejected"
    assert job["result"] is None


def test_diagnostic_op_needs_server_switch_too(client):
    job = run(client, [{"op": "detect_caries"}], allow_diagnostic=True)
    assert job["status"] == "rejected"


def test_unknown_op_fails_without_a_result(client):
    job = run(client, [{"op": "does_not_exist"}])
    assert job["status"] == "failed"
    assert "does_not_exist" in (job["error"] or "")
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
    response = client.post("/jobs", json={"stages": []})
    assert response.status_code == 422


# ── DICOM header parser ─────────────────────────────────────────────────────


def _dicom_bytes(tags: list[tuple[int, int, bytes, bytes]]) -> bytes:
    """Build a minimal explicit-VR little-endian Part 10 file."""
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
    assert header.series_uid == "1.2.840.113619.2.55.3"
    assert header.rows == 512
    assert header.columns == 512


def test_dicom_parser_rejects_non_dicom():
    assert parse_file(b"this is a jpeg, not a dicom file") is None
    assert parse_file(b"") is None


def test_headerless_upload_is_kept_and_flagged(client):
    response = client.post(
        "/dicom/series",
        files=[("files", ("scan.png", b"\x89PNG\r\n\x1a\nnot-a-dicom", "image/png"))],
    )
    assert response.status_code == 201
    series = response.json()
    assert len(series) == 1
    assert series[0]["headerless"] is True
    assert series[0]["instance_count"] == 1
