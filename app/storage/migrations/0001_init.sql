-- 0001_init.sql — initial schema.
--
-- Timestamps are ISO-8601 UTC text (second precision): sortable and comparable
-- in SQL without a unix-epoch conversion, and readable straight out of the
-- sqlite shell. Booleans are INTEGER 0/1. Statuses are text CHECK-constrained to
-- exactly the values app/domain/status.py can produce.

CREATE TABLE settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- One row per OpenAI-compatible base URL + credential set. A build target is an
-- endpoint *plus* a model, so a single endpoint can offer several targets.
CREATE TABLE endpoints (
    id                 INTEGER PRIMARY KEY,
    name               TEXT NOT NULL UNIQUE,
    base_url           TEXT NOT NULL,
    provider_type      TEXT NOT NULL DEFAULT 'openai_compat',
    api_key_encrypted  TEXT NOT NULL DEFAULT '',
    enabled            INTEGER NOT NULL DEFAULT 1,
    -- NULL means "inherit the global value, which also acts as the ceiling".
    timeout_s          INTEGER,
    max_output_tokens  INTEGER,
    temperature        REAL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

-- Tags an admin attaches to an endpoint; every build from that endpoint
-- inherits them, which is how a card says "built by llama3.1-70b on the LAN
-- box" without re-deriving it per build.
CREATE TABLE endpoint_tags (
    endpoint_id  INTEGER NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
    tag          TEXT NOT NULL,
    PRIMARY KEY (endpoint_id, tag)
);

CREATE TABLE models (
    id             INTEGER PRIMARY KEY,
    endpoint_id    INTEGER NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
    model_id       TEXT NOT NULL,
    display_name   TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1,
    discovered_at  TEXT NOT NULL,
    UNIQUE (endpoint_id, model_id)
);

CREATE TABLE prompts (
    id                INTEGER PRIMARY KEY,
    slug              TEXT NOT NULL UNIQUE,
    title             TEXT NOT NULL,
    body              TEXT NOT NULL,
    summary           TEXT NOT NULL DEFAULT '',
    source            TEXT NOT NULL CHECK (source IN ('seed', 'user', 'fork')),
    parent_prompt_id  INTEGER REFERENCES prompts(id) ON DELETE SET NULL,
    hidden            INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX prompts_source_idx ON prompts(source);

-- A build is one prompt run by one endpoint+model target. Refinement does not
-- rewrite history: it appends a row to build_versions.
CREATE TABLE builds (
    id              INTEGER PRIMARY KEY,
    prompt_id       INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    endpoint_id     INTEGER NOT NULL REFERENCES endpoints(id) ON DELETE RESTRICT,
    model_row_id    INTEGER NOT NULL REFERENCES models(id) ON DELETE RESTRICT,
    status          TEXT NOT NULL CHECK (
                        status IN ('queued', 'generating', 'validating',
                                   'published', 'rejected', 'failed', 'cancelled')
                    ),
    latest_version  INTEGER NOT NULL DEFAULT 0,
    hidden          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX builds_prompt_idx ON builds(prompt_id);
CREATE INDEX builds_status_idx ON builds(status);
CREATE INDEX builds_model_idx ON builds(model_row_id);

-- The generated artifact lives in the database next to everything that explains
-- how it was made, so a build page needs no filesystem and a backup is one file.
CREATE TABLE build_versions (
    id                INTEGER PRIMARY KEY,
    build_id          INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    version_no        INTEGER NOT NULL,
    html              TEXT NOT NULL,
    raw_response      TEXT NOT NULL,
    usage_json        TEXT NOT NULL DEFAULT '{}',
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    finish_reason     TEXT NOT NULL DEFAULT '',
    model_returned    TEXT NOT NULL DEFAULT '',
    validation_json   TEXT NOT NULL DEFAULT '{}',
    guard_flags_json  TEXT NOT NULL DEFAULT '[]',
    error_text        TEXT NOT NULL DEFAULT '',
    parent_version_id INTEGER REFERENCES build_versions(id) ON DELETE SET NULL,
    created_at        TEXT NOT NULL,
    UNIQUE (build_id, version_no)
);
CREATE INDEX build_versions_build_idx ON build_versions(build_id, version_no);

CREATE TABLE tags (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE
);

-- Real join table rather than a JSON blob: tag chips and model filters are
-- indexed lookups, and counts come for free.
CREATE TABLE build_tags (
    build_id  INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    tag_id    INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    source    TEXT NOT NULL CHECK (source IN ('endpoint', 'derived', 'manual')),
    PRIMARY KEY (build_id, tag_id)
);
CREATE INDEX build_tags_tag_idx ON build_tags(tag_id);

-- The queue. In-memory queues lose jobs on restart; these rows survive and are
-- recovered at startup, which is what makes a single container acceptable.
CREATE TABLE jobs (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('build', 'refine', 'probe', 'discover')),
    build_id     INTEGER REFERENCES builds(id) ON DELETE CASCADE,
    version_id   INTEGER REFERENCES build_versions(id) ON DELETE SET NULL,
    endpoint_id  INTEGER REFERENCES endpoints(id) ON DELETE CASCADE,
    state        TEXT NOT NULL CHECK (
                     state IN ('queued', 'running', 'cancelling',
                               'done', 'failed', 'cancelled', 'interrupted')
                 ),
    attempts     INTEGER NOT NULL DEFAULT 0,
    run_after    TEXT NOT NULL,
    error        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX jobs_state_idx ON jobs(state, run_after);
CREATE INDEX jobs_build_idx ON jobs(build_id);

CREATE TABLE guard_log (
    id           INTEGER PRIMARY KEY,
    prompt_id    INTEGER REFERENCES prompts(id) ON DELETE SET NULL,
    matched_rule TEXT NOT NULL,
    snippet      TEXT NOT NULL,
    action       TEXT NOT NULL CHECK (action IN ('block', 'flag')),
    created_at   TEXT NOT NULL
);
