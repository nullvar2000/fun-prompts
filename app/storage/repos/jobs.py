"""The job queue: durable rows, atomically claimed.

Claiming uses ``UPDATE … RETURNING`` so two workers can never pick up the same
job, and so a crash mid-job leaves a row that startup recovery can find.
"""

from __future__ import annotations

from app.domain.models import Job
from app.domain.status import JobKind, JobState, require_job_transition
from app.storage.db import Database, utc_now

_JOB_COLUMNS = """
    id, kind, build_id, version_id, endpoint_id, state, attempts,
    run_after, error, instruction, created_at, updated_at
"""


class JobRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def enqueue(
        self,
        kind: JobKind,
        *,
        build_id: int | None = None,
        version_id: int | None = None,
        endpoint_id: int | None = None,
        run_after: str | None = None,
        instruction: str = "",
    ) -> Job:
        now = utc_now()
        rowid = self.db.execute(
            """
            INSERT INTO jobs (kind, build_id, version_id, endpoint_id, state,
                              attempts, run_after, error, instruction, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, '', ?, ?, ?)
            """,
            (
                str(kind),
                build_id,
                version_id,
                endpoint_id,
                str(JobState.QUEUED),
                run_after or now,
                instruction,
                now,
                now,
            ),
        )
        return self.get(int(rowid))  # type: ignore[return-value]

    def get(self, job_id: int) -> Job | None:
        row = self.db.query_one(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ?", (job_id,))
        return Job.from_row(row) if row else None

    def claim_next(self) -> Job | None:
        """Atomically move the oldest due job to ``running``."""
        rows = self.db.query_write(
            f"""
            UPDATE jobs
               SET state = ?, attempts = attempts + 1, updated_at = ?
             WHERE id = (
                SELECT id FROM jobs
                 WHERE state = ? AND run_after <= ?
                 ORDER BY run_after, id
                 LIMIT 1
             )
            RETURNING {_JOB_COLUMNS}
            """,
            (str(JobState.RUNNING), utc_now(), str(JobState.QUEUED), utc_now()),
        )
        return Job.from_row(rows[0]) if rows else None

    def transition(self, job_id: int, target: JobState, *, error: str = "") -> Job:
        current = self.db.query_one("SELECT state FROM jobs WHERE id = ?", (job_id,))
        if current is None:
            raise KeyError(f"job {job_id} does not exist")
        require_job_transition(JobState(current["state"]), target)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE id = ?",
                (str(target), utc_now(), job_id),
            )
            if error:
                conn.execute("UPDATE jobs SET error = ? WHERE id = ?", (error, job_id))
        return self.get(job_id)  # type: ignore[return-value]

    def request_cancel(self, job_id: int) -> Job | None:
        """Queued jobs cancel immediately; running ones get a cooperative flag."""
        job = self.get(job_id)
        if job is None:
            return None
        if job.state is JobState.QUEUED:
            return self.transition(job_id, JobState.CANCELLED)
        if job.state is JobState.RUNNING:
            return self.transition(job_id, JobState.CANCELLING)
        return job

    def cancel_requested(self, job_id: int) -> bool:
        return self.db.query_one(
            "SELECT 1 FROM jobs WHERE id = ? AND state = ?", (job_id, str(JobState.CANCELLING))
        ) is not None

    def requeue(self, job_id: int, *, run_after: str | None = None) -> Job:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET state = ?, run_after = ?, updated_at = ? WHERE id = ?",
                (str(JobState.QUEUED), run_after or utc_now(), utc_now(), job_id),
            )
        return self.get(job_id)  # type: ignore[return-value]

    def recover_interrupted(self) -> list[Job]:
        """Jobs left ``running`` by a dead process become ``interrupted``.

        They are surfaced rather than silently retried: re-running a generation
        costs the user tokens, so a human (or M3's retry policy) decides.
        """
        rows = self.db.query_write(
            f"""
            UPDATE jobs SET state = ?, updated_at = ?
             WHERE state = ?
            RETURNING {_JOB_COLUMNS}
            """,
            (str(JobState.INTERRUPTED), utc_now(), str(JobState.RUNNING)),
        )
        return [Job.from_row(row) for row in rows]

    def for_build(self, build_id: int) -> list[Job]:
        rows = self.db.query(
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE build_id = ? ORDER BY id DESC", (build_id,)
        )
        return [Job.from_row(row) for row in rows]

    def recent(self, limit: int = 50, *, state: JobState | None = None) -> list[Job]:
        sql = f"SELECT {_JOB_COLUMNS} FROM jobs"
        params: list[object] = []
        if state is not None:
            sql += " WHERE state = ?"
            params.append(str(state))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [Job.from_row(row) for row in self.db.query(sql, tuple(params))]

    def pending_count(self) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM jobs WHERE state IN (?, ?, ?)",
            (str(JobState.QUEUED), str(JobState.RUNNING), str(JobState.CANCELLING)),
        )
        return int(row["n"]) if row else 0
