"""Backend selection: who answers a job, and what happens when the private core
is meant to be here and is not.

The failure this guards against is not a crash. It is a job that asked for the
private backend, quietly ran against the fixture instead, and came back with a
mesh and measurements in the envelope. Nothing fails, nothing is logged, and the
geometry is synthetic.
"""

from __future__ import annotations

from importlib.metadata import EntryPoint

import pytest
from fastapi.testclient import TestClient
from helpers import RGBD_CAPTURE, run

from app.config import get_settings
from app.main import create_app
from app.pipeline import BackendRegistry, BackendUnavailable
from app.pipeline import server as server_module
from app.storage import LocalStorage

#: A module that does not exist. Loading this entry point raises for real, which
#: is a truer broken core than any hand-rolled failure would be.
BROKEN = "a_core_that_is_not_installed:Backend"


def _published(name: str, value: str) -> EntryPoint:
    """A real entry point pointed at a module that is not installed.

    The real class rather than a double: it is what `importlib.metadata` hands the
    loader, so a change to what the loader reads off it fails here instead of in
    production.
    """
    return EntryPoint(name=name, value=value, group=server_module.ENTRY_POINT_GROUP)


@pytest.fixture()
def broken_core(monkeypatch):
    """Publish a server backend that will not load, to anything that builds one."""
    monkeypatch.setattr(
        server_module,
        "entry_points",
        lambda group: [_published("server", BROKEN)],
        raising=False,
    )


@pytest.fixture()
def broken_core_client(tmp_path, monkeypatch, broken_core):
    """An app built while the private core is published and will not load.

    Built here rather than taken from `conftest` because the patch has to be in
    place *before* the lifespan constructs the registry -- patching afterwards
    would test nothing.
    """
    monkeypatch.setenv("BONE_VIEWER_MULTI_TENANT", "0")
    monkeypatch.setenv("BONE_VIEWER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BONE_VIEWER_RECORDINGS_DIR", str(tmp_path / "recordings"))
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


# ── absent and broken are different answers ─────────────────────────────────


def test_with_nothing_published_the_core_is_absent_and_that_is_not_a_fault(tmp_path):
    """A checkout of this repo has no private core. It says so and says nothing more."""
    registry = BackendRegistry(storage=LocalStorage(tmp_path))

    assert registry.has_server_backend() is False
    assert registry.server_backend_error() is None


def test_a_private_core_that_will_not_load_keeps_its_reason(tmp_path, broken_core):
    registry = BackendRegistry(storage=LocalStorage(tmp_path))

    assert registry.has_server_backend() is False
    detail = registry.server_backend_error()
    assert detail is not None
    assert "ModuleNotFoundError" in detail


# ── a named backend may not be substituted ──────────────────────────────────


def test_naming_the_absent_backend_refuses_instead_of_falling_back(tmp_path):
    """Auto-mode falling back is right. A named backend falling back is not.

    The fixture implements the pipeline's own shapes, so a job routed to it by
    accident comes back looking like the thing that was asked for.
    """
    registry = BackendRegistry(storage=LocalStorage(tmp_path))

    with pytest.raises(BackendUnavailable) as raised:
        registry.resolve(["reconstruct"], preferred="server")

    assert "server" in str(raised.value)


def test_naming_the_broken_backend_says_why_it_is_broken(tmp_path, broken_core):
    registry = BackendRegistry(storage=LocalStorage(tmp_path))

    with pytest.raises(BackendUnavailable) as raised:
        registry.resolve(["reconstruct"], preferred="server")

    # The reason, not just the absence. Someone who installed the core needs to
    # know it is present and failing, which is a different search from "install
    # the core".
    assert "ModuleNotFoundError" in str(raised.value)


def test_auto_mode_still_falls_back_to_the_fixture(tmp_path):
    """Kept on purpose: absent is the normal case and the app has to work anyway."""
    registry = BackendRegistry(storage=LocalStorage(tmp_path))

    backend = registry.resolve(["reconstruct"])

    assert backend.capabilities().backend == "fixture"


def test_a_named_backend_that_lacks_the_operation_is_refused_too(tmp_path):
    """The same rule one step further in: registered, but not for this job.

    Falling through here would be the same substitution by a different door.
    """
    registry = BackendRegistry(storage=LocalStorage(tmp_path))

    with pytest.raises(Exception) as raised:
        registry.resolve(["an_operation_nobody_implements"], preferred="fixture")

    assert "an_operation_nobody_implements" in str(raised.value)


# ── end to end, because a registry test alone would not catch a wiring slip ──


def test_a_job_naming_a_broken_backend_fails_rather_than_succeeding(broken_core_client):
    """The failure mode being guarded against is a *success*.

    Status `succeeded`, a mesh, measurements, all of it from the fixture, with
    nothing anywhere saying the answer came from a stand-in.
    """
    job = run(broken_core_client, [{"op": "rectify"}], RGBD_CAPTURE, backend="server")

    assert job["status"] == "failed"
    assert "not available" in job["error"]


def test_the_same_job_without_naming_a_backend_still_succeeds(client):
    """Without this, the test above would pass just as well if the job had failed
    for some unrelated reason -- it would be asserting nothing about fallback.
    """
    job = run(client, [{"op": "rectify"}], RGBD_CAPTURE)

    assert job["status"] == "succeeded"
    assert job["backend"] == "fixture"


# ── what the deployment reports about itself ────────────────────────────────


def test_capabilities_is_silent_about_a_core_that_was_never_here(client):
    payload = client.get("/capabilities").json()

    assert payload["server_backend_registered"] is False
    assert payload["server_backend_error"] is None


def test_capabilities_reports_a_broken_core(broken_core_client):
    payload = broken_core_client.get("/capabilities").json()

    assert payload["server_backend_registered"] is False
    assert "ModuleNotFoundError" in payload["server_backend_error"]


def test_diagnostics_reports_a_broken_core(broken_core_client):
    payload = broken_core_client.get("/diagnostics").json()

    assert payload["loaded"]["server_backend_error"] is not None


def test_diagnostics_reports_a_core_that_was_never_here(client):
    payload = client.get("/diagnostics").json()

    assert payload["loaded"]["server_backend_registered"] is False
    assert payload["loaded"]["server_backend_error"] is None
    # Both extension points are on the diagnostics response, because both can be
    # broken in a way that still looks like a working deployment.
    assert payload["solvers"]["available"] == []
    assert payload["solvers"]["load_failures"] == {}
