"""Public schemas of the asynchronous video-analysis jobs (Phase 8).

Job states::

    queued -> processing -> completed          (the analysis finished with a low_risk / suspicious / high_risk verdict)
                         -> unable_to_assess   (the analysis could not be completed; never Low Risk; the job still carries a
                                                result whose risk_score is null and whose reason_code says why)
    queued | processing -> failed              (an internal problem such as shutdown or an unexpected error; no result)

``completed``, ``unable_to_assess`` and ``failed`` are final. Upload validation (size, format, ffprobe) happens BEFORE a
job exists, so an invalid upload is an immediate HTTP error and never occupies a job.

Nothing here carries a file path, a provider detail or a stack trace.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, model_validator

from app.schemas.analysis import AnalysisResponse
from app.schemas.verdict import AnalysisStatus


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    UNABLE_TO_ASSESS = "unable_to_assess"
    FAILED = "failed"


TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.UNABLE_TO_ASSESS, JobStatus.FAILED})

# Valid transitions. Anything else is a bug and raises.
TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.PROCESSING, JobStatus.FAILED}),
    JobStatus.PROCESSING: frozenset({JobStatus.COMPLETED, JobStatus.UNABLE_TO_ASSESS, JobStatus.FAILED}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.UNABLE_TO_ASSESS: frozenset(),
    JobStatus.FAILED: frozenset(),
}

# Safe progress indicators: coarse stage names and a 0-100 number. They describe where the job is, not how long it takes.
STAGE_PROGRESS: dict[str, int] = {
    "queued": 0, "starting": 5, "uploading": 20, "processing_media": 45, "analysing": 70, "scoring": 90, "finished": 100,
}


class JobError(BaseModel):
    code: str
    message: str


class VideoJobAccepted(BaseModel):
    """Body of the HTTP 202 response to a video upload."""
    job_id: str
    status: JobStatus
    stage: str
    progress: int = Field(ge=0, le=100)
    created_at: datetime
    status_url: str


class VideoJobStatus(BaseModel):
    job_id: str
    status: JobStatus
    stage: str
    progress: int = Field(ge=0, le=100)
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None       # set once the job has finished; the record is forgotten after this time
    result: AnalysisResponse | None = None
    error: JobError | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        status = self.status
        if status == JobStatus.COMPLETED:
            ok = self.result is not None and self.result.status == AnalysisStatus.COMPLETED and self.error is None
        elif status == JobStatus.UNABLE_TO_ASSESS:
            ok = self.result is not None and self.result.status == AnalysisStatus.UNABLE_TO_ASSESS and self.error is None
        elif status == JobStatus.FAILED:
            ok = self.result is None and self.error is not None
        else:
            ok = self.result is None and self.error is None
        if not ok:
            raise ValueError("job status, result and error are inconsistent")
        if status in TERMINAL_STATUSES and self.expires_at is None:
            raise ValueError("a finished job must state when it expires")
        return self
