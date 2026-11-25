"""Helpers for Phase 7 scenario tests: the REAL Gemini adapter and risk engine, with a fake SDK client that returns
a controlled fixture (what the model is expected to say). No network, no live Gemini."""
from __future__ import annotations

import io
import json
import struct
import wave
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.providers.gemini import GeminiProvider
from tests import media_samples as ms
from tests.fakes import FakeSDKClient, load_fixture_json

TEXT_PATH = "/api/v1/analyze/text"
AUDIO_PATH = "/api/v1/analyze/audio"


def scenario_client(fake_sdk, upload_dir, fixture: str | dict, **settings_overrides) -> TestClient:
    """An app whose Gemini adapter is backed by the fake SDK returning ``fixture`` (a file name or a raw dict)."""
    data = load_fixture_json(fixture) if isinstance(fixture, str) else fixture
    fake_sdk.generate_result = SimpleNamespace(parsed=None, text=json.dumps(data, ensure_ascii=False))
    settings = Settings(gemini_api_key="test-key-not-real", app_env="test", gemini_max_retries=0, temp_upload_dir=str(upload_dir),
                        rate_limit_text="100000/minute", rate_limit_audio="100000/minute",
                        rate_limit_default="100000/minute", rate_limit_global="100000/minute", **settings_overrides)
    return TestClient(create_app(settings, GeminiProvider(settings, client_factory=fake_sdk)), raise_server_exceptions=False)


def analyze_text(client: TestClient, text: str = "message to analyse") -> dict:
    response = client.post(TEXT_PATH, data={"text": text})
    assert response.status_code == 200, response.text
    return response.json()


def analyze_audio(client: TestClient, content: bytes | None = None, name: str = "clip.wav", mime: str = "audio/wav"):
    return client.post(AUDIO_PATH, files={"file": (name, content if content is not None else ms.wav(), mime)})


def make_wav(seconds: float = 2.0, *, silent: bool = False, rate: int = 8000) -> bytes:
    """A small 16-bit mono WAV: a quiet tone, or digital silence."""
    frames = int(seconds * rate)
    samples = [0] * frames if silent else [int(8000 * ((i // 20) % 2 * 2 - 1)) for i in range(frames)]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(struct.pack(f"<{frames}h", *samples))
    return buffer.getvalue()


def provider_calls(fake_sdk) -> int:
    return sum(len(client.generate_calls) for client in fake_sdk.instances)
