from __future__ import annotations

import logging
import sys

from app.core.logging import RedactingFormatter, log_event, redact

FAKE_KEY = "AIza" + "Sy" + "A" * 33


def test_redact_masks_google_key_shapes():
    assert FAKE_KEY not in redact(f"failed with key={FAKE_KEY} here")
    assert "[redacted API key]" in redact(FAKE_KEY)
    assert redact("nothing secret") == "nothing secret"


def test_formatter_redacts_inside_exception_tracebacks():
    try:
        raise RuntimeError(f"upstream said key {FAKE_KEY}")
    except RuntimeError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom %s", (FAKE_KEY,), sys.exc_info())
    out = RedactingFormatter("%(message)s").format(record)
    assert FAKE_KEY not in out and "[redacted API key]" in out


def test_log_event_is_single_line_key_value(caplog):
    caplog.set_level(logging.INFO, logger="scamshield.test")
    log_event(logging.getLogger("scamshield.test"), logging.INFO, "thing_happened", mode="text", note="a\nb\r\nc")
    assert caplog.messages == ["thing_happened mode=text note=a b  c"]


def test_request_id_is_generated_echoed_and_sanitized(client):
    generated = client.get("/health").headers["x-request-id"]
    assert len(generated) == 16
    assert client.get("/health", headers={"X-Request-ID": "trace-123.ok"}).headers["x-request-id"] == "trace-123.ok"
    for bad in ("has space", "x" * 65, "semi;colon", "<script>"):
        assert client.get("/health", headers={"X-Request-ID": bad}).headers["x-request-id"] != bad


def test_request_id_reaches_the_provider_call_context(client, provider):
    client.post("/api/v1/analyze/text", data={"text": "hi"}, headers={"X-Request-ID": "req-777"})
    assert provider.request_ids == ["req-777"]


# ------------------------------------------------------------------ Phase 5: credentials and headers are redacted
import pytest  # noqa: E402

from app.core.logging import _AccessLogRedactor, register_secret  # noqa: E402


@pytest.mark.parametrize("text,leaked", [
    ("Authorization: Bearer abcdefghijkl1234567", "abcdefghijkl1234567"),
    ("authorization=Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
    ("sent Bearer abcdefgh12345678 to server", "abcdefgh12345678"),
    ("GET /api?api_key=SECRETVALUE123&x=1", "SECRETVALUE123"),
    ("GET /api?key=SECRETVALUE123", "SECRETVALUE123"),
    ("url ?token=tok_live_998877 end", "tok_live_998877"),
    ("password=hunter2horse ok", "hunter2horse"),
    ("X-Goog-Api-Key: " + "AIza" + "x" * 30, "x" * 30),
    ("x-api-key: myverysecretvalue", "myverysecretvalue"),
    ("Cookie: session=abc123def456; theme=dark", "abc123def456"),
])
def test_credentials_in_any_common_shape_are_redacted(text, leaked):
    assert leaked not in redact(text) and "[redacted" in redact(text)


def test_ordinary_text_is_left_alone():
    assert redact("analysis_completed mode=text verdict=high_risk risk_score=96") == "analysis_completed mode=text verdict=high_risk risk_score=96"


def test_registered_secrets_are_masked_wherever_they_appear():
    register_secret("my-configured-secret-value")
    assert "my-configured-secret-value" not in redact("boom: my-configured-secret-value in the middle")


def test_uvicorn_access_lines_lose_their_query_string():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "x", 1, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4:5", "GET", "/api/analyze-text?api_key=SECRETVALUE123&x=1", "1.1", 200), None)
    assert _AccessLogRedactor().filter(record)
    assert "SECRETVALUE123" not in record.getMessage() and "/api/analyze-text?[redacted]" in record.getMessage()


def test_credentials_sent_by_a_client_never_reach_our_logs(client, caplog):
    caplog.set_level(logging.DEBUG, logger="scamshield")
    client.get("/health?api_key=QUERYSECRET123456&token=TOKENSECRET123456", headers={"Authorization": "Bearer HEADERSECRET1234567", "Cookie": "s=COOKIESECRET123456"})
    client.post("/api/analyze-text?key=QUERYSECRET654321", data={"text": "hello", "api_key": "FORMSECRET1234567"},
                headers={"X-Goog-Api-Key": "GOOGHEADERSECRET1234567"})
    for secret in ("QUERYSECRET", "TOKENSECRET", "HEADERSECRET", "COOKIESECRET", "FORMSECRET", "GOOGHEADERSECRET"):
        assert secret not in caplog.text, secret
