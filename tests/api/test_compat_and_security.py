"""Backward compatibility with the existing frontend, plus key/secret handling."""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_pipeline
from app.core.config import Settings
from app.main import create_app
from app.providers.gemini import GeminiProvider
from tests.conftest import TEST_API_KEY
from tests.fakes import FakeProvider, FakeSDKClient, api_error, load_fixture_bytes

FRONTEND_KEY = "AIza" + "SyFrontendTypedKey" + "9" * 17
ORIGINAL_FIELDS = {"risk_score", "verdict", "scam_type", "confidence_level", "signal_scores", "red_flags",
                   "explanation", "recommended_action", "mode", "offline_fallback_used"}


def test_legacy_and_v1_routes_return_identical_bodies(client):
    legacy = client.post("/api/analyze-text", data={"text": "same input"}).json()
    v1 = client.post("/api/v1/analyze/text", data={"text": "same input"}).json()
    assert legacy == v1
    wav = ("c.wav", load_fixture_bytes("tiny.wav"), "audio/wav")
    assert client.post("/api/analyze-audio", files={"file": wav}).json() == client.post("/api/v1/analyze/audio", files={"file": wav}).json()


def test_original_response_fields_and_value_types_are_preserved(client):
    """Everything the existing dashboard reads is still there, with the same types.
    Intentional change: verdict values are canonical (low_risk/suspicious/high_risk) and the frontend maps them."""
    body = client.post("/api/analyze-text", data={"text": "x"}).json()
    assert ORIGINAL_FIELDS <= set(body)
    assert isinstance(body["risk_score"], int) and body["verdict"] in {"low_risk", "suspicious", "high_risk"}
    assert body["confidence_level"] in {"Low", "Medium", "High"} and isinstance(body["red_flags"], list)
    assert all(isinstance(flag, str) for flag in body["red_flags"]) and isinstance(body["scam_type"], str)
    assert isinstance(body["explanation"], str) and isinstance(body["recommended_action"], str)
    assert isinstance(body["offline_fallback_used"], bool) and body["mode"] in {"text", "audio"}
    assert all(isinstance(v, int) for v in body["signal_scores"].values())


def test_legacy_routes_are_marked_deprecated_in_openapi(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert paths["/api/analyze-text"]["post"].get("deprecated") is True
    assert "deprecated" not in paths["/api/v1/analyze/text"]["post"]


def test_multipart_field_names_are_text_and_file(client):
    schema = client.get("/openapi.json").json()["components"]["schemas"]
    props = {p for s in schema.values() for p in s.get("properties", {})}
    assert {"text", "file"} <= props and "api_key" not in props      # api_key is no longer part of the contract


def test_cors_policy_is_unchanged(client):
    ok = client.options("/api/analyze-text", headers={"Origin": "http://localhost:8000", "Access-Control-Request-Method": "POST"})
    bad = client.options("/api/analyze-text", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:8000"
    assert "access-control-allow-origin" not in bad.headers


# ---------------------------------------------------------------- no API key from the frontend
@pytest.mark.parametrize("path", ["/api/analyze-text", "/api/v1/analyze/text"])
def test_api_key_sent_by_an_old_frontend_is_ignored_text(fake_sdk, path):
    settings = Settings(gemini_api_key=TEST_API_KEY)
    app = create_app(settings, GeminiProvider(settings, client_factory=fake_sdk))
    r = TestClient(app).post(path, data={"text": "hello", "api_key": FRONTEND_KEY})
    assert r.status_code == 200 and r.json()["verdict"] == "high_risk"
    assert [c.api_key for c in fake_sdk.instances] == [TEST_API_KEY]      # the server key, never the typed one


def test_api_key_sent_by_an_old_frontend_is_ignored_audio(fake_sdk):
    settings = Settings(gemini_api_key=TEST_API_KEY)
    app = create_app(settings, GeminiProvider(settings, client_factory=fake_sdk))
    TestClient(app).post("/api/analyze-audio", data={"api_key": FRONTEND_KEY},
                         files={"file": ("c.wav", load_fixture_bytes("tiny.wav"), "audio/wav")})
    assert [c.api_key for c in fake_sdk.instances] == [TEST_API_KEY]


def test_a_frontend_key_cannot_rescue_a_server_without_one(fake_sdk):
    settings = Settings(gemini_api_key="")
    app = create_app(settings, GeminiProvider(settings, client_factory=fake_sdk))
    body = TestClient(app).post("/api/analyze-text", data={"text": "hello", "api_key": FRONTEND_KEY}).json()
    assert body["reason_code"] == "CONFIGURATION_ERROR" and fake_sdk.instances == []


# ---------------------------------------------------------------- nothing sensitive leaks
def test_provider_secrets_never_reach_client_or_logs(fake_sdk, caplog):
    caplog.set_level(logging.DEBUG, logger="scamshield")
    secret_text = "SECRET-USER-MESSAGE-31337"
    leaky = api_error(500, f"echo {secret_text} key {TEST_API_KEY} {FRONTEND_KEY} path C:\\Users\\victim\\secret.txt")
    fake_sdk.generate_error = leaky
    settings = Settings(gemini_api_key=TEST_API_KEY)
    app = create_app(settings, GeminiProvider(settings, client_factory=fake_sdk))
    r = TestClient(app).post("/api/analyze-text", data={"text": secret_text, "api_key": FRONTEND_KEY})
    for haystack in (r.text, caplog.text):
        for needle in (secret_text, TEST_API_KEY, FRONTEND_KEY, "victim", "Traceback"):
            assert needle not in haystack, needle


def test_audio_content_and_filename_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG, logger="scamshield")
    client.post("/api/analyze-audio", files={"file": ("my-private-voicemail.wav", b"RIFFsecretaudiobytes", "audio/wav")})
    assert "my-private-voicemail" not in caplog.text and "secretaudiobytes" not in caplog.text


def test_unhandled_errors_return_a_generic_500_without_details(app, caplog):
    caplog.set_level(logging.DEBUG, logger="scamshield")

    def explode():
        raise RuntimeError(f"db password hunter2 key {FRONTEND_KEY} at C:\\srv\\app\\secret.py")

    app.dependency_overrides[get_pipeline] = explode
    r = TestClient(app, raise_server_exceptions=False).post("/api/analyze-text", data={"text": "hi"})
    assert r.status_code == 500 and r.json() == {"detail": "Internal server error."}
    assert "hunter2" not in r.text and FRONTEND_KEY not in r.text and "secret.py" not in r.text
    assert FRONTEND_KEY not in caplog.text                                   # redacted in logs as well


def test_config_object_does_not_expose_the_key_in_logs(fake_sdk, caplog):
    caplog.set_level(logging.DEBUG, logger="scamshield")
    settings = Settings(gemini_api_key=TEST_API_KEY)
    create_app(settings, FakeProvider())
    assert TEST_API_KEY not in caplog.text


# ------------------------------------------------------------------ Phase 5: no credential can be supplied through ANY channel
@pytest.mark.parametrize("channel", ["form", "query", "header-bearer", "header-goog", "header-x-api-key", "cookie"])
def test_a_client_supplied_key_is_never_used_whatever_channel_it_arrives_by(fake_sdk, channel):
    settings = Settings(gemini_api_key=TEST_API_KEY, rate_limit_text="1000/minute", rate_limit_global="1000/minute")
    app = create_app(settings, GeminiProvider(settings, client_factory=fake_sdk))
    kwargs = {"data": {"text": "hello"}, "headers": {}, "params": {}}
    if channel == "form":
        kwargs["data"]["api_key"] = FRONTEND_KEY
    elif channel == "query":
        kwargs["params"] = {"api_key": FRONTEND_KEY, "key": FRONTEND_KEY}
    elif channel == "header-bearer":
        kwargs["headers"]["Authorization"] = f"Bearer {FRONTEND_KEY}"
    elif channel == "header-goog":
        kwargs["headers"]["X-Goog-Api-Key"] = FRONTEND_KEY
    elif channel == "header-x-api-key":
        kwargs["headers"]["X-API-Key"] = FRONTEND_KEY
    else:
        kwargs["headers"]["Cookie"] = f"api_key={FRONTEND_KEY}"
    r = TestClient(app).post("/api/analyze-text", **kwargs)
    assert r.status_code == 200 and r.json()["verdict"] == "high_risk"
    assert [c.api_key for c in fake_sdk.instances] == [TEST_API_KEY]           # only ever the server's own key
    assert FRONTEND_KEY not in r.text and FRONTEND_KEY not in str(r.headers)


def test_the_api_schema_advertises_no_credential_parameters_or_security_schemes(client):
    spec = client.get("/openapi.json").json()
    text = str(spec).lower()
    assert "api_key" not in text and "securityschemes" not in text and "authorization" not in text and "apikey" not in text


def test_no_response_ever_contains_the_configured_key(client):
    for response in (client.post("/api/analyze-text", data={"text": "hello"}), client.get("/health"), client.get("/api/v1/status"),
                     client.get("/openapi.json"), client.get("/"), client.get("/missing"),
                     client.post("/api/analyze-text", data={})):
        assert TEST_API_KEY not in response.text and TEST_API_KEY not in str(response.headers)
