"""Text analysis route. Thin: parse the form, call the service, return the validated response.

Rate limited per client (``RATE_LIMIT_TEXT``) and globally across analysis routes (``RATE_LIMIT_GLOBAL``).
Input errors become 4xx via the central handlers; a failed analysis is returned by the service as an
``unable_to_assess`` response. No API key is accepted from the client: unknown form fields (including an
old frontend's ``api_key``) are never read.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request

from app.api.deps import get_app_settings, get_pipeline
from app.core.config import Settings
from app.core.rate_limit import global_key
from app.schemas.analysis import AnalysisResponse
from app.services import text_service
from app.services.analysis_pipeline import AnalysisPipeline


def build_router(limiter, settings: Settings):
    """Returns ``(router, handler)``; the legacy router re-registers the SAME handler, so both paths share
    one rate-limit counter (using the old path is not a way around the limit)."""
    router = APIRouter(prefix="/api/v1/analyze", tags=["analysis"])

    @router.post("/text", response_model=AnalysisResponse)
    @limiter.shared_limit(settings.rate_limit_text, scope="analysis-text")      # explicit scope: one counter for BOTH URL paths
    @limiter.shared_limit(settings.rate_limit_global, scope="analysis-global", key_func=global_key)
    async def analyze_text_endpoint(
        request: Request,
        text: Annotated[str, Form(...)],
        pipeline: Annotated[AnalysisPipeline, Depends(get_pipeline)],
        app_settings: Annotated[Settings, Depends(get_app_settings)],
    ) -> AnalysisResponse:
        return await text_service.analyze_text(text, pipeline=pipeline, settings=app_settings)

    return router, analyze_text_endpoint
