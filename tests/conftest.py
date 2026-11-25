"""Shared fixtures.

Guarantees for EVERY test (all autouse):
* APP_ENV=test, so a developer's real ``.env`` is never loaded;
* no real Gemini/Google key is visible in the environment;
* ``genai.Client`` cannot be constructed (adapter tests inject ``FakeSDKClient`` instead);
* DNS lookups and outbound connections are refused.
A test that accidentally tried to reach Gemini fails loudly instead of using quota.
"""
from __future__ import annotations

import os

os.environ["APP_ENV"] = "test"  # must be set before app modules are imported

import socket  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from google import genai  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.fakes import FakeProvider, FakeSDKClient  # noqa: E402

TEST_API_KEY = "test-key-not-real"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def private_default_upload_dir(monkeypatch, tmp_path_factory):
    """Settings built without an explicit TEMP_UPLOAD_DIR must not touch the real system temp directory."""
    import app.core.config as config

    scratch = tmp_path_factory.mktemp("default-uploads")
    monkeypatch.setattr(config, "default_upload_dir", lambda: scratch / "scamshield-uploads")
    return scratch


@pytest.fixture(autouse=True)
def no_real_genai_client(monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("A real google.genai.Client must never be constructed in tests.")

    monkeypatch.setattr(genai, "Client", refuse)


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise RuntimeError("Network access is disabled in tests (no live Gemini calls).")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture()
def fake_sdk():
    FakeSDKClient.reset()
    yield FakeSDKClient
    FakeSDKClient.reset()


@pytest.fixture()
def upload_dir(tmp_path):
    """A private upload directory per test, so tests can assert that nothing is left behind."""
    return tmp_path / "uploads"


@pytest.fixture()
def settings(upload_dir) -> Settings:
    # Generous rate limits so ordinary tests never trip them; rate-limit tests build their own Settings.
    return Settings(
        gemini_api_key=TEST_API_KEY, app_env="test", gemini_timeout_seconds=5.0, temp_upload_dir=str(upload_dir),
        rate_limit_text="100000/minute", rate_limit_audio="100000/minute", rate_limit_default="100000/minute",
        rate_limit_global="100000/minute",
    )


@pytest.fixture()
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture()
def app(settings, provider):
    return create_app(settings, provider)


@pytest.fixture()
def client(app):
    return TestClient(app, raise_server_exceptions=False)
