"""CORS policy, security headers, Content-Security-Policy and production exposure."""
from __future__ import annotations

import re
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from app.core.config import FRONTEND_DIR, SettingsError, Settings, load_settings
from app.core.security import CONTENT_SECURITY_POLICY, PERMISSIONS_POLICY
from app.main import create_app
from tests.fakes import FakeProvider

ALLOWED = "http://localhost:8000"
EVIL = "http://evil.example"


def make_client(**over):
    values = dict(gemini_api_key="k", rate_limit_default="100000/minute", rate_limit_text="100000/minute",
                  rate_limit_global="100000/minute")
    values.update(over)
    return TestClient(create_app(Settings(**values), FakeProvider()), raise_server_exceptions=False)


# ------------------------------------------------------------------ CORS
def test_default_local_development_origins_are_allowed(client):
    for origin in ("http://localhost", "http://127.0.0.1", "http://localhost:8000", "http://127.0.0.1:8000"):
        r = client.get("/health", headers={"Origin": origin})
        assert r.headers["access-control-allow-origin"] == origin


def test_an_unapproved_origin_receives_no_cors_permission(client):
    r = client.get("/health", headers={"Origin": EVIL})
    assert "access-control-allow-origin" not in r.headers
    r = client.options("/api/analyze-text", headers={"Origin": EVIL, "Access-Control-Request-Method": "POST"})
    assert r.status_code == 400 and "access-control-allow-origin" not in r.headers


def test_credentials_are_never_allowed_and_origins_are_never_a_wildcard(client):
    r = client.options("/api/analyze-text", headers={"Origin": ALLOWED, "Access-Control-Request-Method": "POST"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == ALLOWED       # echoed, not "*"
    assert "access-control-allow-credentials" not in r.headers
    assert "*" not in r.headers["access-control-allow-origin"]


def test_preflight_allows_only_the_methods_and_headers_in_use(client):
    ok = client.options("/api/analyze-text", headers={"Origin": ALLOWED, "Access-Control-Request-Method": "POST",
                                                     "Access-Control-Request-Headers": "content-type"})
    assert ok.status_code == 200
    assert set(ok.headers["access-control-allow-methods"].replace(" ", "").split(",")) == {"GET", "POST"}
    for method in ("PUT", "DELETE", "PATCH"):
        bad = client.options("/api/analyze-text", headers={"Origin": ALLOWED, "Access-Control-Request-Method": method})
        assert bad.status_code == 400
    for header in ("authorization", "x-custom", "x-api-key"):
        bad = client.options("/api/analyze-text", headers={"Origin": ALLOWED, "Access-Control-Request-Method": "POST",
                                                          "Access-Control-Request-Headers": header})
        assert bad.status_code == 400


def test_the_request_id_and_retry_after_headers_are_exposed_to_browsers(client):
    r = client.get("/health", headers={"Origin": ALLOWED})
    exposed = {h.strip().lower() for h in r.headers["access-control-expose-headers"].split(",")}
    assert {"x-request-id", "retry-after"} <= exposed


def test_origins_are_configurable_and_the_defaults_then_stop_applying():
    c = make_client(cors_allow_origins=("https://app.example.com",))
    assert c.get("/health", headers={"Origin": "https://app.example.com"}).headers["access-control-allow-origin"] == "https://app.example.com"
    assert "access-control-allow-origin" not in c.get("/health", headers={"Origin": ALLOWED}).headers


@pytest.mark.parametrize("value", ["*", "http://localhost, *", "localhost:8000", "ftp://x.example", "http://exa mple.com", "https://a.example/path", "null"])
def test_unsafe_origin_configuration_is_refused_at_startup(value):
    with pytest.raises(SettingsError, match="CORS_ALLOW_ORIGINS"):
        load_settings({"CORS_ALLOW_ORIGINS": value})


def test_valid_origin_lists_are_parsed():
    s = load_settings({"CORS_ALLOW_ORIGINS": "https://a.example.com, http://localhost:5173"})
    assert s.cors_allow_origins == ("https://a.example.com", "http://localhost:5173")


# ------------------------------------------------------------------ security headers
def test_baseline_headers_are_on_every_kind_of_response(client):
    for response in (client.get("/health"), client.get("/"), client.get("/nope"), client.get("/api/analyze-text"),
                     client.post("/api/analyze-text", data={"text": "hi"}), client.get("/api/v1/status")):
        h = response.headers
        assert h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "no-referrer"
        assert h["x-frame-options"] == "DENY"
        assert h["permissions-policy"] == PERMISSIONS_POLICY and "microphone=()" in h["permissions-policy"]


def test_api_responses_are_not_cacheable_but_the_page_is_not_affected(client):
    assert client.post("/api/analyze-text", data={"text": "hi"}).headers["cache-control"] == "no-store"
    assert client.get("/api/v1/status").headers["cache-control"] == "no-store"
    assert "cache-control" not in client.get("/health").headers


def test_the_csp_is_sent_with_the_html_page_only(client):
    assert client.get("/").headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    for response in (client.get("/health"), client.post("/api/analyze-text", data={"text": "hi"})):
        assert "content-security-policy" not in response.headers


def directives() -> dict[str, list[str]]:
    return {d.split()[0]: d.split()[1:] for d in CONTENT_SECURITY_POLICY.split("; ")}


def test_the_csp_blocks_the_dangerous_defaults():
    d = directives()
    assert d["default-src"] == ["'self'"]
    assert d["object-src"] == ["'none'"] and d["base-uri"] == ["'none'"] and d["frame-ancestors"] == ["'none'"]
    assert d["connect-src"] == ["'self'"] and d["form-action"] == ["'self'"]
    assert "*" not in CONTENT_SECURITY_POLICY.split() and "data:" not in d["script-src"] and "'unsafe-eval'" not in CONTENT_SECURITY_POLICY
    assert not any(src in ("http:", "https:") for values in d.values() for src in values)


def test_every_external_resource_the_page_loads_is_allowed_by_the_csp():
    """So the policy cannot silently break the existing frontend."""
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    d = directives()

    def host(url): return f"{urlparse(url).scheme}://{urlparse(url).netloc}"

    for url in re.findall(r'<script[^>]+src="(https?://[^"]+)"', html):
        assert host(url) in d["script-src"], url
    for url in re.findall(r'<link[^>]+href="(https://fonts\.googleapis\.com[^"]+)"', html):
        assert host(url) in d["style-src"], url
    assert "https://fonts.gstatic.com" in d["font-src"]


def test_the_remaining_csp_weakness_is_documented_in_the_policy_comment():
    source = (FRONTEND_DIR.parent / "app" / "core" / "security.py").read_text(encoding="utf-8")
    assert "has NO inline <script>" in source and "style-src still does" in source and "Tailwind Play CDN" in source


def test_script_src_has_no_unsafe_inline_and_style_src_does():
    d = directives()
    assert "'unsafe-inline'" not in d["script-src"] and "'unsafe-inline'" in d["style-src"]


def test_hsts_is_sent_only_when_enabled_and_the_connection_is_https():
    plain = make_client()
    assert "strict-transport-security" not in plain.get("https://testserver/health").headers
    enabled = make_client(enable_hsts=True)
    assert enabled.get("https://testserver/health").headers["strict-transport-security"].startswith("max-age=")
    assert "strict-transport-security" not in enabled.get("http://testserver/health").headers


def test_headers_are_present_on_error_and_rate_limit_responses():
    c = make_client(rate_limit_default="1/minute")
    c.get("/health")
    limited = c.get("/health")
    assert limited.status_code == 429 and limited.headers["x-content-type-options"] == "nosniff"


# ------------------------------------------------------------------ exposure in production
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_interactive_docs_are_disabled_in_production_and_enabled_otherwise(path):
    assert make_client(app_env="production").get(path).status_code == 404
    assert make_client(app_env="development").get(path).status_code == 200


def test_uploads_and_the_source_tree_are_not_served():
    c = make_client()
    for path in ("/scamshield-uploads", "/uploads", "/app/main.py", "/server.py", "/.env", "/frontend/index.html", "/legacy/server.py", "/tests"):
        assert c.get(path).status_code == 404, path
