"""Phase 7: context-aware scoring, the new categories, Unicode-aware duplicate handling, and the evidence contract
extensions. Everything runs through the SAME risk engine as before (no second scoring algorithm)."""
from __future__ import annotations

from typing import get_args

import pytest

from app.schemas.evidence import EvidenceReport, EvidenceSignal, MAX_LIMITATIONS, SignalCategory
from app.schemas.provider_output import ProviderAnalysis
from app.schemas.verdict import Verdict
from app.services import risk_engine as engine
from app.services.risk_engine import assess
from tests.fakes import make_report, make_signal


def score(signals, coverage="full"):
    return assess(make_report(signals, coverage))


# ---- context factor -----------------------------------------------------------------------------------------------
def test_context_factors_are_ordered_and_directed_is_full_strength():
    f = engine.CONTEXT_FACTORS
    assert f["directed_at_recipient"] == 1.0
    assert f["directed_at_recipient"] > f["unclear"] > f["discussed_or_described"] == f["fictional_or_example"] > 0


@pytest.mark.parametrize("context", ["discussed_or_described", "fictional_or_example"])
def test_a_critical_credential_request_that_is_only_discussed_cannot_reach_suspicious(context):
    result = score([make_signal("credential_request", "critical", "strong", context=context)])
    assert result.verdict is Verdict.LOW_RISK and result.risk_score < 25


def test_the_same_signal_scores_lower_when_context_is_unclear_than_when_directed():
    directed = score([make_signal("credential_request", "high", "strong", context="directed_at_recipient")]).risk_score
    unclear = score([make_signal("credential_request", "high", "strong", context="unclear")]).risk_score
    assert 0 < unclear < directed


def test_context_defaults_to_directed_so_older_style_signals_are_not_silently_discounted():
    assert EvidenceSignal(**make_signal()).context == "directed_at_recipient"


def test_discussed_evidence_never_produces_a_confidence_claim():
    result = score([make_signal("credential_request", "critical", "strong", context="discussed_or_described")])
    assert result.confidence is None


def test_directed_evidence_alongside_discussed_evidence_still_scores_the_directed_part():
    both = score([make_signal("credential_request", "critical", "strong", context="discussed_or_described"),
                  make_signal("payment_request", "high", "strong", name="pay_signal")])
    assert both.verdict is Verdict.HIGH_RISK or both.risk_score >= 40


def test_context_must_be_a_known_value():
    with pytest.raises(ValueError):
        EvidenceSignal(**make_signal(context="definitely_a_scam"))


# ---- the new categories -------------------------------------------------------------------------------------------
NEW = {"remote_access_request": 0.60, "advance_fee_or_prize": 0.50, "organization_impersonation": 0.45}


def test_new_categories_exist_with_the_documented_weights():
    assert set(NEW) <= set(get_args(SignalCategory))
    for name, weight in NEW.items():
        assert engine.CATEGORY_WEIGHTS[name] == weight


def test_thresholds_are_unchanged():
    assert [engine.verdict_for_score(s) for s in (0, 24, 25, 59, 60, 100)] == [
        Verdict.LOW_RISK, Verdict.LOW_RISK, Verdict.SUSPICIOUS, Verdict.SUSPICIOUS, Verdict.HIGH_RISK, Verdict.HIGH_RISK]


def test_remote_access_request_counts_towards_the_credential_payment_trap_meter():
    result = score([make_signal("remote_access_request", "critical", "strong")])
    assert result.signal_scores["credential_payment_trap"] > 0 and result.verdict is Verdict.HIGH_RISK


def test_an_organization_claim_alone_is_not_high_risk():
    assert score([make_signal("organization_impersonation", "medium", "moderate")]).verdict is Verdict.LOW_RISK


# ---- duplicates, including non-Latin scripts -----------------------------------------------------------------------
def tamil(name, quote):
    return make_signal("credential_request", "critical", "strong", name=name, quote=quote, source="text_content")


def test_repeated_tamil_quote_is_one_event_not_two():
    one = score([tamil("otp_one", "உடனே OTP பகிருங்கள்")])
    two = score([tamil("otp_one", "உடனே OTP பகிருங்கள்"), tamil("otp_two", "உடனே  OTP   பகிருங்கள்!")])
    assert one.risk_score == two.risk_score


def test_different_tamil_quotes_are_not_merged_by_stripping_the_script():
    a = score([tamil("a_signal", "உடனே OTP பகிருங்கள்")])
    b = score([tamil("a_signal", "உடனே OTP பகிருங்கள்"), tamil("b_signal", "கடவுச்சொல் சொல்லுங்கள்")])
    assert b.risk_score >= a.risk_score and len(b.signals) == 2


def test_hindi_quotes_with_combining_marks_dedupe():
    q = "तुरंत OTP बताइए"
    assert score([tamil("x_signal", q)]).risk_score == score([tamil("x_signal", q), tamil("y_signal", q + "।")]).risk_score


# ---- the evidence contract ------------------------------------------------------------------------------------------
def test_gemini_schema_requires_the_new_fields_while_python_stays_lenient():
    schema = ProviderAnalysis.model_json_schema()
    assert {"language", "language_uncertain", "limitations"} <= set(schema["required"])
    assert {"context", "quote"} <= set(schema["$defs"]["EvidenceSignal"]["required"])
    report = ProviderAnalysis(coverage="full", signals=[])              # an older-style answer still validates
    assert report.language is None and report.limitations == [] and report.language_uncertain is False


def test_gemini_schema_exposes_no_authoritative_fields():
    props = set(ProviderAnalysis.model_json_schema()["properties"])
    assert not props & {"risk_score", "verdict", "status", "confidence", "summary", "recommended_action", "threshold"}


def test_limitations_are_cleaned_clipped_and_capped():
    report = EvidenceReport(coverage="full", signals=[],
                            limitations=["  ", "x" * 500] + [f"note {i}" for i in range(20)])
    assert len(report.limitations) <= MAX_LIMITATIONS and all(0 < len(n) <= 200 for n in report.limitations)


def test_language_is_bounded():
    assert len(EvidenceReport(coverage="full", signals=[], language="L" * 500).language or "") <= 40
