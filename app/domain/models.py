"""Domain entities.

Plain frozen dataclasses that mirror the persisted rows. Repositories map rows
to these; nothing in this module knows SQLite exists.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from app.domain.status import BuildStatus, JobKind, JobState, PromptSource


def _as_bool(value: object) -> bool:
    return bool(value)


@dataclass(frozen=True)
class ModelTarget:
    """One selectable build target: a model behind one endpoint."""

    id: int
    endpoint_id: int
    model_id: str
    display_name: str | None = None
    enabled: bool = True
    discovered_at: str = ""

    @property
    def label(self) -> str:
        return self.display_name or self.model_id

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ModelTarget:
        return cls(
            id=row["id"],
            endpoint_id=row["endpoint_id"],
            model_id=row["model_id"],
            display_name=row["display_name"],
            enabled=_as_bool(row["enabled"]),
            discovered_at=row["discovered_at"],
        )


@dataclass(frozen=True)
class Endpoint:
    id: int
    name: str
    base_url: str
    provider_type: str = "openai_compat"
    api_key_encrypted: str = ""
    enabled: bool = True
    timeout_s: int | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    created_at: str = ""
    updated_at: str = ""
    tags: tuple[str, ...] = ()

    @classmethod
    def from_row(cls, row: sqlite3.Row, tags: tuple[str, ...] = ()) -> Endpoint:
        return cls(
            id=row["id"],
            name=row["name"],
            base_url=row["base_url"],
            provider_type=row["provider_type"],
            api_key_encrypted=row["api_key_encrypted"],
            enabled=_as_bool(row["enabled"]),
            timeout_s=row["timeout_s"],
            max_output_tokens=row["max_output_tokens"],
            temperature=row["temperature"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tags=tags,
        )


@dataclass(frozen=True)
class Prompt:
    id: int
    slug: str
    title: str
    body: str
    summary: str = ""
    source: PromptSource = PromptSource.USER
    parent_prompt_id: int | None = None
    hidden: bool = False
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Prompt:
        return cls(
            id=row["id"],
            slug=row["slug"],
            title=row["title"],
            body=row["body"],
            summary=row["summary"],
            source=PromptSource(row["source"]),
            parent_prompt_id=row["parent_prompt_id"],
            hidden=_as_bool(row["hidden"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True)
class BuildVersion:
    id: int
    build_id: int
    version_no: int
    html: str
    raw_response: str
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    finish_reason: str = ""
    model_returned: str = ""
    validation: dict = field(default_factory=dict)
    guard_flags: tuple[str, ...] = ()
    error_text: str = ""
    parent_version_id: int | None = None
    created_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> BuildVersion:
        return cls(
            id=row["id"],
            build_id=row["build_id"],
            version_no=row["version_no"],
            html=row["html"],
            raw_response=row["raw_response"],
            usage=json.loads(row["usage_json"] or "{}"),
            latency_ms=row["latency_ms"],
            finish_reason=row["finish_reason"],
            model_returned=row["model_returned"],
            validation=json.loads(row["validation_json"] or "{}"),
            guard_flags=tuple(json.loads(row["guard_flags_json"] or "[]")),
            error_text=row["error_text"],
            parent_version_id=row["parent_version_id"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class Build:
    id: int
    prompt_id: int
    endpoint_id: int
    model_row_id: int
    status: BuildStatus
    latest_version: int = 0
    hidden: bool = False
    created_at: str = ""
    updated_at: str = ""
    tags: tuple[str, ...] = ()
    #: Denormalised from the joined prompt row so a gallery card needs one query
    #: instead of one per card. Empty when the query did not join prompts.
    prompt_title: str = ""
    prompt_summary: str = ""
    prompt_slug: str = ""

    @property
    def is_public(self) -> bool:
        return self.status is BuildStatus.PUBLISHED and not self.hidden

    @classmethod
    def from_row(cls, row: sqlite3.Row, tags: tuple[str, ...] = ()) -> Build:
        keys = row.keys()
        return cls(
            id=row["id"],
            prompt_id=row["prompt_id"],
            endpoint_id=row["endpoint_id"],
            model_row_id=row["model_row_id"],
            status=BuildStatus(row["status"]),
            latest_version=row["latest_version"],
            hidden=_as_bool(row["hidden"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tags=tags,
            prompt_title=row["prompt_title"] if "prompt_title" in keys else "",
            prompt_summary=row["prompt_summary"] if "prompt_summary" in keys else "",
            prompt_slug=row["prompt_slug"] if "prompt_slug" in keys else "",
        )


@dataclass(frozen=True)
class Job:
    id: int
    kind: JobKind
    state: JobState
    build_id: int | None = None
    version_id: int | None = None
    endpoint_id: int | None = None
    attempts: int = 0
    run_after: str = ""
    error: str = ""
    #: Follow-up text for a ``refine`` job. Stored here because the queue is the
    #: only durable place for it: a job claimed just before a crash has to be
    #: re-runnable after startup.
    instruction: str = ""
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        return cls(
            id=row["id"],
            kind=JobKind(row["kind"]),
            state=JobState(row["state"]),
            build_id=row["build_id"],
            version_id=row["version_id"],
            endpoint_id=row["endpoint_id"],
            attempts=row["attempts"],
            run_after=row["run_after"],
            error=row["error"],
            instruction=row["instruction"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
