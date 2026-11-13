"""Shared fixtures.

Every test gets a pristine environment (no ambient ``FP_*`` leaking in) and a
``Settings`` pointed at a temp data dir, so nothing ever touches ``/data``.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("FP_") or name == "ADMIN_PASSWORD":
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        admin_password="test-admin-password",
        session_secret="0" * 64,
    )


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client
