"""The build pipeline: submit, generate, extract, validate, publish.

Two halves share one class:

* ``submit`` / ``rerun`` / ``refine`` are synchronous and only touch the
  database: they screen the text, create build or version records and enqueue
  jobs.
* ``run_job`` is asynchronous and does the network work: it drives the build
  through its states, calls the provider, extracts and validates the artifact,
  retries once with a repair instruction, and publishes or records a failure.

Nothing publishes without passing the validator, and every rejected attempt is
kept in ``build_failures`` so a misbehaving endpoint can be read about later.

A ``refine`` job is the exception to the state machine, deliberately: a
follow-up turn adds a *version* to a build that is already published, so the
build status never moves. A published build has no legal transitions out of
``PUBLISHED`` — and that is what keeps the currently served artifact online while
its next version is being generated. The job carries all the progress, and a
failed refine leaves the published version exactly where it was.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app import logsetup
from app.build.extract import extract_html
from app.build.guard import (
    GuardVerdict,
    PromptRejected,
    builder_system_prompt,
    scan_prompt,
    task_block,
)
from app.build.refine import plan_refine
from app.build.tagging import derive_tags
from app.build.validate import repair_instruction, validate_artifact
from app.config import Settings
from app.domain.errors import ValidationError
from app.domain.models import Build, BuildVersion, Endpoint, Job, Prompt
from app.domain.status import BuildStatus, JobKind, JobState
from app.providers.base import (
    ChatMessage,
    Completion,
    ProviderAdapter,
    ProviderError,
    ProviderTimeout,
)
from app.providers.registry import Limits, UnknownProvider, resolve_limits
from app.providers.url_guard import UrlNotAllowed
from app.storage.db import Database
from app.storage.repos.builds import BuildRepo
from app.storage.repos.endpoints import EndpointRepo
from app.storage.repos.guard import GuardLogRepo
from app.storage.repos.jobs import JobRepo
from app.storage.repos.prompts import PromptRepo

log = logsetup.get("app.build.orchestrator")

CANCEL_POLL_S = 0.5
#: How much of a rejected attempt is echoed back for the repair retry.
ECHO_CLIP_CHARS = 8000


class Cancelled(ValueError):
    """Raised internally when a job is asked to stop mid-flight."""


@dataclass(frozen=True)
class Target:
    endpoint_id: int
    model_row_id: int

    def as_pair(self) -> tuple[int, int]:
        return self.endpoint_id, self.model_row_id


@dataclass(frozen=True)
class SubmitResult:
    prompt: Prompt
    builds: tuple[Build, ...]
    jobs: tuple[Job, ...]


@dataclass(frozen=True)
class RefineResult:
    """One queued follow-up turn. The new version does not exist yet: it appears
    when the job runs, and ``from_version_id`` is the one it was based on."""

    build: Build
    job: Job
    from_version_id: int


class BuildPipeline:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        provider_for: Callable[[Endpoint], ProviderAdapter],
    ) -> None:
        self.db = db
        self.settings = settings
        self.provider_for = provider_for
        self.prompts = PromptRepo(db)
        self.builds = BuildRepo(db)
        self.endpoints = EndpointRepo(db)
        self.jobs = JobRepo(db)
        self.guard_log = GuardLogRepo(db)

    # --- submission (synchronous: no network) --------------------------------
    def _screen(self, text: str, *, prompt_id: int | None = None) -> GuardVerdict:
        """Screen submitted text and log whatever it finds."""
        verdict = scan_prompt(text, allow_markers=self.settings.allow_prompt_override_markers)
        if not verdict.ok:
            self.guard_log.log(
                matched_rule=verdict.rule_id,
                snippet=verdict.snippet,
                action="block",
                prompt_id=prompt_id,
            )
            raise PromptRejected(verdict)
        for flag in verdict.flags:
            self.guard_log.log(matched_rule=flag, snippet="", action="flag", prompt_id=prompt_id)
        return verdict

    def submit(
        self,
        *,
        body: str,
        targets: Sequence[Target],
        title: str | None = None,
        summary: str = "",
    ) -> SubmitResult:
        self._check_body(body)
        resolved = self._check_targets(targets)

        self._screen(body)
        prompt = self.prompts.create(
            title=title or self._default_title(body), body=body, summary=summary
        )
        return self._schedule(prompt, resolved)

    def fork(
        self,
        prompt_id: int,
        *,
        body: str,
        targets: Sequence[Target],
        title: str | None = None,
    ) -> SubmitResult:
        """Copy a stored prompt, edit the copy, and build the copy.

        The edited text is screened like any new submission: forking is not a way
        around the guard, and the lineage is kept so the gallery can show where a
        prompt came from.
        """
        self._check_body(body)
        original = self.prompts.get(prompt_id)
        if original is None:
            raise ValidationError(f"prompt {prompt_id} does not exist")
        resolved = self._check_targets(targets)

        self._screen(body, prompt_id=prompt_id)
        forked = self.prompts.fork(prompt_id, title=title or None, body=body)
        if forked is None:
            raise ValidationError(f"prompt {prompt_id} could not be forked")
        log.info("prompt %s forked as %s", original.slug, forked.slug)
        return self._schedule(forked, resolved)

    def rerun(self, prompt_id: int, targets: Sequence[Target]) -> SubmitResult:
        """Queue the same stored prompt against other targets."""
        prompt = self.prompts.get(prompt_id)
        if prompt is None:
            raise ValidationError(f"prompt {prompt_id} does not exist")
        resolved = self._check_targets(targets)
        return self._schedule(prompt, resolved)

    def refine(self, build_id: int, instruction: str, *, version_id: int | None = None) -> RefineResult:
        """Queue a follow-up turn on one build, on that build's own model.

        Only a published build can be refined: the follow-up text is applied to a
        version that exists, and a build that never published gets a re-run
        instead. The model is never part of this call — that is what makes the
        version chain mean something.
        """
        text = (instruction or "").strip()
        if not text:
            raise ValidationError("a follow-up instruction is required")
        if len(text) > self.settings.max_prompt_chars:
            raise ValidationError(
                f"instruction is {len(text)} chars, over the limit of {self.settings.max_prompt_chars}"
            )

        build = self.builds.get(build_id)
        if build is None:
            raise ValidationError(f"build {build_id} does not exist")
        if build.status is not BuildStatus.PUBLISHED:
            raise ValidationError(
                f"build {build_id} is {build.status}; only a published build can be refined"
            )

        source = self._refine_source(build, version_id)
        self._screen(text, prompt_id=build.prompt_id)

        job = self.jobs.enqueue(
            JobKind.REFINE,
            build_id=build.id,
            version_id=source.id,
            endpoint_id=build.endpoint_id,
            instruction=text,
        )
        log.info("refine queued for build %s from version %s", build.id, source.version_no)
        return RefineResult(build=build, job=job, from_version_id=source.id)

    def _refine_source(self, build: Build, version_id: int | None) -> BuildVersion:
        """Which published version the follow-up turn starts from."""
        if version_id is not None:
            version = self.builds.get_version(version_id)
            if version is None or version.build_id != build.id:
                raise ValidationError(f"version {version_id} is not part of build {build.id}")
            return version
        version = self.builds.latest_version(build.id)
        if version is None:
            raise ValidationError(f"build {build.id} has no published version to refine")
        return version

    def request_cancel(self, job_id: int) -> Job | None:
        return self.jobs.request_cancel(job_id)

    def _check_body(self, body: str) -> None:
        text = (body or "").strip()
        if not text:
            raise ValidationError("prompt body is empty")
        if len(text) > self.settings.max_prompt_chars:
            raise ValidationError(
                f"prompt is {len(text)} chars, over the limit of {self.settings.max_prompt_chars}"
            )

    @staticmethod
    def _default_title(body: str) -> str:
        """Title a prompt from its first line, so the gallery stays readable."""
        for line in body.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                return (stripped[:70] + "…") if len(stripped) > 70 else stripped
        return "untitled prompt"

    def _check_targets(self, targets: Sequence[Target]) -> list[tuple[Endpoint, int]]:
        if not targets:
            raise ValidationError("at least one endpoint+model target is required")
        if len(targets) > self.settings.max_comparison_targets:
            raise ValidationError(
                f"{len(targets)} targets requested, limit is {self.settings.max_comparison_targets}"
            )
        resolved: list[tuple[Endpoint, int]] = []
        for target in targets:
            endpoint = self.endpoints.get(target.endpoint_id)
            if endpoint is None:
                raise ValidationError(f"endpoint {target.endpoint_id} does not exist")
            if not endpoint.enabled:
                raise ValidationError(f"endpoint {endpoint.name} is disabled")
            model = self.endpoints.get_model(target.model_row_id)
            if model is None or model.endpoint_id != endpoint.id:
                raise ValidationError(
                    f"model {target.model_row_id} is not offered by {endpoint.name}"
                )
            if not model.enabled:
                raise ValidationError(f"model {model.label} is disabled")
            resolved.append((endpoint, model.id))
        return resolved

    def _schedule(self, prompt: Prompt, targets: Sequence[tuple[Endpoint, int]]) -> SubmitResult:
        builds: list[Build] = []
        jobs: list[Job] = []
        for endpoint, model_row_id in targets:
            build = self.builds.create(
                prompt_id=prompt.id, endpoint_id=endpoint.id, model_row_id=model_row_id
            )
            builds.append(build)
            jobs.append(
                self.jobs.enqueue(
                    JobKind.BUILD, build_id=build.id, endpoint_id=endpoint.id
                )
            )
        return SubmitResult(prompt=prompt, builds=tuple(builds), jobs=tuple(jobs))

    # --- execution (asynchronous: network + validation) -----------------------
    async def run_job(self, job: Job) -> Job:
        if job.kind is JobKind.REFINE:
            return await self._run_refine(job)

        build = self.builds.get(job.build_id) if job.build_id else None
        if build is None:
            return self._fail_job(job, stage="provider", error="job has no build")

        endpoint = self.endpoints.get(build.endpoint_id)
        model = self.endpoints.get_model(build.model_row_id)
        if endpoint is None or model is None:
            return self._fail_build(
                job, build, stage="provider", error="endpoint or model no longer exists"
            )
        if not endpoint.enabled:
            return self._fail_build(
                job, build, stage="provider", error=f"endpoint {endpoint.name} is disabled"
            )

        try:
            provider = self.provider_for(endpoint)
        except UnknownProvider as exc:
            return self._fail_build(job, build, stage="provider", error=str(exc))

        self.builds.set_status(build.id, BuildStatus.GENERATING)
        limits = resolve_limits(endpoint, self.settings)
        messages = [
            ChatMessage("system", builder_system_prompt(limits.max_output_tokens)),
            ChatMessage("user", task_block(self._prompt_body(build))),
        ]

        try:
            return await self._attempt_publish(
                job, build, provider, model.model_id, messages, limits
            )
        except Cancelled:
            return self._cancel(job, build)
        except (ProviderTimeout, UrlNotAllowed) as exc:
            return self._fail_build(
                job, build, stage="provider", error=str(exc), retryable=True
            )
        except ProviderError as exc:
            return self._fail_build(job, build, stage="provider", error=str(exc))
        finally:
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()

    async def _run_refine(self, job: Job) -> Job:
        """One follow-up turn. The build's status is never touched."""
        build = self.builds.get(job.build_id) if job.build_id else None
        if build is None:
            return self._fail_job(job, stage="provider", error="refine job has no build")
        if not job.instruction.strip():
            return self._fail_refine(job, build, stage="provider", error="refine job has no instruction")

        source = self.builds.get_version(job.version_id) if job.version_id else None
        if source is None or source.build_id != build.id:
            return self._fail_refine(job, build, stage="provider", error="the version being refined is gone")

        endpoint = self.endpoints.get(build.endpoint_id)
        model = self.endpoints.get_model(build.model_row_id)
        if endpoint is None or model is None:
            return self._fail_refine(job, build, stage="provider", error="endpoint or model no longer exists")
        if not endpoint.enabled:
            return self._fail_refine(job, build, stage="provider", error=f"endpoint {endpoint.name} is disabled")

        try:
            provider = self.provider_for(endpoint)
        except UnknownProvider as exc:
            return self._fail_refine(job, build, stage="provider", error=str(exc))

        limits = resolve_limits(endpoint, self.settings)
        plan = plan_refine(
            prompt_body=self._prompt_body(build),
            previous_html=source.html,
            instruction=job.instruction,
            max_output_tokens=limits.max_output_tokens,
            context_chars=self.settings.refine_context_chars,
        )
        log.info(
            "refining build %s (version %s → %s, %s chars of context%s)",
            build.id,
            source.version_no,
            source.version_no + 1,
            plan.context_chars,
            ", clipped" if plan.clipped else "",
        )

        try:
            return await self._attempt_publish(
                job,
                build,
                provider,
                model.model_id,
                plan.messages,
                limits,
                parent_version_id=source.id,
                keep_published=True,
                extra_validation=plan.as_dict(),
            )
        except Cancelled:
            return self._cancel_refine(job, build)
        except (ProviderTimeout, UrlNotAllowed) as exc:
            return self._fail_refine(job, build, stage="provider", error=str(exc), retryable=True)
        except ProviderError as exc:
            return self._fail_refine(job, build, stage="provider", error=str(exc))
        finally:
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()

    async def _attempt_publish(
        self,
        job: Job,
        build: Build,
        provider: ProviderAdapter,
        model_id: str,
        messages: list[ChatMessage],
        limits: Limits,
        *,
        parent_version_id: int | None = None,
        keep_published: bool = False,
        extra_validation: dict | None = None,
    ) -> Job:
        """Generate → extract → validate → publish, with one repair retry.

        ``keep_published`` is the refine case: the build is already published and
        its status machine has no way out of ``PUBLISHED``, so the job carries the
        progress and a rejection cannot take the current version offline.
        """
        attempt = 0
        while True:
            self._check_cancel(job)
            completion = await self._complete(provider, model_id, messages, limits, job)

            extracted = extract_html(completion.text)
            if not keep_published:
                self.builds.set_status(build.id, BuildStatus.VALIDATING)
            if not extracted.html:
                self.builds.record_failure(
                    build.id,
                    job_id=job.id,
                    stage="extract",
                    error_text=f"no HTML document found in response ({extracted.source})",
                    raw_response=completion.text,
                )
                return self._publish_failed(job, build, keep_published)

            report = validate_artifact(extracted.html, max_bytes=self.settings.max_artifact_bytes)
            if report.ok:
                version = self.builds.add_version(
                    build.id,
                    html=extracted.html,
                    raw_response=completion.text,
                    usage=completion.usage,
                    latency_ms=completion.latency_ms,
                    finish_reason=completion.finish_reason,
                    model_returned=completion.model_returned,
                    validation={
                        **report.as_dict(),
                        "extract": extracted.source,
                        "model_latency_ms": completion.latency_ms,
                        **(extra_validation or {}),
                    },
                    parent_version_id=parent_version_id,
                )
                self._apply_tags(build)
                if not keep_published:
                    self.builds.set_status(build.id, BuildStatus.PUBLISHED)
                log.info(
                    "build %s published (version %s, %s bytes, %s ms)",
                    build.id,
                    version.version_no,
                    report.size_bytes,
                    completion.latency_ms,
                )
                return self.jobs.transition(job.id, JobState.DONE)

            self.builds.record_failure(
                build.id,
                job_id=job.id,
                stage="validate",
                error_text="artifact rejected: " + ", ".join(report.violations),
                violations=report.violations,
                raw_response=completion.text,
            )
            if attempt >= self.settings.job_repair_retries:
                return self._publish_failed(job, build, keep_published)

            attempt += 1
            log.info("build %s failed validation (%s); repairing", build.id, report.violations)
            if not keep_published:
                self.builds.set_status(build.id, BuildStatus.GENERATING)
            messages = [
                *messages,
                ChatMessage("assistant", completion.text[:ECHO_CLIP_CHARS]),
                ChatMessage("user", repair_instruction(report)),
            ]

    async def _complete(
        self,
        provider: ProviderAdapter,
        model_id: str,
        messages: list[ChatMessage],
        limits: Limits,
        job: Job,
    ) -> Completion:
        """Call the provider, but abandon it if cancellation is requested."""
        task = asyncio.create_task(
            provider.complete(
                model=model_id,
                messages=messages,
                max_output_tokens=limits.max_output_tokens,
            )
        )
        watcher = asyncio.create_task(self._cancel_watcher(job.id, task))
        try:
            return await task
        except asyncio.CancelledError as exc:
            raise Cancelled(f"job {job.id} cancelled during generation") from exc
        finally:
            watcher.cancel()

    async def _cancel_watcher(self, job_id: int, task: asyncio.Task) -> None:
        while not task.done():
            await asyncio.sleep(CANCEL_POLL_S)
            if self.jobs.cancel_requested(job_id):
                task.cancel()
                return

    def _check_cancel(self, job: Job) -> None:
        if self.jobs.cancel_requested(job.id):
            raise Cancelled(f"job {job.id} cancellation requested")

    def _apply_tags(self, build: Build) -> None:
        endpoint = self.endpoints.get(build.endpoint_id)
        prompt = self.prompts.get(build.prompt_id)
        if endpoint is not None and endpoint.tags:
            self.builds.tag_build(build.id, endpoint.tags, "endpoint")
        if prompt is not None:
            self.builds.tag_build(build.id, derive_tags(prompt.body), "derived")

    def _prompt_body(self, build: Build) -> str:
        prompt = self.prompts.get(build.prompt_id)
        return prompt.body if prompt else ""

    # --- outcomes -------------------------------------------------------------
    def _fail_job(self, job: Job, *, stage: str, error: str) -> Job:
        log.warning("job %s failed (%s): %s", job.id, stage, error)
        return self.jobs.transition(job.id, JobState.FAILED, error=error)

    def _fail_build(
        self,
        job: Job,
        build: Build,
        *,
        stage: str,
        error: str,
        retryable: bool = False,
    ) -> Job:
        self.builds.record_failure(
            build.id, job_id=job.id, stage=stage, error_text=error, raw_response=""
        )
        current = self.builds.get(build.id)
        if current and current.status is not BuildStatus.FAILED:
            self.builds.set_status(build.id, BuildStatus.FAILED)

        if retryable and job.attempts < self.settings.job_max_attempts:
            log.info("job %s retrying (attempt %s of %s)", job.id, job.attempts, self.settings.job_max_attempts)
            self.builds.set_status(build.id, BuildStatus.QUEUED)
            return self.jobs.requeue(job.id)

        log.warning("build %s failed (%s): %s", build.id, stage, error)
        return self.jobs.transition(job.id, JobState.FAILED, error=error)

    def _reject(self, job: Job, build: Build) -> Job:
        self.builds.set_status(build.id, BuildStatus.REJECTED)
        return self.jobs.transition(job.id, JobState.FAILED, error="artifact failed validation")

    def _publish_failed(self, job: Job, build: Build, keep_published: bool) -> Job:
        """Stop a rejected attempt.

        For a refine that means *only* the job fails: the version that is already
        published stays published and stays served. Turning the build REJECTED
        would take a working app offline because one follow-up turn went badly.
        """
        if keep_published:
            return self.jobs.transition(job.id, JobState.FAILED, error="revision failed validation")
        return self._reject(job, build)

    def _fail_refine(
        self,
        job: Job,
        build: Build,
        *,
        stage: str,
        error: str,
        retryable: bool = False,
    ) -> Job:
        """Record a refine failure on the build, but leave the build itself alone."""
        self.builds.record_failure(
            build.id, job_id=job.id, stage=stage, error_text=error, raw_response=""
        )
        if retryable and job.attempts < self.settings.job_max_attempts:
            log.info(
                "refine job %s retrying (attempt %s of %s)",
                job.id,
                job.attempts,
                self.settings.job_max_attempts,
            )
            return self.jobs.requeue(job.id)
        log.warning("refine of build %s failed (%s): %s", build.id, stage, error)
        return self.jobs.transition(job.id, JobState.FAILED, error=error)

    def _cancel_refine(self, job: Job, build: Build) -> Job:
        self.builds.record_failure(
            build.id, job_id=job.id, stage="cancelled", error_text="refine cancelled by request"
        )
        current = self.jobs.get(job.id)
        if current and current.state is JobState.CANCELLING:
            return self.jobs.transition(job.id, JobState.CANCELLED)
        return self.jobs.transition(job.id, JobState.FAILED, error="cancelled")

    def _cancel(self, job: Job, build: Build) -> Job:
        self.builds.record_failure(
            build.id, job_id=job.id, stage="cancelled", error_text="cancelled by request"
        )
        self.builds.set_status(build.id, BuildStatus.CANCELLED)
        current = self.jobs.get(job.id)
        if current and current.state is JobState.CANCELLING:
            return self.jobs.transition(job.id, JobState.CANCELLED)
        return self.jobs.transition(job.id, JobState.FAILED, error="cancelled")
