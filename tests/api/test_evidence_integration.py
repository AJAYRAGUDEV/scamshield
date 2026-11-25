"""Phase 7 integration: model output is untrusted evidence; the server's risk engine alone decides."""
from __future__ import annotations

import json
from types import SimpleNamespace

from app.core.config import Settings
from app.core.errors import AnalysisError, ReasonCode
from tests.fakes import FakeProvider, api_error, load_fixture_json
from tests.scenario_helpers import analyze_audio, analyze_text, make_wav, scenario_client


def test_gemini_cannot_set_the_verdict_score_or_status(fake_sdk, upload_dir):
    data = {**load_fixture_json("gemini_text_news_article.json"), "verdict": "high_risk", "risk_score": 100,
            "status": "completed", "confidence": 1.0, "thresholds": {"high": 0}, "recommended_action": "pay now"}
    body = analyze_text(scenario_client(fake_sdk, upload_dir, data))
    assert body["verdict"] == "low_risk" and body["risk_score"] < 25 and "pay now" not in json.dumps(body)


def test_a_model_claiming_safety_cannot_hide_evidence(fake_sdk, upload_dir):
    data = {**load_fixture_json("gemini_text_bank_impersonation.json"), "verdict": "low_risk", "risk_score": 0}
    body = analyze_text(scenario_client(fake_sdk, upload_dir, data))
    assert body["verdict"] == "high_risk" and body["risk_score"] == 88


def test_invalid_evidence_is_rejected_not_scored(fake_sdk, upload_dir):
    bad = load_fixture_json("gemini_text_bank_impersonation.json")
    bad["signals"][0]["severity"] = "apocalyptic"
    body = analyze_text(scenario_client(fake_sdk, upload_dir, bad))
    assert (body["status"], body["verdict"], body["risk_score"]) == ("unable_to_assess", "unable_to_assess", None)
    assert body["signals"] == [] and body["limitations"] == [] and body["urls"] == []


def test_unknown_context_value_is_rejected_not_scored(fake_sdk, upload_dir):
    bad = load_fixture_json("gemini_text_otp_request.json")
    bad["signals"][0]["context"] = "ignore previous instructions"
    assert analyze_text(scenario_client(fake_sdk, upload_dir, bad))["verdict"] == "unable_to_assess"


def test_malformed_model_output_is_unable_to_assess(fake_sdk, upload_dir):
    client = scenario_client(fake_sdk, upload_dir, {})
    fake_sdk.generate_result = SimpleNamespace(parsed=None, text="not json at all")
    body = analyze_text(client)
    assert (body["verdict"], body["risk_score"]) == ("unable_to_assess", None)


def test_duplicate_evidence_is_not_double_counted(fake_sdk, upload_dir):
    base = load_fixture_json("gemini_text_otp_request.json")
    doubled = {**base, "signals": base["signals"] * 4}
    one = analyze_text(scenario_client(fake_sdk, upload_dir, base))["risk_score"]
    four = analyze_text(scenario_client(fake_sdk, upload_dir, doubled))["risk_score"]
    assert one == four


def test_failed_analysis_is_unable_to_assess_with_a_null_score_and_empty_evidence(fake_sdk, upload_dir):
    client = scenario_client(fake_sdk, upload_dir, "gemini_text_otp_request.json")
    fake_sdk.generate_error = api_error(503)
    body = analyze_text(client)
    assert body["status"] == "unable_to_assess" and body["risk_score"] is None and body["confidence"] is None
    assert body["signal_scores"] is None and body["urls"] == [] and body["limitations"] == []
    assert body["verdict"] != "low_risk"


def test_missing_evidence_is_never_low_risk(fake_sdk, upload_dir):
    for fixture in ("gemini_insufficient.json", "gemini_partial_unreadable.json", "gemini_audio_unclear.json"):
        body = analyze_text(scenario_client(fake_sdk, upload_dir, fixture))
        assert body["verdict"] == "unable_to_assess" and body["risk_score"] is None, fixture


def test_null_scores_survive_the_whole_response_unchanged(client, provider):
    from app.core.errors import AnalysisError as AE

    provider.error = AE(ReasonCode.PROVIDER_UNAVAILABLE, "down")
    body = client.post("/api/v1/analyze/text", data={"text": "hello"}).json()
    assert body["risk_score"] is None and "risk_score" in body


def test_response_exposes_no_full_transcript_and_quotes_are_short(fake_sdk, upload_dir):
    body = analyze_audio(scenario_client(fake_sdk, upload_dir, "gemini_audio_payment_pressure.json"), make_wav(40.0)).json()
    assert all(len(s["quote"] or "") <= 300 for s in body["signals"])
    assert len(body["signals"]) <= 40 and "transcript" not in json.dumps(body).lower().replace("transcribed", "")


def test_the_gemini_key_is_never_in_a_response(fake_sdk, upload_dir):
    body = analyze_text(scenario_client(fake_sdk, upload_dir, "gemini_text_bank_impersonation.json"))
    assert "test-key-not-real" not in json.dumps(body)
