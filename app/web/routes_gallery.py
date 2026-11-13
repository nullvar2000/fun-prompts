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
from app.build.orchestrator import Target
from app.config import Settings
from app.domain.errors import ValidationError
from app.domain.models import Build, BuildVersion, Job, Prompt
from app.domain.status import BuildStatus, JobKind, JobState
from app.web.artifact_server import artifact_headers
from app.web.security import client_key, same_origin_write
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
    "rate-limited": ("flash-error", "Too many revisions from this address for now."),
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
    if _throttled(request):
        return _back_to_build(build.id, "rate-limited")

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


# --- compare ------------------------------------------------------------------
class CompareSelection:
    """The compare query string: which builds, and which of them are hidden.

    Show/hide has to survive a reload and a shared link, so it lives in the URL
    rather than in a script: ``b`` is the selection, ``hide`` the part of it
    currently folded away. Toggles are ordinary links.
    """

    def __init__(self, builds: list[int], hidden: list[int]) -> None:
        self.builds = list(dict.fromkeys(builds))
        self.hidden = [i for i in dict.fromkeys(hidden) if i in self.builds]

    @property
    def visible(self) -> list[int]:
        return [build_id for build_id in self.builds if build_id not in self.hidden]

    def params(self, *, hide: list[int] | None = None) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {"b": [str(i) for i in self.builds]}
        chosen = self.hidden if hide is None else hide
        if chosen:
            out["hide"] = [str(i) for i in chosen]
        return out

    def url(self, *, hide: list[int] | None = None) -> str:
        query = urlencode(self.params(hide=hide), doseq=True)
        return f"/compare?{query}"

    def toggle_url(self, build_id: int) -> str:
        if build_id in self.hidden:
            return self.url(hide=[i for i in self.hidden if i != build_id])
        return self.url(hide=[*self.hidden, build_id])

    def show_all_url(self) -> str:
        return self.url(hide=[])

    def drop_url(self, build_id: int) -> str:
        """Take a build out of the comparison entirely."""
        remaining = [i for i in self.builds if i != build_id]
        hide = [i for i in self.hidden if i != build_id and i in remaining]
        params: dict[str, list[str]] = {"b": [str(i) for i in remaining]}
        if hide:
            params["hide"] = [str(i) for i in hide]
        query = urlencode(params, doseq=True)
        return f"/compare?{query}" if query else "/gallery"


@router.get("/compare")
def compare(
    request: Request,
    b: list[int] = Query(default_factory=list),
    hide: list[int] = Query(default_factory=list),
):
    """One prompt, up to ``FP_MAX_COMPARISON_TARGETS`` outputs, side by side.

    Every output is framed from the artifact origin under the same sandbox rules
    as the build page; the comparison is only a layout.
    """
    settings = _settings(request)
    container = _container(request)
    selection = CompareSelection(b, hide)
    if not selection.builds:
        raise HTTPException(400, "pick at least one build to compare")

    limit = settings.max_comparison_targets
    truncated = len(selection.builds) > limit
    if truncated:
        selection.builds = selection.builds[:limit]
        selection.hidden = [i for i in selection.hidden if i in selection.builds]

    outputs: list[dict] = []
    prompt: Prompt | None = None
    for build_id in selection.builds:
        build = container.builds.get(build_id)
        if build is None or build.hidden:
            raise HTTPException(404, "no such build")
        build_prompt = container.prompts.get(build.prompt_id)
        if build_prompt is None or build_prompt.hidden:
            raise HTTPException(404, "this build is not published")
        if prompt is None:
            prompt = build_prompt
        elif prompt.id != build_prompt.id:
            # A comparison only means anything if the inputs were identical.
            # Comparing across prompts is a different question, and a different page.
            raise HTTPException(400, "a comparison compares one prompt across endpoints")
        version = container.builds.latest_version(build.id)
        outputs.append(
            {
                "build": build,
                "version": version,
                "endpoint": container.endpoints.get(build.endpoint_id),
                "model": container.endpoints.get_model(build.model_row_id),
                "artifact_url": None if version is None else _artifact_url(settings, version.id),
                "visible": build.id in selection.visible,
            }
        )

    if prompt is None:  # every selected build 404s before this point
        raise HTTPException(404, "no such build")
    visible = [output for output in outputs if output["visible"]]
    return templates.TemplateResponse(
        request=request,
        name="compare.html",
        context={
            "artifact_origin": settings.artifact_base_url,
            "prompt": prompt,
            "outputs": outputs,
            "visible": visible,
            # Exactly one shown means the layout stops being a grid and the
            # artifact gets the page.
            "single": len(visible) == 1,
            "selection": selection,
            "max_targets": limit,
            "truncated": truncated,
        },
    )


# --- submit a prompt (public write, origin-checked) ---------------------------
_SUBMIT_NOTICES = {
    "no-targets": "Pick at least one endpoint and model.",
    "too-many-targets": "That is more pairs than one prompt fans out to.",
    "empty": "A prompt is needed.",
    "too-long": "That prompt is longer than the limit.",
    "bad-target": "One of those pairs is not something this gallery offers.",
    "unavailable": "That endpoint or model is not available right now.",
    "refused": "The submission guard refused that prompt. Nothing was queued.",
    "gone": "That prompt is not here any more.",
    "rate-limited": "Too many submissions from this address for now.",
}


def _target_options(request: Request) -> list[dict]:
    """Every enabled endpoint+model pair, as checkbox values.

    One field carries both halves of the pair (``endpoint_id:model_row_id``) so
    a browser can send a selection without any script.
    """
    container = _container(request)
    return [
        {
            "value": f"{endpoint.id}:{model.id}",
            "label": f"{endpoint.name} · {model.label or model.model_id}",
        }
        for endpoint, model in container.endpoints.enabled_targets()
    ]


def _parse_targets(values: list[str]) -> tuple[list[Target], bool]:
    """Checkbox values back into targets. ``False`` means one was not a pair."""
    targets: list[Target] = []
    understood = True
    for raw in values:
        head, sep, tail = raw.partition(":")
        if not sep:
            understood = False
            continue
        try:
            targets.append(Target(int(head), int(tail)))
        except ValueError:
            understood = False
    return targets, understood


def _compare_url(build_ids: list[int]) -> str:
    query = urlencode({"b": [str(build_id) for build_id in build_ids]}, doseq=True)
    return f"/compare?{query}"


def _throttled(request: Request) -> int | None:
    """Seconds this address must wait, or None if it may submit.

    Only the public write routes ask. The admin panel and the CLI are not
    visitors, and cancelling a job is never throttled — that saves work.
    """
    return _container(request).throttle.check(client_key(request))


def _new_context(
    request: Request, *, notice: str, body: str, title: str, picked: set[str], wait: int | None = None
) -> dict:
    settings = _settings(request)
    options = _target_options(request)
    limit = settings.max_comparison_targets
    return {
        "artifact_origin": settings.artifact_base_url,
        "notice": _SUBMIT_NOTICES.get(notice),
        "body": body,
        "title": title,
        "options": options,
        # Pre-ticked so the form is usable as it stands: the first pairs listed.
        "picked": picked or {option["value"] for option in options[:limit]},
        "max_targets": limit,
        "max_prompt_chars": settings.max_prompt_chars,
        "wait_seconds": wait,
    }


def _new_page(
    request: Request,
    *,
    notice: str = "",
    body: str = "",
    title: str = "",
    picked: set[str] | None = None,
    wait: int | None = None,
):
    """Render (or re-render) the submission form.

    A rejected submission comes back here with the text intact rather than
    through a redirect: nothing was created, so there is nothing to protect from
    a refresh, and a prompt is not something to lose to a query string.
    """
    return templates.TemplateResponse(
        request=request,
        name="new.html",
        context=_new_context(
            request, notice=notice, body=body, title=title, picked=picked or set(), wait=wait
        ),
    )


@router.get("/new")
def new_prompt_page(request: Request):
    return _new_page(request)


@router.post("/new")
async def new_prompt(
    request: Request,
    body: str = Form(""),
    title: str = Form(""),
    b: list[str] = Form(default_factory=list),
):
    """Fan one prompt out to up to ``FP_MAX_COMPARISON_TARGETS`` pairs."""
    _same_origin(request)
    settings = _settings(request)
    container = _container(request)
    text = (body or "").strip()
    pairs, understood = _parse_targets(b)

    wait = _throttled(request)
    if wait:
        return _new_page(
            request, notice="rate-limited", body=body, title=title, picked=set(b), wait=wait
        )
    if not understood:
        return _new_page(request, notice="bad-target", body=body, title=title, picked=set(b))
    if not pairs:
        return _new_page(request, notice="no-targets", body=body, title=title, picked=set(b))
    if len(pairs) > settings.max_comparison_targets:
        return _new_page(request, notice="too-many-targets", body=body, title=title, picked=set(b))
    if not text:
        return _new_page(request, notice="empty", body=body, title=title, picked=set(b))
    if len(text) > settings.max_prompt_chars:
        return _new_page(request, notice="too-long", body=body, title=title, picked=set(b))

    try:
        result = container.pipeline.submit(
            body=text, title=(title.strip() or None), targets=pairs
        )
    except PromptRejected:
        return _new_page(request, notice="refused", body=body, title=title, picked=set(b))
    except ValidationError:
        return _new_page(request, notice="unavailable", body=body, title=title, picked=set(b))

    return RedirectResponse(url=_compare_url([build.id for build in result.builds]), status_code=303)


# --- prompt detail ------------------------------------------------------------
def _prompt_or_404(slug: str, container) -> Prompt:  # noqa: ANN001
    prompt = container.prompts.get_by_slug(slug)
    if prompt is None or prompt.hidden:
        raise HTTPException(404, "no such prompt")
    return prompt


@router.get("/prompt/{slug}")
def prompt_page(request: Request, slug: str, notice: str = ""):
    settings = _settings(request)
    container = _container(request)
    prompt = _prompt_or_404(slug, container)
    builds = [build for build in container.builds.for_prompt(prompt.id) if build.is_public]
    parent = (
        container.prompts.get(prompt.parent_prompt_id) if prompt.parent_prompt_id else None
    )
    endpoint_names = {e.id: e.name for e in container.endpoints.list()}
    model_ids: dict[int, str] = {}
    for build in builds:
        model = container.endpoints.get_model(build.model_row_id)
        if model is not None:
            model_ids[build.model_row_id] = model.model_id
    options = _target_options(request)
    limit = settings.max_comparison_targets
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
            # Pre-ticked so the forms are useful without editing them: the first
            # builds of this prompt, in the order the page lists them (oldest
            # first), and the first pairs the gallery offers.
            "max_targets": limit,
            "compare_defaults": {build.id for build in builds[:limit]},
            "options": options,
            "picked": {option["value"] for option in options[:limit]},
            "notice": _SUBMIT_NOTICES.get(notice),
        },
    )


@router.post("/prompt/{slug}/run")
async def rerun_prompt(request: Request, slug: str, b: list[str] = Form(default_factory=list)):
    """Build this stored prompt again, against the chosen pairs.

    The stored text is not screened a second time: it passed the guard when it
    was submitted, and re-running is the whole point of keeping it. Editing the
    text is a fork, which screens what was typed.
    """
    _same_origin(request)
    settings = _settings(request)
    container = _container(request)
    prompt = _prompt_or_404(slug, container)

    pairs, understood = _parse_targets(b)
    bad = f"/prompt/{slug}?notice="
    if _throttled(request):
        return RedirectResponse(url=f"{bad}rate-limited", status_code=303)
    if not understood:
        return RedirectResponse(url=f"{bad}bad-target", status_code=303)
    if not pairs:
        return RedirectResponse(url=f"{bad}no-targets", status_code=303)
    if len(pairs) > settings.max_comparison_targets:
        return RedirectResponse(url=f"{bad}too-many-targets", status_code=303)

    try:
        result = container.pipeline.rerun(prompt.id, pairs)
    except ValidationError:
        return RedirectResponse(url=f"{bad}unavailable", status_code=303)
    return RedirectResponse(url=_compare_url([build.id for build in result.builds]), status_code=303)


def _fork_context(
    request: Request,
    prompt: Prompt,
    *,
    notice: str,
    body: str,
    title: str,
    picked: set[str],
    wait: int | None = None,
) -> dict:
    context = _new_context(
        request, notice=notice, body=body, title=title, picked=picked, wait=wait
    )
    context["prompt"] = prompt
    return context


@router.get("/prompt/{slug}/fork")
def fork_prompt_page(request: Request, slug: str):
    """The prompt text, editable, with the pairs to build it on."""
    container = _container(request)
    prompt = _prompt_or_404(slug, container)
    return templates.TemplateResponse(
        request=request,
        name="fork.html",
        context=_fork_context(
            request,
            prompt,
            notice="",
            body=prompt.body,
            title=prompt.title,
            picked=set(),
        ),
    )


@router.post("/prompt/{slug}/fork")
async def fork_prompt(
    request: Request,
    slug: str,
    body: str = Form(""),
    title: str = Form(""),
    b: list[str] = Form(default_factory=list),
):
    """Fork a prompt, edit the copy, and build the copy."""
    _same_origin(request)
    settings = _settings(request)
    container = _container(request)
    prompt = _prompt_or_404(slug, container)

    text = (body or "").strip()
    pairs, understood = _parse_targets(b)

    def render(notice: str, wait: int | None = None):
        """Re-render the fork form with the typed text intact."""
        return templates.TemplateResponse(
            request=request,
            name="fork.html",
            context=_fork_context(
                request, prompt, notice=notice, body=body, title=title, picked=set(b), wait=wait
            ),
        )

    wait = _throttled(request)
    if wait:
        return render("rate-limited", wait)

    for condition, code in (
        (not understood, "bad-target"),
        (not pairs, "no-targets"),
        (len(pairs) > settings.max_comparison_targets, "too-many-targets"),
        (not text, "empty"),
        (len(text) > settings.max_prompt_chars, "too-long"),
    ):
        if condition:
            return render(code)

    try:
        result = container.pipeline.fork(
            prompt.id, body=text, title=(title.strip() or None), targets=pairs
        )
    except PromptRejected:
        return render("refused")
    except ValidationError:
        return render("unavailable")
    return RedirectResponse(url=_compare_url([build.id for build in result.builds]), status_code=303)


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
