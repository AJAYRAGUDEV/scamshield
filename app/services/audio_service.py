"""Audio analysis workflow.

validate metadata -> take a capacity slot -> stream to a private file (size cap + magic-byte check on the
first bytes) -> analyze -> always clean up.
"""
from __future__ import annotations

import asyncio
from time import perf_counter

from fastapi import UploadFile

from app.core.config import Settings
from app.core.errors import AnalysisError, ReasonCode
from app.schemas.analysis import AnalysisResponse
from app.services.analysis_pipeline import AnalysisPipeline
from app.services.audio_inspection import inspect_audio
from app.services.media_validation import (
    AUDIO_KINDS, AUDIO_MIME_TYPES, check_audio_content, validate_audio_metadata,
)
from app.utils.temp_files import saved_upload

ALLOWED_EXTENSIONS = frozenset(AUDIO_MIME_TYPES)   # kept for callers/tests that list the supported types
assert ALLOWED_EXTENSIONS == frozenset(AUDIO_KINDS)


async def analyze_audio(upload: UploadFile, *, pipeline: AnalysisPipeline, settings: Settings) -> AnalysisResponse:
    started = perf_counter()
    try:
        suffix = validate_audio_metadata(upload.filename, upload.content_type)     # 400 / 415 before any I/O
        async with pipeline.admit():                                                 # 503 when at capacity
            try:
                async with saved_upload(
                    upload,
                    suffix=suffix,
                    max_bytes=settings.max_audio_bytes,
                    too_large_message=f"Audio file exceeds the {settings.max_audio_size_mb} MB limit.",
                    empty_message="Audio file is empty.",
                    upload_dir=settings.upload_dir,
                    inspect_head=lambda head: check_audio_content(head, suffix),     # 415 on a content mismatch
                ) as saved:
                    facts = await asyncio.to_thread(inspect_audio, saved.path, saved.kind)
                    if facts.silent:
                        # Silence is not evidence of safety, and the model is never asked to analyse nothing.
                        raise AnalysisError(ReasonCode.INSUFFICIENT_EVIDENCE, "silent recording: provider not called")
                    return await pipeline.run_audio(saved.path, size=saved.size, duration=facts.duration_seconds)
            except AnalysisError as exc:  # local file I/O failure
                return pipeline.fail("audio", exc, started=started)
    finally:
        await upload.close()
