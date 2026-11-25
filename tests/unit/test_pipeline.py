from __future__ import annotations

import asyncio
import logging

import pytest

from app.core.errors import AnalysisError, ReasonCode
from app.services import analysis_pipeline, risk_engine
from app.services.analysis_pipeline import AnalysisPipeline
from tests.fakes import FakeProvider, make_report, make_signal, provider_result


def run(coro):
    return asyncio.run(coro)


def test_success_goes_through_the_risk_engine():
    resp = run(AnalysisPipeline(FakeProvider(provider_result())).run_text("hello"))
    assert (resp.status, resp.verdict, resp.risk_score, resp.mode, resp.reason_code, resp.offline_fallback_used) == \
           ("completed", "high_risk", 96, "text", None, False)


def test_text_and_audio_use_the_same_engine_function(monkeypatch, tmp_path):
    calls = []
    real = risk_engine.assess
    monkeypatch.setattr(analysis_pipeline.risk_engine, "assess", lambda report: (calls.append(report), real(report))[1])
    clip = tmp_path / "c.wav"
    clip.write_bytes(b"abc")
    pipeline = AnalysisPipeline(FakeProvider())
    run(pipeline.run_text("hello"))
    run(pipeline.run_audio(clip, size=3))
    assert len(calls) == 2


def test_audio_mode_and_path_are_passed_through(tmp_path):
    clip = tmp_path / "c.wav"
    clip.write_bytes(b"abc")
    provider = FakeProvider()
    resp = run(AnalysisPipeline(provider).run_audio(clip, size=3))
    assert resp.mode == "audio" and resp.verdict == "high_risk" and provider.audio_paths == [clip] and provider.audio_bytes == [b"abc"]


@pytest.mark.parametrize("reason", list(ReasonCode))
def test_every_failure_becomes_unable_to_assess_with_a_null_score(reason):
    resp = run(AnalysisPipeline(FakeProvider(error=AnalysisError(reason, "detail"))).run_text("hello"))
    assert (resp.status, resp.verdict, resp.risk_score, resp.confidence, resp.reason_code) == \
           ("unable_to_assess", "unable_to_assess", None, None, reason)
    assert resp.signal_scores is None and resp.signals == []


def test_unexpected_exceptions_do_not_escape_and_do_not_leak_text():
    resp = run(AnalysisPipeline(FakeProvider(error=RuntimeError("secret internals AIzaXXXX"))).run_text("hello"))
    assert resp.reason_code == ReasonCode.INTERNAL_ANALYSIS_ERROR and "secret" not in resp.model_dump_json()


@pytest.mark.parametrize("name", ["gemini_partial_unreadable.json", "gemini_insufficient.json"])
def test_unreadable_content_is_insufficient_evidence_not_low_risk(name):
    resp = run(AnalysisPipeline(FakeProvider(provider_result(name))).run_text("hello"))
    assert (resp.verdict, resp.risk_score, resp.reason_code) == ("unable_to_assess", None, ReasonCode.INSUFFICIENT_EVIDENCE)


def test_clean_content_is_low_risk_only_with_full_coverage():
    resp = run(AnalysisPipeline(FakeProvider(provider_result("gemini_clean.json"))).run_text("hello"))
    assert (resp.verdict, resp.risk_score, resp.confidence, resp.coverage) == ("low_risk", 0, None, "full")


@pytest.mark.parametrize("report,mode", [
    (make_report([make_signal(source="audio_speech")]), "text"),
    (make_report([make_signal(source="text_content")]), "audio"),
    (make_report([make_signal("fake_interface", source="text_content")]), "text"),
    (make_report([make_signal(source="text_content", timestamp_start=1.0)]), "text"),
])
def test_evidence_that_is_impossible_for_the_mode_is_an_invalid_provider_response(report, mode, tmp_path):
    clip = tmp_path / "c.wav"
    clip.write_bytes(b"abc")
    pipeline = AnalysisPipeline(FakeProvider(result=report, audio_result=report))
    resp = run(pipeline.run_text("x") if mode == "text" else pipeline.run_audio(clip))
    assert resp.verdict == "unable_to_assess" and resp.reason_code == ReasonCode.INVALID_PROVIDER_RESPONSE


def test_cancellation_is_not_swallowed():
    async def go():
        task = asyncio.create_task(AnalysisPipeline(FakeProvider(delay=5)).run_text("x"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())


def test_concurrency_is_bounded():
    provider = FakeProvider(delay=0.05)
    pipeline = AnalysisPipeline(provider, max_concurrency=2)

    async def go():
        await asyncio.gather(*(pipeline.run_text(f"m{i}") for i in range(8)))

    run(go())
    assert provider.max_in_flight == 2 and len(provider.text_calls) == 8


def test_slot_is_released_after_failures():
    provider = FakeProvider(error=AnalysisError(ReasonCode.PROVIDER_TIMEOUT))
    pipeline = AnalysisPipeline(provider, max_concurrency=1)

    async def go():
        for _ in range(3):
            await pipeline.run_text("x")

    run(go())
    assert len(provider.text_calls) == 3


def test_logs_outcome_metadata_but_never_user_content(caplog):
    caplog.set_level(logging.DEBUG, logger="scamshield")
    secret = "SECRET-USER-MESSAGE-424242"
    run(AnalysisPipeline(FakeProvider()).run_text(secret))
    run(AnalysisPipeline(FakeProvider(error=AnalysisError(ReasonCode.PROVIDER_RATE_LIMITED, "APIError http=429"))).run_text(secret))
    run(AnalysisPipeline(FakeProvider(provider_result("gemini_insufficient.json"))).run_text(secret))
    text = caplog.text
    assert "analysis_completed mode=text verdict=high_risk risk_score=96" in text
    assert "analysis_failed mode=text reason_code=PROVIDER_RATE_LIMITED" in text
    assert "analysis_failed mode=text reason_code=INSUFFICIENT_EVIDENCE" in text
    assert secret not in text and "Inspector Sharma" not in text       # evidence text is not logged either
