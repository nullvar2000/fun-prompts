"""Build/version queries: the reads the gallery and comparison views depend on."""

from __future__ import annotations

import pytest

from app.domain.errors import TransitionError
from app.domain.status import BuildStatus
from app.storage.db import Database
from app.storage.repos.builds import BuildRepo
from tests.helpers import make_endpoint, make_prompt


@pytest.fixture
def builds(db: Database) -> BuildRepo:
    return BuildRepo(db)


def _build(db: Database, builds: BuildRepo, *, title: str, tags: tuple[str, ...] = (), html: str = "<html><body>ok</body></html>") -> int:
    endpoint, models = make_endpoint(db, name=f"ep-{title}")
    prompt = make_prompt(db, title=title, body=f"prompt for {title}")
    build = builds.create(prompt_id=prompt.id, endpoint_id=endpoint.id, model_row_id=models[0].id)
    builds.add_version(build.id, html=html, raw_response=html)
    builds.tag_build(build.id, tags, "derived")
    builds.set_status(build.id, BuildStatus.GENERATING)
    builds.set_status(build.id, BuildStatus.VALIDATING)
    builds.set_status(build.id, BuildStatus.PUBLISHED)
    return build.id


def test_build_creation_requires_real_parents(db: Database, builds: BuildRepo) -> None:
    from sqlite3 import IntegrityError

    endpoint, models = make_endpoint(db)
    prompt = make_prompt(db)
    with pytest.raises(IntegrityError):
        builds.create(prompt_id=prompt.id, endpoint_id=endpoint.id, model_row_id=4242)


def test_versions_are_numbered_and_latest_wins(db: Database, builds: BuildRepo) -> None:
    endpoint, models = make_endpoint(db)
    prompt = make_prompt(db)
    build = builds.create(prompt_id=prompt.id, endpoint_id=endpoint.id, model_row_id=models[0].id)
    first = builds.add_version(build.id, html="v1", raw_response="v1", model_returned="model-a")
    second = builds.add_version(
        build.id, html="v2", raw_response="v2", parent_version_id=first.id, latency_ms=250
    )

    assert (first.version_no, second.version_no) == (1, 2)
    assert second.parent_version_id == first.id
    assert second.latency_ms == 250
    assert builds.latest_version(build.id).html == "v2"  # type: ignore[union-attr]
    assert [v.html for v in builds.versions(build.id)] == ["v1", "v2"]
    assert builds.get(build.id).latest_version == 2  # type: ignore[union-attr]


def test_illegal_status_change_is_refused(db: Database, builds: BuildRepo) -> None:
    build_id = _build(db, builds, title="Legal")
    with pytest.raises(TransitionError):
        builds.set_status(build_id, BuildStatus.GENERATING)


def test_tag_filters_are_and_combined(db: Database, builds: BuildRepo) -> None:
    _build(db, builds, title="Dice", tags=("games", "random"))
    _build(db, builds, title="Timer", tags=("productivity",))

    both = builds.list(tags=("games", "random"))
    assert [b.id for b in both] == [b.id for b in builds.list(tags=("random", "games"))]
    assert len(both) == 1
    assert builds.list(tags=("games", "productivity")) == []
    assert len(builds.list(tags=("games",))) == 1


def test_search_covers_prompt_text_and_tags(db: Database, builds: BuildRepo) -> None:
    _build(db, builds, title="Pomodoro", tags=("focus",))
    endpoint, models = make_endpoint(db, name="ep-matching")
    prompt = make_prompt(db, title="Unrelated", body="this one mentions pomodoro in the body")
    build = builds.create(prompt_id=prompt.id, endpoint_id=endpoint.id, model_row_id=models[0].id)
    builds.add_version(build.id, html="ok", raw_response="ok")
    builds.set_status(build.id, BuildStatus.GENERATING)
    builds.set_status(build.id, BuildStatus.VALIDATING)
    builds.set_status(build.id, BuildStatus.PUBLISHED)

    titles = {b.id for b in builds.list(search="pomodoro")}
    assert len(titles) == 2
    assert {b.id for b in builds.list(search="FOCUS")} == {
        b.id for b in builds.list(tags=("focus",))
    }
    assert builds.list(search="nothing-matches") == []


def test_hidden_and_unpublished_builds_stay_out_of_the_gallery(db: Database, builds: BuildRepo) -> None:
    visible = _build(db, builds, title="Visible")
    hidden = _build(db, builds, title="Hidden")
    builds.set_hidden(hidden, True)

    endpoint, models = make_endpoint(db, name="ep-draft")
    prompt = make_prompt(db, title="Draft")
    draft = builds.create(prompt_id=prompt.id, endpoint_id=endpoint.id, model_row_id=models[0].id)

    gallery = {b.id for b in builds.list()}
    assert visible in gallery and hidden not in gallery and draft.id not in gallery
    assert hidden in {b.id for b in builds.list(include_hidden=True)}
    # Every status, hidden included: this is the admin view.
    everything = builds.list(statuses=tuple(BuildStatus), include_hidden=True)
    assert {hidden, visible, draft.id} == {b.id for b in everything}


def test_sorting_is_stable_and_explicit(db: Database, builds: BuildRepo) -> None:
    first = _build(db, builds, title="Alpha")
    second = _build(db, builds, title="Beta")
    builds.add_version(second, html="v2", raw_response="v2")

    assert [b.id for b in builds.list(sort="newest")] == [second, first]
    assert [b.id for b in builds.list(sort="oldest")] == [first, second]
    assert [b.id for b in builds.list(sort="most-refined")][0] == second


def test_random_showcase_returns_distinct_published_builds(db: Database, builds: BuildRepo) -> None:
    ids = {_build(db, builds, title=f"App {n}") for n in range(6)}
    picks = builds.random_published(3)
    assert len(picks) == 3
    assert len({p.id for p in picks}) == 3
    assert {p.id for p in picks} <= ids
    assert builds.random_published(99) and len(builds.random_published(99)) == 6


def test_tag_counts_and_model_options_drive_the_filter_ui(db: Database, builds: BuildRepo) -> None:
    _build(db, builds, title="One", tags=("tools",))
    _build(db, builds, title="Two", tags=("tools", "games"))

    counts = dict(builds.tag_counts())
    assert counts["tools"] == 2 and counts["games"] == 1
    options = builds.model_options()
    assert len(options) == 2
    labels = {label for _, label in options}
    assert any(label == "MODEL-A · ep-One" for label in labels)
    assert all(label.count(" · ") == 1 for label in labels)


def test_untag_removes_only_the_named_tag(db: Database, builds: BuildRepo) -> None:
    build_id = _build(db, builds, title="Untag", tags=("games", "random"))
    builds.untag_build(build_id, "games")
    assert builds.get(build_id).tags == ("random",)  # type: ignore[union-attr]


def test_for_prompt_lists_every_target_of_a_prompt(db: Database, builds: BuildRepo) -> None:
    prompt = make_prompt(db, title="Shared prompt")
    endpoint, models = make_endpoint(db, name="two-models", models=("m-a", "m-b"))
    first = builds.create(prompt_id=prompt.id, endpoint_id=endpoint.id, model_row_id=models[0].id)
    second = builds.create(prompt_id=prompt.id, endpoint_id=endpoint.id, model_row_id=models[1].id)
    ids = {b.id for b in builds.for_prompt(prompt.id)}
    assert ids == {first.id, second.id}
    assert first.prompt_id == second.prompt_id
