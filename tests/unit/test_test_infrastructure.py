"""Meta-tests: the suite cannot reach Gemini, and the fakes behave."""
from __future__ import annotations

import os
import socket

import pytest
from google import genai

from tests.fakes import FakeProvider, FakeSDKClient


def test_real_genai_client_cannot_be_constructed():
    with pytest.raises(RuntimeError, match="must never be constructed"):
        genai.Client(api_key="x")


def test_environment_is_isolated_from_real_keys():
    assert os.environ["APP_ENV"] == "test"
    assert all(name not in os.environ for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_MODEL"))


def test_network_is_blocked():
    with pytest.raises(RuntimeError, match="Network access is disabled"):
        socket.getaddrinfo("generativelanguage.googleapis.com", 443)
    with pytest.raises(RuntimeError, match="Network access is disabled"):
        socket.create_connection(("generativelanguage.googleapis.com", 443))


def test_fake_sdk_state_is_reset_between_tests_part_1(fake_sdk):
    fake_sdk.generate_error = RuntimeError("leak?")
    FakeSDKClient(api_key="k")


def test_fake_sdk_state_is_reset_between_tests_part_2(fake_sdk):
    assert fake_sdk.generate_error is None and fake_sdk.instances == []


def test_fake_sdk_has_no_sync_surface():
    client = FakeSDKClient(api_key="k")
    assert not hasattr(client, "models") and not hasattr(client, "files")   # only client.aio exists


def test_fake_provider_satisfies_the_provider_interface():
    from app.providers.base import AnalysisProvider

    for name in ("analyze_text", "analyze_audio", "aclose"):
        assert callable(getattr(FakeProvider(), name)) and hasattr(AnalysisProvider, name)
