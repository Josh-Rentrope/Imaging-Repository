"""Shared fixtures.

The app is built per test against a fresh data directory, so a test that writes
a job, a source or an artifact cannot be affected by one that ran before it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app


@pytest.fixture(autouse=True)
def single_viewer(monkeypatch):
    """Pin the pre-tenancy behaviour for the suite that predates it.

    A test client is neither loopback nor cookie-bearing, so with tenancy on each
    new client would mint its own viewer. That is correct behaviour, and it would
    quietly change what a hundred existing assertions are looking at, so the
    tests written before viewers existed keep the flat namespace they assume.

    Autouse, so it is in place before any fixture that builds an app. The
    fixtures below that want tenancy set it explicitly afterwards.
    """
    monkeypatch.setenv("BONE_VIEWER_MULTI_TENANT", "0")


def _over(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("BONE_VIEWER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BONE_VIEWER_RECORDINGS_DIR", str(tmp_path / "recordings"))
    monkeypatch.delenv("BONE_VIEWER_ENABLE_DIAGNOSTIC", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return create_app()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """The flat namespace: everything in one place, as before viewers existed."""
    with TestClient(_over(tmp_path, monkeypatch, BONE_VIEWER_MULTI_TENANT="0")) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture()
def viewer(tmp_path, monkeypatch):
    """A client that is not on loopback, so its cookie is what decides the viewer.

    The loopback shortcut is turned off deliberately: a test client should
    exercise the path a real remote viewer takes, since that is the one the
    deployment uses.
    """
    app = _over(
        tmp_path, monkeypatch, BONE_VIEWER_MULTI_TENANT="1", BONE_VIEWER_LOOPBACK_IS_LOCAL="0"
    )
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture()
def viewers(tmp_path, monkeypatch):
    """A factory for clients that share one data directory and not one cookie.

    Two of these are two browsers: the same deployment, the same store, separate
    cookies. That is the situation viewer separation has to hold up under, and it
    cannot be expressed with a single client.
    """
    monkeypatch.setenv("BONE_VIEWER_MULTI_TENANT", "1")
    monkeypatch.setenv("BONE_VIEWER_LOOPBACK_IS_LOCAL", "0")
    app = _over(tmp_path, monkeypatch)

    opened: list[TestClient] = []

    def make() -> TestClient:
        client = TestClient(app)
        client.__enter__()
        opened.append(client)
        return client

    try:
        yield make
    finally:
        for client in opened:
            client.__exit__(None, None, None)
        get_settings.cache_clear()


@pytest.fixture()
def loopback(tmp_path, monkeypatch):
    """A client whose peer address is loopback, as a local `uvicorn` run is.

    `TestClient` reports `testclient` as the peer by default, which is neither
    loopback nor resolvable, so the address is supplied to make the local case
    testable at all.
    """
    app = _over(
        tmp_path, monkeypatch, BONE_VIEWER_MULTI_TENANT="1", BONE_VIEWER_LOOPBACK_IS_LOCAL="1"
    )
    with TestClient(app, client=("127.0.0.1", 50000)) as test_client:
        yield test_client
    get_settings.cache_clear()
