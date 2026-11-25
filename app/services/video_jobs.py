"""In-memory background jobs for video analysis (Phase 8).

Design (single process, single worker initially):

* ``submit`` is called by the upload route AFTER the file has been validated. It takes OWNERSHIP of the private work
  directory holding the video; the directory is deleted when the job ends in any way (completed, unable_to_assess,
  failed, timeout, cancellation, shutdown, expiry). Nothing is kept permanently.
* A fixed number of worker tasks (``VIDEO_WORKERS``, default 1) read from a queue. The number of queued + running jobs is
  capped (``VIDEO_MAX_ACTIVE_JOBS``, and per client ``VIDEO_MAX_ACTIVE_JOBS_PER_CLIENT``), so background work can never
  grow without bound. Each job runs under a hard timeout. The Gemini call itself still goes through the shared pipeline,
  so it also respects the global Gemini concurrency limit.
* Job ids are 192-bit random tokens. Finished jobs are forgotten ``VIDEO_JOB_TTL_SECONDS`` after they finish (purged
  lazily and by a small janitor task); the total number of stored records is capped.
* State changes go through one function that rejects invalid transitions (see ``app/schemas/jobs.py``).

Limitations: jobs live only in this process's memory. A restart, crash or deployment loses every job (running jobs are
marked ``failed`` on a clean shutdown) and a status request for such an id returns 404. There is no sharing between
processes: with more than one worker process each would have its own, unrelated job table.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from app.core.config import Settings
from app.core.errors import InputError, ReasonCode, ServiceBusy
from app.core.logging import log_event
from app.schemas.analysis import AnalysisResponse
from app.schemas.jobs import (STAGE_PROGRESS, TERMINAL_STATUSES, TRANSITIONS, JobError, JobStatus, VideoJobAccepted,
                              VideoJobStatus)
from app.schemas.verdict import AnalysisStatus
from app.utils.temp_files import remove_work_dir

logger = logging.getLogger("scamshield.jobs")

MAX_STORED_JOBS = 500
JANITOR_INTERVAL_SECONDS = 30.0
STATUS_PATH = "/api/v1/video-status/"

# analyze(path, size, duration, on_stage) -> AnalysisResponse. Supplied by the application (AnalysisPipeline.run_video).
Analyzer = Callable[[Path, int | None, float | None, Callable[[str], None]], Awaitable[AnalysisResponse]]


class InvalidTransition(ValueError):
    pass


class TooManyJobs(InputError):
    """One client already has the maximum number of active jobs."""

    status_code = 429
    code = "TOO_MANY_JOBS"

    def __init__(self):
        super().__init__("You already have video analyses in progress. Wait for one to finish, then try again.",
                         headers={"Retry-After": "30"})


@dataclass
class VideoJob:
    id: str
    owner: str
    created_at: float
    updated_at: float
    status: JobStatus = JobStatus.QUEUED
    stage: str = "queued"
    finished_at: float | None = None
    result: AnalysisResponse | None = None
    error: JobError | None = None
    size: int | None = None
    duration: float | None = None
    work_dir: Path | None = field(default=None, repr=False)     # never exposed
    path: Path | None = field(default=None, repr=False)         # never exposed

    @property
    def active(self) -> bool:
        return self.status not in TERMINAL_STATUSES


def _iso(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


class JobManager:
    def __init__(self, settings: Settings, analyze: Analyzer, clock: Callable[[], float] = time.time):
        self._settings = settings
        self._analyze = analyze
        self._clock = clock
        self._jobs: dict[str, VideoJob] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Start the workers and the janitor on the running loop (idempotent; restarts them if the loop changed)."""
        loop = asyncio.get_running_loop()
        if self._tasks and self._loop is loop and not all(t.done() for t in self._tasks):
            return
        self._loop = loop
        self._queue = asyncio.Queue()
        for job in self._jobs.values():                       # a previous loop's queued work cannot continue
            if job.status == JobStatus.QUEUED:
                self._finish_failed(job, "SERVER_RESTARTED", "The analysis was interrupted. Please submit the video again.")
        self._tasks = [asyncio.create_task(self._worker(), name=f"video-worker-{i}") for i in range(self._settings.video_workers)]
        self._tasks.append(asyncio.create_task(self._janitor(), name="video-janitor"))

    async def aclose(self) -> None:
        """Stop the workers, mark unfinished jobs failed and delete every remaining temporary file."""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job in list(self._jobs.values()):
            if job.active:
                self._finish_failed(job, "SERVER_SHUTDOWN", "The server was shutting down. Please submit the video again.")
            self._release_files(job)
        log_event(logger, logging.INFO, "jobs_closed", remaining=len(self._jobs))

    # ------------------------------------------------------------------ submission and lookup
    def active_count(self) -> int:
        return sum(1 for job in self._jobs.values() if job.active)

    def check_capacity(self, owner: str) -> None:
        """Raise before any expensive work (e.g. receiving a 100 MB upload) if no job could be accepted."""
        self._purge()
        if self.active_count() >= self._settings.video_max_active_jobs:
            log_event(logger, logging.WARNING, "video_capacity_refused", active=self.active_count())
            raise ServiceBusy(retry_after_seconds=15)
        if sum(1 for j in self._jobs.values() if j.active and j.owner == owner) >= self._settings.video_max_active_jobs_per_client:
            raise TooManyJobs()

    def submit(self, owner: str, path: Path, work_dir: Path, *, size: int | None, duration: float | None) -> VideoJobAccepted:
        """Create a queued job that now OWNS ``work_dir``. Raises (leaving ``work_dir`` with the caller) if refused."""
        self.check_capacity(owner)
        if len(self._jobs) >= MAX_STORED_JOBS:
            self._evict_oldest_finished()
            if len(self._jobs) >= MAX_STORED_JOBS:
                raise ServiceBusy(retry_after_seconds=30)
        self.start()                                          # before the job exists: a loop change must not fail it
        now = self._clock()
        job = VideoJob(id=secrets.token_urlsafe(24), owner=owner, created_at=now, updated_at=now,
                       size=size, duration=duration, work_dir=work_dir, path=path)
        self._jobs[job.id] = job
        self._queue.put_nowait(job.id)
        log_event(logger, logging.INFO, "job_created", job=job.id[:8], size=size, duration=duration)
        return VideoJobAccepted(job_id=job.id, status=job.status, stage=job.stage, progress=STAGE_PROGRESS[job.stage],
                                created_at=_iso(job.created_at), status_url=STATUS_PATH + job.id)

    def get(self, job_id: str) -> VideoJob | None:
        self._purge()
        return self._jobs.get(job_id)

    def public_status(self, job: VideoJob) -> VideoJobStatus:
        expires = _iso(job.finished_at + self._settings.video_job_ttl_seconds) if job.finished_at is not None else None
        return VideoJobStatus(
            job_id=job.id, status=job.status, stage=job.stage, progress=STAGE_PROGRESS.get(job.stage, 0),
            created_at=_iso(job.created_at), updated_at=_iso(job.updated_at), expires_at=expires,
            result=job.result, error=job.error,
        )

    # ------------------------------------------------------------------ state changes
    def _transition(self, job: VideoJob, new: JobStatus, *, stage: str | None = None) -> None:
        if new not in TRANSITIONS[job.status]:
            raise InvalidTransition(f"{job.status} -> {new}")
        job.status = new
        job.updated_at = self._clock()
        if stage is not None:
            job.stage = stage
        if new in TERMINAL_STATUSES:
            job.finished_at = job.updated_at

    def _set_stage(self, job: VideoJob, stage: str) -> None:
        if job.active and stage in STAGE_PROGRESS:
            job.stage = stage
            job.updated_at = self._clock()

    def _finish_failed(self, job: VideoJob, code: str, message: str) -> None:
        if not job.active:
            return
        job.error = JobError(code=code, message=message)
        self._transition(job, JobStatus.FAILED, stage="finished")
        self._release_files(job)
        log_event(logger, logging.WARNING, "job_failed", job=job.id[:8], code=code)

    def _release_files(self, job: VideoJob) -> None:
        work_dir, job.work_dir, job.path = job.work_dir, None, None
        if work_dir is not None:
            remove_work_dir(work_dir)

    # ------------------------------------------------------------------ workers
    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            if job is None or job.status != JobStatus.QUEUED:
                continue                                   # expired, failed or already handled
            try:
                await self._run(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                        # a bug here must not kill the worker
                log_event(logger, logging.ERROR, "worker_error", error=type(exc).__name__)

    async def _run(self, job: VideoJob) -> None:
        self._transition(job, JobStatus.PROCESSING, stage="starting")
        started = time.monotonic()
        try:
            async with asyncio.timeout(self._settings.video_job_timeout_seconds):
                response = await self._analyze(job.path, job.size, job.duration, lambda stage: self._set_stage(job, stage))
            self._set_stage(job, "scoring")
            final = JobStatus.COMPLETED if response.status == AnalysisStatus.COMPLETED else JobStatus.UNABLE_TO_ASSESS
            job.result = response
            self._transition(job, final, stage="finished")
        except TimeoutError:
            job.result = AnalysisResponse.unable_to_assess("video", ReasonCode.PROVIDER_TIMEOUT)
            self._transition(job, JobStatus.UNABLE_TO_ASSESS, stage="finished")
            log_event(logger, logging.WARNING, "job_timeout", job=job.id[:8])
        except asyncio.CancelledError:
            self._finish_failed(job, "JOB_CANCELLED", "The analysis was cancelled. Please submit the video again.")
            raise
        except Exception as exc:                            # the pipeline already converts expected failures
            log_event(logger, logging.ERROR, "job_internal_error", job=job.id[:8], error=type(exc).__name__)
            self._finish_failed(job, "INTERNAL_ERROR", "An unexpected internal error stopped the analysis.")
        finally:
            self._release_files(job)                        # the video is deleted whatever happened
        log_event(logger, logging.INFO, "job_finished", job=job.id[:8], status=job.status,
                  duration_s=round(time.monotonic() - started, 2))

    # ------------------------------------------------------------------ expiry
    def _purge(self) -> int:
        now = self._clock()
        ttl = self._settings.video_job_ttl_seconds
        expired = [j.id for j in self._jobs.values() if j.finished_at is not None and now - j.finished_at >= ttl]
        for job_id in expired:
            job = self._jobs.pop(job_id)
            self._release_files(job)
        if expired:
            log_event(logger, logging.INFO, "jobs_expired", count=len(expired))
        return len(expired)

    def _evict_oldest_finished(self) -> None:
        finished = sorted((j for j in self._jobs.values() if not j.active), key=lambda j: j.finished_at or 0.0)
        if finished:
            self._jobs.pop(finished[0].id, None)

    async def _janitor(self) -> None:
        interval = min(JANITOR_INTERVAL_SECONDS, max(1.0, self._settings.video_job_ttl_seconds / 2))
        while True:
            await asyncio.sleep(interval)
            self._purge()
