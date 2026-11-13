-- 0002_build_failures.sql — keep what a failed generation actually returned.
--
-- Publishing is gated by the artifact validator, so a rejected attempt never
-- becomes a build_version. Without this table the raw response would be lost and
-- a misbehaving endpoint could not be debugged. The failure history is the
-- admin panel's view into what an endpoint really said.

CREATE TABLE build_failures (
    id              INTEGER PRIMARY KEY,
    build_id        INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    job_id          INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    stage           TEXT NOT NULL CHECK (stage IN ('provider', 'extract', 'validate', 'interrupted', 'cancelled')),
    error_text      TEXT NOT NULL DEFAULT '',
    violations_json TEXT NOT NULL DEFAULT '[]',
    raw_response    TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX build_failures_build_idx ON build_failures(build_id, id);
