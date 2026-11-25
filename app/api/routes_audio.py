"""Audio analysis route. Thin: hand the upload to the service and return its validated response.

Rate limited per client (``RATE_LIMIT_AUDIO``) and globally across analysis routes. The request body is
size-limited before this handler runs (``BodyLimitMiddleware``); the service validates the file's name,
type and content before and while it is stored.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, UploadFile

from app.api.deps import get_app_settings, get_pipeline
from app.core.config import Settings
from app.core.rate_limit import global_key
from app.schemas.analysis import AnalysisResponse
from app.services import audio_service
from app.services.analysis_pipeline import AnalysisPipeline


def build_router(limiter, settings: Settings):
    router = APIRouter(prefix="/api/v1/analyze", tags=["analysis"])

    @router.post("/audio", response_model=AnalysisResponse)
    @limiter.shared_limit(settings.rate_limit_audio, scope="analysis-audio")      # explicit scope: one counter for BOTH URL paths
    @limiter.shared_limit(settings.rate_limit_global, scope="analysis-global", key_func=global_key)
    async def analyze_audio_endpoint(
        request: Request,
        file: Annotated[UploadFile, File(...)],
        pipeline: Annotated[AnalysisPipeline, Depends(get_pipeline)],
        app_settings: Annotated[Settings, Depends(get_app_settings)],
    ) -> AnalysisResponse:
        return await audio_service.analyze_audio(file, pipeline=pipeline, settings=app_settings)

    return router, analyze_audio_endpoint
