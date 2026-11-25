"""Verdict thresholds and the public response schema (status / verdict / score invariants)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.errors import REASON_MESSAGES, ReasonCode
from app.schemas.analysis import AnalysisResponse
from app.schemas.verdict import LOW_RISK_MAX, SUSPICIOUS_MAX, AnalysisStatus, Verdict, verdict_for_score
from app.services import risk_engine
from app.services.response_builder import build_completed_response
from tests.fakes import make_report, make_signal


# ------------------------------------------------------------------ thresholds
@pytest.mark.parametrize("score,verdict", [
    (0, "low_risk"), (1, "low_risk"), (24, "low_risk"),
    (25, "suspicious"), (26, "suspicious"), (58, "suspicious"), (59, "suspicious"),
    (60, "high_risk"), (61, "high_risk"), (99, "high_risk"), (100, "high_risk"),
])
def test_approved_thresholds(score, verdict):
    assert verdict_for_score(score) == verdict and verdict_for_score(score).value == verdict


def test_threshold_constants_are_the_approved_ones():
    assert (LOW_RISK_MAX, SUSPICIOUS_MAX) == (24, 59)


@pytest.mark.parametrize("bad", [-1, 101, 24.5, "25", None, True, False, float("nan")])
def test_scores_outside_0_100_or_not_integers_are_rejected(bad):
    with pytest.raises(ValueError):
        verdict_for_score(bad)


def test_canonical_values_are_the_only_verdict_strings():
    assert {v.value for v in Verdict} == {"low_risk", "suspicious", "high_risk", "unable_to_assess"}
    assert {s.value for s in AnalysisStatus} == {"completed", "unable_to_assess"}
    for display_label in ("Low Risk", "Low risk", "HIGH RISK", "Medium Risk", "Safe", "High Risk"):
        with pytest.raises(ValueError):
            Verdict(display_label)


def test_the_engine_reexports_the_single_threshold_function():
    assert risk_engine.verdict_for_score is verdict_for_score


# ------------------------------------------------------------------ completed response
def completed(signals=None, coverage="full"):
    signals = signals if signals is not None else [make_signal("credential_request", "critical", "strong")]
    return build_completed_response(risk_engine.assess(make_report(signals, coverage)), "text")


def test_completed_response_shape():
    body = completed().model_dump(mode="json")
    assert body["status"] == "completed" and body["verdict"] == "high_risk" and body["risk_score"] == 70
    assert body["reason_code"] is None and body["offline_fallback_used"] is False and body["mode"] == "text"
    assert set(body) == {"status", "verdict", "risk_score", "confidence", "confidence_level", "reason_code", "coverage",
                         "summary", "scam_type", "explanation", "recommended_action", "signals", "red_flags",
                         "signal_scores", "mode", "offline_fallback_used", "language", "language_uncertain",
                         "limitations", "urls"}
    assert set(body["signals"][0]) == {"category", "name", "description", "source", "quote", "timestamp_start",
                                       "timestamp_end", "severity", "reliability", "impact", "corroborating_signals",
                                       "why_it_matters", "context"}


def rebuilt(**over):
    data = completed().model_dump()
    data.update(over)
    return AnalysisResponse(**data)


def test_verdict_must_match_the_score_thresholds_so_5_cannot_be_high_risk():
    """Closes audit finding P7 (a score of 5 with 'High Risk' used to be accepted)."""
    with pytest.raises(ValidationError):
        rebuilt(risk_score=5)                         # verdict is still high_risk
    with pytest.raises(ValidationError):
        rebuilt(risk_score=70, verdict="suspicious")
    assert rebuilt(risk_score=5, verdict="low_risk").verdict == Verdict.LOW_RISK


@pytest.mark.parametrize("over", [
    {"verdict": "unable_to_assess"},               # completed status with the failure verdict
    {"risk_score": None}, {"risk_score": 101}, {"risk_score": -1},
    {"reason_code": "PROVIDER_TIMEOUT"}, {"signal_scores": None}, {"coverage": None},
    {"verdict": "High Risk"}, {"mode": "audio_video"}, {"confidence": 1.5}, {"red_flags": ["x"] * 21},
    {"status": "done"},
])
def test_invalid_completed_responses_are_rejected(over):
    with pytest.raises(ValidationError):
        rebuilt(**over)


# ------------------------------------------------------------------ unable_to_assess
@pytest.mark.parametrize("reason", list(ReasonCode))
def test_unable_to_assess_representation(reason):
    body = AnalysisResponse.unable_to_assess("audio", reason).model_dump(mode="json")
    assert body["status"] == "unable_to_assess" and body["verdict"] == "unable_to_assess"
    assert body["risk_score"] is None and body["confidence"] is None and body["confidence_level"] is None
    assert body["signal_scores"] is None and body["signals"] == [] and body["red_flags"] == [] and body["coverage"] is None
    assert body["reason_code"] == reason.value and body["mode"] == "audio"
    assert REASON_MESSAGES[reason] in body["explanation"] and "not a low-risk result" in body["explanation"]
    assert "do not treat this content as safe" in body["recommended_action"]


@pytest.mark.parametrize("over", [
    {"risk_score": 0}, {"risk_score": 10}, {"confidence": 0.5}, {"confidence_level": "Low"},
    {"verdict": "low_risk"}, {"verdict": "high_risk"}, {"reason_code": None},
    {"signal_scores": {"authority_impersonation": 0, "urgency_isolation": 0, "credential_payment_trap": 0, "synthetic_or_script_signals": 0}},
    {"red_flags": ["x"]},
])
def test_unable_to_assess_can_never_carry_a_score_or_a_safe_verdict(over):
    data = AnalysisResponse.unable_to_assess("text", ReasonCode.PROVIDER_TIMEOUT).model_dump()
    data.update(over)
    with pytest.raises(ValidationError):
        AnalysisResponse(**data)


def test_every_reason_code_has_a_user_safe_message():
    assert set(REASON_MESSAGES) == set(ReasonCode)
    expected = {"CONFIGURATION_ERROR", "PROVIDER_AUTH_ERROR", "PROVIDER_RATE_LIMITED", "PROVIDER_TIMEOUT", "PROVIDER_UNAVAILABLE",
                "PROVIDER_ERROR", "INVALID_PROVIDER_RESPONSE", "INSUFFICIENT_EVIDENCE", "MEDIA_PROCESSING_FAILED", "INTERNAL_ANALYSIS_ERROR"}
    assert {r.value for r in ReasonCode} == expected
    for message in REASON_MESSAGES.values():
        assert "Traceback" not in message and "AIza" not in message
