"""Shared fixtures.

Every test gets a pristine environment (no ambient ``FP_*`` leaking in) and a
``Settings`` pointed at a temp data dir, so nothing ever touches ``/data``.
"""

from __future__ import annotations

import os

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.storage.db import Database


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
        # TestClient apps must not grab ports; the artifact origin is tested
        # directly (ASGI-level, and once over real TCP) instead.
        run_artifact_server=False,
    )


@pytest.fixture
def bare_settings(settings: Settings) -> Settings:
    """Same as ``settings`` but with seeding off, for isolated repository tests."""
    return replace(settings, seed_on_start=False)


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.db_path, busy_timeout_ms=settings.db_idle_timeout_ms)
    # Not "migrate() returned something": a TestClient in the same test may well
    # have migrated this file first, and which fixture runs first depends on the
    # order of the test's arguments. What matters is that the schema is there.
    database.migrate()
    assert database.version() >= 1
    return database


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def bare_client(bare_settings: Settings):
    """Client with no seeded content, so counts are meaningful."""
    with TestClient(create_app(bare_settings)) as test_client:
        yield test_client
