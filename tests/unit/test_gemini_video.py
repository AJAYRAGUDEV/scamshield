"""Gemini video processing through the existing provider (fake SDK client; no network, no live Gemini)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from google.genai.types import FileState

from app.core.config import Settings
from app.core.errors import AnalysisError, ReasonCode as R
from app.providers.gemini.client import GeminiProvider
from app.providers.gemini.prompts import VIDEO_PROMPT, VIDEO_SYSTEM_PROMPT
from tests.conftest import TEST_API_KEY
from tests.fakes import FakeSDKClient, api_error, load_fixture_json


def run(coro):
    return asyncio.run(coro)


def provider(fake_sdk, **over) -> GeminiProvider:
    base = dict(gemini_api_key=TEST_API_KEY, gemini_model="test-model", gemini_timeout_seconds=5.0, gemini_max_retries=0,
                gemini_video_timeout_seconds=5.0, gemini_file_poll_interval_seconds=0.01, gemini_file_ready_timeout_seconds=1.0)
    base.update(over)
    return GeminiProvider(Settings(**base), client_factory=fake_sdk)


@pytest.fixture()
def video(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00" * 64)
    return path


def fixture_result(name="gemini_video_fake_bank.json"):
    return SimpleNamespace(parsed=None, text=json.dumps(load_fixture_json(name)))


def sdk(fake_sdk, result="gemini_video_fake_bank.json", upload_state=FileState.PROCESSING, states=(FileState.PROCESSING, FileState.ACTIVE)):
    fake_sdk.generate_result = fixture_result(result)
    fake_sdk.upload_state = upload_state
    fake_sdk.file_state_sequence = list(states)


# ------------------------------------------------------------------ success and state handling
def test_upload_wait_for_processing_analyse_then_delete(fake_sdk, video):
    sdk(fake_sdk)
    stages = []
    report = run(provider(fake_sdk).analyze_video(video, stages.append))
    client = fake_sdk.instances[-1]
    assert report.coverage == "full" and any(s.source == "video_text" for s in report.signals)
    assert client.order == ["upload", "get", "get", "generate", "delete"]              # analysis only after the file is ACTIVE
    assert client.deleted == ["files/fake-123"] and client.get_calls == ["files/fake-123"] * 2
    assert stages == ["uploading", "processing_media", "analysing"]


def test_an_already_active_file_is_not_polled(fake_sdk, video):
    sdk(fake_sdk, upload_state=FileState.ACTIVE)
    run(provider(fake_sdk).analyze_video(video))
    assert fake_sdk.instances[-1].order == ["upload", "generate", "delete"]


def test_request_uses_the_video_prompts_the_configured_model_and_structured_output(fake_sdk, video):
    sdk(fake_sdk, upload_state=FileState.ACTIVE)
    run(provider(fake_sdk).analyze_video(video))
    call = fake_sdk.instances[-1].generate_calls[0]
    assert call["model"] == "test-model" and call["contents"][1] == VIDEO_PROMPT and call["contents"][0].name == "files/fake-123"
    cfg = call["config"]
    assert cfg.system_instruction == VIDEO_SYSTEM_PROMPT and cfg.response_mime_type == "application/json"
    assert cfg.temperature == 0.1 and cfg.response_schema.__name__ == "EvidenceReport"


def test_the_upload_declares_the_validated_mime_type_and_a_video_sized_timeout(fake_sdk, tmp_path):
    for suffix, mime in ((".mp4", "video/mp4"), (".mov", "video/quicktime"), (".webm", "video/webm")):
        path = tmp_path / f"clip{suffix}"
        path.write_bytes(b"\x00" * 16)
        sdk(fake_sdk, upload_state=FileState.ACTIVE)
        run(provider(fake_sdk, gemini_video_timeout_seconds=42.0).analyze_video(path))
        config = fake_sdk.instances[-1].upload_configs[-1]
        assert config.mime_type == mime and config.http_options.timeout == 42_000


def test_a_failed_gemini_file_ends_the_analysis_and_is_still_cleaned_up(fake_sdk, video):
    sdk(fake_sdk, states=(FileState.PROCESSING, FileState.FAILED))
    with pytest.raises(AnalysisError) as info:
        run(provider(fake_sdk).analyze_video(video))
    client = fake_sdk.instances[-1]
    assert info.value.reason_code == R.MEDIA_PROCESSING_FAILED and "generate" not in client.order and client.deleted == ["files/fake-123"]


def test_a_file_that_never_becomes_ready_times_out_and_is_cleaned_up(fake_sdk, video):
    sdk(fake_sdk, states=(FileState.PROCESSING,))
    with pytest.raises(AnalysisError) as info:
        run(provider(fake_sdk, gemini_file_ready_timeout_seconds=0.1).analyze_video(video))
    client = fake_sdk.instances[-1]
    assert info.value.reason_code == R.PROVIDER_TIMEOUT and "generate" not in client.order and client.deleted == ["files/fake-123"]
    assert 1 <= len(client.get_calls) <= 15                                     # polling is bounded, not a tight loop


@pytest.mark.parametrize("state", [None, FileState.STATE_UNSPECIFIED])
def test_an_unknown_state_is_never_treated_as_ready(fake_sdk, video, state):
    sdk(fake_sdk, upload_state=state, states=(state,))
    with pytest.raises(AnalysisError) as info:
        run(provider(fake_sdk, gemini_file_ready_timeout_seconds=0.1).analyze_video(video))
    assert info.value.reason_code == R.PROVIDER_TIMEOUT and "generate" not in fake_sdk.instances[-1].order


def test_the_poll_interval_is_respected(fake_sdk, video):
    sdk(fake_sdk, states=(FileState.PROCESSING,))
    with pytest.raises(AnalysisError):
        run(provider(fake_sdk, gemini_file_poll_interval_seconds=0.05, gemini_file_ready_timeout_seconds=0.3).analyze_video(video))
    assert len(fake_sdk.instances[-1].get_calls) <= 8


# ------------------------------------------------------------------ failures
def test_overall_deadline_covers_slow_generation_and_still_deletes_the_remote_file(fake_sdk, video):
    sdk(fake_sdk, upload_state=FileState.ACTIVE)
    fake_sdk.generate_delay = 1.0
    with pytest.raises(AnalysisError) as info:
        run(provider(fake_sdk, gemini_video_timeout_seconds=0.1).analyze_video(video))
    assert info.value.reason_code == R.PROVIDER_TIMEOUT and fake_sdk.instances[-1].deleted == ["files/fake-123"]


@pytest.mark.parametrize("status, reason", [(503, R.PROVIDER_UNAVAILABLE), (401, R.PROVIDER_AUTH_ERROR), (429, R.PROVIDER_RATE_LIMITED),
                                             (404, R.CONFIGURATION_ERROR), (400, R.PROVIDER_ERROR)])
def test_provider_api_failures_are_translated_and_cleaned_up(fake_sdk, video, status, reason):
    sdk(fake_sdk, upload_state=FileState.ACTIVE)
    fake_sdk.generate_error = api_error(status)
    with pytest.raises(AnalysisError) as info:
        run(provider(fake_sdk).analyze_video(video))
    assert info.value.reason_code == reason and fake_sdk.instances[-1].deleted == ["files/fake-123"]


def test_a_transient_failure_is_retried_once_only_and_never_re_uploads(fake_sdk, video):
    sdk(fake_sdk, upload_state=FileState.ACTIVE)
    fake_sdk.generate_error_sequence = [api_error(503), None]
    run(provider(fake_sdk, gemini_max_retries=1).analyze_video(video))
    client = fake_sdk.instances[-1]
    assert len(client.generate_calls) == 2 and len(client.uploaded) == 1 and client.deleted == ["files/fake-123"]
    fake_sdk.reset()
    sdk(fake_sdk, upload_state=FileState.ACTIVE)
    fake_sdk.generate_error = api_error(503)
    with pytest.raises(AnalysisError):
        run(provider(fake_sdk, gemini_max_retries=1).analyze_video(video))
    assert len(fake_sdk.instances[-1].generate_calls) == 2                          # one retry, then it stops: no retry storm


def test_malformed_structured_output_is_rejected_and_cleaned_up(fake_sdk, video):
    fake_sdk.upload_state = FileState.ACTIVE
    for bad in ("not json", json.dumps({"coverage": "mostly", "signals": []}), json.dumps({"coverage": "full"}), ""):
        fake_sdk.reset()
        fake_sdk.upload_state = FileState.ACTIVE
        fake_sdk.generate_result = SimpleNamespace(parsed=None, text=bad)
        with pytest.raises(AnalysisError) as info:
            run(provider(fake_sdk).analyze_video(video))
        assert info.value.reason_code == R.INVALID_PROVIDER_RESPONSE and fake_sdk.instances[-1].deleted == ["files/fake-123"]


def test_authoritative_fields_in_the_output_are_ignored(fake_sdk, video):
    data = {**load_fixture_json("gemini_video_harmless.json"), "verdict": "low_risk", "risk_score": 0, "analysis_complete": True}
    fake_sdk.upload_state = FileState.ACTIVE
    fake_sdk.generate_result = SimpleNamespace(parsed=None, text=json.dumps(data))
    report = run(provider(fake_sdk).analyze_video(video))
    assert not hasattr(report, "risk_score") and not hasattr(report, "verdict")


def test_a_get_failure_while_waiting_is_translated_and_cleaned_up(fake_sdk, video):
    sdk(fake_sdk)
    fake_sdk.get_error = api_error(503)
    with pytest.raises(AnalysisError) as info:
        run(provider(fake_sdk).analyze_video(video))
    assert info.value.reason_code == R.PROVIDER_UNAVAILABLE and fake_sdk.instances[-1].deleted == ["files/fake-123"]


def test_upload_failure_means_nothing_to_delete(fake_sdk, video):
    fake_sdk.upload_error = api_error(503)
    with pytest.raises(AnalysisError) as info:
        run(provider(fake_sdk).analyze_video(video))
    assert info.value.reason_code == R.PROVIDER_UNAVAILABLE and fake_sdk.instances[-1].deleted == [] and "generate" not in fake_sdk.instances[-1].order


def test_a_failing_remote_delete_does_not_hide_a_good_result(fake_sdk, video):
    sdk(fake_sdk, upload_state=FileState.ACTIVE)
    fake_sdk.delete_error = api_error(500)
    assert run(provider(fake_sdk).analyze_video(video)).coverage == "full"


def test_cancellation_still_deletes_the_remote_file(fake_sdk, video):
    sdk(fake_sdk, upload_state=FileState.ACTIVE)
    fake_sdk.generate_delay = 5.0

    async def go():
        task = asyncio.create_task(provider(fake_sdk).analyze_video(video))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    assert fake_sdk.instances[-1].deleted == ["files/fake-123"]


def test_quota_error_starts_the_shared_cooldown_for_video_too(fake_sdk, video):
    sdk(fake_sdk, upload_state=FileState.ACTIVE)
    fake_sdk.generate_error = api_error(429)
    p = provider(fake_sdk, gemini_quota_cooldown_seconds=60.0)
    with pytest.raises(AnalysisError):
        run(p.analyze_video(video))
    calls_before = len(fake_sdk.instances[-1].generate_calls)
    with pytest.raises(AnalysisError) as info:
        run(p.analyze_video(video))
    assert info.value.reason_code == R.PROVIDER_RATE_LIMITED and len(fake_sdk.instances[-1].generate_calls) == calls_before


def test_without_a_key_nothing_is_uploaded(fake_sdk, video):
    p = GeminiProvider(Settings(gemini_api_key=""), client_factory=fake_sdk)
    with pytest.raises(AnalysisError) as info:
        run(p.analyze_video(video))
    assert info.value.reason_code == R.CONFIGURATION_ERROR and fake_sdk.instances == []


def test_the_key_is_only_taken_from_settings_and_one_client_is_shared(fake_sdk, video):
    sdk(fake_sdk, upload_state=FileState.ACTIVE)
    p = provider(fake_sdk)
    run(p.analyze_video(video))
    run(p.analyze_text("hello"))
    assert len(fake_sdk.instances) == 1 and fake_sdk.instances[0].api_key == TEST_API_KEY      # no second Gemini client


def test_text_and_audio_prompts_are_unchanged_by_the_video_rules():
    from app.providers.gemini.prompts import SYSTEM_PROMPT
    assert "VIDEO" not in SYSTEM_PROMPT and VIDEO_SYSTEM_PROMPT.startswith(SYSTEM_PROMPT.split("\n\nReturn the required")[0])
