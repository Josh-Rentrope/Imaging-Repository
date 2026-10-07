"""Job helpers shared by the test modules."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

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


def settle(client: TestClient, job: dict, timeout: float = 60.0) -> dict:
    """Poll a job until it stops being queued or running.

    Submission returns as soon as the job is accepted — the work happens on a
    thread — so every assertion about an outcome has to wait for it.
    """
    deadline = time.monotonic() + timeout
    while job["status"] in ("queued", "running"):
        assert time.monotonic() < deadline, f"job never finished: {job}"
        time.sleep(0.02)
        job = client.get(f"/jobs/{job['job_id']}").json()
    return job


def submit(client: TestClient, stages: list[dict], capture: dict | None = None, **extra) -> dict:
    response = client.post("/jobs", json={"stages": stages, "capture": capture, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def run(client: TestClient, stages: list[dict], capture: dict | None = None, **extra) -> dict:
    return settle(client, submit(client, stages, capture, **extra))
