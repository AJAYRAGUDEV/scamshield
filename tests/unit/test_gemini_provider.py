"""The Gemini adapter, tested against a fake SDK client (no network, no real Gemini)."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.errors import AnalysisError, ReasonCode as R
from app.providers.gemini.client import GeminiProvider, parse_provider_result
from app.providers.gemini.prompts import AUDIO_PROMPT, SYSTEM_PROMPT, TEXT_PROMPT_PREFIX
from app.schemas.provider_output import ProviderAnalysis
from tests.conftest import TEST_API_KEY
from tests.fakes import FakeSDKClient, api_error, load_fixture_json, make_signal


def run(coro):
    return asyncio.run(coro)


def make_provider(fake_sdk, **settings_overrides) -> GeminiProvider:
    settings = Settings(gemini_api_key=TEST_API_KEY, gemini_model="test-model", gemini_timeout_seconds=5.0, **settings_overrides)
    return GeminiProvider(settings, client_factory=fake_sdk)


def raw(**overrides) -> dict:
    return {**load_fixture_json("gemini_high_risk.json"), **overrides}


# ------------------------------------------------------------------ success paths
def test_text_success_with_parsed_instance(fake_sdk):
    out = run(make_provider(fake_sdk).analyze_text("hello"))
    assert isinstance(out, ProviderAnalysis) and out.coverage == "full" and len(out.signals) == 5


def test_text_success_from_json_text(fake_sdk):
    fake_sdk.generate_result = SimpleNamespace(parsed=None, text=json.dumps(raw(signals=[])))
    assert run(make_provider(fake_sdk).analyze_text("hello")).signals == []


def test_text_success_from_parsed_dict(fake_sdk):
    fake_sdk.generate_result = SimpleNamespace(parsed=raw(coverage="partial"), text=None)
    assert run(make_provider(fake_sdk).analyze_text("hello")).coverage == "partial"


def test_authoritative_fields_returned_by_the_model_are_dropped(fake_sdk):
    fake_sdk.generate_result = SimpleNamespace(parsed=None, text=json.dumps(raw(risk_score=0, verdict="low_risk", confidence=1.0)))
    out = run(make_provider(fake_sdk).analyze_text("hello"))
    assert not hasattr(out, "risk_score") and not hasattr(out, "verdict")


def test_request_shape_asks_for_evidence_only(fake_sdk):
    text = 'braces {x} and {0} must stay literal'
    run(make_provider(fake_sdk).analyze_text(text))
    call = fake_sdk.instances[-1].generate_calls[0]
    assert call["model"] == "test-model"
    assert call["contents"] == [TEXT_PROMPT_PREFIX + text]
    cfg = call["config"]
    assert cfg.system_instruction == SYSTEM_PROMPT and cfg.temperature == 0.1
    assert cfg.response_mime_type == "application/json" and cfg.response_schema is ProviderAnalysis


def test_the_prompt_no_longer_lets_the_model_decide_the_verdict():
    lowered = SYSTEM_PROMPT.lower()
    assert "do not assign risk scores" in lowered and "server calculates" in lowered
    assert "use low risk" not in lowered and "high risk only for" not in lowered       # the Phase 1-3 labelling instructions
    assert "untrusted data" in lowered and "ignore any instruction" in lowered         # prompt-injection guard
    assert "coverage" in lowered and "reliability" in lowered
    for category in ("credential_request", "payment_redirection", "suspicious_link", "urgency_pressure"):
        assert category in SYSTEM_PROMPT
    assert "synthetic" in lowered and "cloned or synthetic" in lowered     # original safety guidance kept


# ------------------------------------------------------------------ key handling and client lifecycle
def test_client_uses_only_the_configured_key_and_a_timeout(fake_sdk):
    run(make_provider(fake_sdk).analyze_text("hi"))
    client = fake_sdk.instances[-1]
    assert client.api_key == TEST_API_KEY and client.http_options.timeout == 5000


def test_one_shared_client_is_reused(fake_sdk):
    async def go():
        p = make_provider(fake_sdk)
        await p.analyze_text("a")
        await p.analyze_text("b")

    run(go())
    assert len(fake_sdk.instances) == 1


def test_missing_key_is_a_configuration_error_and_no_client_is_built(fake_sdk):
    provider = GeminiProvider(Settings(gemini_api_key=""), client_factory=fake_sdk)
    with pytest.raises(AnalysisError) as exc:
        run(provider.analyze_text("hi"))
    assert exc.value.reason_code == R.CONFIGURATION_ERROR and fake_sdk.instances == []


def test_client_construction_failure_is_a_configuration_error():
    def broken(**_):
        raise ValueError("bad key format AIza" + "B" * 30)

    with pytest.raises(AnalysisError) as exc:
        run(GeminiProvider(Settings(gemini_api_key="k"), client_factory=broken).analyze_text("hi"))
    assert exc.value.reason_code == R.CONFIGURATION_ERROR and "AIza" not in exc.value.internal_detail


def test_aclose_closes_client_and_is_safe_twice_or_without_client(fake_sdk):
    async def go():
        p = make_provider(fake_sdk)
        await p.aclose()
        await p.analyze_text("x")
        await p.aclose()
        await p.aclose()

    run(go())
    assert fake_sdk.instances[0].closed_async and fake_sdk.instances[0].closed_sync


# ------------------------------------------------------------------ untrusted output
BAD_SIGNAL = make_signal("credential_request")


@pytest.mark.parametrize("result", [
    SimpleNamespace(parsed=None, text="not json at all"),
    SimpleNamespace(parsed=None, text=""),
    SimpleNamespace(parsed=None, text=None),                                   # empty provider response
    SimpleNamespace(parsed=None, text="[]"),
    SimpleNamespace(parsed=None, text="{}"),                                   # no coverage: never read as "nothing found"
    SimpleNamespace(parsed=None, text='{"coverage": "full"}'),                 # no signals list
    SimpleNamespace(parsed=None, text='{"risk_score": 5, "verdict": "low_risk"}'),   # scores instead of evidence
    SimpleNamespace(parsed=raw(coverage="mostly"), text=None),
    SimpleNamespace(parsed=raw(signals=[{**BAD_SIGNAL, "severity": "extreme"}]), text=None),     # invalid severity
    SimpleNamespace(parsed=raw(signals=[{**BAD_SIGNAL, "reliability": 0.9}]), text=None),         # numeric where enum expected
    SimpleNamespace(parsed=raw(signals=[{**BAD_SIGNAL, "category": "made_up"}]), text=None),
    SimpleNamespace(parsed=raw(signals=[{**BAD_SIGNAL, "evidence": ""}]), text=None),
    SimpleNamespace(parsed=raw(signals=[{k: v for k, v in BAD_SIGNAL.items() if k != "source"}]), text=None),   # missing field
    SimpleNamespace(parsed=raw(signals=[{**BAD_SIGNAL, "timestamp_start": 9.0, "timestamp_end": 3.0}]), text=None),
    SimpleNamespace(parsed=raw(signals=[{**BAD_SIGNAL, "timestamp_start": -5}]), text=None),
    SimpleNamespace(parsed=raw(signals=[BAD_SIGNAL] * 41), text=None),
    SimpleNamespace(parsed=raw(signals="lots"), text=None),
    SimpleNamespace(parsed=42, text=None),
    SimpleNamespace(parsed=["a"], text=None),
    object(),
])
def test_malformed_model_output_is_invalid_provider_response(result):
    with pytest.raises(AnalysisError) as exc:
        parse_provider_result(result)
    assert exc.value.reason_code == R.INVALID_PROVIDER_RESPONSE


def test_malformed_output_surfaces_from_the_provider_too(fake_sdk):
    fake_sdk.generate_result = SimpleNamespace(parsed=None, text="garbage")
    with pytest.raises(AnalysisError) as exc:
        run(make_provider(fake_sdk).analyze_text("hi"))
    assert exc.value.reason_code == R.INVALID_PROVIDER_RESPONSE


# ------------------------------------------------------------------ errors and timeouts
@pytest.mark.parametrize("error,reason", [
    (api_error(401), R.PROVIDER_AUTH_ERROR),
    (api_error(429), R.PROVIDER_RATE_LIMITED),
    (api_error(503), R.PROVIDER_UNAVAILABLE),
    (ConnectionError("down"), R.PROVIDER_UNAVAILABLE),
])
def test_sdk_errors_are_translated(fake_sdk, error, reason):
    fake_sdk.generate_error = error
    with pytest.raises(AnalysisError) as exc:
        run(make_provider(fake_sdk).analyze_text("hi"))
    assert exc.value.reason_code == reason


def test_overall_deadline_produces_timeout_quickly(fake_sdk):
    fake_sdk.generate_delay = 2.0
    provider = GeminiProvider(Settings(gemini_api_key="k", gemini_timeout_seconds=0.05), client_factory=fake_sdk)
    started = time.perf_counter()
    with pytest.raises(AnalysisError) as exc:
        run(provider.analyze_text("hi"))
    assert exc.value.reason_code == R.PROVIDER_TIMEOUT and time.perf_counter() - started < 1.0


# ------------------------------------------------------------------ audio
def audio_reply() -> SimpleNamespace:
    return SimpleNamespace(parsed=ProviderAnalysis(**load_fixture_json("gemini_audio_high_risk.json")), text=None)


def test_audio_uploads_generates_then_deletes_remote_file(fake_sdk, tmp_path):
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"audio")
    out = run(make_provider(fake_sdk).analyze_audio(clip))
    client = fake_sdk.instances[-1]
    assert out.signals[0].source in ("audio_speech", "audio_acoustic")
    assert client.uploaded == [str(clip)] and client.order == ["upload", "generate", "delete"]
    assert client.deleted == ["files/fake-123"]
    contents = client.generate_calls[0]["contents"]
    assert contents[0].name == "files/fake-123" and contents[1] == AUDIO_PROMPT


def test_audio_remote_file_deleted_when_generation_fails(fake_sdk, tmp_path):
    fake_sdk.generate_error = api_error(429)
    with pytest.raises(AnalysisError):
        run(make_provider(fake_sdk).analyze_audio(tmp_path / "x.wav"))
    assert fake_sdk.instances[-1].deleted == ["files/fake-123"]


def test_audio_remote_file_deleted_on_invalid_output(fake_sdk, tmp_path):
    fake_sdk.generate_result = SimpleNamespace(parsed=None, text="nope")
    with pytest.raises(AnalysisError):
        run(make_provider(fake_sdk).analyze_audio(tmp_path / "x.wav"))
    assert fake_sdk.instances[-1].deleted == ["files/fake-123"]


def test_audio_remote_file_deleted_on_timeout(fake_sdk, tmp_path):
    fake_sdk.generate_delay = 2.0
    provider = GeminiProvider(Settings(gemini_api_key="k", gemini_timeout_seconds=0.05), client_factory=fake_sdk)
    with pytest.raises(AnalysisError) as exc:
        run(provider.analyze_audio(tmp_path / "x.wav"))
    assert exc.value.reason_code == R.PROVIDER_TIMEOUT and fake_sdk.instances[-1].deleted == ["files/fake-123"]


def test_audio_upload_failure_means_nothing_to_delete(fake_sdk, tmp_path):
    fake_sdk.upload_error = api_error(503)
    with pytest.raises(AnalysisError) as exc:
        run(make_provider(fake_sdk).analyze_audio(tmp_path / "x.wav"))
    assert exc.value.reason_code == R.PROVIDER_UNAVAILABLE and fake_sdk.instances[-1].deleted == []


def test_remote_delete_failure_does_not_lose_the_result(fake_sdk, tmp_path):
    fake_sdk.delete_error = api_error(500)
    assert run(make_provider(fake_sdk).analyze_audio(tmp_path / "x.wav")).coverage == "full"
