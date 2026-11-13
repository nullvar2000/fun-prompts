"""Bring the first boot up with content instead of an empty gallery.

Two things are imported, once:

* ``prompts/*.md`` become prompt records (source ``seed``).
* ``seed/apps/<slug>.html`` become *published builds* attached to those prompts,
  attributed to a disabled ``seed`` endpoint so the schema keeps its invariant
  that every build has a real endpoint+model target.

Re-running is a no-op: the ``settings`` table records when the import happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.build.tagging import derive_tags
from app.domain.models import Build
from app.domain.status import BuildStatus, PromptSource
from app.storage.db import Database, utc_now

SEED_ENDPOINT_NAME = "seed"
SEED_MODEL_ID = "imported-static"
SEED_MARKER_KEY = "seed_imported_at"


@dataclass
class SeedReport:
    prompts: int = 0
    builds: int = 0
    skipped: int = 0

    @property
    def imported(self) -> bool:
        return self.skipped == 0


class SeedImporter:
    def __init__(
        self,
        db: Database,
        *,
        prompts_dir: Path,
        seed_apps_dir: Path,
    ) -> None:
        self.db = db
        self.prompts_dir = Path(prompts_dir)
        self.seed_apps_dir = Path(seed_apps_dir)

    def run(self) -> SeedReport:
        if self._already_imported():
            return SeedReport(skipped=1)

        report = SeedReport()
        endpoint_id = self._seed_endpoint()
        model_row_id = self._seed_model(endpoint_id)

        for prompt_path in sorted(self.prompts_dir.glob("*.md")):
            body = prompt_path.read_text(encoding="utf-8").strip()
            if not body:
                continue
            slug = prompt_path.stem
            prompt = self.db_prompts().get_by_slug(slug) or self._create_prompt(slug, body)
            report.prompts += 1

            artifact = self.seed_apps_dir / f"{slug}.html"
            if artifact.is_file():
                self._publish_build(prompt.id, endpoint_id, model_row_id, artifact.read_text(encoding="utf-8"), body)
                report.builds += 1

        self._mark_imported()
        return report

    # --- internals -----------------------------------------------------------
    def db_prompts(self):  # noqa: ANN201 - avoids a circular import at module load
        from app.storage.repos.prompts import PromptRepo

        return PromptRepo(self.db)

    def _already_imported(self) -> bool:
        row = self.db.query_one("SELECT value FROM settings WHERE key = ?", (SEED_MARKER_KEY,))
        return row is not None

    def _mark_imported(self) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (SEED_MARKER_KEY, utc_now()),
        )

    def _seed_endpoint(self) -> int:
        row = self.db.query_one(
            "SELECT id FROM endpoints WHERE name = ?", (SEED_ENDPOINT_NAME,)
        )
        if row:
            return int(row["id"])
        now = utc_now()
        rowid = int(
            self.db.execute(
                """
                INSERT INTO endpoints (name, base_url, provider_type, api_key_encrypted,
                                       enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, 0, ?, ?)
                """,
                (SEED_ENDPOINT_NAME, "none://seed", "seed", "", now, now),
            )
        )
        for tag in ("seed", "imported"):
            self.db.execute(
                "INSERT OR IGNORE INTO endpoint_tags (endpoint_id, tag) VALUES (?, ?)",
                (rowid, tag),
            )
        return rowid

    def _seed_model(self, endpoint_id: int) -> int:
        row = self.db.query_one(
            "SELECT id FROM models WHERE endpoint_id = ? AND model_id = ?",
            (endpoint_id, SEED_MODEL_ID),
        )
        if row:
            return int(row["id"])
        return int(
            self.db.execute(
                """
                INSERT INTO models (endpoint_id, model_id, display_name, enabled, discovered_at)
                VALUES (?, ?, ?, 0, ?)
                """,
                (endpoint_id, SEED_MODEL_ID, "Imported build", utc_now()),
            )
        )

    def _create_prompt(self, slug: str, body: str):  # noqa: ANN202
        return self.db_prompts().create(
            slug=slug,
            title=_title(slug, body),
            body=body,
            summary=_summary(body),
            source=PromptSource.SEED,
        )

    def _publish_build(
        self, prompt_id: int, endpoint_id: int, model_row_id: int, html: str, prompt_body: str
    ) -> Build:
        from app.storage.repos.builds import BuildRepo

        repo = BuildRepo(self.db)
        build = repo.create(
            prompt_id=prompt_id,
            endpoint_id=endpoint_id,
            model_row_id=model_row_id,
            status=BuildStatus.PUBLISHED,
        )
        repo.add_version(
            build.id,
            html=html,
            raw_response=html,
            finish_reason="imported",
            model_returned=SEED_MODEL_ID,
            validation={"source": "seed-import", "ok": True},
        )
        repo.tag_build(build.id, ("seed", "imported"), "endpoint")
        repo.tag_build(build.id, derive_tags(prompt_body), "derived")
        return build


def _title(slug: str, body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return slug.replace("-", " ").title()


def _summary(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:160]
    return ""


def main() -> int:  # pragma: no cover - console entry point
    import argparse

    from app.config import load_settings

    parser = argparse.ArgumentParser(description="Import seed prompts and builds")
    parser.add_argument("--force", action="store_true", help="clear the marker and import again")
    args = parser.parse_args()

    settings = load_settings()
    db = Database(settings.db_path, busy_timeout_ms=settings.db_idle_timeout_ms)
    db.migrate()
    if args.force:
        db.execute("DELETE FROM settings WHERE key = ?", (SEED_MARKER_KEY,))

    report = SeedImporter(
        db, prompts_dir=settings.prompts_dir, seed_apps_dir=settings.seed_apps_dir
    ).run()
    if report.skipped:
        print("seed import already recorded; nothing to do (use --force to redo)")
        return 0
    print(f"seeded {report.prompts} prompts, {report.builds} published builds")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
