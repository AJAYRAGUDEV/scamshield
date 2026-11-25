"""Test doubles. No test ever talks to Gemini.

* ``FakeProvider``   - implements the app's ``AnalysisProvider`` interface. Used for service/API tests.
* ``FakeSDKClient``  - stands in for ``google.genai.Client`` (the async surface ``client.aio``). Used to
                       test the Gemini adapter itself (parsing, error translation, cleanup, timeouts).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

from google.genai import errors as genai_errors
from google.genai.types import FileState

from app.schemas.evidence import EvidenceReport
from app.schemas.provider_output import ProviderAnalysis

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def load_fixture_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def provider_result(name: str = "gemini_high_risk.json") -> ProviderAnalysis:
    """A validated evidence report loaded from a fixture (what Gemini would return)."""
    return ProviderAnalysis(**load_fixture_json(name))


def make_signal(category: str = "credential_request", severity: str = "high", reliability: str = "strong", **overrides) -> dict:
    """A raw evidence signal dict with sensible defaults; override any field."""
    data = dict(category=category, name=f"{category}_signal", evidence=f"Evidence for {category}",
                source="text_content", severity=severity, reliability=reliability)
    data.update(overrides)
    return data


def make_report(signals: list[dict] | None = None, coverage: str = "full") -> EvidenceReport:
    return EvidenceReport(coverage=coverage, signals=signals or [])


def api_error(code: int, message: str = "boom", status: str = "ERR") -> genai_errors.APIError:
    """A real google-genai APIError, built without any network access."""
    return genai_errors.APIError(code, {"error": {"code": code, "message": message, "status": status}})


# --------------------------------------------------------------------------- FakeProvider
class FakeProvider:
    def __init__(self, result: ProviderAnalysis | None = None, error: BaseException | None = None, delay: float = 0.0,
                 blocking: bool = False, audio_result: ProviderAnalysis | None = None,
                 video_result: ProviderAnalysis | None = None):
        self.result = result if result is not None else provider_result()
        self.audio_result = audio_result if audio_result is not None else provider_result("gemini_audio_high_risk.json")
        self.video_result = video_result if video_result is not None else provider_result("gemini_video_fake_bank.json")
        self.error = error
        self.delay = delay
        self.blocking = blocking          # time.sleep (BLOCKS the loop) instead of asyncio.sleep
        self.text_calls: list[str] = []
        self.url_facts: list[str | None] = []
        self.audio_paths: list[Path] = []
        self.video_paths: list[Path] = []
        self.video_existed: list[bool] = []
        self.audio_bytes: list[bytes] = []
        self.audio_existed: list[bool] = []
        self.request_ids: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.closed = False

    async def _work(self, result: ProviderAnalysis) -> ProviderAnalysis:
        from app.core.logging import request_id_var

        self.request_ids.append(request_id_var.get())
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                if self.blocking:
                    time.sleep(self.delay)
                else:
                    await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return result
        finally:
            self.in_flight -= 1

    async def analyze_text(self, text: str, url_facts: str | None = None) -> ProviderAnalysis:
        self.text_calls.append(text)
        self.url_facts.append(url_facts)
        return await self._work(self.result)

    async def analyze_audio(self, path: Path) -> ProviderAnalysis:
        self.audio_paths.append(path)
        self.audio_existed.append(path.exists())
        self.audio_bytes.append(path.read_bytes())
        return await self._work(self.audio_result)

    async def analyze_video(self, path: Path, on_stage=None) -> ProviderAnalysis:
        self.video_paths.append(path)
        self.video_existed.append(path.exists())
        if on_stage:
            for stage in ("uploading", "processing_media", "analysing"):
                on_stage(stage)
        return await self._work(self.video_result)

    async def aclose(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- FakeSDKClient
class FakeSDKClient:
    """Mimics the async surface of ``google.genai.Client``. Behaviour is configured through class
    attributes that the ``fake_sdk`` fixture resets before every test. The sync surface
    (``client.models`` / ``client.files``) deliberately does not exist, so any sync SDK call in
    the adapter would fail the test."""

    instances: list["FakeSDKClient"] = []
    generate_result = None
    generate_error: BaseException | None = None
    generate_error_sequence: list = []     # consumed one per call; None = succeed that call
    generate_delay = 0.0
    upload_error: BaseException | None = None
    delete_error: BaseException | None = None
    upload_state = None                    # FileState returned by upload() (None = no state, as for audio)
    file_state_sequence: list = []         # states returned by successive files.get() calls (the last one repeats)
    get_error: BaseException | None = None

    def __init__(self, api_key=None, http_options=None, **_):
        self.api_key = api_key
        self.http_options = http_options
        self.generate_calls: list[dict] = []
        self.uploaded: list[str] = []
        self.upload_configs: list = []
        self.get_calls: list[str] = []
        self.deleted: list[str] = []
        self.order: list[str] = []
        self.closed_async = False
        self.closed_sync = False
        self.aio = SimpleNamespace(
            models=SimpleNamespace(generate_content=self._generate),
            files=SimpleNamespace(upload=self._upload, delete=self._delete, get=self._get),
            aclose=self._aclose,
        )
        FakeSDKClient.instances.append(self)

    async def _generate(self, **kwargs):
        self.order.append("generate")
        self.generate_calls.append(kwargs)
        if FakeSDKClient.generate_delay:
            await asyncio.sleep(FakeSDKClient.generate_delay)
        if FakeSDKClient.generate_error_sequence:
            scripted = FakeSDKClient.generate_error_sequence.pop(0)
            if scripted is not None:
                raise scripted
        elif FakeSDKClient.generate_error:
            raise FakeSDKClient.generate_error
        if FakeSDKClient.generate_result is not None:
            return FakeSDKClient.generate_result
        is_text = isinstance(kwargs["contents"][0], str)       # audio calls start with the uploaded file
        return SimpleNamespace(parsed=provider_result("gemini_high_risk.json" if is_text else "gemini_audio_high_risk.json"), text=None)

    async def _upload(self, *, file, config=None):
        self.order.append("upload")
        self.uploaded.append(file)
        self.upload_configs.append(config)
        if FakeSDKClient.upload_error:
            raise FakeSDKClient.upload_error
        return SimpleNamespace(name="files/fake-123", state=FakeSDKClient.upload_state)

    async def _get(self, *, name, config=None):
        self.order.append("get")
        self.get_calls.append(name)
        if FakeSDKClient.get_error:
            raise FakeSDKClient.get_error
        seq = FakeSDKClient.file_state_sequence
        state = (seq.pop(0) if len(seq) > 1 else seq[0]) if seq else FileState.ACTIVE
        return SimpleNamespace(name=name, state=state)

    async def _delete(self, *, name, config=None):
        self.order.append("delete")
        self.deleted.append(name)
        if FakeSDKClient.delete_error:
            raise FakeSDKClient.delete_error

    async def _aclose(self):
        self.closed_async = True

    def close(self):
        self.closed_sync = True

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.generate_result = None
        cls.generate_error = None
        cls.generate_error_sequence = []
        cls.generate_delay = 0.0
        cls.upload_error = None
        cls.delete_error = None
        cls.upload_state = None
        cls.file_state_sequence = []
        cls.get_error = None
