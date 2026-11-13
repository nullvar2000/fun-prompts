"""Gallery pages: home, browse, build detail, prompt detail, downloads.

Templates stay dumb: filters, links, pagination and the active/inactive state of
each chip are computed here, so the templates render what they are given and the
query-building rules are testable without HTML.

Nothing here renders artifact HTML. Generated pages are framed from the separate
artifact origin (:mod:`app.web.artifact_server`) with ``sandbox``, and the only
artifact bytes this origin emits are sent with ``Content-Disposition: attachment``.

The two write routes (revise a build, cancel a revision) are public — the gallery
has no session — so they are guarded by an origin check instead of a CSRF token:
see :func:`app.web.security.same_origin_write`.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

from app.build.guard import PromptRejected
from app.config import Settings
from app.domain.errors import ValidationError
from app.domain.models import Build, BuildVersion, Job
from app.domain.status import BuildStatus, JobKind, JobState
from app.web.artifact_server import artifact_headers
from app.web.security import same_origin_write
from app.web.templating import templates

router = APIRouter(tags=["gallery"])

PAGE_SIZE = 12
HOME_SHOWCASE = 3
_SORTS = ("newest", "oldest", "recently-built", "most-refined", "title")
#: Statuses where polling is pointless: the build cannot change again.
_TERMINAL = {BuildStatus.PUBLISHED, BuildStatus.REJECTED, BuildStatus.FAILED, BuildStatus.CANCELLED}
#: Job states where a refine could still change which version is served.
_OPEN_JOBS = {JobState.QUEUED, JobState.RUNNING, JobState.CANCELLING, JobState.INTERRUPTED}

#: What a refine redirect can report: (css class, copy). Fixed copy only — the
#: instruction itself is never echoed back, because it may contain anything a
#: model later repeats.
_REFINE_NOTICES = {
    "queued": ("flash-ok", "Revision queued. It runs against this build's own model."),
    "empty": ("flash-error", "An instruction is needed — tell it what to change."),
    "too-long": ("flash-error", "That instruction is longer than the prompt limit."),
    "refused": ("flash-error", "The submission guard refused that instruction. Nothing was queued."),
    "not-published": ("flash-error", "Only a published build can be revised."),
    "cancelled": ("flash-ok", "Revision cancelled."),
    "no-job": ("flash-error", "That revision is not running any more."),
}


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


def _refine_state(container, build_id: int) -> tuple[Job | None, Job | None]:  # noqa: ANN001
    """This build's open revise job, and its most recent revise job.

    The open one matters even when the build is published: the page has to say
    which version is live and keep polling, because a new one may take its place
    at any moment. The most recent one matters when it failed, so the page can
    say the revision did not happen without implying the app is broken.
    """
    open_job: Job | None = None
    latest: Job | None = None
    for job in container.jobs.for_build(build_id):  # newest first
        if job.kind is not JobKind.REFINE:
            continue
        if latest is None:
            latest = job
        if open_job is None and job.state in _OPEN_JOBS:
            open_job = job
    return open_job, latest


def _version_rows(container, build_id: int) -> list[dict]:  # noqa: ANN001
    """The version list, with lineage the template can render without thinking.

    ``parent_no`` turns "v3" into "v3 from v2" in the history, and ``clipped``
    says when the model did not see the whole previous document.
    """
    versions = container.builds.versions(build_id)
    numbers = {version.id: version.version_no for version in versions}
    return [
        {
            "id": version.id,
            "version_no": version.version_no,
            "created_at": version.created_at,
            "parent_no": numbers.get(version.parent_version_id)
            if version.parent_version_id
            else None,
            "clipped": bool(version.validation.get("refine_clipped")),
            "context_chars": version.validation.get("refine_context_chars"),
        }
        for version in versions
    ]


def _refine_notice(request: Request) -> dict | None:
    """The one-word report a revise redirect left in the query string.

    Only the code travels in the URL, and the copy here is fixed: what the user
    typed is never put back into the page that redirected them.
    """
    notice = _REFINE_NOTICES.get(request.query_params.get("refine") or "")
    return None if notice is None else {"kind": notice[0], "text": notice[1]}


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
    refine_job, last_refine = _refine_state(container, build.id)
    rows = _version_rows(container, build.id)
    return {
        "artifact_origin": settings.artifact_base_url,
        "build": build,
        "version": version,
        "prompt": prompt,
        "endpoint": endpoint,
        "model": model,
        "artifact_url": None if version is None else _artifact_url(settings, version.id),
        "versions": rows,
        "version_row": next(
            (row for row in rows if version and row["id"] == version.id), None
        ),
        "failures": container.builds.failures(build.id),
        # A published build with a revise job running is not settled: polling has
        # to keep going or the page shows a version that is no longer the latest.
        "terminal": build.status in _TERMINAL and refine_job is None,
        "refine_job": refine_job,
        # Fixed copy only: the job's own error text may quote an endpoint, and a
        # public page does not repeat that.
        "refine_failed": last_refine is not None and last_refine.state is JobState.FAILED,
        "refine_notice": _refine_notice(request),
        "refine_max_chars": settings.max_prompt_chars,
        "next_version_no": build.latest_version + 1,
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


# --- revise a build (public write, origin-checked) ----------------------------
def _same_origin(request: Request) -> None:
    """The gallery's substitute for a CSRF token: same origin, or refuse.

    There is no session here to hold a token, so what is checked is the origin
    the browser attached to the request.
    """
    same_origin_write(request, _settings(request))


def _back_to_build(build_id: int, notice: str) -> RedirectResponse:
    """303 back to the build page with one word about what happened.

    The instruction is never echoed back through the URL: it is free text, and
    putting it in a redirect target invites it to be rendered somewhere it was
    not meant to be.
    """
    return RedirectResponse(url=f"/build/{build_id}?refine={notice}", status_code=303)


@router.post("/build/{build_id}/refine")
def refine_build(request: Request, build_id: int, instruction: str = Form("")):
    """Queue a follow-up turn on this build, against the build's own model."""
    _same_origin(request)
    settings = _settings(request)
    container = _container(request)
    build = _build_or_404(build_id, container)
    if build.hidden:
        raise HTTPException(404, "this build is hidden")

    text = (instruction or "").strip()
    if not text:
        return _back_to_build(build.id, "empty")
    if len(text) > settings.max_prompt_chars:
        return _back_to_build(build.id, "too-long")

    try:
        container.pipeline.refine(build.id, text)
    except PromptRejected:
        return _back_to_build(build.id, "refused")
    except ValidationError:
        # Not published, no stored version, gone away: the page says which.
        return _back_to_build(build.id, "not-published")
    return _back_to_build(build.id, "queued")


@router.post("/build/{build_id}/refine/cancel")
def cancel_refine(request: Request, build_id: int, job_id: int = Form(...)):
    """Stop a revision that is queued or running."""
    _same_origin(request)
    container = _container(request)
    build = _build_or_404(build_id, container)

    job = container.jobs.get(job_id)
    if job is None or job.build_id != build.id or job.kind is not JobKind.REFINE:
        return _back_to_build(build.id, "no-job")
    result = container.pipeline.request_cancel(job.id)
    if result is None or result.state not in {JobState.CANCELLED, JobState.CANCELLING}:
        return _back_to_build(build.id, "no-job")
    return _back_to_build(build.id, "cancelled")


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
