"""Video analysis end to end: REAL Gemini adapter (fake SDK) + REAL risk engine + job manager. The model's answer is a
controlled fixture. These prove how the SERVER treats each possible answer; they cannot show what real Gemini says."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from google.genai.types import FileState

from app.services import risk_engine
from app.schemas.verdict import verdict_for_score
from tests.fakes import api_error, load_fixture_json
from tests.video_helpers import gemini_video_app, leftovers, post_video, wait_for_job


def analyse(fake_sdk, upload_dir, fixture, *, runner=None, states=(FileState.PROCESSING, FileState.ACTIVE), upload_state=FileState.PROCESSING, **settings):
    app = gemini_video_app(fake_sdk, upload_dir, fixture, runner=runner, **settings)
    fake_sdk.upload_state = upload_state
    fake_sdk.file_state_sequence = list(states)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = post_video(client)
        assert response.status_code == 202, response.text
        return wait_for_job(client, response.json()["job_id"])


def result(body):
    assert body["result"] is not None, body
    return body["result"]


def categories(res):
    return {s["category"] for s in res["signals"]}


# ------------------------------------------------------------------ detection areas
def test_fake_bank_video_combines_visual_on_screen_text_and_spoken_evidence(fake_sdk, upload_dir):
    body = analyse(fake_sdk, upload_dir, "gemini_video_fake_bank.json")
    res = result(body)
    assert body["status"] == "completed" and (res["verdict"], res["risk_score"], res["mode"]) == ("high_risk", 94, "video")
    assert {s["source"] for s in res["signals"]} == {"video_visual", "video_text", "video_audio"}
    assert {"fake_interface", "credential_request", "authority_impersonation", "urgency_pressure"} <= categories(res)
    otp = next(s for s in res["signals"] if s["category"] == "credential_request")
    assert otp["quote"].startswith("Enter the OTP") and (otp["timestamp_start"], otp["timestamp_end"]) == (10.0, 14.0)


def test_qr_payment_request_and_prize_fee(fake_sdk, upload_dir):
    res = result(analyse(fake_sdk, upload_dir, "gemini_video_qr_payment.json"))
    assert res["verdict"] == "high_risk" and {"payment_request", "advance_fee_or_prize"} <= categories(res)


def test_a_harmless_video_is_low_risk_with_explicit_limitations(fake_sdk, upload_dir):
    res = result(analyse(fake_sdk, upload_dir, "gemini_video_harmless.json"))
    assert (res["verdict"], res["risk_score"], res["signals"]) == ("low_risk", 0, [])
    assert any("does not identify people" in n for n in res["limitations"])


def test_a_security_awareness_video_is_not_flagged_as_a_scam(fake_sdk, upload_dir):
    res = result(analyse(fake_sdk, upload_dir, "gemini_video_security_awareness.json"))
    assert res["verdict"] == "low_risk" and {s["context"] for s in res["signals"]} == {"discussed_or_described"} and res["confidence"] is None


def test_manipulation_indicators_alone_are_not_fraud_and_are_labelled_as_not_proof(fake_sdk, upload_dir):
    res = result(analyse(fake_sdk, upload_dir, "gemini_video_manipulation_only.json"))
    assert res["verdict"] != "high_risk" and res["risk_score"] < 60
    assert categories(res) == {"visual_manipulation"}
    assert any("not proof" in n for n in res["limitations"])
    text = " ".join([res["summary"], res["explanation"], res["recommended_action"]]).lower()
    for claim in ("deepfake", "is fake", "forged", "authentic", "genuine video"):
        assert claim not in text


def test_even_unlimited_critical_manipulation_evidence_can_never_reach_high_risk_alone():
    from tests.fakes import make_report, make_signal
    signals = [make_signal("visual_manipulation", "critical", "strong", source="video_visual", name=f"edit_{i}_signal",
                           evidence=f"Distinct editing artefact number {i}", timestamp_start=float(i * 10), timestamp_end=float(i * 10 + 1))
               for i in range(30)]
    assessment = risk_engine.assess(make_report(signals))
    assert assessment.risk_score < 60 and assessment.verdict.value != "high_risk"


# ------------------------------------------------------------------ incomplete analysis is never Low Risk
@pytest.mark.parametrize("fixture", ["gemini_video_unclear.json", "gemini_video_no_content.json"])
def test_unclear_or_empty_footage_is_unable_to_assess_with_a_null_score(fake_sdk, upload_dir, fixture):
    body = analyse(fake_sdk, upload_dir, fixture)
    res = result(body)
    assert body["status"] == "unable_to_assess" and (res["verdict"], res["risk_score"], res["signals"]) == ("unable_to_assess", None, [])
    assert res["reason_code"] == "INSUFFICIENT_EVIDENCE" and res["verdict"] != "low_risk"


def test_evidence_for_the_wrong_mode_is_rejected_not_scored(fake_sdk, upload_dir):
    body = analyse(fake_sdk, upload_dir, "gemini_video_wrong_sources.json")      # audio_speech source inside a video analysis
    assert body["status"] == "unable_to_assess" and result(body)["reason_code"] == "INVALID_PROVIDER_RESPONSE" and result(body)["risk_score"] is None


@pytest.mark.parametrize("bad", [{"coverage": "full", "signals": [{"category": "made_up", "name": "x_y", "evidence": "e", "source": "video_text", "severity": "high", "reliability": "strong"}]},
                                  {"coverage": "full", "signals": [{"category": "credential_request", "name": "otp", "evidence": "e", "source": "video_text", "severity": "high", "reliability": 0.92}]},
                                  {"coverage": "full", "signals": [{"category": "credential_request", "name": "otp", "evidence": "e", "source": "video_text", "severity": "high", "reliability": "strong", "timestamp_start": "12"}]},
                                  {"signals": []}, {"coverage": "full", "signals": "none"}])
def test_malformed_structured_output_is_unable_to_assess(fake_sdk, upload_dir, bad):
    body = analyse(fake_sdk, upload_dir, bad)
    assert body["status"] == "unable_to_assess" and result(body)["reason_code"] == "INVALID_PROVIDER_RESPONSE" and result(body)["risk_score"] is None
    assert fake_sdk.instances[-1].deleted == ["files/fake-123"] and leftovers(upload_dir) == []


def test_unparseable_model_text_is_unable_to_assess(fake_sdk, upload_dir):
    app = gemini_video_app(fake_sdk, upload_dir, {})
    fake_sdk.generate_result = SimpleNamespace(parsed=None, text="I think this is a scam!")
    fake_sdk.upload_state = FileState.ACTIVE
    with TestClient(app) as client:
        body = wait_for_job(client, post_video(client).json()["job_id"])
    assert body["status"] == "unable_to_assess" and result(body)["verdict"] != "low_risk"


# ------------------------------------------------------------------ the model cannot decide
def test_gemini_cannot_set_the_verdict_score_status_or_thresholds(fake_sdk, upload_dir):
    data = {**load_fixture_json("gemini_video_manipulation_only.json"), "verdict": "high_risk", "risk_score": 100, "status": "completed",
            "analysis_complete": True, "confidence": 1.0, "thresholds": {"high": 0}, "recommended_action": "pay now"}
    res = result(analyse(fake_sdk, upload_dir, data))
    assert res["verdict"] != "high_risk" and res["risk_score"] < 60 and "pay now" not in json.dumps(res)


def test_a_model_claiming_safety_cannot_hide_evidence(fake_sdk, upload_dir):
    data = {**load_fixture_json("gemini_video_fake_bank.json"), "verdict": "low_risk", "risk_score": 0}
    assert result(analyse(fake_sdk, upload_dir, data))["verdict"] == "high_risk"


def test_the_approved_thresholds_are_unchanged_for_video():
    assert [verdict_for_score(s).value for s in (0, 24, 25, 59, 60, 100)] == ["low_risk", "low_risk", "suspicious", "suspicious", "high_risk", "high_risk"]


def test_video_evidence_goes_through_the_one_existing_risk_engine(fake_sdk, upload_dir, monkeypatch):
    calls = []
    real = risk_engine.assess
    monkeypatch.setattr(risk_engine, "assess", lambda report: calls.append(report) or real(report))
    res = result(analyse(fake_sdk, upload_dir, "gemini_video_fake_bank.json"))
    assert len(calls) == 1 and len(calls[0].signals) == 4
    assert res["risk_score"] == real(calls[0]).risk_score                              # the served score is the engine's, unmodified


def test_duplicate_evidence_does_not_inflate_the_score(fake_sdk, upload_dir):
    duplicated = result(analyse(fake_sdk, upload_dir, "gemini_video_duplicates.json"))
    single_report = {"coverage": "full", "language": "English", "language_uncertain": False, "limitations": [],
                     "signals": [load_fixture_json("gemini_video_duplicates.json")["signals"][0]]}
    single = result(analyse(fake_sdk, upload_dir, single_report))
    assert duplicated["risk_score"] == single["risk_score"] == 70
    assert len(duplicated["signals"]) == 1 and duplicated["signals"][0]["corroborating_signals"] == 2


# ------------------------------------------------------------------ timestamps
def test_timestamps_beyond_the_videos_real_length_are_removed_and_disclosed(fake_sdk, upload_dir):
    res = result(analyse(fake_sdk, upload_dir, "gemini_video_timestamps_outside.json"))          # fake ffprobe says the video is 30.5 s
    by = {s["category"]: s for s in res["signals"]}
    assert by["credential_request"]["timestamp_start"] is None and by["credential_request"]["timestamp_end"] is None
    assert by["urgency_pressure"]["timestamp_start"] == 1.0
    assert any("timestamps were removed" in n and "video" in n for n in res["limitations"])


def test_timestamps_are_never_invented(fake_sdk, upload_dir):
    res = result(analyse(fake_sdk, upload_dir, "gemini_video_no_timestamps.json"))
    assert all(s["timestamp_start"] is None and s["timestamp_end"] is None for s in res["signals"])
    assert not any("imestamps are approximate" in n for n in res["limitations"])


def test_kept_timestamps_are_labelled_approximate(fake_sdk, upload_dir):
    res = result(analyse(fake_sdk, upload_dir, "gemini_video_fake_bank.json"))
    assert any("approximate" in n and "video's length" in n for n in res["limitations"])


# ------------------------------------------------------------------ Gemini failures become unable_to_assess and are cleaned up
def test_gemini_file_processing_failure(fake_sdk, upload_dir):
    body = analyse(fake_sdk, upload_dir, "gemini_video_fake_bank.json", states=(FileState.PROCESSING, FileState.FAILED))
    res = result(body)
    assert body["status"] == "unable_to_assess" and (res["reason_code"], res["risk_score"]) == ("MEDIA_PROCESSING_FAILED", None)
    assert fake_sdk.instances[-1].deleted == ["files/fake-123"] and leftovers(upload_dir) == []


def test_gemini_never_ready(fake_sdk, upload_dir):
    body = analyse(fake_sdk, upload_dir, "gemini_video_fake_bank.json", states=(FileState.PROCESSING,), gemini_file_ready_timeout_seconds=0.15)
    assert result(body)["reason_code"] == "PROVIDER_TIMEOUT" and fake_sdk.instances[-1].deleted == ["files/fake-123"]


def test_gemini_overall_timeout(fake_sdk, upload_dir):
    fake_sdk.generate_delay = 1.0
    body = analyse(fake_sdk, upload_dir, "gemini_video_fake_bank.json", gemini_video_timeout_seconds=0.1)
    assert result(body)["reason_code"] == "PROVIDER_TIMEOUT" and result(body)["risk_score"] is None
    assert fake_sdk.instances[-1].deleted == ["files/fake-123"] and leftovers(upload_dir) == []


@pytest.mark.parametrize("status, reason", [(503, "PROVIDER_UNAVAILABLE"), (429, "PROVIDER_RATE_LIMITED"), (401, "PROVIDER_AUTH_ERROR"), (404, "CONFIGURATION_ERROR")])
def test_gemini_api_failures(fake_sdk, upload_dir, status, reason):
    fake_sdk.generate_error = api_error(status)
    body = analyse(fake_sdk, upload_dir, "gemini_video_fake_bank.json")
    res = result(body)
    assert (body["status"], res["verdict"], res["risk_score"], res["reason_code"]) == ("unable_to_assess", "unable_to_assess", None, reason)
    assert fake_sdk.instances[-1].deleted == ["files/fake-123"] and leftovers(upload_dir) == []


def test_gemini_upload_failure(fake_sdk, upload_dir):
    fake_sdk.upload_error = api_error(503)
    body = analyse(fake_sdk, upload_dir, "gemini_video_fake_bank.json")
    assert result(body)["reason_code"] == "PROVIDER_UNAVAILABLE" and fake_sdk.instances[-1].deleted == [] and leftovers(upload_dir) == []


def test_missing_api_key_is_a_configuration_result_and_nothing_is_uploaded(fake_sdk, upload_dir):
    app = gemini_video_app(fake_sdk, upload_dir, "gemini_video_fake_bank.json", gemini_api_key="")
    with TestClient(app) as client:
        body = wait_for_job(client, post_video(client).json()["job_id"])
    assert result(body)["reason_code"] == "CONFIGURATION_ERROR" and fake_sdk.instances == [] and leftovers(upload_dir) == []


def test_failed_processing_never_returns_low_risk_whatever_the_cause(fake_sdk, upload_dir):
    for error in (api_error(500), api_error(504), api_error(400), RuntimeError("boom")):
        fake_sdk.reset()
        fake_sdk.generate_error = error
        body = analyse(fake_sdk, upload_dir, "gemini_video_harmless.json", states=(FileState.ACTIVE,), upload_state=FileState.ACTIVE)
        assert result(body)["verdict"] == "unable_to_assess" and result(body)["risk_score"] is None


def test_the_api_key_and_remote_names_never_appear_in_job_output(fake_sdk, upload_dir):
    body = analyse(fake_sdk, upload_dir, "gemini_video_fake_bank.json")
    assert "test-key-not-real" not in json.dumps(body) and "files/fake" not in json.dumps(body)
