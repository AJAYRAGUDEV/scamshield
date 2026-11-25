"""FastAPI dependencies. Everything comes from ``app.state`` so tests can build an app with a fake provider."""
from __future__ import annotations

from fastapi import Request

from app.core.config import Settings
from app.services.analysis_pipeline import AnalysisPipeline


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_pipeline(request: Request) -> AnalysisPipeline:
    return request.app.state.pipeline
