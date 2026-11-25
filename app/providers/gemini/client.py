"""Gemini implementation of the provider interface.

All Gemini-specific code lives in this package. Uses the SDK's NATIVE async client
(``client.aio``), so no provider call blocks the event loop and no worker threads are created.

Bounds: every analysis call runs under one overall deadline (``GEMINI_TIMEOUT_SECONDS``) and the
SDK's per-request HTTP timeout is set to the same value.

Quota protection:
* The SDK's own retries are switched OFF (``attempts=1``); this module owns the only retry policy, so
  retries cannot multiply.
* At most ``GEMINI_MAX_RETRIES`` extra attempts, only for transient failures (HTTP 5xx / connection
  errors, ``PROVIDER_UNAVAILABLE``), with capped exponential backoff and jitter, inside the overall
  deadline. Authentication errors, invalid requests, timeouts and quota errors are never retried.
* After a provider quota error (HTTP 429) new analyses are refused locally for
  ``GEMINI_QUOTA_COOLDOWN_SECONDS`` instead of hammering an exhausted quota.
* Nothing is cached and no request content is stored.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path
from typing import Any, Callable

from google import genai
from google.genai import types
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import AnalysisError, ReasonCode
from app.core.logging import log_event
from app.providers.gemini.errors import translate_exception
from app.providers.gemini.prompts import AUDIO_PROMPT, SYSTEM_PROMPT, VIDEO_PROMPT, VIDEO_SYSTEM_PROMPT, build_text_prompt
from app.schemas.provider_output import ProviderAnalysis

logger = logging.getLogger("scamshield.gemini")

VIDEO_MIME_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}
REMOTE_DELETE_TIMEOUT_SECONDS = 10.0
RETRY_BASE_DELAY_SECONDS = 0.5
RETRY_MAX_DELAY_SECONDS = 2.0


def parse_provider_result(result: Any) -> ProviderAnalysis:
    """Validate a Gemini response. Output is untrusted: anything malformed becomes
    ``INVALID_PROVIDER_RESPONSE`` instead of an unhandled exception."""
    try:
        parsed = getattr(result, "parsed", None)
        if isinstance(parsed, ProviderAnalysis):
            return parsed
        if parsed is not None:
            return ProviderAnalysis.model_validate(parsed)
        text = getattr(result, "text", None)
        if not text:
            raise ValueError("empty model output")
        return ProviderAnalysis.model_validate_json(text)
    except (ValidationError, ValueError, TypeError, AttributeError) as exc:
        raise AnalysisError(ReasonCode.INVALID_PROVIDER_RESPONSE, type(exc).__name__) from exc


class GeminiProvider:
    def __init__(self, settings: Settings, client_factory: Callable[..., Any] | None = None):
        self._settings = settings
        self._client_factory = client_factory
        self._client: Any = None
        self._cooldown_until = 0.0          # time.monotonic() deadline after a provider quota error

    def _check_cooldown(self) -> None:
        if time.monotonic() < self._cooldown_until:
            raise AnalysisError(ReasonCode.PROVIDER_RATE_LIMITED, "quota cooldown active (provider not called)")

    def _note_failure(self, error: AnalysisError) -> None:
        cooldown = self._settings.gemini_quota_cooldown_seconds
        if error.reason_code == ReasonCode.PROVIDER_RATE_LIMITED and cooldown > 0:
            self._cooldown_until = time.monotonic() + cooldown
            log_event(logger, logging.WARNING, "quota_cooldown_started", seconds=cooldown)

    # ------------------------------------------------------------------ client
    def _get_client(self) -> Any:
        if not self._settings.gemini_configured:
            raise AnalysisError(ReasonCode.CONFIGURATION_ERROR, "GEMINI_API_KEY is not set")
        if self._client is None:
            factory = self._client_factory or genai.Client  # resolved at call time
            try:
                self._client = factory(
                    api_key=self._settings.gemini_api_key,  # the ONLY source of the key
                    http_options=types.HttpOptions(
                        timeout=int(self._settings.gemini_timeout_seconds * 1000),
                        retry_options=types.HttpRetryOptions(attempts=1),   # no hidden SDK retries
                    ),
                )
            except Exception as exc:
                raise AnalysisError(ReasonCode.CONFIGURATION_ERROR, f"client init failed: {type(exc).__name__}") from exc
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.aio.aclose()
            client.close()
        except Exception as exc:  # shutdown must not fail
            log_event(logger, logging.WARNING, "client_close_failed", error=type(exc).__name__)

    # --------------------------------------------------------------- operations
    async def _guarded(self, operation: Callable[[], Any], timeout: float | None = None) -> ProviderAnalysis:
        """Run ``operation`` under the overall deadline and translate every failure."""
        try:
            async with asyncio.timeout(timeout if timeout is not None else self._settings.gemini_timeout_seconds):
                return await operation()
        except AnalysisError as exc:
            self._note_failure(exc)
            raise
        except Exception as exc:  # includes TimeoutError from the deadline
            error = translate_exception(exc)
            self._note_failure(error)
            raise error from exc

    async def _generate(self, client: Any, contents: list[object], system: str = SYSTEM_PROMPT) -> ProviderAnalysis:
        attempt = 0
        while True:
            try:
                result = await client.aio.models.generate_content(
                    model=self._settings.gemini_model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        temperature=0.1,
                        response_mime_type="application/json",
                        response_schema=ProviderAnalysis,
                    ),
                )
                break
            except Exception as exc:
                error = translate_exception(exc)
                if error.reason_code != ReasonCode.PROVIDER_UNAVAILABLE or attempt >= self._settings.gemini_max_retries:
                    raise error from exc                      # auth / invalid request / quota / timeout: never retried
                attempt += 1
                delay = min(RETRY_MAX_DELAY_SECONDS, RETRY_BASE_DELAY_SECONDS * 2 ** (attempt - 1)) + random.uniform(0, 0.25)
                log_event(logger, logging.WARNING, "provider_retry", attempt=attempt, delay=round(delay, 2), detail=error.internal_detail)
                await asyncio.sleep(delay)
        return parse_provider_result(result)

    async def analyze_text(self, text: str, url_facts: str | None = None) -> ProviderAnalysis:
        self._check_cooldown()
        client = self._get_client()
        return await self._guarded(lambda: self._generate(client, [build_text_prompt(text, url_facts)]))

    async def analyze_audio(self, path: Path) -> ProviderAnalysis:
        self._check_cooldown()
        client = self._get_client()

        async def operation() -> ProviderAnalysis:
            uploaded = await client.aio.files.upload(file=str(path))
            remote_name = getattr(uploaded, "name", None)
            try:
                return await self._generate(client, [uploaded, AUDIO_PROMPT])
            finally:
                if remote_name:
                    await self._delete_remote(client, remote_name)

        return await self._guarded(operation)

    async def analyze_video(self, path: Path, on_stage: Callable[[str], None] | None = None) -> ProviderAnalysis:
        """Upload the video, wait (bounded) until Gemini has finished processing it, analyse it, ALWAYS delete the remote file.

        ``on_stage`` receives the coarse stage names ``uploading``, ``processing_media`` and ``analysing`` so a job can show
        safe progress. Uses the same client, retry policy, cooldown and error translation as text and audio.
        """
        self._check_cooldown()
        client = self._get_client()
        settings = self._settings
        stage = on_stage or (lambda _name: None)
        mime = VIDEO_MIME_TYPES.get(path.suffix.lower())

        async def operation() -> ProviderAnalysis:
            stage("uploading")
            upload_config = types.UploadFileConfig(
                mime_type=mime, http_options=types.HttpOptions(timeout=int(settings.gemini_video_timeout_seconds * 1000)),
            )
            uploaded = await client.aio.files.upload(file=str(path), config=upload_config)
            remote_name = getattr(uploaded, "name", None)
            try:
                stage("processing_media")
                ready = await self._wait_until_active(client, uploaded, remote_name)
                stage("analysing")
                return await self._generate(client, [ready, VIDEO_PROMPT], system=VIDEO_SYSTEM_PROMPT)
            finally:
                if remote_name:
                    await self._delete_remote(client, remote_name)

        return await self._guarded(operation, timeout=settings.gemini_video_timeout_seconds)

    async def _wait_until_active(self, client: Any, uploaded: Any, name: str | None) -> Any:
        """Poll the uploaded file until its state is ACTIVE. FAILED -> error; still not ready at the deadline -> timeout.

        Gemini file states (verified against the installed SDK): STATE_UNSPECIFIED, PROCESSING, ACTIVE, FAILED. An unknown or
        missing state is treated as "not ready yet" and polled again, never as ready.
        """
        if not name:
            raise AnalysisError(ReasonCode.PROVIDER_ERROR, "upload returned no file name")
        deadline = time.monotonic() + self._settings.gemini_file_ready_timeout_seconds
        current = uploaded
        while True:
            state = getattr(getattr(current, "state", None), "value", getattr(current, "state", None))
            if state == "ACTIVE":
                return current
            if state == "FAILED":
                raise AnalysisError(ReasonCode.MEDIA_PROCESSING_FAILED, "provider could not process the video")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AnalysisError(ReasonCode.PROVIDER_TIMEOUT, "video was not ready before the deadline")
            await asyncio.sleep(min(self._settings.gemini_file_poll_interval_seconds, remaining))
            current = await client.aio.files.get(name=name)

    async def _delete_remote(self, client: Any, name: str) -> None:
        try:
            async with asyncio.timeout(REMOTE_DELETE_TIMEOUT_SECONDS):
                await client.aio.files.delete(name=name)
        except Exception as exc:
            log_event(logger, logging.WARNING, "remote_file_delete_failed", error=type(exc).__name__)
