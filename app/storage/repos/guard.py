"""Prompt-injection detector log: every blocked or flagged submission is kept."""

from __future__ import annotations

from app.storage.db import Database, utc_now


class GuardLogRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def log(
        self,
        *,
        matched_rule: str,
        snippet: str,
        action: str = "block",
        prompt_id: int | None = None,
    ) -> int:
        rowid = self.db.execute(
            """
            INSERT INTO guard_log (prompt_id, matched_rule, snippet, action, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (prompt_id, matched_rule, snippet[:200], action, utc_now()),
        )
        return int(rowid)

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self.db.query(
            """
            SELECT g.*, p.title AS prompt_title FROM guard_log g
            LEFT JOIN prompts p ON p.id = g.prompt_id
            ORDER BY g.id DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in rows]

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM guard_log")
        return int(row["n"]) if row else 0
