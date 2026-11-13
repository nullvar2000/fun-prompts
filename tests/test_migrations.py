"""The migration runner and the invariants the schema is trusted to enforce."""

from __future__ import annotations

import sqlite3

import pytest

from app.storage.db import Database


def test_initial_migration_creates_every_table(db: Database) -> None:
    names = {
        row["name"]
        for row in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "schema_version",
        "settings",
        "endpoints",
        "endpoint_tags",
        "models",
        "prompts",
        "builds",
        "build_versions",
        "tags",
        "build_tags",
        "jobs",
        "guard_log",
    } <= names


def test_migrations_are_recorded_and_idempotent(db: Database) -> None:
    assert db.version() == 1
    row = db.query_one("SELECT name FROM schema_version WHERE version = 1")
    assert row["name"] == "init"
    # A second boot must not re-run anything.
    assert db.migrate() == []
    assert db.version() == 1


def test_foreign_keys_are_enforced(db: Database) -> None:
    assert db.query_one("PRAGMA foreign_keys")["foreign_keys"] == 1
    now = "2026-01-01T00:00:00+00:00"
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO builds (prompt_id, endpoint_id, model_row_id, status,
                                latest_version, hidden, created_at, updated_at)
            VALUES (9999, 9999, 9999, 'queued', 0, 0, ?, ?)
            """,
            (now, now),
        )


def test_status_columns_reject_values_the_domain_cannot_produce(db: Database) -> None:
    now = "2026-01-01T00:00:00+00:00"
    db.execute(
        """
        INSERT INTO prompts (slug, title, body, summary, source, parent_prompt_id,
                             hidden, created_at, updated_at)
        VALUES ('x', 'x', 'x', '', 'user', NULL, 0, ?, ?)
        """,
        (now, now),
    )
    prompt_id = int(db.query_one("SELECT id FROM prompts WHERE slug = 'x'")["id"])
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO builds (prompt_id, endpoint_id, model_row_id, status,
                                latest_version, hidden, created_at, updated_at)
            VALUES (?, 1, 1, 'totally-made-up', 0, 0, ?, ?)
            """,
            (prompt_id, now, now),
        )


def test_wal_mode_is_enabled_for_concurrent_reads(tmp_path) -> None:
    database = Database(tmp_path / "wal.db")
    database.migrate()
    assert database.query_one("PRAGMA journal_mode")["journal_mode"] == "wal"


def test_transaction_rolls_back_on_error(db: Database) -> None:
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO prompts (slug, title, body, summary, source,
                                     parent_prompt_id, hidden, created_at, updated_at)
                VALUES ('rollback', 'r', 'b', '', 'user', NULL, 0, 'x', 'x')
                """
            )
            raise RuntimeError("boom")
    assert db.query_one("SELECT id FROM prompts WHERE slug = 'rollback'") is None
