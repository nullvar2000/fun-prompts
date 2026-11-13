"""Gallery pages: home, browse, build detail, prompt detail, downloads.

Templates stay dumb: filters, links, pagination and the active/inactive state of
each chip are computed here, so the templates render what they are given and the
query-building rules are testable without HTML.

Nothing here renders artifact HTML. Generated pages are framed from the separate
artifact origin (:mod:`app.web.artifact_server`) with ``sandbox``, and the only
artifact bytes this origin emits are sent with ``Content-Disposition: attachment``.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from app.config import Settings
from app.domain.models import Build, BuildVersion
from app.domain.status import BuildStatus
from app.web.artifact_server import artifact_headers
from app.web.templating import templates

router = APIRouter(tags=["gallery"])

PAGE_SIZE = 12
HOME_SHOWCASE = 3
_SORTS = ("newest", "oldest", "recently-built", "most-refined", "title")
#: Statuses where polling is pointless: the build cannot change again.
_TERMINAL = {BuildStatus.PUBLISHED, BuildStatus.REJECTED, BuildStatus.FAILED, BuildStatus.CANCELLED}


class Filters:
    """The gallery query string, kept in one place so links round-trip."""

    def __init__(self, q: str, tags: list[str], models: list[int], sort: str, page: int) -> None:
        self.q = q.strip()
        self.tags = [t for t in tags if t]
        self.models = models
        self.sort = sort if sort in _SORTS else "newest"
        self.page = max(1, page)

    def params(self, **overrides: object) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        if self.q:
            out["q"] = [self.q]
        if self.tags:
            out["tag"] = list(self.tags)
        if self.models:
            out["model"] = [str(m) for m in self.models]
        if self.sort != "newest":
            out["sort"] = [self.sort]
        for key, value in overrides.items():
            if value is None or value == [] or value == "":
                out.pop(key, None)
            else:
                out[key] = [str(v) for v in value] if isinstance(value, list) else [str(value)]
        return out

    def url(self, *, page: int | None = None, **overrides: object) -> str:
        """A gallery link. ``page`` replaces the current page; page 1 is implicit."""
        params = self.params(**overrides)
        target = self.page if page is None else page
        if target > 1:
            params["page"] = [str(target)]
        query = urlencode(params, doseq=True)
        return f"/gallery?{query}" if query else "/gallery"

    def toggle_tag_url(self, tag: str) -> str:
        tags = [t for t in self.tags if t != tag]
        if tag not in self.tags:
            tags.append(tag)
        return self.url(page=1, tag=tags or None)

    def toggle_model_url(self, model_row_id: int) -> str:
        models = [m for m in self.models if m != model_row_id]
        if model_row_id not in self.models:
            models.append(model_row_id)
        return self.url(page=1, model=models or None)


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _container(request: Request):  # noqa: ANN202 - Container, imported lazily
    return request.app.state.container


def _artifact_url(settings: Settings, version_id: int) -> str:
    return f"{settings.artifact_base_url}/artifact/{version_id}"


def _build_or_404(build_id: int, container) -> Build:  # noqa: ANN001
    build = container.builds.get(build_id)
    if build is None:
        raise HTTPException(404, "no such build")
    return build


def _version_for_page(build: Build, requested: int | None, container) -> BuildVersion | None:  # noqa: ANN001
    """Which version a build page shows: a named one if it belongs to this build."""
    if requested is not None:
        version = container.builds.get_version(requested)
        if version is not None and version.build_id == build.id:
            return version
    return container.builds.latest_version(build.id)


# --- home ---------------------------------------------------------------------
@router.get("/")
def home(request: Request):
    settings = _settings(request)
    container = _container(request)
    showcase = container.builds.random_published(HOME_SHOWCASE)
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={
            "artifact_origin": settings.artifact_base_url,
            "showcase": showcase,
            "app_count": container.builds.count(),
            "prompt_count": len(container.prompts.list()),
            "endpoint_count": len(container.endpoints.list(enabled_only=True)),
            "targets": container.endpoints.enabled_targets(),
        },
    )


# --- browse -------------------------------------------------------------------
@router.get("/gallery")
def gallery(
    request: Request,
    q: str = "",
    tag: list[str] = Query(default_factory=list),
    model: list[int] = Query(default_factory=list),
    sort: str = "newest",
    page: int = 1,
):
    settings = _settings(request)
    container = _container(request)
    filters = Filters(q=q, tags=tag, models=model, sort=sort, page=page)

    total = container.builds.count(
        tags=tuple(filters.tags),
        model_row_ids=tuple(filters.models),
        search=filters.q,
    )
    builds = container.builds.list(
        tags=tuple(filters.tags),
        model_row_ids=tuple(filters.models),
        search=filters.q,
        sort=filters.sort,
        limit=PAGE_SIZE,
        offset=(filters.page - 1) * PAGE_SIZE,
    )
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    return templates.TemplateResponse(
        request=request,
        name="gallery.html",
        context={
            "artifact_origin": settings.artifact_base_url,
            "builds": builds,
            "total": total,
            "pages": pages,
            "filters": filters,
            "sorts": _SORTS,
            "model_labels": dict(container.builds.model_options()),
            "tag_options": [
                {
                    "name": name,
                    "count": count,
                    "url": filters.toggle_tag_url(name),
                    "active": name in filters.tags,
                }
                for name, count in container.builds.tag_counts()
            ],
            "model_options": [
                {
                    "model_row_id": model_row_id,
                    "label": label,
                    "url": filters.toggle_model_url(model_row_id),
                    "active": model_row_id in filters.models,
                }
                for model_row_id, label in container.builds.model_options()
            ],
        },
    )


# --- build detail -------------------------------------------------------------
def _build_context(request: Request, build: Build, version: BuildVersion | None) -> dict:
    settings = _settings(request)
    container = _container(request)
    prompt = container.prompts.get(build.prompt_id)
    endpoint = container.endpoints.get(build.endpoint_id)
    model = container.endpoints.get_model(build.model_row_id)
    return {
        "artifact_origin": settings.artifact_base_url,
        "build": build,
        "version": version,
        "prompt": prompt,
        "endpoint": endpoint,
        "model": model,
        "artifact_url": None if version is None else _artifact_url(settings, version.id),
        "versions": container.builds.versions(build.id),
        "failures": container.builds.failures(build.id),
        "terminal": build.status in _TERMINAL,
    }


@router.get("/build/{build_id}")
def build_page(request: Request, build_id: int, version: int | None = None):
    container = _container(request)
    build = _build_or_404(build_id, container)
    if build.hidden:
        raise HTTPException(404, "this build is hidden")
    return templates.TemplateResponse(
        request=request,
        name="build.html",
        context=_build_context(request, build, _version_for_page(build, version, container)),
    )


@router.get("/build/{build_id}/status")
def build_status_fragment(request: Request, build_id: int, version: int | None = None):
    """The piece a build page re-fetches until the build settles."""
    container = _container(request)
    build = _build_or_404(build_id, container)
    return templates.TemplateResponse(
        request=request,
        name="partials/build_stage.html",
        context=_build_context(request, build, _version_for_page(build, version, container)),
    )


# --- prompt detail ------------------------------------------------------------
@router.get("/prompt/{slug}")
def prompt_page(request: Request, slug: str):
    settings = _settings(request)
    container = _container(request)
    prompt = container.prompts.get_by_slug(slug)
    if prompt is None or prompt.hidden:
        raise HTTPException(404, "no such prompt")
    builds = [b for b in container.builds.for_prompt(prompt.id) if b.is_public]
    parent = (
        container.prompts.get(prompt.parent_prompt_id) if prompt.parent_prompt_id else None
    )
    endpoint_names = {e.id: e.name for e in container.endpoints.list()}
    model_ids: dict[int, str] = {}
    for build in builds:
        model = container.endpoints.get_model(build.model_row_id)
        if model is not None:
            model_ids[build.model_row_id] = model.model_id
    return templates.TemplateResponse(
        request=request,
        name="prompt.html",
        context={
            "artifact_origin": settings.artifact_base_url,
            "prompt": prompt,
            "builds": builds,
            "parent": parent,
            "endpoint_names": endpoint_names,
            "model_ids": model_ids,
        },
    )


# --- downloads ----------------------------------------------------------------
@router.get("/download/build/{build_id}")
def download_latest(request: Request, build_id: int):
    """The newest version of a build, for links that do not know version ids."""
    container = _container(request)
    build = _build_or_404(build_id, container)
    version = container.builds.latest_version(build.id)
    if version is None:
        raise HTTPException(404, "this build has no published version")
    return download(request, version.id)


@router.get("/download/{version_id}")
def download(request: Request, version_id: int):
    """Artifact bytes as an attachment, on the gallery origin only.

    Keeping the download here rather than on the artifact origin means a saved
    file is always the stored version, and the artifact origin never has to
    decide anything about filenames.
    """
    settings = _settings(request)
    container = _container(request)
    version = container.builds.get_version(version_id)
    if version is None:
        raise HTTPException(404, "no such version")
    build = container.builds.get(version.build_id)
    if build is None or not build.is_public:
        raise HTTPException(404, "this artifact is not published")
    prompt = container.prompts.get(build.prompt_id)
    if prompt is None or prompt.hidden:
        raise HTTPException(404, "this artifact is not published")

    body = version.html.encode("utf-8")
    filename = f"{prompt.slug}-v{version.version_no}.html"
    headers = {
        **artifact_headers(settings),
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(len(body)),
    }
    return Response(content=body, media_type="text/html; charset=utf-8", headers=headers)
