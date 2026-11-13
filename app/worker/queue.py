"""Background build worker.

One asyncio loop inside the web process: it claims due jobs from the SQLite
queue and runs them with a bounded number of concurrent generations.

The trade-off worth stating: generation is async (network), while the repository
calls are synchronous SQLite. Those calls are short single statements, so they
run inline rather than adding a threadpool hop; the write path uses WAL, and API
handlers that touch the database run on FastAPI's threadpool. On a LAN-scale
deployment with a couple of concurrent builds this is the simplest thing that
behaves correctly.

On shutdown the worker stops claiming and cancels in-flight tasks. It does not
rewrite those job rows: they stay ``running`` so the next boot's recovery turns
them into ``interrupted``, which is the honest state — we do not know whether the
endpoint finished.
"""

from __future__ import annotations

import asyncio

from app import logsetup
from app.build.orchestrator import BuildPipeline
from app.config import Settings
from app.domain.models import Job
from app.domain.status import BuildStatus, JobState
from app.storage.repos.builds import BuildRepo
from app.storage.repos.jobs import JobRepo

log = logsetup.get("app.worker")


class BuildWorker:
    def __init__(
        self,
        *,
        jobs: JobRepo,
        builds: BuildRepo,
        pipeline: BuildPipeline,
        settings: Settings,
    ) -> None:
        self.jobs = jobs
        self.builds = builds
        self.pipeline = pipeline
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.worker_concurrency)
        self._running: dict[int, asyncio.Task] = {}
        self._loop_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # --- lifecycle -----------------------------------------------------------
    def recover_interrupted(self) -> int:
        """Turn jobs orphaned by a dead process into visible failures."""
        recovered = 0
        for job in self.jobs.recover_interrupted():
            recovered += 1
            if job.build_id is None:
                continue
            build = self.builds.get(job.build_id)
            if build is None:
                continue
            self.builds.record_failure(
                job.build_id,
                job_id=job.id,
                stage="interrupted",
                error_text="worker stopped while the job was running",
            )
            if build.status not in {
                BuildStatus.PUBLISHED,
                BuildStatus.FAILED,
                BuildStatus.REJECTED,
                BuildStatus.CANCELLED,
            }:
                self.builds.set_status(job.build_id, BuildStatus.FAILED)
        if recovered:
            log.warning("recovered %s interrupted job(s) from a previous run", recovered)
        return recovered

    async def start(self) -> None:
        if self._loop_task is not None:
            return
        self._stop.clear()
        self._loop_task = asyncio.create_task(self._loop(), name="build-worker")
        log.info(
            "worker started (concurrency %s, poll %sms)",
            self.settings.worker_concurrency,
            self.settings.worker_poll_interval_ms,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        for task in list(self._running.values()):
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()
        log.info("worker stopped")

    @property
    def running_job_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._running))

    def status(self) -> dict:
        return {
            "running": len(self._running),
            "concurrency": self.settings.worker_concurrency,
            "pending": self.jobs.pending_count(),
        }

    # --- internals -----------------------------------------------------------
    async def _loop(self) -> None:
        poll = self.settings.worker_poll_interval_ms / 1000
        while not self._stop.is_set():
            try:
                job = self.jobs.claim_next()
            except Exception:  # pragma: no cover - sqlite hiccup, keep polling
                log.exception("failed to claim a job")
                await asyncio.sleep(poll)
                continue

            if job is None:
                await asyncio.sleep(poll)
                continue

            task = asyncio.create_task(self._run(job), name=f"job-{job.id}")
            self._running[job.id] = task
            task.add_done_callback(lambda finished, job_id=job.id: self._on_done(job_id, finished))

    def _on_done(self, job_id: int, task: asyncio.Task) -> None:
        self._running.pop(job_id, None)
        if task.cancelled():
            log.info("job %s task cancelled at shutdown", job_id)

    async def _run(self, job: Job) -> None:
        async with self._semaphore:
            try:
                await self.pipeline.run_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a bug must not take the loop down
                log.exception("job %s crashed", job.id)
                try:
                    current = self.jobs.get(job.id)
                    if current and current.state not in {
                        JobState.DONE,
                        JobState.FAILED,
                        JobState.CANCELLED,
                        JobState.INTERRUPTED,
                    }:
                        self.jobs.transition(job.id, JobState.FAILED, error=f"worker error: {exc}")
                except Exception:  # pragma: no cover - terminal-state race
                    log.warning("could not mark job %s failed", job.id)
