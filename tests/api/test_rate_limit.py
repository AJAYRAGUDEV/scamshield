"""Rate limiting (slowapi, in-memory): per client, global, and not bypassable through request headers."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core.config import Settings
from app.core.security import make_client_key
from app.main import create_app
from tests import media_samples as ms
from tests.fakes import FakeProvider

WAV = ms.wav()


def make_app(provider=None, upload_dir=None, **over):
    values = dict(gemini_api_key="k", rate_limit_text="3/minute", rate_limit_audio="2/minute",
                  rate_limit_default="5/minute", rate_limit_global="1000/minute")
    if upload_dir is not None:
        values["temp_upload_dir"] = str(upload_dir)
    values.update(over)
    provider = provider or FakeProvider()
    return create_app(Settings(**values), provider), provider


def client_at(app, ip="203.0.113.10"):
    return TestClient(app, client=(ip, 50000), raise_server_exceptions=False)


def text(c, path="/api/analyze-text", **kw):
    return c.post(path, data={"text": "hello"}, **kw)


def audio(c, path="/api/analyze-audio", **kw):
    return c.post(path, files={"file": ("c.wav", WAV, "audio/wav")}, **kw)


def assert_limited(r):
    assert r.status_code == 429
    body = r.json()
    assert body == {"detail": "Too many requests. Please wait a moment and try again.", "code": "RATE_LIMITED"}
    assert r.headers["retry-after"].isdigit() and int(r.headers["retry-after"]) >= 1


# ------------------------------------------------------------------ basics
def test_requests_within_the_limit_succeed_and_the_next_one_gets_429():
    app, provider = make_app()
    c = client_at(app)
    assert [text(c).status_code for _ in range(3)] == [200, 200, 200]
    assert_limited(text(c))
    assert len(provider.text_calls) == 3                      # the limited request never reached the provider


def test_audio_has_its_own_limit_and_is_rejected_before_any_upload_processing(upload_dir):
    app, provider = make_app(upload_dir=upload_dir)
    c = client_at(app)
    assert [audio(c).status_code for _ in range(2)] == [200, 200]
    assert_limited(audio(c))
    assert len(provider.audio_paths) == 2 and not list(upload_dir.rglob("*"))


def test_text_and_audio_limits_are_independent_of_each_other():
    app, _ = make_app()
    c = client_at(app)
    for _ in range(3):
        text(c)
    assert_limited(text(c))
    assert audio(c).status_code == 200


def test_the_legacy_and_v1_paths_share_one_counter():
    app, provider = make_app()
    c = client_at(app)
    assert text(c, "/api/analyze-text").status_code == 200
    assert text(c, "/api/v1/analyze/text").status_code == 200
    assert text(c, "/api/analyze-text").status_code == 200
    assert_limited(text(c, "/api/v1/analyze/text"))           # switching paths is not a way around the limit
    assert_limited(text(c, "/api/analyze-text"))
    assert len(provider.text_calls) == 3


def test_the_429_body_is_the_same_on_every_limited_route():
    app, _ = make_app(rate_limit_default="1/minute", rate_limit_text="1/minute", rate_limit_audio="1/minute")
    c = client_at(app)
    text(c)
    audio(c)
    c.get("/health")
    c.get("/api/v1/status")
    c.get("/")
    for r in (text(c), audio(c), c.get("/health"), c.get("/api/v1/status"), c.get("/")):
        assert_limited(r)


def test_error_responses_that_were_rejected_early_still_count_but_valid_ones_are_unaffected_by_other_clients():
    app, _ = make_app()
    for _ in range(3):
        text(client_at(app, "203.0.113.1"))
    assert_limited(text(client_at(app, "203.0.113.1")))
    assert text(client_at(app, "203.0.113.2")).status_code == 200      # each client has its own allowance


def test_a_disabled_limiter_never_returns_429():
    app, _ = make_app(rate_limit_enabled=False)
    c = client_at(app)
    assert all(text(c).status_code == 200 for _ in range(10))


def test_health_status_and_the_web_page_are_limited_too():
    app, _ = make_app(rate_limit_default="2/minute")
    c = client_at(app)
    for path in ("/health", "/api/v1/status", "/"):
        c2 = client_at(make_app(rate_limit_default="2/minute")[0])
        assert [c2.get(path).status_code for _ in range(2)] == [200, 200]
        assert_limited(c2.get(path))


# ------------------------------------------------------------------ global limit (quota protection across clients)
def test_a_global_limit_caps_all_clients_combined_across_text_and_audio():
    app, provider = make_app(rate_limit_global="4/minute", rate_limit_text="100/minute", rate_limit_audio="100/minute")
    codes = []
    for i in range(6):
        c = client_at(app, f"198.51.100.{i + 1}")             # six different clients
        codes.append((text(c) if i % 2 == 0 else audio(c)).status_code)
    assert codes == [200, 200, 200, 200, 429, 429]
    assert len(provider.text_calls) + len(provider.audio_paths) == 4


# ------------------------------------------------------------------ the limit cannot be dodged by changing headers
@pytest.mark.parametrize("header", ["X-Forwarded-For", "X-Real-IP", "Forwarded", "X-Client-IP", "True-Client-IP", "CF-Connecting-IP",
                                    "X-Forwarded-Host", "X-Request-ID", "User-Agent", "Authorization", "Cookie", "Origin"])
def test_changing_a_request_header_does_not_get_a_fresh_allowance(header):
    app, provider = make_app()
    c = client_at(app)
    for i in range(3):
        assert text(c, headers={header: f"10.9.8.{i}"}).status_code == 200
    assert_limited(text(c, headers={header: "10.9.8.250"}))
    assert len(provider.text_calls) == 3


def test_forwarded_for_from_an_untrusted_peer_is_ignored_even_when_proxies_are_configured():
    app, _ = make_app(trusted_proxy_ips=("10.0.0.0/8",))
    c = client_at(app, "203.0.113.77")                         # NOT a trusted proxy
    for i in range(3):
        text(c, headers={"X-Forwarded-For": f"1.1.1.{i}"})
    assert_limited(text(c, headers={"X-Forwarded-For": "9.9.9.9"}))


# ------------------------------------------------------------------ trusted proxy support
def request_with(peer, **headers) -> Request:
    scope = {"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()], "client": (peer, 1234)}
    return Request(scope)


@pytest.fixture()
def key():
    return make_client_key(Settings(trusted_proxy_ips=("10.0.0.0/8", "192.168.1.1")))


def test_default_identity_is_the_tcp_peer():
    k = make_client_key(Settings())
    assert k(request_with("203.0.113.5", **{"x-forwarded-for": "1.2.3.4"})) == "203.0.113.5"


def test_a_trusted_proxy_may_name_the_client(key):
    assert key(request_with("10.0.0.5", **{"x-forwarded-for": "203.0.113.9"})) == "203.0.113.9"


def test_the_client_cannot_choose_its_identity_by_prefixing_the_header(key):
    """Proxies APPEND the real client address; everything the client put in front of it is ignored."""
    assert key(request_with("10.0.0.5", **{"x-forwarded-for": "1.2.3.4, 5.6.7.8, 203.0.113.9"})) == "203.0.113.9"


def test_chained_trusted_proxies_are_skipped(key):
    assert key(request_with("10.0.0.5", **{"x-forwarded-for": "203.0.113.9, 10.0.0.77, 192.168.1.1"})) == "203.0.113.9"


@pytest.mark.parametrize("value", ["", "garbage", "203.0.113.9, not-an-ip", ",,,"])
def test_malformed_forwarded_headers_fall_back_to_the_proxy_address(key, value):
    assert key(request_with("10.0.0.5", **{"x-forwarded-for": value})) == "10.0.0.5"


def test_a_missing_peer_is_handled():
    scope = {"type": "http", "headers": [], "client": None}
    assert make_client_key(Settings())(Request(scope)) == "unknown"


def test_behind_a_trusted_proxy_each_forwarded_client_gets_its_own_allowance():
    app, _ = make_app(trusted_proxy_ips=("10.0.0.0/8",))
    c = client_at(app, "10.0.0.5")
    for _ in range(3):
        assert text(c, headers={"X-Forwarded-For": "203.0.113.1"}).status_code == 200
    assert_limited(text(c, headers={"X-Forwarded-For": "203.0.113.1"}))
    assert text(c, headers={"X-Forwarded-For": "203.0.113.2"}).status_code == 200
