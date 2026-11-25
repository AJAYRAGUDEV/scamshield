"""Video upload workflow (Phase 8): validate everything cheap, validate with ffprobe, then hand a job to the background.

metadata (name / extension / MIME) -> capacity pre-check (before receiving 100 MB) -> stream to a private file
(size cap + signature check while streaming) -> ffprobe (integrity, video stream, duration limit) -> create the job.

Everything that can reject the upload happens here, BEFORE a job exists and before Gemini is contacted. The private
directory is owned by this function until the job is created, and is deleted here on any failure, including a client
disconnect (cancellation) or an error inside ffprobe.
"""
from __future__ import annotations

import re

from fastapi import UploadFile

from app.core.config import Settings
from app.core.errors import NotFound
from app.schemas.jobs import VideoJobAccepted, VideoJobStatus
from app.services.media_validation import check_video_content, validate_video_metadata
from app.services.video_inspection import Runner, inspect_video
from app.services.video_jobs import JobManager
from app.utils.temp_files import remove_work_dir, store_upload

JOB_ID = re.compile(r"[A-Za-z0-9_-]{16,64}")


async def submit_video(upload: UploadFile, *, jobs: JobManager, settings: Settings, owner: str,
                       runner: Runner | None = None) -> VideoJobAccepted:
    try:
        suffix = validate_video_metadata(upload.filename, upload.content_type)        # 400 / 415 before any I/O
        jobs.check_capacity(owner)                                                      # 503 / 429 before receiving the body
        saved = await store_upload(
            upload,
            suffix=suffix,
            max_bytes=settings.max_video_bytes,
            too_large_message=f"Video file exceeds the {settings.max_video_size_mb} MB limit.",
            empty_message="Video file is empty.",
            upload_dir=settings.upload_dir,
            inspect_head=lambda head: check_video_content(head, suffix),               # 415 on a content mismatch
        )
        try:
            facts = await inspect_video(saved.path, suffix, settings, runner)           # 422 / 503
            return jobs.submit(owner, saved.path, saved.work_dir, size=saved.size, duration=facts.duration_seconds)
        except BaseException:
            remove_work_dir(saved.work_dir)
            raise
    finally:
        await upload.close()


def job_status(job_id: str, *, jobs: JobManager) -> VideoJobStatus:
    job = jobs.get(job_id) if JOB_ID.fullmatch(job_id) else None
    if job is None:
        raise NotFound("No video analysis was found for this id. It may have expired or the server may have restarted.")
    return jobs.public_status(job)
