"""Compatibility wrappers for the original endpoints used by the existing frontend.

Same handlers (so the SAME rate-limit counters), same multipart field names (``text`` / ``file``).
Credentials are never read from the request: an ``api_key`` field sent by an older frontend is ignored.
"""
from fastapi import APIRouter

from app.schemas.analysis import AnalysisResponse


def build_router(text_handler, audio_handler) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["legacy"])
    router.add_api_route("/analyze-text", text_handler, methods=["POST"], response_model=AnalysisResponse, deprecated=True)
    router.add_api_route("/analyze-audio", audio_handler, methods=["POST"], response_model=AnalysisResponse, deprecated=True)
    return router
