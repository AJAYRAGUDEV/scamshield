"""Server-generated wording: deterministic, calibrated, never a claim of safety or proof."""
from __future__ import annotations

import re

import pytest

from app.schemas.verdict import Verdict
from app.services import risk_engine
from app.services.response_builder import CAVEAT, CATEGORY_PHRASES, RECOMMENDED_ACTIONS, build_completed_response
from tests.fakes import make_report, make_signal


def build(signals, coverage="full", mode="text"):
    return build_completed_response(risk_engine.assess(make_report(signals, coverage)), mode)


HIGH = [make_signal("authority_impersonation", "critical", "strong", evidence="Claims to be a CBI inspector"),
        make_signal("threat_coercion", "critical", "strong", evidence="Threatens arrest"),
        make_signal("payment_request", "high", "strong", evidence="Demands a UPI transfer")]
SUSPICIOUS = [make_signal("urgency_pressure", "medium", "moderate"), make_signal("payment_request", "medium", "moderate")]
LOW = [make_signal("authority_impersonation", "low", "weak", evidence="Mentions the official app")]


def test_every_category_has_a_phrase():
    from typing import get_args
    from app.schemas.evidence import SignalCategory
    assert set(CATEGORY_PHRASES) == set(get_args(SignalCategory))


@pytest.mark.parametrize("signals,verdict", [(HIGH, Verdict.HIGH_RISK), (SUSPICIOUS, Verdict.SUSPICIOUS), (LOW, Verdict.LOW_RISK), ([], Verdict.LOW_RISK)])
def test_recommended_action_follows_the_server_verdict(signals, verdict):
    response = build(signals)
    assert response.verdict == verdict and response.recommended_action == RECOMMENDED_ACTIONS[verdict]


def test_high_risk_text_names_the_main_indicators():
    response = build(HIGH)
    assert response.summary.startswith("High risk") and "a claim of authority" in response.summary
    assert response.scam_type.startswith("Possible scam pattern")
    assert "Claims to be a CBI inspector" in response.explanation and "Do not send money" in response.recommended_action


def test_suspicious_text_is_hedged():
    response = build(SUSPICIOUS)
    assert "not conclusive" in response.summary and response.recommended_action.startswith("Pause and verify")


def test_low_risk_text_never_claims_safety_and_always_carries_the_caveat():
    for signals in (LOW, []):
        response = build(signals)
        text = " ".join([response.summary, response.scam_type, response.explanation, response.recommended_action]).lower()
        assert "does not prove" in text
        text = text.replace(CAVEAT.lower(), "").replace("does not make the content safe", "")   # the deliberate negations
        assert not re.search(r"\b(is|are|it's|this is|content is|message is) (completely |totally |definitely |entirely )?(safe|legitimate|genuine|authentic)\b", text), text
        assert "guarantee" not in text and "100%" not in text


def test_no_evidence_wording():
    response = build([])
    assert response.scam_type == "No scam indicators identified" and "no scam indicators were identified" in response.summary
    assert response.red_flags == [] and response.signals == [] and response.confidence is None and response.confidence_level is None


def test_partial_coverage_is_disclosed():
    response = build([make_signal("credential_request", "critical", "strong")], "partial")
    assert response.coverage == "partial" and "Only part of the content" in response.explanation


def test_wording_never_claims_to_prove_a_scam_or_a_deepfake():
    for signals in (HIGH, [make_signal("synthetic_voice_indicator", "critical", "strong", source="audio_acoustic")]):
        text = " ".join(build(signals, mode="audio").model_dump(mode="json")[k] for k in ("summary", "explanation", "scam_type")).lower()
        assert "deepfake" not in text and "proves" not in text.replace("does not prove", "") and "certainly" not in text


def test_red_flags_are_the_counted_evidence_strongest_first_and_capped():
    many = [make_signal("urgency_pressure", name=f"u{i}", evidence=f"distinct {i}", quote=f"quote {i:03d} text") for i in range(30)]
    flags = build(many).red_flags
    assert len(flags) <= 20
    response = build(HIGH)
    assert response.red_flags == [s.description for s in response.signals]
    assert response.signals[0].category in ("authority_impersonation", "threat_coercion")


def test_text_is_identical_for_identical_evidence():
    assert build(HIGH).model_dump() == build(list(reversed(HIGH))).model_dump()


def test_an_unable_assessment_cannot_be_built_into_a_completed_response():
    with pytest.raises(ValueError):
        build_completed_response(risk_engine.assess(make_report([], "insufficient")), "text")
