-- 0004_truncated_stage.sql — allow "truncated" as a failure stage.
--
-- When a completion stops at the output-token ceiling, the cut-off document
-- explains the gate failure by itself: the fix is a smaller answer, not a
-- different one. Recording that cause as its own stage lets the public pages
-- name it and the repair retry advise for it. SQLite cannot edit a CHECK
-- constraint in place, so the table is rebuilt: same columns, same index,
-- one extra allowed value.

ALTER TABLE build_failures RENAME TO build_failures_old;

CREATE TABLE build_failures (
    id              INTEGER PRIMARY KEY,
    build_id        INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    job_id          INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    stage           TEXT NOT NULL CHECK (stage IN ('provider', 'extract', 'truncated', 'validate', 'interrupted', 'cancelled')),
    error_text      TEXT NOT NULL DEFAULT '',
    violations_json TEXT NOT NULL DEFAULT '[]',
    raw_response    TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
INSERT INTO build_failures SELECT * FROM build_failures_old;
DROP TABLE build_failures_old;
CREATE INDEX build_failures_build_idx ON build_failures(build_id, id);
