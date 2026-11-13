"""Endpoints and the models discovered behind them."""

from __future__ import annotations

import sqlite3

from app.domain.errors import ValidationError
from app.domain.models import Endpoint, ModelTarget
from app.storage.db import Database, utc_now

_COLUMNS = """
    id, name, base_url, provider_type, api_key_encrypted, enabled,
    timeout_s, max_output_tokens, temperature, created_at, updated_at
"""


class EndpointRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --- endpoints ----------------------------------------------------------
    def create(
        self,
        *,
        name: str,
        base_url: str,
        provider_type: str = "openai_compat",
        api_key_encrypted: str = "",
        enabled: bool = True,
        tags: tuple[str, ...] = (),
        timeout_s: int | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Endpoint:
        now = utc_now()
        try:
            with self.db.transaction() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO endpoints (name, base_url, provider_type, api_key_encrypted,
                                           enabled, timeout_s, max_output_tokens, temperature,
                                           created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (name, base_url, provider_type, api_key_encrypted, int(enabled),
                     timeout_s, max_output_tokens, temperature, now, now),
                )
                endpoint_id = int(cursor.lastrowid)
                self._insert_tags(conn, endpoint_id, tags)
        except sqlite3.IntegrityError as exc:
            # Names are how operators and the UI refer to endpoints, so a
            # collision is a user-facing problem, not a database one.
            raise ValidationError(f"an endpoint named {name!r} already exists") from exc
        return self.get(endpoint_id)  # type: ignore[return-value]

    def get(self, endpoint_id: int) -> Endpoint | None:
        row = self.db.query_one(f"SELECT {_COLUMNS} FROM endpoints WHERE id = ?", (endpoint_id,))
        return Endpoint.from_row(row, self._tags_for(endpoint_id)) if row else None

    def get_by_name(self, name: str) -> Endpoint | None:
        row = self.db.query_one(f"SELECT {_COLUMNS} FROM endpoints WHERE name = ?", (name,))
        return Endpoint.from_row(row, self._tags_for(int(row["id"]))) if row else None

    def list(self, *, enabled_only: bool = False) -> list[Endpoint]:
        sql = f"SELECT {_COLUMNS} FROM endpoints"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY name"
        rows = self.db.query(sql)
        return [Endpoint.from_row(row, self._tags_for(int(row["id"]))) for row in rows]

    def update(
        self,
        endpoint_id: int,
        *,
        name: str | None = None,
        base_url: str | None = None,
        provider_type: str | None = None,
        api_key_encrypted: str | None = None,
        enabled: bool | None = None,
        timeout_s: int | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        tags: tuple[str, ...] | None = None,
        clear: tuple[str, ...] = (),
    ) -> Endpoint | None:
        """Partial update: ``None`` means leave alone.

        ``clear`` names columns to set back to NULL, i.e. "no per-endpoint
        override, fall back to the environment ceilings". ``None`` cannot carry
        that meaning here, because it already means "not supplied".
        """
        unknown = set(clear) - {"timeout_s", "max_output_tokens", "temperature"}
        if unknown:
            raise ValidationError(f"cannot clear {', '.join(sorted(unknown))}")
        assignments: list[str] = []
        params: list[object] = []
        for column, value in (
            ("name", name),
            ("base_url", base_url),
            ("provider_type", provider_type),
            ("api_key_encrypted", api_key_encrypted),
            ("enabled", None if enabled is None else int(enabled)),
            ("timeout_s", timeout_s),
            ("max_output_tokens", max_output_tokens),
            ("temperature", temperature),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                params.append(value)
        for column in sorted(clear):
            assignments.append(f"{column} = NULL")
        if not assignments and tags is None:
            return self.get(endpoint_id)

        assignments.append("updated_at = ?")
        params.append(utc_now())
        params.append(endpoint_id)
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE endpoints SET {', '.join(assignments)} WHERE id = ?",
                tuple(params),
            )
            if tags is not None:
                conn.execute("DELETE FROM endpoint_tags WHERE endpoint_id = ?", (endpoint_id,))
                self._insert_tags(conn, endpoint_id, tags)
        return self.get(endpoint_id)

    def delete(self, endpoint_id: int) -> None:
        self.db.execute("DELETE FROM endpoints WHERE id = ?", (endpoint_id,))

    # --- discovered models --------------------------------------------------
    def replace_models(
        self, endpoint_id: int, discovered: tuple[tuple[str, str | None], ...]
    ) -> tuple[int, int]:
        """Sync the model list after discovery. Returns (added, removed).

        Rows that already exist keep their ``enabled`` flag, so disabling one
        model survives a re-discovery.
        """
        now = utc_now()
        with self.db.transaction() as conn:
            existing = {
                row["model_id"]: row["id"]
                for row in conn.execute(
                    "SELECT id, model_id FROM models WHERE endpoint_id = ?", (endpoint_id,)
                )
            }
            seen = {model_id for model_id, _ in discovered}
            added = 0
            for model_id, display_name in discovered:
                if model_id in existing:
                    conn.execute(
                        "UPDATE models SET display_name = ?, discovered_at = ? WHERE id = ?",
                        (display_name, now, existing[model_id]),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO models (endpoint_id, model_id, display_name,
                                            enabled, discovered_at)
                        VALUES (?, ?, ?, 1, ?)
                        """,
                        (endpoint_id, model_id, display_name, now),
                    )
                    added += 1
            removed = 0
            for model_id, row_id in existing.items():
                if model_id not in seen:
                    conn.execute("DELETE FROM models WHERE id = ?", (row_id,))
                    removed += 1
        return added, removed

    def list_models(self, endpoint_id: int, *, enabled_only: bool = False) -> list[ModelTarget]:
        sql = "SELECT * FROM models WHERE endpoint_id = ?"
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY display_name IS NULL, display_name, model_id"
        return [ModelTarget.from_row(row) for row in self.db.query(sql, (endpoint_id,))]

    def get_model(self, model_row_id: int) -> ModelTarget | None:
        row = self.db.query_one("SELECT * FROM models WHERE id = ?", (model_row_id,))
        return ModelTarget.from_row(row) if row else None

    def set_model_enabled(self, model_row_id: int, enabled: bool) -> None:
        self.db.execute("UPDATE models SET enabled = ? WHERE id = ?", (int(enabled), model_row_id))

    def enabled_targets(self) -> list[tuple[Endpoint, ModelTarget]]:
        """Every selectable target, for the submission form's endpoint picker."""
        rows = self.db.query(
            """
            SELECT m.* FROM models m
            JOIN endpoints e ON e.id = m.endpoint_id
            WHERE e.enabled = 1 AND m.enabled = 1
            ORDER BY e.name, m.display_name IS NULL, m.display_name, m.model_id
            """
        )
        endpoints_by_id = {endpoint.id: endpoint for endpoint in self.list(enabled_only=True)}
        return [
            (endpoints_by_id[row["endpoint_id"]], ModelTarget.from_row(row))
            for row in rows
            if row["endpoint_id"] in endpoints_by_id
        ]

    # --- helpers ------------------------------------------------------------
    @staticmethod
    def _insert_tags(conn: sqlite3.Connection, endpoint_id: int, tags: tuple[str, ...]) -> None:
        for tag in {t.strip().lower() for t in tags if t.strip()}:
            conn.execute(
                "INSERT OR IGNORE INTO endpoint_tags (endpoint_id, tag) VALUES (?, ?)",
                (endpoint_id, tag),
            )

    def _tags_for(self, endpoint_id: int) -> tuple[str, ...]:
        rows = self.db.query(
            "SELECT tag FROM endpoint_tags WHERE endpoint_id = ? ORDER BY tag", (endpoint_id,)
        )
        return tuple(row["tag"] for row in rows)
