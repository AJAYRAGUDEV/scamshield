"""Video analysis routes (Phase 8): submit a video (HTTP 202 + job id) and poll the job.

POST /api/analyze-video, /api/v1/analyze/video   multipart field ``file`` (MP4, MOV, WebM)
GET  /api/video-status/{job_id}, /api/v1/video-status/{job_id}

The upload is validated before a job is created (see ``video_service``), so invalid uploads are immediate HTTP errors.
Submissions are rate limited per client (``RATE_LIMIT_VIDEO``) and share the global analysis limit; both URL spellings
use the same limit counters. No ``from __future__ import annotations``: FastAPI must resolve the real annotations.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile

from app.api.deps import get_app_settings
from app.core.config import Settings
from app.core.rate_limit import global_key
from app.core.security import make_client_key
from app.schemas.jobs import VideoJobAccepted, VideoJobStatus
from app.services import video_service
from app.services.video_jobs import JobManager


def get_jobs(request: Request) -> JobManager:
    return request.app.state.jobs


def build_router(limiter, settings: Settings) -> APIRouter:
    router = APIRouter(tags=["video"])
    client_key = make_client_key(settings)

    @limiter.shared_limit(settings.rate_limit_video, scope="analysis-video")
    @limiter.shared_limit(settings.rate_limit_global, scope="analysis-global", key_func=global_key)
    async def analyze_video_endpoint(
        request: Request,
        response: Response,
        file: Annotated[UploadFile, File(...)],
        jobs: Annotated[JobManager, Depends(get_jobs)],
        app_settings: Annotated[Settings, Depends(get_app_settings)],
    ) -> VideoJobAccepted:
        accepted = await video_service.submit_video(
            file, jobs=jobs, settings=app_settings, owner=client_key(request),
            runner=getattr(request.app.state, "video_runner", None),
        )
        response.headers["Location"] = accepted.status_url
        return accepted

    @limiter.shared_limit(settings.rate_limit_default, scope="default")
    async def video_status_endpoint(
        request: Request,
        job_id: str,
        jobs: Annotated[JobManager, Depends(get_jobs)],
    ) -> VideoJobStatus:
        return video_service.job_status(job_id, jobs=jobs)

    for path in ("/api/v1/analyze/video", "/api/analyze-video"):
        router.add_api_route(path, analyze_video_endpoint, methods=["POST"], status_code=202, response_model=VideoJobAccepted)
    for path in ("/api/v1/video-status/{job_id}", "/api/video-status/{job_id}"):
        router.add_api_route(path, video_status_endpoint, methods=["GET"], response_model=VideoJobStatus)
    return router
