"""Shared analysis workflow for text, audio and video.

    provider evidence -> mode validation -> server enrichment -> risk engine -> response builder -> response

Server enrichment (Phase 7): text gets the structural link findings appended as evidence; audio has its timestamps
checked against the recording's real length. Then the SAME risk engine scores both.

The provider only reports evidence. The score, verdict and confidence come from
``risk_engine.assess`` and every piece of user-facing text from ``response_builder``. The failure
policy lives here, in exactly one place: an analysis that could not be completed is ALWAYS
``unable_to_assess`` with a null score (never Low Risk), and no provider/model quirk may escape as an
unhandled exception.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from typing import Awaitable, Callable

from app.core.errors import AnalysisError, ReasonCode, ServiceBusy
from app.core.logging import log_event, log_traceback
from app.providers.base import AnalysisProvider
from app.schemas.analysis import AnalysisResponse
from app.schemas.evidence import Mode, validate_report_for_mode
from app.schemas.provider_output import ProviderAnalysis
from app.schemas.verdict import AnalysisStatus
from app.services import risk_engine
from app.services.evidence_enrichment import add_url_evidence, build_limitations, verify_timestamps
from app.services.response_builder import ResponseExtras, build_completed_response
from app.services.url_analysis import UrlAnalysis

logger = logging.getLogger("scamshield.analysis")


class AnalysisPipeline:
    """``admit()`` is the front door: at most ``max_concurrency`` analyses run and at most ``max_queue``
    more wait; anything beyond that is refused immediately with ServiceBusy (HTTP 503) instead of piling
    up. The semaphore then bounds the provider calls actually running. Everything is released in
    ``finally`` blocks, so failures, timeouts and cancellations cannot leak capacity. Single process only."""

    def __init__(self, provider: AnalysisProvider, *, max_concurrency: int = 4, max_queue: int = 8):
        self._provider = provider
        self._slots = asyncio.Semaphore(max_concurrency)  # bounds in-flight provider calls
        self._capacity = max_concurrency + max_queue
        self._admitted = 0

    @asynccontextmanager
    async def admit(self):
        if self._admitted >= self._capacity:
            log_event(logger, logging.WARNING, "capacity_refused", admitted=self._admitted, capacity=self._capacity)
            raise ServiceBusy()
        self._admitted += 1
        try:
            yield
        finally:
            self._admitted -= 1

    @property
    def admitted(self) -> int:
        return self._admitted

    async def run_text(self, text: str, *, urls: UrlAnalysis | None = None) -> AnalysisResponse:
        urls = urls or UrlAnalysis.empty()
        return await self._run("text", lambda: self._provider.analyze_text(text, url_facts=urls.prompt_facts), size=len(text), urls=urls)

    async def run_audio(self, path: Path, *, size: int | None = None, duration: float | None = None) -> AnalysisResponse:
        return await self._run("audio", lambda: self._provider.analyze_audio(path), size=size, duration=duration)

    async def run_video(self, path: Path, *, size: int | None = None, duration: float | None = None,
                        on_stage: Callable[[str], None] | None = None) -> AnalysisResponse:
        """Analyse a stored video. Shares the Gemini concurrency slots with text and audio (no second client or limiter)."""
        return await self._run("video", lambda: self._provider.analyze_video(path, on_stage), size=size, duration=duration)

    def fail(self, mode: Mode, error: AnalysisError, *, started: float | None = None) -> AnalysisResponse:
        """Record a failed analysis and build its ``unable_to_assess`` response."""
        log_event(
            logger, logging.WARNING, "analysis_failed",
            mode=mode, reason_code=error.reason_code, detail=error.internal_detail,
            duration_ms=round((perf_counter() - started) * 1000, 1) if started else "-",
        )
        return AnalysisResponse.unable_to_assess(mode, error.reason_code)

    async def _run(self, mode: Mode, call: Callable[[], Awaitable[ProviderAnalysis]], *, size: int | None,
                   urls: UrlAnalysis | None = None, duration: float | None = None) -> AnalysisResponse:
        started = perf_counter()
        try:
            async with self._slots:
                report = await call()

            try:
                validate_report_for_mode(report, mode)
            except ValueError as exc:
                raise AnalysisError(ReasonCode.INVALID_PROVIDER_RESPONSE, f"evidence/mode mismatch: {exc}") from exc

            timestamps_removed = 0
            if mode == "text":
                report = add_url_evidence(report, urls or UrlAnalysis.empty())
            else:
                report, timestamps_removed = verify_timestamps(report, duration)

            assessment = risk_engine.assess(report)
            if assessment.status != AnalysisStatus.COMPLETED:
                raise AnalysisError(assessment.reason_code or ReasonCode.INSUFFICIENT_EVIDENCE,
                                    f"coverage={assessment.coverage}")
            response = build_completed_response(assessment, mode, ResponseExtras(
                language=report.language,
                language_uncertain=report.language_uncertain,
                limitations=build_limitations(report, mode=mode, urls=urls, timestamps_removed=timestamps_removed,
                                              duration_known=duration is not None),
                urls=list(urls.findings) if (mode == "text" and urls) else [],
            ))
        except AnalysisError as exc:
            return self.fail(mode, exc, started=started)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # safety net: no provider/model quirk may become an unhandled 500
            log_event(logger, logging.ERROR, "analysis_internal_error", mode=mode, error=type(exc).__name__)
            log_traceback(logger, "analysis_internal_error traceback", exc)
            return self.fail(mode, AnalysisError(ReasonCode.INTERNAL_ANALYSIS_ERROR, type(exc).__name__), started=started)

        log_event(
            logger, logging.INFO, "analysis_completed",
            mode=mode, verdict=response.verdict, risk_score=response.risk_score, confidence=response.confidence,
            coverage=response.coverage, signals=len(response.signals), language=response.language or "-",
            links=len(response.urls), timestamps_removed=timestamps_removed,
            input_size=size if size is not None else "-",
            duration_ms=round((perf_counter() - started) * 1000, 1),
        )
        return response
