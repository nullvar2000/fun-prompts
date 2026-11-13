"""Prompt records: seeded, user-submitted, or forked from another prompt."""

from __future__ import annotations

from app.domain.models import Prompt
from app.domain.slugs import slugify, unique_slug
from app.domain.status import PromptSource
from app.storage.db import Database, utc_now

_COLUMNS = """
    id, slug, title, body, summary, source, parent_prompt_id,
    hidden, created_at, updated_at
"""


class PromptRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        *,
        title: str,
        body: str,
        summary: str = "",
        source: PromptSource = PromptSource.USER,
        slug: str | None = None,
        parent_prompt_id: int | None = None,
    ) -> Prompt:
        base = slugify(slug or title)
        taken = {row["slug"] for row in self.db.query("SELECT slug FROM prompts")}
        slug_value = unique_slug(base, taken)
        now = utc_now()
        rowid = self.db.execute(
            """
            INSERT INTO prompts (slug, title, body, summary, source,
                                 parent_prompt_id, hidden, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (slug_value, title, body, summary, str(source), parent_prompt_id, now, now),
        )
        return self.get(int(rowid))  # type: ignore[return-value]

    def get(self, prompt_id: int) -> Prompt | None:
        row = self.db.query_one(f"SELECT {_COLUMNS} FROM prompts WHERE id = ?", (prompt_id,))
        return Prompt.from_row(row) if row else None

    def get_by_slug(self, slug: str) -> Prompt | None:
        row = self.db.query_one(f"SELECT {_COLUMNS} FROM prompts WHERE slug = ?", (slug,))
        return Prompt.from_row(row) if row else None

    def list(self, *, include_hidden: bool = False, source: PromptSource | None = None) -> list[Prompt]:
        sql = f"SELECT {_COLUMNS} FROM prompts"
        clauses: list[str] = []
        params: list[object] = []
        if not include_hidden:
            clauses.append("hidden = 0")
        if source is not None:
            clauses.append("source = ?")
            params.append(str(source))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC"
        return [Prompt.from_row(row) for row in self.db.query(sql, tuple(params))]

    def update(
        self,
        prompt_id: int,
        *,
        title: str | None = None,
        body: str | None = None,
        summary: str | None = None,
    ) -> Prompt | None:
        assignments: list[str] = []
        params: list[object] = []
        for column, value in (("title", title), ("body", body), ("summary", summary)):
            if value is not None:
                assignments.append(f"{column} = ?")
                params.append(value)
        if not assignments:
            return self.get(prompt_id)
        assignments.append("updated_at = ?")
        params += [utc_now(), prompt_id]
        self.db.execute(
            f"UPDATE prompts SET {', '.join(assignments)} WHERE id = ?", tuple(params)
        )
        return self.get(prompt_id)

    def set_hidden(self, prompt_id: int, hidden: bool) -> None:
        self.db.execute(
            "UPDATE prompts SET hidden = ?, updated_at = ? WHERE id = ?",
            (int(hidden), utc_now(), prompt_id),
        )

    def fork(self, prompt_id: int, *, title: str | None = None, body: str | None = None) -> Prompt | None:
        """Copy a prompt, keeping lineage so forks stay traceable in the UI."""
        original = self.get(prompt_id)
        if original is None:
            return None
        return self.create(
            title=title or f"{original.title} (fork)",
            body=body if body is not None else original.body,
            summary=original.summary,
            source=PromptSource.FORK,
            parent_prompt_id=original.id,
        )

    def delete(self, prompt_id: int) -> None:
        """Cascades to builds and their versions by FK design."""
        self.db.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))
