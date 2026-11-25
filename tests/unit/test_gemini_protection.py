"""Gemini resource protection: bounded retries, no retry storms, quota cooldown, cleanup."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.errors import AnalysisError, ReasonCode as R
from app.providers.gemini import client as gemini_client
from app.providers.gemini.client import GeminiProvider
from tests.fakes import api_error, load_fixture_json, provider_result


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(gemini_client, "RETRY_BASE_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(gemini_client, "RETRY_MAX_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(gemini_client.random, "uniform", lambda a, b: 0.0)


def provider_with(fake_sdk, **over) -> GeminiProvider:
    values = {"gemini_api_key": "k", "gemini_timeout_seconds": 5.0, "gemini_quota_cooldown_seconds": 30.0, **over}
    return GeminiProvider(Settings(**values), client_factory=fake_sdk)


def calls(fake_sdk) -> int:
    return sum(len(c.generate_calls) for c in fake_sdk.instances)


# ------------------------------------------------------------------ retries are bounded and selective
def test_a_transient_failure_is_retried_once_and_then_succeeds(fake_sdk):
    fake_sdk.generate_error_sequence = [api_error(503), None]
    assert run(provider_with(fake_sdk, gemini_max_retries=1).analyze_text("hi")).coverage == "full"
    assert calls(fake_sdk) == 2


@pytest.mark.parametrize("retries,expected_calls", [(0, 1), (1, 2), (2, 3), (3, 4)])
def test_retries_are_bounded_by_the_setting(fake_sdk, retries, expected_calls):
    fake_sdk.generate_error = api_error(503)
    with pytest.raises(AnalysisError) as exc:
        run(provider_with(fake_sdk, gemini_max_retries=retries).analyze_text("hi"))
    assert exc.value.reason_code == R.PROVIDER_UNAVAILABLE and calls(fake_sdk) == expected_calls


@pytest.mark.parametrize("error", [
    api_error(401), api_error(403), api_error(404), api_error(400, "bad schema"),
    api_error(400, "API key not valid. Please pass a valid API key."), api_error(429), api_error(504), api_error(408), TimeoutError(),
])
def test_authentication_invalid_request_quota_and_timeout_failures_are_never_retried(fake_sdk, error):
    fake_sdk.generate_error = error
    with pytest.raises(AnalysisError):
        run(provider_with(fake_sdk, gemini_max_retries=3).analyze_text("hi"))
    assert calls(fake_sdk) == 1


def test_connection_errors_count_as_transient(fake_sdk):
    fake_sdk.generate_error_sequence = [ConnectionError("reset"), None]
    run(provider_with(fake_sdk, gemini_max_retries=1).analyze_text("hi"))
    assert calls(fake_sdk) == 2


def test_malformed_model_output_is_not_a_reason_to_call_gemini_again(fake_sdk):
    fake_sdk.generate_result = SimpleNamespace(parsed=None, text="garbage")
    with pytest.raises(AnalysisError) as exc:
        run(provider_with(fake_sdk, gemini_max_retries=3).analyze_text("hi"))
    assert exc.value.reason_code == R.INVALID_PROVIDER_RESPONSE and calls(fake_sdk) == 1


def test_the_sdks_own_retries_are_switched_off_so_retries_cannot_multiply(fake_sdk):
    run(provider_with(fake_sdk).analyze_text("hi"))
    assert fake_sdk.instances[-1].http_options.retry_options.attempts == 1


def test_the_overall_deadline_also_bounds_retries(fake_sdk, monkeypatch):
    monkeypatch.setattr(gemini_client, "RETRY_BASE_DELAY_SECONDS", 5.0)      # a long backoff...
    monkeypatch.setattr(gemini_client, "RETRY_MAX_DELAY_SECONDS", 5.0)
    fake_sdk.generate_error = api_error(503)
    settings = Settings(gemini_api_key="k", gemini_timeout_seconds=0.2, gemini_max_retries=3)
    provider = GeminiProvider(settings, client_factory=fake_sdk)
    with pytest.raises(AnalysisError) as exc:
        run(provider.analyze_text("hi"))
    assert exc.value.reason_code == R.PROVIDER_TIMEOUT and calls(fake_sdk) == 1      # ...is cut off by the deadline


# ------------------------------------------------------------------ audio cleanup across retries
def audio_reply():
    return SimpleNamespace(parsed=provider_result("gemini_audio_high_risk.json"), text=None)


def test_audio_retry_uploads_once_deletes_once_and_calls_generate_twice(fake_sdk, tmp_path):
    clip = tmp_path / "c.wav"
    clip.write_bytes(b"x")
    fake_sdk.generate_result = audio_reply()
    fake_sdk.generate_error_sequence = [api_error(503), None]
    run(provider_with(fake_sdk, gemini_max_retries=1).analyze_audio(clip))
    client = fake_sdk.instances[-1]
    assert len(client.uploaded) == 1 and len(client.generate_calls) == 2 and client.deleted == ["files/fake-123"]
    assert client.order == ["upload", "generate", "generate", "delete"]


def test_audio_remote_file_is_deleted_even_when_every_retry_fails(fake_sdk, tmp_path):
    fake_sdk.generate_error = api_error(503)
    with pytest.raises(AnalysisError):
        run(provider_with(fake_sdk, gemini_max_retries=2).analyze_audio(tmp_path / "c.wav"))
    assert fake_sdk.instances[-1].deleted == ["files/fake-123"]


# ------------------------------------------------------------------ quota cooldown
class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(gemini_client.time, "monotonic", c)
    return c


def test_after_a_quota_error_gemini_is_not_called_again_during_the_cooldown(fake_sdk, clock):
    provider = provider_with(fake_sdk)
    fake_sdk.generate_error = api_error(429)
    with pytest.raises(AnalysisError) as first:
        run(provider.analyze_text("one"))
    assert first.value.reason_code == R.PROVIDER_RATE_LIMITED and calls(fake_sdk) == 1

    fake_sdk.generate_error = None
    clock.now += 10
    with pytest.raises(AnalysisError) as second:
        run(provider.analyze_text("two"))
    assert second.value.reason_code == R.PROVIDER_RATE_LIMITED and "cooldown" in second.value.internal_detail
    assert calls(fake_sdk) == 1                                              # no second call to Gemini


def test_the_cooldown_ends_and_normal_service_resumes(fake_sdk, clock):
    provider = provider_with(fake_sdk)
    fake_sdk.generate_error = api_error(429)
    with pytest.raises(AnalysisError):
        run(provider.analyze_text("one"))
    fake_sdk.generate_error = None
    clock.now += 31
    assert run(provider.analyze_text("two")).coverage == "full" and calls(fake_sdk) == 2


def test_the_cooldown_can_be_disabled(fake_sdk, clock):
    provider = provider_with(fake_sdk, gemini_quota_cooldown_seconds=0.0)
    fake_sdk.generate_error = api_error(429)
    for _ in range(3):
        with pytest.raises(AnalysisError):
            run(provider.analyze_text("x"))
    assert calls(fake_sdk) == 3


def test_the_cooldown_applies_to_audio_without_uploading(fake_sdk, clock, tmp_path):
    provider = provider_with(fake_sdk)
    fake_sdk.generate_error = api_error(429)
    with pytest.raises(AnalysisError):
        run(provider.analyze_text("x"))
    with pytest.raises(AnalysisError) as exc:
        run(provider.analyze_audio(tmp_path / "c.wav"))
    assert exc.value.reason_code == R.PROVIDER_RATE_LIMITED and fake_sdk.instances[-1].uploaded == []


@pytest.mark.parametrize("error", [api_error(500), api_error(401), api_error(400, "bad"), TimeoutError()])
def test_only_quota_errors_start_a_cooldown(fake_sdk, clock, error):
    provider = provider_with(fake_sdk, gemini_max_retries=0)
    fake_sdk.generate_error = error
    with pytest.raises(AnalysisError):
        run(provider.analyze_text("x"))
    fake_sdk.generate_error = None
    assert run(provider.analyze_text("y")).coverage == "full"


def test_a_failed_validation_of_model_output_does_not_start_a_cooldown(fake_sdk, clock):
    provider = provider_with(fake_sdk)
    fake_sdk.generate_result = SimpleNamespace(parsed=None, text="garbage")
    with pytest.raises(AnalysisError):
        run(provider.analyze_text("x"))
    fake_sdk.generate_result = None
    assert run(provider.analyze_text("y")).coverage == "full"


def test_nothing_about_the_request_is_cached(fake_sdk):
    """Identical requests are NOT deduplicated or cached: each is a fresh, independent provider call."""
    provider = provider_with(fake_sdk)
    run(provider.analyze_text("same text"))
    run(provider.analyze_text("same text"))
    assert calls(fake_sdk) == 2
