"""The comparison view: one prompt, up to four outputs, side by side.

What is checked here is the framing and the selection, not the artifacts: every
column must be an iframe on the artifact origin under the same sandbox rules as
the build page, the show/hide state must live in the URL, and one output shown
means the layout stops being a grid.
"""

from __future__ import annotations

from itertools import count

import pytest
from fastapi.testclient import TestClient

from app.domain.status import BuildStatus
from app.storage.db import Database
from app.storage.repos.builds import BuildRepo
from app.storage.repos.prompts import PromptRepo
from tests.helpers import make_endpoint

ARTIFACT = "<!DOCTYPE html><html><head><title>app</title></head><body><b>MARK</b></body></html>"
_names = count()


def siblings(
    db: Database,
    *,
    count: int = 2,
    body: str = "a dice roller with a history list",
    title: str | None = None,
    status: BuildStatus = BuildStatus.PUBLISHED,
) -> tuple[int, list[int]]:
    """One prompt, built ``count`` times by one endpoint's ``count`` models.

    Returns ``(prompt_id, build_ids)`` — a comparison's worth of builds, because
    the whole point is that the input was identical.
    """
    name = f"cmp-{next(_names)}"
    endpoint, models = make_endpoint(db, name=name, models=tuple(f"m{i}" for i in range(count)))
    prompt = PromptRepo(db).create(title=title or name, body=body)
    builds = BuildRepo(db)
    created: list[int] = []
    for model in models:
        build = builds.create(prompt_id=prompt.id, endpoint_id=endpoint.id, model_row_id=model.id)
        if status is not BuildStatus.QUEUED:
            builds.add_version(build.id, html=ARTIFACT, raw_response=ARTIFACT)
            for step in (BuildStatus.GENERATING, BuildStatus.VALIDATING, status):
                builds.set_status(build.id, step)
        created.append(build.id)
    return prompt.id, created


def _url(build_ids: list[int], hidden: list[int] | None = None) -> str:
    parts = [f"b={i}" for i in build_ids]
    if hidden:
        parts += [f"hide={i}" for i in hidden]
    return "/compare?" + "&".join(parts)


def test_the_prompt_page_offers_a_selection_of_its_builds(bare_client: TestClient, db: Database) -> None:
    prompt_id, ids = siblings(db, count=3)
    slug = PromptRepo(db).get(prompt_id).slug  # type: ignore[union-attr]
    page = bare_client.get(f"/prompt/{slug}")
    assert 'action="/compare"' in page.text
    assert 'method="get"' in page.text
    for build_id in ids:
        assert f'value="{build_id}"' in page.text
    assert page.text.count("checked") == 3
    assert "compare up to 4" in page.text


def test_the_selection_is_only_offered_when_there_is_something_to_compare(
    bare_client: TestClient, db: Database
) -> None:
    _, ids = siblings(db, count=1)
    slug = PromptRepo(db).get(BuildRepo(db).get(ids[0]).prompt_id).slug  # type: ignore[union-attr]
    assert 'action="/compare"' not in bare_client.get(f"/prompt/{slug}").text


def test_every_output_is_framed_from_the_artifact_origin(bare_client: TestClient, db: Database) -> None:
    _, ids = siblings(db, count=3)
    page = bare_client.get(_url(ids))
    assert page.status_code == 200
    assert page.text.count("<iframe") == 3
    assert 'class="compare compare-grid"' in page.text
    assert 'sandbox="allow-scripts"' in page.text
    # Same rules as the build page: an opaque origin, so a frame can touch its own
    # DOM and neither the gallery nor the frames beside it.
    assert "allow-same-origin" not in page.text
    assert "/artifact/" in page.text


def test_hide_and_show_are_links_in_the_url(bare_client: TestClient, db: Database) -> None:
    _, ids = siblings(db, count=3)
    page = bare_client.get(_url(ids))
    assert f'href="/compare?b={ids[0]}&amp;b={ids[1]}&amp;b={ids[2]}&amp;hide={ids[1]}"' in page.text

    hidden = bare_client.get(_url(ids, hidden=[ids[1]]))
    assert hidden.text.count("<iframe") == 2
    assert 'class="compare-col is-hidden"' in hidden.text
    assert "Hidden. Its toggles are above." in hidden.text
    # And showing it again is a link back to the original selection.
    assert f'href="/compare?b={ids[0]}&amp;b={ids[1]}&amp;b={ids[2]}"' in hidden.text


def test_one_output_shown_takes_the_page(bare_client: TestClient, db: Database) -> None:
    _, ids = siblings(db, count=3)
    single = bare_client.get(_url(ids, hidden=ids[1:]))
    assert single.status_code == 200
    assert 'class="compare compare-single"' in single.text
    assert single.text.count("<iframe") == 1
    assert "One output shown, so it has the page." in single.text

    grid = bare_client.get(_url(ids[:2]))
    assert 'class="compare compare-grid"' in grid.text


def test_the_prompt_is_pinned_at_the_bottom_of_the_page(bare_client: TestClient, db: Database) -> None:
    _, ids = siblings(db, count=2, body="a dice roller with a history list")
    page = bare_client.get(_url(ids))
    panel = page.text.split('class="prompt-panel"')
    assert len(panel) == 2
    assert "a dice roller with a history list" in panel[1]
    assert "the prompt" in panel[1]


def test_removing_an_output_takes_it_out_of_the_selection(
    bare_client: TestClient, db: Database
) -> None:
    _, ids = siblings(db, count=3)
    page = bare_client.get(_url(ids, hidden=[ids[0]]))
    remaining = f"/compare?b={ids[1]}&amp;b={ids[2]}"
    assert f'href="{remaining}"' in page.text

    dropped = bare_client.get(f"/compare?b={ids[1]}&b={ids[2]}")
    assert dropped.text.count("<iframe") == 2


def test_more_than_the_limit_is_truncated_saying_so(bare_client: TestClient, db: Database) -> None:
    _, ids = siblings(db, count=4)
    other_prompt, extra = siblings(db, count=1)  # noqa: F841 - only need one more build id
    too_many = _url([*ids, *extra])
    page = bare_client.get(too_many)
    assert page.status_code == 200
    assert page.text.count("<iframe") == 4
    assert "showing the first 4 of the selection" in page.text


def test_duplicated_ids_are_collapsed(bare_client: TestClient, db: Database) -> None:
    _, ids = siblings(db, count=2)
    page = bare_client.get(f"/compare?b={ids[0]}&b={ids[0]}&b={ids[1]}")
    assert page.text.count("<iframe") == 2


def test_a_selection_spanning_two_prompts_is_refused(bare_client: TestClient, db: Database) -> None:
    _, first = siblings(db, count=2, title="one prompt")
    _, second = siblings(db, count=1, title="another prompt")
    response = bare_client.get(_url([*first, *second]))
    assert response.status_code == 400
    assert "one prompt" in response.json()["detail"]


def test_selections_that_cannot_be_shown(bare_client: TestClient, db: Database) -> None:
    assert bare_client.get("/compare").status_code == 400

    _, ids = siblings(db, count=2)
    assert bare_client.get(_url([*ids, 99999])).status_code == 404

    BuildRepo(db).set_hidden(ids[1], True)
    assert bare_client.get(_url(ids)).status_code == 404

    prompt = BuildRepo(db).get(ids[0]).prompt_id  # type: ignore[union-attr]
    PromptRepo(db).set_hidden(prompt, True)
    assert bare_client.get(_url([ids[0]])).status_code == 404


def test_a_build_that_has_not_published_yet_shows_its_state_not_an_iframe(
    bare_client: TestClient, db: Database
) -> None:
    prompt_id, ids = siblings(db, count=2)
    builds = BuildRepo(db)
    source = builds.get(ids[0])
    pending = builds.create(
        prompt_id=prompt_id, endpoint_id=source.endpoint_id, model_row_id=source.model_row_id  # type: ignore[union-attr]
    )
    builds.set_status(pending.id, BuildStatus.GENERATING)

    page = bare_client.get(_url([ids[0], pending.id]))
    assert page.status_code == 200
    assert page.text.count("<iframe") == 1
    assert "generating — no artifact yet" in page.text
    assert f'href="/build/{pending.id}"' in page.text


def test_an_unpublished_build_of_the_same_prompt_has_no_frame(
    bare_client: TestClient, db: Database
) -> None:
    prompt_id, ids = siblings(db, count=3)
    builds = BuildRepo(db)
    extra = builds.create(
        prompt_id=prompt_id, endpoint_id=builds.get(ids[0]).endpoint_id, model_row_id=builds.get(ids[0]).model_row_id  # type: ignore[union-attr]
    )
    builds.set_status(extra.id, BuildStatus.GENERATING)

    page = bare_client.get(_url([*ids, extra.id]))
    assert page.text.count("<iframe") == 3
    assert "generating — no artifact yet" in page.text
    assert "build page" in page.text


@pytest.mark.parametrize("hidden", [[], [1], [0, 1]])
def test_hiding_everything_offers_a_way_back(bare_client: TestClient, db: Database, hidden: list[int]) -> None:
    _, ids = siblings(db, count=2)
    page = bare_client.get(_url(ids, hidden=[ids[i] for i in hidden]))
    if hidden == [0, 1]:
        assert "Every output is hidden" in page.text
        assert "show them all" in page.text
    else:
        assert "Every output is hidden" not in page.text
