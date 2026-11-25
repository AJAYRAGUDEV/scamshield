from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from app.core.errors import AnalysisError, ReasonCode as R
from app.providers.gemini.errors import translate_exception
from tests.fakes import api_error


@pytest.mark.parametrize("exc,reason", [
    (api_error(401), R.PROVIDER_AUTH_ERROR),
    (api_error(403), R.PROVIDER_AUTH_ERROR),
    (api_error(400, "API key not valid. Please pass a valid API key."), R.PROVIDER_AUTH_ERROR),
    (api_error(400, "bad schema"), R.PROVIDER_ERROR),
    (api_error(404), R.CONFIGURATION_ERROR),
    (api_error(429), R.PROVIDER_RATE_LIMITED),
    (api_error(408), R.PROVIDER_TIMEOUT),
    (api_error(504), R.PROVIDER_TIMEOUT),
    (api_error(500), R.PROVIDER_UNAVAILABLE),
    (api_error(502), R.PROVIDER_UNAVAILABLE),
    (api_error(503), R.PROVIDER_UNAVAILABLE),
    (api_error(418), R.PROVIDER_ERROR),
    (TimeoutError(), R.PROVIDER_TIMEOUT),
    (asyncio.TimeoutError(), R.PROVIDER_TIMEOUT),
    (httpx.ReadTimeout("slow"), R.PROVIDER_TIMEOUT),
    (httpx.ConnectError("refused"), R.PROVIDER_UNAVAILABLE),
    (ConnectionError("reset"), R.PROVIDER_UNAVAILABLE),
    (socket.gaierror("no dns"), R.PROVIDER_UNAVAILABLE),
    (RuntimeError("anything else"), R.PROVIDER_ERROR),
])
def test_exception_translation(exc, reason):
    translated = translate_exception(exc)
    assert isinstance(translated, AnalysisError) and translated.reason_code == reason


def test_httpx2_exceptions_are_recognised_too():
    """google-genai may raise httpx2 exceptions; matching is by class name, not by library."""
    httpx2 = pytest.importorskip("httpx2")
    assert translate_exception(httpx2.ReadTimeout("slow")).reason_code == R.PROVIDER_TIMEOUT
    assert translate_exception(httpx2.ConnectError("refused")).reason_code == R.PROVIDER_UNAVAILABLE


def test_analysis_errors_pass_through_unchanged():
    original = AnalysisError(R.INVALID_PROVIDER_RESPONSE, "x")
    assert translate_exception(original) is original


def test_internal_detail_contains_no_provider_message():
    detail = translate_exception(api_error(429, "user said SECRET-TEXT and key AIza" + "A" * 30)).internal_detail
    assert "SECRET" not in detail and "AIza" not in detail
    assert "APIError" in detail and "429" in detail
