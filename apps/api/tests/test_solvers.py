"""Camera-pose solvers, and the result contract they write into."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from helpers import RGB_NO_FIDUCIAL, RGBD_CAPTURE, run

from app.pipeline import solvers


def photo_capture(**overrides) -> dict:
    capture = {"capture_id": "c-photos", "modality": "rgb", "frames": [{"frame_id": 0}]}
    capture.update(overrides)
    return capture


class StandIn:
    """A solver that solves nothing, for exercising the seam.

    Shaped like the real thing: the interface is the contract a private
    implementation satisfies, so a stand-in that does not satisfy it is not a
    useful test of the seam.
    """

    def __init__(self, name: str, tool: str, licence: str, **outcome) -> None:
        self.info = solvers.SolverInfo(
            name=name,
            tool=tool,
            description="Solves camera positions from a set of photographs.",
            algorithm="structure-from-motion",
            accepts=frozenset({solvers.InputShape.UNORDERED}),
            licence=licence,
        )
        self._outcome = outcome

    def available(self):
        return True, None

    def solve(self, frames, params):
        return solvers.PoseOutcome(
            poses=[{"frame_id": frame["frame_id"]} for frame in frames], **self._outcome
        )


@pytest.fixture()
def installed(monkeypatch):
    """Register a stand-in solver, and take it back out afterwards."""

    def _install(solver: StandIn) -> StandIn:
        monkeypatch.setitem(solvers._REGISTERED, solver.info.name, solver)
        monkeypatch.setitem(solvers.CATALOGUE, solver.info.name, solver.info)
        return solver

    return _install


# ── what this deployment reports ────────────────────────────────────────────


def test_solvers_are_reported_with_what_they_accept(client):
    """A client can only offer a choice it knows works here."""
    payload = client.get("/solvers").json()
    by_name = {row["name"]: row for row in payload["solvers"]}

    assert {"colmap", "orbslam2", "openmvg", "hloc"} <= set(by_name)
    # Nothing is installed in this checkout, and saying so is the point: an
    # availability list that assumes the tools exist offers buttons that fail.
    assert payload["available"] == []
    assert all(not row["available"] for row in by_name.values())

    # The input shape is the load-bearing distinction between the two families.
    assert "unordered_images" in by_name["colmap"]["accepts"]
    assert by_name["orbslam2"]["accepts"] == ["image_sequence"]
    assert by_name["orbslam2"]["licence"] == "GPLv3"
    assert by_name["colmap"]["licence"] == "BSD-3-Clause"


def test_every_catalogue_entry_is_described_in_one_sentence():
    """These strings are shown to a reader; a fragment is not a description."""
    for name, info in solvers.CATALOGUE.items():
        assert info.description.endswith("."), name
        assert info.description[0].isupper(), name
        assert info.accepts, name
        # A solver with no stated licence is the one that gets shipped by accident.
        assert info.licence, name


def test_the_two_families_take_different_input(client):
    """COLMAP and ORB-SLAM2 are not interchangeable, and the survey says so."""
    by_name = {row["name"]: row for row in client.get("/solvers").json()["solvers"]}
    assert solvers.InputShape.SEQUENCE in solvers.CATALOGUE["orbslam2"].accepts
    assert solvers.InputShape.UNORDERED not in solvers.CATALOGUE["orbslam2"].accepts
    assert solvers.InputShape.UNORDERED in solvers.CATALOGUE["colmap"].accepts
    assert by_name["openmvg"]["accepts"] == ["image_sequence", "unordered_images"]


def test_an_unavailable_solver_can_be_asked_about_without_running_it(client):
    """The survey answers, and answers without invoking anything."""
    payload = client.get("/solvers").json()
    assert payload["shapes"]
    assert {shape["name"] for shape in payload["shapes"]} == {
        "unordered_images",
        "image_sequence",
        "device_poses",
    }
    for shape in payload["shapes"]:
        assert shape["description"].endswith(".")


# ── refusing rather than substituting ───────────────────────────────────────


def test_a_named_solver_that_is_not_installed_fails_rather_than_falling_back(client):
    """Silently using a different solver is a wrong answer that looks right."""
    job = run(client, [{"op": "estimate_poses", "params": {"solver": "colmap"}}], photo_capture())
    assert job["status"] == "failed"
    assert "not installed" in job["error"]
    assert "COLMAP" in job["error"]


def test_a_sequence_solver_is_refused_an_unordered_set(client):
    """ORB-SLAM2 tracks between consecutive frames; a shuffled folder is not that."""
    job = run(client, [{"op": "estimate_poses", "params": {"solver": "orbslam2"}}], photo_capture())
    assert job["status"] == "failed"
    assert "does not accept" in job["error"]
    assert "InputShape" not in job["error"]
    assert "sequence of overlapping frames" in job["error"]


def test_an_unknown_solver_names_the_ones_that_exist(client):
    job = run(client, [{"op": "estimate_poses", "params": {"solver": "meshroom"}}], photo_capture())
    assert job["status"] == "failed"
    assert "not a known pose solver" in job["error"]
    assert "colmap" in job["error"]


def test_an_empty_solver_name_is_treated_as_absent(client):
    """A form that submits an empty string means "no preference", not "none"."""
    job = run(client, [{"op": "estimate_poses", "params": {"solver": ""}}], photo_capture())
    assert job["status"] == "succeeded"
    assert job["result"]["geometry"]["pose_source"] == "placeholder"


# ── the two honest paths, and the one that is neither ───────────────────────


def test_device_poses_are_never_re_solved(client):
    """A phone that knows where it was does not pay for structure-from-motion."""
    job = run(
        client,
        [{"op": "estimate_poses"}],
        {
            "capture_id": "c-device",
            "modality": "rgbd",
            "frames": [{"frame_id": 0, "pose": {"t": [0, 0, 0]}}],
        },
    )
    assert job["status"] == "succeeded"
    geometry = job["result"]["geometry"]
    assert geometry["pose_source"] == "device"
    assert geometry["poses_solved"] == 0
    assert geometry["scale_known"] is True
    # No framework ran, so none is credited with the trajectory.
    assert "pose_solver" not in geometry


def test_photos_with_no_solver_say_placeholder_rather_than_claiming_a_solve(client):
    """`poses_solved: N` with nothing behind it is the number that gets quoted."""
    job = run(
        client,
        [{"op": "estimate_poses"}],
        {"capture_id": "c-none", "modality": "rgb", "frames": [{"frame_id": 0}, {"frame_id": 1}]},
    )
    assert job["status"] == "succeeded"
    geometry = job["result"]["geometry"]
    assert geometry["pose_source"] == "placeholder"
    assert geometry["poses_solved"] == 0
    assert geometry["scale_known"] is False
    assert any("No camera-pose solver is installed" in w for w in job["result"]["warnings"])


# ── the seam a private implementation plugs into ────────────────────────────


def test_a_registered_solver_runs_and_is_credited(client, installed):
    installed(StandIn("colmap", "COLMAP", "BSD-3-Clause", residual_px=0.71))

    job = run(
        client,
        [{"op": "estimate_poses", "params": {"solver": "colmap"}}],
        {"capture_id": "c-solved", "modality": "rgb", "frames": [{"frame_id": 0}, {"frame_id": 1}]},
    )
    assert job["status"] == "succeeded", job["error"]
    geometry = job["result"]["geometry"]
    assert geometry["pose_solver"] == "colmap"
    assert geometry["pose_source"] == "sfm"
    assert geometry["poses_solved"] == 2
    assert geometry["reprojection_residual_px"] == 0.71
    # A solved trajectory has scale only up to an unknown factor.
    assert geometry["scale_known"] is False

    # Which framework ran is in the manifest, so two runs are comparable.
    stage = job["result"]["provenance"]["stages"][-1]
    assert stage["tool"] == "COLMAP"
    assert stage["licence"] == "BSD-3-Clause"


def test_an_installed_solver_is_used_when_none_is_named(client, installed):
    installed(StandIn("openmvg", "openMVG", "MPL-2.0"))

    job = run(client, [{"op": "estimate_poses"}], photo_capture())
    assert job["status"] == "succeeded", job["error"]
    assert job["result"]["geometry"]["pose_solver"] == "openmvg"


def test_two_frameworks_over_one_set_are_distinguishable(client, installed):
    """The point of carrying the solver in provenance: two runs are comparable."""
    installed(StandIn("colmap", "COLMAP", "BSD-3-Clause", residual_px=0.71))
    installed(StandIn("openmvg", "openMVG", "MPL-2.0", residual_px=1.4))

    capture = {"capture_id": "c-compare", "modality": "rgb", "frames": [{"frame_id": 0}]}
    first = run(client, [{"op": "estimate_poses", "params": {"solver": "colmap"}}], capture)
    second = run(client, [{"op": "estimate_poses", "params": {"solver": "openmvg"}}], capture)

    assert first["result"]["geometry"]["pose_solver"] == "colmap"
    assert second["result"]["geometry"]["pose_solver"] == "openmvg"
    assert first["result"]["geometry"]["reprojection_residual_px"] != (
        second["result"]["geometry"]["reprojection_residual_px"]
    )


def test_a_solver_that_claims_to_be_available_and_then_is_not(client, installed, monkeypatch):
    """`available()` is advisory; a solver that fails anyway fails the job."""
    solver = installed(StandIn("hloc", "hloc", "Apache-2.0"))

    def explode(frames, params):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(solver, "solve", explode)
    job = run(client, [{"op": "estimate_poses", "params": {"solver": "hloc"}}], photo_capture())

    assert job["status"] == "failed"
    assert "CUDA out of memory" in job["error"]


def test_discovery_of_a_broken_entry_point_does_not_stop_a_run(monkeypatch):
    """A solver that will not import is unavailable, not fatal."""
    class Broken:
        def load(self):
            raise ImportError("no torch")

    monkeypatch.setattr(
        solvers, "entry_points", lambda group: [Broken()], raising=False
    )
    assert solvers.load_registered() == []


# ── the contract is checked, not assumed ────────────────────────────────────


def test_every_produced_envelope_matches_the_result_contract(client):
    """models.py claims envelopes are validated against the schema. It was not.

    The drift this hid was real: `estimate_poses` wrote five fields into
    `geometry` while the schema set `additionalProperties: false` and listed
    none of them, so the documented contract and the emitted one had quietly
    stopped agreeing.
    """
    schema = json.loads(
        (Path(__file__).resolve().parents[3] / "contracts" / "results.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = jsonschema.Draft202012Validator(schema)

    captures = [
        ("rgbd", RGBD_CAPTURE),
        ("rgb", RGB_NO_FIDUCIAL),
        # A capture with its own poses, so the `device` branch is covered too.
        (
            "device poses",
            {"capture_id": "c-1", "modality": "rgbd", "frames": [{"frame_id": 0, "pose": {}}]},
        ),
    ]
    workflows = [
        [{"op": "rectify"}, {"op": "reconstruct"}, {"op": "segment"}, {"op": "measure"}],
        [{"op": "estimate_poses"}],
        [{"op": "px2tooth"}],
    ]

    checked = 0
    for label, capture in captures:
        for stages in workflows:
            job = run(client, stages, capture)
            if job["status"] != "succeeded":
                continue
            errors = sorted(validator.iter_errors(job["result"]), key=lambda e: list(e.path))
            assert not errors, (
                f"{label} / {[stage['op'] for stage in stages]}: "
                + "; ".join(f"{'/'.join(str(p) for p in e.path)}: {e.message}" for e in errors)
            )
            checked += 1

    assert checked >= 3, "the contract test did not actually run any workflow"
