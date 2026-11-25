"""POST /api/analyze-text (legacy path) and /api/v1/analyze/text with a fake provider."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import AnalysisError, ReasonCode
from app.main import create_app
from app.providers.gemini import GeminiProvider
from tests.conftest import TEST_API_KEY
from tests.fakes import FakeProvider, FakeSDKClient, api_error, provider_result

PATHS = ["/api/analyze-text", "/api/v1/analyze/text"]
RESPONSE_KEYS = {"status", "verdict", "risk_score", "confidence", "confidence_level", "reason_code", "coverage", "summary",
                 "scam_type", "explanation", "recommended_action", "signals", "red_flags", "signal_scores", "mode",
                 "offline_fallback_used", "language", "language_uncertain", "limitations", "urls"}


@pytest.mark.parametrize("path", PATHS)
def test_valid_request(client, provider, path):
    r = client.post(path, data={"text": "  send otp now  "})
    body = r.json()
    assert r.status_code == 200 and set(body) == RESPONSE_KEYS
    assert set(body["signal_scores"]) == {"authority_impersonation", "urgency_isolation",
                                          "credential_payment_trap", "synthetic_or_script_signals"}
    assert (body["status"], body["mode"], body["offline_fallback_used"], body["reason_code"]) == ("completed", "text", False, None)
    assert (body["verdict"], body["risk_score"]) == ("high_risk", 96)
    assert provider.text_calls == ["send otp now"]            # stripped before the provider


@pytest.mark.parametrize("path", PATHS)
def test_low_risk_completed_assessment(path, settings):
    app = create_app(settings, FakeProvider(provider_result("gemini_low_risk.json")))
    body = TestClient(app).post(path, data={"text": "your parcel arrives tomorrow"}).json()
    assert (body["status"], body["verdict"], body["risk_score"], body["reason_code"]) == ("completed", "low_risk", 10, None)
    assert body["confidence_level"] == "Low"


@pytest.mark.parametrize("path", PATHS)
def test_empty_form_value_is_422_exactly_like_the_original(client, provider, path):
    """FastAPI treats an empty form value as a missing field. Verified against legacy/server.py."""
    assert client.post(path, data={"text": ""}).status_code == 422 and provider.text_calls == []


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("text", ["   ", "\n\t "])
def test_blank_text_is_400(client, provider, path, text):
    r = client.post(path, data={"text": text})
    assert r.status_code == 400 and r.json() == {"detail": "Text must not be empty.", "code": "INVALID_INPUT"}
    assert provider.text_calls == []


@pytest.mark.parametrize("path", PATHS)
def test_text_limits(client, provider, path):
    assert client.post(path, data={"text": "a" * 20_000}).status_code == 200
    r = client.post(path, data={"text": "a" * 20_001})
    assert r.status_code == 413 and r.json() == {"detail": "Text exceeds the 20,000 character limit.", "code": "PAYLOAD_TOO_LARGE"}
    assert len(provider.text_calls) == 1


def test_text_limit_comes_from_settings(provider):
    app = create_app(Settings(gemini_api_key="k", max_text_length=50), provider)
    c = TestClient(app)
    assert c.post("/api/analyze-text", data={"text": "a" * 50}).status_code == 200
    assert c.post("/api/analyze-text", data={"text": "a" * 51}).json()["detail"] == "Text exceeds the 50 character limit."


@pytest.mark.parametrize("path", PATHS)
def test_missing_field_is_422(client, path):
    assert client.post(path, data={}).status_code == 422


@pytest.mark.parametrize("reason", [ReasonCode.INVALID_PROVIDER_RESPONSE, ReasonCode.PROVIDER_TIMEOUT,
                                    ReasonCode.PROVIDER_UNAVAILABLE, ReasonCode.PROVIDER_RATE_LIMITED,
                                    ReasonCode.PROVIDER_AUTH_ERROR, ReasonCode.CONFIGURATION_ERROR])
@pytest.mark.parametrize("path", PATHS)
def test_provider_failures_are_unable_to_assess(settings, path, reason):
    app = create_app(settings, FakeProvider(error=AnalysisError(reason, "internal")))
    r = TestClient(app).post(path, data={"text": "hello"})
    body = r.json()
    assert r.status_code == 200
    assert (body["status"], body["verdict"], body["risk_score"], body["reason_code"]) == ("unable_to_assess", "unable_to_assess", None, reason.value)
    assert body["red_flags"] == [] and body["signals"] == [] and body["signal_scores"] is None


def test_unexpected_provider_exception_is_still_unable_to_assess(settings):
    app = create_app(settings, FakeProvider(error=RuntimeError("kaboom")))
    body = TestClient(app).post("/api/analyze-text", data={"text": "hello"}).json()
    assert body["verdict"] == "unable_to_assess" and body["reason_code"] == "INTERNAL_ANALYSIS_ERROR"


# ---- end to end through the real Gemini adapter with a fake SDK
def gemini_app(fake_sdk, key=TEST_API_KEY):
    settings = Settings(gemini_api_key=key, app_env="test", gemini_timeout_seconds=5.0)
    return create_app(settings, GeminiProvider(settings, client_factory=fake_sdk))


def test_missing_gemini_key_is_a_clear_configuration_result(fake_sdk):
    r = TestClient(gemini_app(fake_sdk, key="")).post("/api/analyze-text", data={"text": "hello"})
    body = r.json()
    assert r.status_code == 200 and body["verdict"] == "unable_to_assess" and body["risk_score"] is None
    assert body["reason_code"] == "CONFIGURATION_ERROR" and "GEMINI_API_KEY" in body["explanation"]
    assert fake_sdk.instances == []


def test_quota_error_from_gemini_reaches_the_client_as_a_reason_code_only(fake_sdk):
    fake_sdk.generate_error = api_error(429, "Quota exceeded for project 123456 key AIza" + "Q" * 30)
    r = TestClient(gemini_app(fake_sdk)).post("/api/analyze-text", data={"text": "hello"})
    assert r.json()["reason_code"] == "PROVIDER_RATE_LIMITED"
    assert "123456" not in r.text and "AIza" not in r.text


def test_garbage_from_gemini_is_unable_to_assess(fake_sdk):
    from types import SimpleNamespace

    fake_sdk.generate_result = SimpleNamespace(parsed=None, text="<html>not json</html>")
    body = TestClient(gemini_app(fake_sdk)).post("/api/analyze-text", data={"text": "hello"}).json()
    assert (body["verdict"], body["risk_score"], body["reason_code"]) == ("unable_to_assess", None, "INVALID_PROVIDER_RESPONSE")


def test_gemini_timeout_is_unable_to_assess(fake_sdk):
    fake_sdk.generate_delay = 2.0
    settings = Settings(gemini_api_key="k", gemini_timeout_seconds=0.05)
    app = create_app(settings, GeminiProvider(settings, client_factory=fake_sdk))
    body = TestClient(app).post("/api/analyze-text", data={"text": "hello"}).json()
    assert (body["verdict"], body["risk_score"], body["reason_code"]) == ("unable_to_assess", None, "PROVIDER_TIMEOUT")


def test_successful_gemini_flow_end_to_end(fake_sdk):
    body = TestClient(gemini_app(fake_sdk)).post("/api/analyze-text", data={"text": "hello"}).json()
    assert (body["status"], body["verdict"], body["risk_score"]) == ("completed", "high_risk", 96)
    assert body["mode"] == "text" and body["offline_fallback_used"] is False
