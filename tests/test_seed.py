"""Seed import: a first boot must show a gallery, not an empty database."""

from __future__ import annotations

from app.config import REPO_ROOT
from app.domain.status import BuildStatus
from app.storage.db import Database
from app.storage.repos.builds import BuildRepo
from app.storage.repos.prompts import PromptRepo
from app.storage.seed import SEED_MARKER_KEY, SeedImporter


def _importer(db: Database) -> SeedImporter:
    return SeedImporter(
        db, prompts_dir=REPO_ROOT / "prompts", seed_apps_dir=REPO_ROOT / "seed" / "apps"
    )


def test_first_boot_seeds_every_prompt_and_the_available_builds(db: Database) -> None:
    report = _importer(db).run()
    assert report.prompts == 4
    assert report.builds == 3
    assert report.imported


def test_a_prompt_without_an_artifact_is_still_seeded(db: Database) -> None:
    _importer(db).run()
    rows = db.query(
        """
        SELECT p.slug, COUNT(b.id) AS n
        FROM prompts p LEFT JOIN builds b ON b.prompt_id = p.id
        GROUP BY p.slug
        """
    )
    counts = {row["slug"]: row["n"] for row in rows}
    assert counts["neoncybercycle"] == 0
    assert counts["dice-roller"] == 1


def test_seed_import_runs_once(db: Database) -> None:
    _importer(db).run()
    second = _importer(db).run()
    assert second.skipped == 1 and second.prompts == 0
    assert db.query("SELECT value FROM settings WHERE key = ?", (SEED_MARKER_KEY,))


def test_seeded_builds_are_published_and_visible_in_the_gallery(db: Database) -> None:
    _importer(db).run()
    builds = BuildRepo(db)
    gallery = builds.list()
    assert len(gallery) == 3
    assert {b.status for b in gallery} == {BuildStatus.PUBLISHED}
    assert all(b.is_public for b in gallery)
    assert all(b.latest_version == 1 for b in gallery)


def test_seeded_builds_carry_endpoint_and_derived_tags(db: Database) -> None:
    _importer(db).run()
    builds = BuildRepo(db)
    prompts = PromptRepo(db)

    dice = builds.for_prompt(prompts.get_by_slug("dice-roller").id)[0]  # type: ignore[union-attr]
    assert {"seed", "imported"} <= set(dice.tags)
    assert {"random", "single-file"} <= set(dice.tags)

    # Derivation is real, not a constant: different prompts earn different tags.
    pomodoro = builds.for_prompt(prompts.get_by_slug("pomodoro").id)[0]  # type: ignore[union-attr]
    assert "productivity" in pomodoro.tags and "random" not in pomodoro.tags

    counts = dict(builds.tag_counts())
    assert counts["seed"] == 3
    assert counts["single-file"] == 3


def test_seeded_artifacts_are_readable_and_attributed(db: Database) -> None:
    _importer(db).run()
    builds = BuildRepo(db)
    version = builds.latest_version(builds.list()[0].id)
    assert version is not None
    assert version.html.startswith("<!DOCTYPE html>") or version.html.startswith("<html")
    assert version.model_returned == "imported-static"
    assert version.finish_reason == "imported"
    assert version.validation["ok"] is True


def test_seed_endpoint_is_disabled_so_it_cannot_be_picked_for_new_work(db: Database) -> None:
    _importer(db).run()
    row = db.query_one("SELECT enabled FROM endpoints WHERE name = 'seed'")
    assert row["enabled"] == 0


def test_app_startup_seeds_and_hands_the_database_to_the_app(client) -> None:
    db = client.app.state.db
    assert isinstance(db, Database)
    assert len(BuildRepo(db).list()) == 3


def test_seeding_can_be_switched_off(bare_client) -> None:
    assert BuildRepo(bare_client.app.state.db).list() == []
    settings = bare_client.app.state.settings
    assert settings.seed_on_start is False
