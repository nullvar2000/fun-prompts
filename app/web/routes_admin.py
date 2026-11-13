"""The admin panel: ``/admin`` on the gallery origin.

Everything that mutates lives here and behind the session, which is why the
submission API has no endpoint-write routes: an unauthenticated write surface
would let any generated page that reaches ``:8080`` reconfigure which endpoints
the builder talks to. The session and CSRF gate live in :mod:`app.web.auth`;
every route here is reached only with a live session.

Deliberately not here: any rendering of artifact HTML. Failure records show the
raw response an endpoint returned — that is the point of the admin panel — but it
is escaped into text, never framed or inlined.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import logsetup
from app.build.orchestrator import Target
from app.config import Settings
from app.domain.errors import ValidationError
from app.domain.models import Endpoint
from app.domain.status import BuildStatus, JobKind, JobState, TERMINAL_JOB_STATES
from app.providers import registry
from app.providers.registry import models_to_rows, policy_for
from app.providers.url_guard import UrlNotAllowed, validate_endpoint_url
from app.secrets import looks_masked, mask
from app.web.auth import (
    LoginAttempts,
    check_password,
    clear_session_cookie,
    client_key,
    login_session,
    set_session_cookie,
)
from app.web.templating import templates

log = logsetup.get("app.admin")

router = APIRouter(prefix="/admin", tags=["admin"])


# --- request helpers ----------------------------------------------------------
@dataclass(frozen=True)
class Form:
    """Admin form fields as text: first value wins, always ``str``.

    Written by hand rather than bound to FastAPI body models because these forms
    have optional fields where "blank" and "absent" mean different things, and
    because the panel must never accept an upload or a non-text part.
    """

    single: dict[str, str] = field(default_factory=dict)
    multi: dict[str, list[str]] = field(default_factory=dict)

    def get(self, name: str, default: str = "") -> str:
        value = self.single.get(name, default)
        return value.strip() if isinstance(value, str) else default

    def getlist(self, name: str) -> list[str]:
        return [value.strip() for value in self.multi.get(name, [])]

    def sent(self, name: str) -> bool:
        return name in self.single

    def flag(self, name: str) -> bool:
        """Checkbox-style field: present and not an explicit false."""
        return self.get(name, "0").lower() not in ("", "0", "false", "off")


async def _form(request: Request) -> Form:
    """Parse the body as text fields.

    The CSRF gate already buffered the body, so re-reading it here is cheap.
    """
    data = await request.form()
    multi: dict[str, list[str]] = {}
    for name, value in data.multi_items():
        if isinstance(value, str):
            multi.setdefault(name, []).append(value)
    return Form(single={name: values[0] for name, values in multi.items()}, multi=multi)


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _container(request: Request):  # noqa: ANN202 - Container
    return request.app.state.container


def _flash(path: str, message: str, kind: str = "ok") -> RedirectResponse:
    """Redirect with a one-line notice. The message goes in the query string so
    the browser's PRG pattern still works and the notice survives a refresh."""
    query = urlencode({"msg": message[:300], "kind": kind})
    # Some return paths are already filtered, e.g. /admin/prompts?show_hidden=1.
    separator = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{separator}{query}", status_code=303)


def _page(request: Request, name: str, status_code: int = 200, **context):  # noqa: ANN003, ANN202
    settings = _settings(request)
    container = _container(request)
    # The notice kind comes from the query string, so it is whitelisted: it ends
    # up as a CSS class name and must not be whatever a link author chose.
    kind = request.query_params.get("kind", "ok")
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={
            "artifact_origin": settings.artifact_base_url,
            "settings": settings,
            "container": container,
            "msg": request.query_params.get("msg", ""),
            "kind": kind if kind in ("ok", "error") else "ok",
            "status_counts": container.builds.count_by_status(),
            **context,
        },
        status_code=status_code,
    )


def _targets_from(values: list[str]) -> list[Target]:
    """Parse ``endpoint_id:model_row_id`` checkbox values."""
    targets: list[Target] = []
    for value in values:
        endpoint, sep, model = value.partition(":")
        if not sep:
            continue
        try:
            targets.append(Target(endpoint_id=int(endpoint), model_row_id=int(model)))
        except ValueError:
            continue
    return targets


# --- login --------------------------------------------------------------------
@router.get("/login")
def login_form(request: Request):
    session = getattr(request.state, "session", None)
    if session is not None and session.authed:
        return RedirectResponse("/admin", status_code=303)
    limiter: LoginAttempts = request.app.state.login_attempts
    return _page(
        request,
        "admin/login.html",
        error=request.query_params.get("error", ""),
        attempts_left=limiter.remaining(client_key(request)),
    )


@router.post("/login")
async def login_submit(request: Request):
    settings = _settings(request)
    limiter: LoginAttempts = request.app.state.login_attempts
    key = client_key(request)

    if not limiter.allow(key):
        log.warning("login throttled for %s", key)
        return _page(
            request,
            "admin/login.html",
            status_code=429,
            error="too many failed logins from this client; try again later",
            attempts_left=limiter.remaining(key),
        )

    fields = await _form(request)
    if not check_password(fields.get("password"), settings):
        return _page(
            request,
            "admin/login.html",
            status_code=401,
            error="wrong password",
            attempts_left=limiter.remaining(key),
        )

    limiter.reset(key)
    response = RedirectResponse("/admin", status_code=303)
    set_session_cookie(response, settings, login_session(int(settings.session_ttl_hours * 3600)))
    log.info("admin login from %s", key)
    return response


@router.post("/logout")
def logout(request: Request):
    response = RedirectResponse("/admin/login", status_code=303)
    clear_session_cookie(response)
    return response


# --- dashboard ----------------------------------------------------------------
@router.get("")
def dashboard(request: Request):
    container = _container(request)
    return _page(
        request,
        "admin/dashboard.html",
        pending=container.jobs.pending_count(),
        worker=container.worker.status(),
        guard_count=container.guard_log.count(),
        failure_count=container.builds.failure_count(),
        recent_jobs=container.jobs.recent(10),
        recent_failures=container.builds.recent_failures(5),
        endpoints=container.endpoints.list(),
    )


# --- endpoints ----------------------------------------------------------------
def _endpoint_or_404(container, endpoint_id: int) -> Endpoint:  # noqa: ANN001
    endpoint = container.endpoints.get(endpoint_id)
    if endpoint is None:
        raise HTTPException(404, "no such endpoint")
    return endpoint


def _limits(fields: Form, settings: Settings) -> tuple[dict, tuple[str, ...], list[str]]:
    """Parse the per-endpoint limits, clamping them to the environment ceilings.

    Returns ``(values, clear, notes)``: ``values`` holds what to set, ``clear``
    names the columns to send back to NULL (no per-endpoint override), and
    ``notes`` is non-empty when the input was unusable.
    """
    notes: list[str] = []
    values: dict[str, object] = {}
    clear: list[str] = []

    def optional_int(name: str, ceiling: int | None) -> None:
        raw = fields.get(name)
        if not raw:
            if fields.sent(name):
                clear.append(name)
            return
        try:
            parsed = int(raw)
        except ValueError:
            notes.append(f"{name} must be a whole number")
            return
        if parsed < 1:
            notes.append(f"{name} must be positive")
            return
        if ceiling is not None and parsed > ceiling:
            values[name] = ceiling
            notes.append(f"{name} clamped to the {ceiling} set by the environment")
        else:
            values[name] = parsed

    optional_int("timeout_s", settings.job_timeout_s)
    optional_int("max_output_tokens", settings.max_output_tokens)

    raw_temp = fields.get("temperature")
    if raw_temp:
        try:
            temperature = float(raw_temp)
        except ValueError:
            notes.append("temperature must be a number")
        else:
            if not 0.0 <= temperature <= 2.0:
                notes.append("temperature must be between 0 and 2")
            else:
                values["temperature"] = temperature
    elif fields.sent("temperature"):
        clear.append("temperature")
    return values, tuple(clear), notes


def _tags_from(fields: Form) -> tuple[str, ...]:
    return tuple(sorted({tag.strip().lower() for tag in fields.get("tags").split(",") if tag.strip()}))


def _key_update(request: Request, fields: Form) -> str | None:
    """What to store in ``api_key_encrypted``: ``None`` means leave it alone.

    The plaintext never round-trips through the form, so a blank field cannot
    mean "set it to blank" — it means "keep what is there".
    """
    container = _container(request)
    if fields.flag("clear_key"):
        return ""
    supplied = fields.get("api_key")
    if not supplied or looks_masked(supplied):
        # Blank keeps the stored key. A pasted-back masked value ("sk-…-1234")
        # is how a masked display field round-trips by accident, so it keeps it
        # too rather than storing the mask as the secret.
        return None
    return container.key_ring.encrypt(supplied)


def _no_adapter(endpoint: Endpoint) -> bool:
    """True for endpoints nothing can call, e.g. the ``seed`` placeholder.

    The bundled apps are recorded against a fake endpoint so they stay browsable;
    probing or discovering on it would only raise :class:`UnknownProvider`.
    """
    return endpoint.provider_type not in registry.known_types()


@router.get("/endpoints")
def endpoints_list(request: Request):
    container = _container(request)
    return _page(
        request,
        "admin/endpoints.html",
        rows=[
            {
                "endpoint": endpoint,
                "models": container.endpoints.list_models(endpoint.id),
                "key_state": "set" if endpoint.api_key_encrypted else "none",
            }
            for endpoint in container.endpoints.list()
        ],
        provider_types=list(registry.known_types()),
    )


@router.get("/endpoints/new")
def endpoint_new(request: Request):
    return _page(
        request,
        "admin/endpoint_form.html",
        endpoint=None,
        models=[],
        key_state="",
        provider_types=list(registry.known_types()),
        policy=registry.policy_for(_settings(request)),
    )


@router.post("/endpoints")
async def endpoint_create(request: Request):
    settings = _settings(request)
    container = _container(request)
    fields = await _form(request)

    name = fields.get("name")
    base_url = fields.get("base_url").rstrip("/")
    provider_type = fields.get("provider_type") or "openai_compat"
    if not name:
        return _flash("/admin/endpoints/new", "a name is required", "error")
    if not base_url.startswith(("http://", "https://")):
        return _flash("/admin/endpoints/new", "base URL must start with http:// or https://", "error")
    if provider_type not in registry.known_types():
        return _flash("/admin/endpoints/new", f"unknown provider type {provider_type!r}", "error")

    try:
        await validate_endpoint_url(base_url, policy_for(settings))
    except UrlNotAllowed as exc:
        return _flash("/admin/endpoints/new", f"refused: {exc}", "error")

    limits, _clear, notes = _limits(fields, settings)
    if notes:
        return _flash("/admin/endpoints/new", "; ".join(notes), "error")

    try:
        endpoint = container.endpoints.create(
            name=name,
            base_url=base_url,
            provider_type=provider_type,
            api_key_encrypted=_key_update(request, fields) or "",
            enabled=fields.flag("enabled"),
            tags=_tags_from(fields),
            timeout_s=limits.get("timeout_s"),  # type: ignore[arg-type]
            max_output_tokens=limits.get("max_output_tokens"),  # type: ignore[arg-type]
            temperature=limits.get("temperature"),  # type: ignore[arg-type]
        )
    except ValidationError as exc:
        return _flash("/admin/endpoints/new", str(exc), "error")

    log.info("admin created endpoint %s (%s)", endpoint.name, endpoint.base_url)
    return _flash("/admin/endpoints", f"created endpoint {endpoint.name}")


@router.get("/endpoints/{endpoint_id}")
def endpoint_detail(request: Request, endpoint_id: int):
    container = _container(request)
    endpoint = _endpoint_or_404(container, endpoint_id)
    return _page(
        request,
        "admin/endpoint_form.html",
        endpoint=endpoint,
        models=container.endpoints.list_models(endpoint.id),
        key_state=mask(container.key_ring.decrypt(endpoint.api_key_encrypted))
        if endpoint.api_key_encrypted
        else "",
        provider_types=list(dict.fromkeys([*registry.known_types(), endpoint.provider_type])),
        policy=registry.policy_for(_settings(request)),
        endpoint_failures=container.builds.recent_failures(10, endpoint_id=endpoint.id),
    )


@router.post("/endpoints/{endpoint_id}")
async def endpoint_update(request: Request, endpoint_id: int):
    settings = _settings(request)
    container = _container(request)
    endpoint = _endpoint_or_404(container, endpoint_id)
    fields = await _form(request)

    base_url = fields.get("base_url").rstrip("/")
    if base_url and not _no_adapter(endpoint):
        # A no-adapter endpoint (the ``seed`` placeholder) has a label where an
        # address would go; checking it would only reject the thing that makes it
        # safely uncalleable.
        if not base_url.startswith(("http://", "https://")):
            return _flash(f"/admin/endpoints/{endpoint_id}", "base URL must start with http:// or https://", "error")
        try:
            await validate_endpoint_url(base_url, policy_for(settings))
        except UrlNotAllowed as exc:
            return _flash(f"/admin/endpoints/{endpoint_id}", f"refused: {exc}", "error")

    limits, clear, notes = _limits(fields, settings)
    if notes:
        return _flash(f"/admin/endpoints/{endpoint_id}", "; ".join(notes), "error")

    try:
        container.endpoints.update(
            endpoint.id,
            name=fields.get("name") or None,
            base_url=base_url or None,
            api_key_encrypted=_key_update(request, fields),
            enabled=fields.flag("enabled"),
            tags=_tags_from(fields),
            clear=clear,
            **limits,  # type: ignore[arg-type]
        )
    except ValidationError as exc:
        return _flash(f"/admin/endpoints/{endpoint_id}", str(exc), "error")
    return _flash(f"/admin/endpoints/{endpoint_id}", f"saved {endpoint.name}")


@router.post("/endpoints/{endpoint_id}/enabled")
async def endpoint_enabled(request: Request, endpoint_id: int):
    container = _container(request)
    endpoint = _endpoint_or_404(container, endpoint_id)
    fields = await _form(request)
    enable = fields.flag("enabled")
    container.endpoints.update(endpoint.id, enabled=enable)
    return _flash(f"/admin/endpoints/{endpoint_id}", f"{endpoint.name} {'enabled' if enable else 'disabled'}")


@router.post("/endpoints/{endpoint_id}/delete")
async def endpoint_delete(request: Request, endpoint_id: int):
    container = _container(request)
    endpoint = _endpoint_or_404(container, endpoint_id)
    fields = await _form(request)
    if fields.get("confirm") != endpoint.name:
        return _flash(f"/admin/endpoints/{endpoint_id}", "type the endpoint name to confirm deletion", "error")
    container.endpoints.delete(endpoint.id)
    log.warning("admin deleted endpoint %s", endpoint.name)
    return _flash("/admin/endpoints", f"deleted {endpoint.name}")


async def _with_provider(request: Request, endpoint: Endpoint, action):  # noqa: ANN001, ANN202
    """Run one provider call and always close the adapter.

    Each adapter owns its own connection pool and event loop discipline, so the
    panel borrows one for a single discovery or probe and closes it immediately.
    """
    container = _container(request)
    provider = container.provider_for(endpoint)
    try:
        return await action(provider)
    finally:
        close = getattr(provider, "aclose", None)
        if close is not None:
            await close()


@router.post("/endpoints/{endpoint_id}/discover")
async def endpoint_discover(request: Request, endpoint_id: int):
    container = _container(request)
    endpoint = _endpoint_or_404(container, endpoint_id)
    back = f"/admin/endpoints/{endpoint_id}"
    if _no_adapter(endpoint):
        return _flash(back, f"{endpoint.provider_type} endpoints have nothing to ask for models", "error")

    async def action(provider):  # noqa: ANN001, ANN202
        try:
            models = await provider.list_models()
        except Exception as exc:
            return _flash(back, f"discovery failed: {exc}", "error")
        added, removed = container.endpoints.replace_models(endpoint.id, models_to_rows(models))
        return _flash(back, f"discovered {len(models)} models ({added} added, {removed} removed)")

    return await _with_provider(request, endpoint, action)


@router.post("/endpoints/{endpoint_id}/probe")
async def endpoint_probe(request: Request, endpoint_id: int):
    container = _container(request)
    endpoint = _endpoint_or_404(container, endpoint_id)
    back = f"/admin/endpoints/{endpoint_id}"
    if _no_adapter(endpoint):
        return _flash(back, f"{endpoint.provider_type} endpoints cannot be probed", "error")

    async def action(provider):  # noqa: ANN001, ANN202
        try:
            result = await provider.probe()
        except Exception as exc:
            return _flash(back, f"probe failed: {exc}", "error")
        return _flash(
            back,
            f"{'reachable' if result.ok else 'NOT reachable'}: {result.detail}",
            "ok" if result.ok else "error",
        )

    return await _with_provider(request, endpoint, action)


@router.post("/endpoints/{endpoint_id}/models/{model_row_id}/enabled")
async def model_enabled(request: Request, endpoint_id: int, model_row_id: int):
    container = _container(request)
    _endpoint_or_404(container, endpoint_id)
    model = container.endpoints.get_model(model_row_id)
    if model is None or model.endpoint_id != endpoint_id:
        raise HTTPException(404, "that model does not belong to this endpoint")
    fields = await _form(request)
    enable = fields.flag("enabled")
    container.endpoints.set_model_enabled(model_row_id, enable)
    return _flash(f"/admin/endpoints/{endpoint_id}", f"{model.model_id} {'enabled' if enable else 'disabled'}")


# --- prompts ------------------------------------------------------------------
PROMPTS_BACK = "/admin/prompts?show_hidden=1"


@router.get("/prompts")
def prompts_list(request: Request, show_hidden: int = 0):
    container = _container(request)
    prompts = container.prompts.list(include_hidden=True)
    hidden_count = sum(1 for prompt in prompts if prompt.hidden)
    if not show_hidden:
        prompts = [prompt for prompt in prompts if not prompt.hidden]
    return _page(
        request,
        "admin/prompts.html",
        prompts=prompts,
        build_groups={
            prompt.id: container.builds.for_prompt(prompt.id, include_hidden=True) for prompt in prompts
        },
        hidden_count=hidden_count,
        show_hidden=show_hidden,
        targets=container.endpoints.enabled_targets(),
    )


def _prompt_or_404(container, prompt_id: int):  # noqa: ANN001, ANN202
    prompt = container.prompts.get(prompt_id)
    if prompt is None:
        raise HTTPException(404, "no such prompt")
    return prompt


@router.post("/prompts/{prompt_id}")
async def prompt_update(request: Request, prompt_id: int):
    settings = _settings(request)
    container = _container(request)
    prompt = _prompt_or_404(container, prompt_id)
    fields = await _form(request)

    body = fields.get("body")
    if not body:
        return _flash(PROMPTS_BACK, "a prompt cannot be empty", "error")
    if len(body) > settings.max_prompt_chars:
        return _flash(
            PROMPTS_BACK,
            f"prompt is {len(body)} chars, over the limit of {settings.max_prompt_chars}",
            "error",
        )
    container.prompts.update(
        prompt.id,
        title=fields.get("title") or prompt.title,
        body=body,
        summary=fields.get("summary"),
    )
    return _flash(PROMPTS_BACK, f"saved prompt {prompt.slug}")


@router.post("/prompts/{prompt_id}/hidden")
async def prompt_hidden(request: Request, prompt_id: int):
    container = _container(request)
    prompt = _prompt_or_404(container, prompt_id)
    fields = await _form(request)
    hide = fields.flag("hidden")
    container.prompts.set_hidden(prompt.id, hide)
    return _flash(PROMPTS_BACK, f"{prompt.title} {'hidden' if hide else 'visible'}")


@router.post("/prompts/{prompt_id}/fork")
async def prompt_fork(request: Request, prompt_id: int):
    container = _container(request)
    prompt = _prompt_or_404(container, prompt_id)
    fields = await _form(request)
    forked = container.prompts.fork(prompt.id, title=fields.get("title") or None)
    if forked is None:
        return _flash(PROMPTS_BACK, "that prompt could not be forked", "error")
    return _flash(PROMPTS_BACK, f"forked {prompt.slug} to {forked.slug}")


@router.post("/prompts/{prompt_id}/delete")
async def prompt_delete(request: Request, prompt_id: int):
    container = _container(request)
    prompt = _prompt_or_404(container, prompt_id)
    fields = await _form(request)
    if fields.get("confirm") != "delete":
        return _flash(PROMPTS_BACK, "type 'delete' to confirm", "error")
    container.prompts.delete(prompt.id)
    log.warning("admin deleted prompt %s and its builds", prompt.slug)
    return _flash(PROMPTS_BACK, f"deleted {prompt.slug}")


@router.post("/prompts/{prompt_id}/run")
async def prompt_run(request: Request, prompt_id: int):
    """Re-run a stored prompt against the chosen endpoints — no editing needed."""
    container = _container(request)
    prompt = _prompt_or_404(container, prompt_id)
    fields = await _form(request)
    targets = _targets_from(fields.getlist("targets"))
    if not targets:
        return _flash(PROMPTS_BACK, "choose at least one endpoint + model", "error")
    try:
        result = container.pipeline.rerun(prompt.id, targets)
    except ValidationError as exc:
        return _flash(PROMPTS_BACK, str(exc), "error")
    return _flash(PROMPTS_BACK, f"queued {len(result.builds)} build(s) for {prompt.slug}")


# --- builds -------------------------------------------------------------------
@router.get("/builds")
def builds_list(request: Request, status: str = ""):
    container = _container(request)
    try:
        wanted = (BuildStatus(status),) if status else None
    except ValueError:
        wanted = None
    builds = container.builds.list(
        statuses=wanted or tuple(BuildStatus),
        include_hidden=True,
        limit=100,
        sort="recently-built",
    )
    # One pass over recent jobs instead of a query per row: the panel wants to
    # offer cancel/retry, which means knowing what the queue is holding.
    active = {
        job.build_id: job
        for job in container.jobs.recent(200)
        if job.build_id is not None and job.state not in TERMINAL_JOB_STATES
    }
    # Name lookups done once for the page: a build row stores ids, and the panel
    # is unreadable without the endpoint and model names next to them.
    endpoints = container.endpoints.list()
    endpoint_names = {endpoint.id: endpoint.name for endpoint in endpoints}
    model_labels: dict[int, str] = {}
    for endpoint in endpoints:
        for model in container.endpoints.list_models(endpoint.id):
            model_labels[model.id] = model.label
    return _page(
        request,
        "admin/builds.html",
        builds=builds,
        statuses=[str(s) for s in BuildStatus],
        active_status=status,
        active_jobs=active,
        endpoint_names=endpoint_names,
        model_labels=model_labels,
    )


def _build_or_404(container, build_id: int):  # noqa: ANN001, ANN202
    build = container.builds.get(build_id)
    if build is None:
        raise HTTPException(404, "no such build")
    return build


@router.post("/builds/{build_id}/hidden")
async def build_hidden(request: Request, build_id: int):
    container = _container(request)
    build = _build_or_404(container, build_id)
    fields = await _form(request)
    hide = fields.flag("hidden")
    container.builds.set_hidden(build.id, hide)
    return _flash("/admin/builds", f"build {build.id} {'hidden' if hide else 'visible'}")


@router.post("/builds/{build_id}/delete")
async def build_delete(request: Request, build_id: int):
    container = _container(request)
    build = _build_or_404(container, build_id)
    fields = await _form(request)
    if fields.get("confirm") != "delete":
        return _flash("/admin/builds", "type 'delete' to confirm", "error")
    container.builds.delete(build.id)
    log.warning("admin deleted build %s", build.id)
    return _flash("/admin/builds", f"deleted build {build.id}")


@router.post("/builds/{build_id}/retry")
async def build_retry(request: Request, build_id: int):
    container = _container(request)
    build = _build_or_404(container, build_id)
    if build.status is BuildStatus.PUBLISHED:
        return _flash(
            "/admin/builds",
            "a published build is not re-generated; refine adds a version (M6)",
            "error",
        )
    if build.status in (BuildStatus.QUEUED, BuildStatus.GENERATING, BuildStatus.VALIDATING):
        return _flash("/admin/builds", "that build is already queued or running", "error")
    endpoint = container.endpoints.get(build.endpoint_id)
    if endpoint is None or not endpoint.enabled:
        return _flash("/admin/builds", "its endpoint is gone or disabled", "error")

    container.builds.set_status(build.id, BuildStatus.QUEUED)
    job = container.jobs.enqueue(JobKind.BUILD, build_id=build.id, endpoint_id=endpoint.id)
    return _flash("/admin/builds", f"requeued build {build.id} as job {job.id}")


@router.post("/builds/{build_id}/cancel")
async def build_cancel(request: Request, build_id: int):
    container = _container(request)
    _build_or_404(container, build_id)
    for job in reversed(container.jobs.for_build(build_id)):
        if job.state in (JobState.QUEUED, JobState.RUNNING):
            container.pipeline.request_cancel(job.id)
            return _flash("/admin/builds", f"cancel requested for job {job.id}")
    return _flash("/admin/builds", "that build has nothing running to cancel", "error")


@router.post("/builds/{build_id}/tags")
async def build_tag(request: Request, build_id: int):
    container = _container(request)
    _build_or_404(container, build_id)
    fields = await _form(request)
    tag = fields.get("tag").lower()
    if not tag:
        return _flash("/admin/builds", "enter a tag", "error")
    container.builds.tag_build(build_id, (tag,), "manual")
    return _flash("/admin/builds", f"tagged build {build_id} with {tag}")


@router.post("/builds/{build_id}/tags/remove")
async def build_untag(request: Request, build_id: int):
    container = _container(request)
    _build_or_404(container, build_id)
    fields = await _form(request)
    container.builds.untag_build(build_id, fields.get("tag"))
    return _flash("/admin/builds", f"removed that tag from build {build_id}")


# --- logs ---------------------------------------------------------------------
@router.get("/failures")
def failures(request: Request, limit: int = 100):
    container = _container(request)
    return _page(request, "admin/failures.html", failures=container.builds.recent_failures(min(500, max(1, limit))))


@router.get("/guard")
def guard(request: Request, limit: int = 100):
    container = _container(request)
    return _page(request, "admin/guard.html", entries=container.guard_log.recent(min(500, max(1, limit))))


@router.get("/tags")
def tags(request: Request):
    container = _container(request)
    return _page(request, "admin/tags.html", tags=container.builds.tag_catalog())


def configure_login_throttle(app, settings: Settings) -> None:  # noqa: ANN001
    """Attach the login throttle. Called once at app start, not per request."""
    app.state.login_attempts = LoginAttempts(settings.login_max_attempts_per_hour)
