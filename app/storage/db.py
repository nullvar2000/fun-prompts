"""SQLite access: connections, pragmas, transactions and the migration runner.

SQLite is synchronous. Route handlers that touch it are therefore plain ``def``
functions (FastAPI runs those on a threadpool), and the async worker wraps its
calls in ``anyio.to_thread``. A connection is opened per operation rather than
shared, because connections are not safe to use across threads.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def utc_now() -> str:
    """Second-precision ISO-8601 UTC: sortable, comparable, human-readable."""
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=max(self.busy_timeout_ms, 0) / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        # WAL lets the gallery keep reading while a build writes.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One atomic unit of work: commits on success, rolls back on any error."""
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        connection = self.connect()
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        connection = self.connect()
        try:
            return connection.execute(sql, params).fetchone()
        finally:
            connection.close()

    def query_write(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Run a statement that writes *and* returns rows (``RETURNING``).

        ``sqlite3`` leaves an open transaction after DML, so a plain read-style
        helper would silently roll the write back on close. Anything with
        ``RETURNING`` has to go through a real transaction.
        """
        with self.transaction() as connection:
            return connection.execute(sql, params).fetchall()

    def execute(self, sql: str, params: tuple = ()) -> int:
        """Run a single statement, returning the new rowid where one applies."""
        with self.transaction() as connection:
            cursor = connection.execute(sql, params)
            return cursor.lastrowid or cursor.rowcount

    def version(self) -> int:
        row = self.query_one("SELECT MAX(version) AS v FROM schema_version")
        return int(row["v"]) if row and row["v"] is not None else 0

    def migrate(self) -> list[int]:
        """Apply pending ``NNNN_*.sql`` files in order. Returns applied versions."""
        applied: list[int] = []
        with self.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version     INTEGER PRIMARY KEY,
                    name        TEXT NOT NULL,
                    applied_at  TEXT NOT NULL
                )
                """
            )
            current = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_version").fetchall()
            }
            for version, name, sql in self._pending(current):
                connection.executescript(sql)
                connection.execute(
                    "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, utc_now()),
                )
                applied.append(version)
        return applied

    @staticmethod
    def _pending(current: set[int]) -> list[tuple[int, str, str]]:
        pending = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version_text, _, stem = path.name.partition("_")
            if not version_text.isdigit():
                raise ValueError(f"migration {path.name} must start with a number")
            version = int(version_text)
            if version in current:
                continue
            pending.append((version, Path(stem).stem, path.read_text(encoding="utf-8")))
        return pending
