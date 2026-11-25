"""Errors are concise, stable and leak nothing: no paths, stack traces, keys, input echoes or provider text."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_pipeline
from app.core.config import Settings
from app.core.errors import AnalysisError, ReasonCode
from app.main import create_app
from tests import media_samples as ms
from tests.conftest import TEST_API_KEY
from tests.fakes import FakeProvider, FakeSDKClient, api_error

SECRET_TEXT = "SECRET-USER-TEXT-98765"
LEAK_PATTERNS = [r"Traceback", r"site-packages", r"C:\\", r"C:/", r"/home/", r"/Users/", r"\.py\b", r"line \d+", r"AIza[0-9A-Za-z_\-]{10,}",
                 re.escape(TEST_API_KEY), re.escape(str(Path(__file__).resolve().parents[2])), r"scamshield-uploads", r"up-[0-9a-f]{6,}"]


def assert_no_leaks(text: str) -> None:
    for pattern in LEAK_PATTERNS:
        assert not re.search(pattern, text), f"{pattern!r} leaked in: {text[:200]}"


def assert_error_shape(response, status: int, code: str) -> None:
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"detail", "code"} and isinstance(body["detail"], str) and body["code"] == code
    assert 0 < len(body["detail"]) < 200
    assert_no_leaks(response.text)


# ------------------------------------------------------------------ stable codes and one shape
def test_every_client_error_has_the_same_shape_and_a_stable_code(client, provider, upload_dir):
    wav = ms.wav()
    audio = lambda **kw: client.post("/api/analyze-audio", files={"file": (kw.get("name", "a.wav"), kw.get("content", wav), kw.get("mime", "audio/wav"))})
    assert_error_shape(client.post("/api/analyze-text", data={"text": "   "}), 400, "INVALID_INPUT")
    assert_error_shape(client.post("/api/analyze-text", data={"text": "a" * 25_000}), 413, "PAYLOAD_TOO_LARGE")
    assert_error_shape(client.post("/api/analyze-text", data={}), 422, "INVALID_REQUEST")
    assert_error_shape(audio(name="a.exe"), 415, "UNSUPPORTED_MEDIA_TYPE")
    assert_error_shape(audio(content=ms.DISGUISED["windows_executable"]), 415, "UNSUPPORTED_MEDIA_TYPE")
    assert_error_shape(audio(content=b""), 400, "INVALID_INPUT")
    assert_error_shape(client.get("/does-not-exist"), 404, "NOT_FOUND")
    assert_error_shape(client.get("/api/analyze-text"), 405, "METHOD_NOT_ALLOWED")


def test_validation_errors_name_the_field_but_never_echo_what_was_sent(client):
    r = client.post("/api/analyze-audio", data={"file": SECRET_TEXT, "api_key": TEST_API_KEY, "other": "x"})   # a string where a file is required
    assert r.status_code == 422 and "file" in r.json()["detail"]
    assert SECRET_TEXT not in r.text and TEST_API_KEY not in r.text
    r = client.post("/api/analyze-text", data={"wrong_field": SECRET_TEXT})
    assert r.status_code == 422 and "text" in r.json()["detail"] and SECRET_TEXT not in r.text


# ------------------------------------------------------------------ unhandled and provider failures
def test_an_unhandled_exception_returns_a_generic_500_with_no_details(app):
    def explode():
        raise RuntimeError(f"db password hunter2 at C:\\srv\\app\\secret.py key {TEST_API_KEY} {SECRET_TEXT}")

    app.dependency_overrides[get_pipeline] = explode
    r = TestClient(app, raise_server_exceptions=False).post("/api/analyze-text", data={"text": SECRET_TEXT})
    assert r.status_code == 500 and r.json() == {"detail": "Internal server error."}
    assert "hunter2" not in r.text and SECRET_TEXT not in r.text
    assert_no_leaks(r.text)


@pytest.mark.parametrize("path", ["/api/analyze-text", "/api/v1/analyze/text"])
def test_provider_failures_keep_the_unable_to_assess_contract_and_leak_nothing(fake_sdk, path):
    fake_sdk.generate_error = api_error(503, f"upstream said {SECRET_TEXT} using key {TEST_API_KEY} at /srv/google/internal.py line 88")
    settings = Settings(gemini_api_key=TEST_API_KEY, gemini_max_retries=0, rate_limit_text="1000/minute", rate_limit_global="1000/minute")
    from app.providers.gemini import GeminiProvider

    app = create_app(settings, GeminiProvider(settings, client_factory=fake_sdk))
    r = TestClient(app).post(path, data={"text": "hello"})
    body = r.json()
    assert r.status_code == 200
    assert (body["status"], body["verdict"], body["risk_score"], body["reason_code"]) == ("unable_to_assess", "unable_to_assess", None, "PROVIDER_UNAVAILABLE")
    assert body["verdict"] != "low_risk" and SECRET_TEXT not in r.text
    assert_no_leaks(r.text)


@pytest.mark.parametrize("reason", list(ReasonCode))
def test_no_reason_code_response_contains_internal_detail(settings, reason):
    app = create_app(settings, FakeProvider(error=AnalysisError(reason, f"INTERNALDETAIL-X {SECRET_TEXT} C:\\secret\\x.py AIza{'z' * 30}")))
    r = TestClient(app).post("/api/analyze-text", data={"text": "hello"})
    assert r.status_code == 200 and "INTERNALDETAIL-X" not in r.text and SECRET_TEXT not in r.text
    assert_no_leaks(r.text)


def test_local_file_errors_do_not_reveal_paths(client, monkeypatch):
    from app.utils import temp_files

    def broken(*a, **k):
        raise PermissionError(r"C:\Users\victim\AppData\Local\Temp\scamshield-uploads\up-abc\secret.wav")

    monkeypatch.setattr(temp_files, "_open_exclusive", broken)
    r = client.post("/api/analyze-audio", files={"file": ("a.wav", ms.wav(), "audio/wav")})
    assert r.json()["reason_code"] == "MEDIA_PROCESSING_FAILED"
    assert "victim" not in r.text and "up-abc" not in r.text
    assert_no_leaks(r.text)


# ------------------------------------------------------------------ the generic status endpoint
def test_status_reports_ready_or_not_ready_and_nothing_else(fake_sdk):
    for key, expected in ((TEST_API_KEY, "ready"), ("", "not_ready")):
        app = create_app(Settings(gemini_api_key=key), FakeProvider())
        r = TestClient(app).get("/api/v1/status")
        assert r.status_code == 200 and r.json() == {"status": expected}
        assert TEST_API_KEY not in r.text and TEST_API_KEY not in str(r.headers) and "key" not in r.text.lower()


def test_the_configured_key_never_appears_in_any_public_page_or_header(client):
    for path in ("/health", "/", "/openapi.json", "/api/v1/status"):
        r = client.get(path)
        assert TEST_API_KEY not in r.text and TEST_API_KEY not in str(r.headers), path
    for path in ("/health", "/api/v1/status"):
        assert "GEMINI_API_KEY" not in client.get(path).text
