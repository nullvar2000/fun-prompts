"""Builds, versions and the gallery query surface.

Every read the gallery and comparison views need lives here so the web layer
never assembles SQL, and so search/filter/sort behaviour is testable on its own.
"""

from __future__ import annotations

import json

from app.domain.models import Build, BuildVersion
from app.domain.status import BuildStatus, require_build_transition
from app.storage.db import Database, utc_now

_BUILD_COLUMNS = """
    b.id, b.prompt_id, b.endpoint_id, b.model_row_id, b.status,
    b.latest_version, b.hidden, b.created_at, b.updated_at,
    p.title AS prompt_title, p.summary AS prompt_summary
"""

_SORTS: dict[str, str] = {
    "newest": "b.created_at DESC, b.id DESC",
    "oldest": "b.created_at ASC, b.id ASC",
    "most-refined": "b.latest_version DESC, b.created_at DESC",
    "recently-built": "b.updated_at DESC, b.id DESC",
    "title": "p.title ASC, b.id DESC",
}


class BuildRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --- creation and status -------------------------------------------------
    def create(
        self,
        *,
        prompt_id: int,
        endpoint_id: int,
        model_row_id: int,
        status: BuildStatus = BuildStatus.QUEUED,
    ) -> Build:
        """Create a build row.

        ``status`` exists so an imported artifact can start out ``published``:
        creating a row in a terminal state is not a transition, so the state
        machine still governs every *change*. Anything the pipeline runs should
        use the default and move through the states.
        """
        now = utc_now()
        rowid = self.db.execute(
            """
            INSERT INTO builds (prompt_id, endpoint_id, model_row_id, status,
                                latest_version, hidden, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, 0, ?, ?)
            """,
            (prompt_id, endpoint_id, model_row_id, str(status), now, now),
        )
        return self.get(int(rowid))  # type: ignore[return-value]

    def get(self, build_id: int) -> Build | None:
        row = self.db.query_one(
            f"SELECT {_BUILD_COLUMNS} FROM builds b JOIN prompts p ON p.id = b.prompt_id "
            "WHERE b.id = ?",
            (build_id,),
        )
        return Build.from_row(row, self._tags_for(build_id)) if row else None

    def set_status(self, build_id: int, target: BuildStatus) -> Build:
        """Status changes go through the domain state machine, never raw SQL."""
        current = self.db.query_one("SELECT status FROM builds WHERE id = ?", (build_id,))
        if current is None:
            raise KeyError(f"build {build_id} does not exist")
        require_build_transition(BuildStatus(current["status"]), target)
        self.db.execute(
            "UPDATE builds SET status = ?, updated_at = ? WHERE id = ?",
            (str(target), utc_now(), build_id),
        )
        return self.get(build_id)  # type: ignore[return-value]

    def set_hidden(self, build_id: int, hidden: bool) -> None:
        self.db.execute(
            "UPDATE builds SET hidden = ?, updated_at = ? WHERE id = ?",
            (int(hidden), utc_now(), build_id),
        )

    def delete(self, build_id: int) -> None:
        self.db.execute("DELETE FROM builds WHERE id = ?", (build_id,))

    # --- versions ------------------------------------------------------------
    def add_version(
        self,
        build_id: int,
        *,
        html: str,
        raw_response: str,
        usage: dict | None = None,
        latency_ms: int = 0,
        finish_reason: str = "",
        model_returned: str = "",
        validation: dict | None = None,
        guard_flags: tuple[str, ...] = (),
        error_text: str = "",
        parent_version_id: int | None = None,
    ) -> BuildVersion:
        with self.db.transaction() as conn:
            next_no = int(
                conn.execute(
                    "SELECT COALESCE(MAX(version_no), 0) + 1 FROM build_versions WHERE build_id = ?",
                    (build_id,),
                ).fetchone()[0]
            )
            cursor = conn.execute(
                """
                INSERT INTO build_versions (build_id, version_no, html, raw_response,
                                            usage_json, latency_ms, finish_reason,
                                            model_returned, validation_json,
                                            guard_flags_json, error_text,
                                            parent_version_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    build_id,
                    next_no,
                    html,
                    raw_response,
                    json.dumps(usage or {}),
                    latency_ms,
                    finish_reason,
                    model_returned,
                    json.dumps(validation or {}),
                    json.dumps(list(guard_flags)),
                    error_text,
                    parent_version_id,
                    utc_now(),
                ),
            )
            conn.execute(
                "UPDATE builds SET latest_version = ?, updated_at = ? WHERE id = ?",
                (next_no, utc_now(), build_id),
            )
            row = conn.execute(
                "SELECT * FROM build_versions WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
        return BuildVersion.from_row(row)

    def versions(self, build_id: int) -> list[BuildVersion]:
        rows = self.db.query(
            "SELECT * FROM build_versions WHERE build_id = ? ORDER BY version_no",
            (build_id,),
        )
        return [BuildVersion.from_row(row) for row in rows]

    def latest_version(self, build_id: int) -> BuildVersion | None:
        row = self.db.query_one(
            "SELECT * FROM build_versions WHERE build_id = ? ORDER BY version_no DESC LIMIT 1",
            (build_id,),
        )
        return BuildVersion.from_row(row) if row else None

    def get_version(self, version_id: int) -> BuildVersion | None:
        row = self.db.query_one("SELECT * FROM build_versions WHERE id = ?", (version_id,))
        return BuildVersion.from_row(row) if row else None

    # --- failure history -----------------------------------------------------
    def record_failure(
        self,
        build_id: int,
        *,
        job_id: int | None,
        stage: str,
        error_text: str = "",
        violations: tuple[str, ...] = (),
        raw_response: str = "",
    ) -> int:
        """Keep the rejected attempt. Publishing is gated, debugging is not."""
        rowid = self.db.execute(
            """
            INSERT INTO build_failures (build_id, job_id, stage, error_text,
                                        violations_json, raw_response, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                build_id,
                job_id,
                stage,
                error_text[:4000],
                json.dumps(list(violations)),
                raw_response[:20_000],
                utc_now(),
            ),
        )
        return int(rowid)

    def failures(self, build_id: int) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM build_failures WHERE build_id = ? ORDER BY id DESC", (build_id,)
        )
        out = []
        for row in rows:
            record = dict(row)
            record["violations"] = json.loads(record.pop("violations_json") or "[]")
            out.append(record)
        return out

    # --- gallery reads -------------------------------------------------------
    def list(
        self,
        *,
        tags: tuple[str, ...] = (),
        model_row_ids: tuple[int, ...] = (),
        search: str = "",
        sort: str = "newest",
        limit: int = 24,
        offset: int = 0,
        include_hidden: bool = False,
        statuses: tuple[BuildStatus, ...] = (BuildStatus.PUBLISHED,),
    ) -> list[Build]:
        order = _SORTS.get(sort, _SORTS["newest"])
        sql = f"SELECT {_BUILD_COLUMNS} FROM builds b JOIN prompts p ON p.id = b.prompt_id"
        clauses: list[str] = []
        params: list[object] = []

        if statuses:
            clauses.append("b.status IN (" + ", ".join("?" * len(statuses)) + ")")
            params += [str(s) for s in statuses]
        if not include_hidden:
            clauses.append("b.hidden = 0 AND p.hidden = 0")
        for tag in tags:
            clauses.append(
                """
                EXISTS (
                    SELECT 1 FROM build_tags bt JOIN tags t ON t.id = bt.tag_id
                    WHERE bt.build_id = b.id AND t.name = ?
                )
                """
            )
            params.append(tag)
        if model_row_ids:
            clauses.append("b.model_row_id IN (" + ", ".join("?" * len(model_row_ids)) + ")")
            params += list(model_row_ids)
        if search:
            needle = f"%{search.strip()}%"
            clauses.append(
                """
                (p.title LIKE ? OR p.summary LIKE ? OR p.body LIKE ?
                 OR EXISTS (SELECT 1 FROM build_tags bt2 JOIN tags t2 ON t2.id = bt2.tag_id
                            WHERE bt2.build_id = b.id AND t2.name LIKE ?))
                """
            )
            params += [needle, needle, needle, needle]

        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        params += [limit, offset]

        rows = self.db.query(sql, tuple(params))
        return [Build.from_row(row, self._tags_for(int(row["id"]))) for row in rows]

    def random_published(self, count: int = 3) -> list[Build]:
        rows = self.db.query(
            f"""
            SELECT {_BUILD_COLUMNS} FROM builds b
            JOIN prompts p ON p.id = b.prompt_id
            WHERE b.status = ? AND b.hidden = 0 AND p.hidden = 0
            ORDER BY RANDOM() LIMIT ?
            """,
            (str(BuildStatus.PUBLISHED), count),
        )
        return [Build.from_row(row, self._tags_for(int(row["id"]))) for row in rows]

    def count(self, **kwargs) -> int:  # noqa: ANN003 - mirrors list() filters
        rows = self.list(limit=10_000, offset=0, **kwargs)
        return len(rows)

    def for_prompt(self, prompt_id: int, *, include_hidden: bool = False) -> list[Build]:
        sql = f"SELECT {_BUILD_COLUMNS} FROM builds b JOIN prompts p ON p.id = b.prompt_id WHERE b.prompt_id = ?"
        if not include_hidden:
            sql += " AND b.hidden = 0"
        sql += " ORDER BY b.created_at, b.id"
        return [Build.from_row(row, self._tags_for(int(row["id"]))) for row in self.db.query(sql, (prompt_id,))]

    # --- tags ----------------------------------------------------------------
    def tag_build(self, build_id: int, tags: tuple[str, ...], source: str) -> None:
        """Attach tags, remembering where each came from (endpoint/derived/manual)."""
        with self.db.transaction() as conn:
            for tag in {t.strip().lower() for t in tags if t.strip()}:
                cursor = conn.execute("SELECT id FROM tags WHERE name = ?", (tag,))
                row = cursor.fetchone()
                if row is None:
                    cursor = conn.execute("INSERT INTO tags (name) VALUES (?)", (tag,))
                    tag_id = int(cursor.lastrowid)
                else:
                    tag_id = int(row["id"])
                conn.execute(
                    """
                    INSERT INTO build_tags (build_id, tag_id, source) VALUES (?, ?, ?)
                    ON CONFLICT(build_id, tag_id) DO NOTHING
                    """,
                    (build_id, tag_id, source),
                )

    def untag_build(self, build_id: int, tag: str) -> None:
        self.db.execute(
            """
            DELETE FROM build_tags
            WHERE build_id = ? AND tag_id = (SELECT id FROM tags WHERE name = ?)
            """,
            (build_id, tag.strip().lower()),
        )

    def tag_counts(self, *, published_only: bool = True) -> list[tuple[str, int]]:
        sql = """
            SELECT t.name, COUNT(*) AS n
            FROM tags t
            JOIN build_tags bt ON bt.tag_id = t.id
            JOIN builds b ON b.id = bt.build_id
        """
        if published_only:
            sql += " WHERE b.status = ? AND b.hidden = 0"
            rows = self.db.query(sql + " GROUP BY t.name ORDER BY n DESC, t.name", (str(BuildStatus.PUBLISHED),))
        else:
            rows = self.db.query(sql + " GROUP BY t.name ORDER BY n DESC, t.name")
        return [(row["name"], int(row["n"])) for row in rows]

    def model_options(self) -> list[tuple[int, str]]:
        """(model_row_id, label) pairs for the gallery's model filter."""
        rows = self.db.query(
            """
            SELECT m.id, m.model_id, m.display_name, e.name AS endpoint_name
            FROM models m JOIN endpoints e ON e.id = m.endpoint_id
            ORDER BY e.name, m.display_name IS NULL, m.display_name, m.model_id
            """
        )
        return [
            (
                int(row["id"]),
                f"{row['display_name'] or row['model_id']} · {row['endpoint_name']}",
            )
            for row in rows
        ]

    def _tags_for(self, build_id: int) -> tuple[str, ...]:
        rows = self.db.query(
            """
            SELECT t.name FROM tags t
            JOIN build_tags bt ON bt.tag_id = t.id
            WHERE bt.build_id = ? ORDER BY t.name
            """,
            (build_id,),
        )
        return tuple(row["name"] for row in rows)
