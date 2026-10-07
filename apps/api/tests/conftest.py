"""Shared fixtures.

The app is built per test against a fresh data directory, so a test that writes
a job, a source or an artifact cannot be affected by one that ran before it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


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
