"""Text analysis workflow: validate input, take a capacity slot, delegate to the shared pipeline."""
from __future__ import annotations

from app.core.config import Settings
from app.core.errors import InvalidInput, PayloadTooLarge
from app.schemas.analysis import AnalysisResponse
from app.services.analysis_pipeline import AnalysisPipeline
from app.services.url_analysis import analyze_urls


async def analyze_text(text: str, *, pipeline: AnalysisPipeline, settings: Settings) -> AnalysisResponse:
    text = text.strip()
    if not text:
        raise InvalidInput("Text must not be empty.")
    if len(text) > settings.max_text_length:
        raise PayloadTooLarge(f"Text exceeds the {settings.max_text_length:,} character limit.")
    urls = analyze_urls(text)                    # structure only: no link is ever visited
    async with pipeline.admit():
        return await pipeline.run_text(text, urls=urls)
