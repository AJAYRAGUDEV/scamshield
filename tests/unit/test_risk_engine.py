"""The risk engine: deterministic, bounded, duplicate-aware, reliability-aware."""
from __future__ import annotations

import random
from typing import get_args

import pytest

from app.core.errors import ReasonCode
from app.schemas.evidence import EvidenceReport, SignalCategory
from app.schemas.verdict import AnalysisStatus, Verdict
from app.services import risk_engine as engine
from app.services.risk_engine import assess, confidence_level_for
from tests.fakes import make_report, make_signal

CATEGORIES = list(get_args(SignalCategory))


def score_of(signals, coverage="full"):
    return assess(make_report(signals, coverage)).risk_score


def sig(category="credential_request", severity="high", reliability="strong", **kw):
    return make_signal(category, severity, reliability, **kw)


def many(n, category="urgency_pressure", severity="critical", reliability="strong"):
    """n INDEPENDENT events of one category (distinct names, descriptions and quotes)."""
    return [sig(category, severity, reliability, name=f"{category}_{i}", evidence=f"distinct evidence {i}",
                quote=f"completely different quote {i:03d} text") for i in range(n)]


# ------------------------------------------------------------------ configuration sanity
def test_weight_tables_cover_every_category_and_level():
    assert set(engine.CATEGORY_WEIGHTS) == set(CATEGORIES)
    assert all(0 < w <= 1 for w in engine.CATEGORY_WEIGHTS.values())
    assert set(engine.SEVERITY_FACTORS) == {"low", "medium", "high", "critical"}
    assert set(engine.RELIABILITY_FACTORS) == {"weak", "moderate", "strong"}
    s, r = engine.SEVERITY_FACTORS, engine.RELIABILITY_FACTORS
    assert s["low"] < s["medium"] < s["high"] < s["critical"] <= 1
    assert r["weak"] < r["moderate"] < r["strong"] <= 1


def test_every_vector_category_exists_and_vectors_do_not_overlap():
    flat = [c for cats in engine.VECTOR_CATEGORIES.values() for c in cats]
    assert set(flat) <= set(CATEGORIES) and len(flat) == len(set(flat))


def test_thresholds_are_the_approved_ones():
    assert (engine.LOW_RISK_MAX, engine.SUSPICIOUS_MAX) == (24, 59)


# ------------------------------------------------------------------ threshold boundaries through the engine
@pytest.mark.parametrize("weight,score,verdict", [
    (0.0001, 0, Verdict.LOW_RISK),
    (0.2449, 24, Verdict.LOW_RISK),          # top of Low Risk
    (0.2451, 25, Verdict.SUSPICIOUS),        # bottom of Suspicious
    (0.5949, 59, Verdict.SUSPICIOUS),        # top of Suspicious
    (0.5951, 60, Verdict.HIGH_RISK),         # bottom of High Risk
    (1.0, 100, Verdict.HIGH_RISK),
])
def test_boundary_scores_map_to_the_approved_verdicts(monkeypatch, weight, score, verdict):
    monkeypatch.setitem(engine.CATEGORY_WEIGHTS, "other_suspicious", weight)   # one critical+strong signal => impact == weight
    result = assess(make_report([sig("other_suspicious", "critical", "strong")]))
    assert (result.risk_score, result.verdict, result.status) == (score, verdict, AnalysisStatus.COMPLETED)


# ------------------------------------------------------------------ bounds and determinism
def random_report(rng: random.Random, coverage="full") -> EvidenceReport:
    signals = []
    for i in range(rng.randint(0, 25)):
        category = rng.choice(CATEGORIES)
        signals.append(sig(category, rng.choice(["low", "medium", "high", "critical"]), rng.choice(["weak", "moderate", "strong"]),
                           name=f"{category}_{rng.randint(0, 4)}", evidence=f"evidence {rng.randint(0, 6)}",
                           quote=rng.choice([None, f"quote number {rng.randint(0, 6)}"]),
                           source="audio_speech", timestamp_start=rng.choice([None, float(rng.randint(0, 60))])))
    return make_report(signals, coverage)


def test_score_is_always_between_0_and_100_and_matches_the_verdict():
    rng = random.Random(20240601)
    for _ in range(400):
        result = assess(random_report(rng))
        assert isinstance(result.risk_score, int) and 0 <= result.risk_score <= 100
        assert result.verdict == engine.verdict_for_score(result.risk_score)
        assert result.confidence is None or 0 < result.confidence <= 0.95


def test_same_evidence_always_gives_the_same_result_whatever_the_order():
    rng = random.Random(7)
    for _ in range(100):
        report = random_report(rng)
        first = assess(report)
        shuffled = list(report.signals)
        rng.shuffle(shuffled)
        again = assess(EvidenceReport(coverage=report.coverage, signals=shuffled))
        assert (again.risk_score, again.verdict, again.confidence, dict(again.signal_scores or {})) == \
               (first.risk_score, first.verdict, first.confidence, dict(first.signal_scores or {}))
        assert [(s.signal.category, s.signal.name, s.impact) for s in again.signals] == \
               [(s.signal.category, s.signal.name, s.impact) for s in first.signals]


def test_maximum_evidence_cannot_exceed_100():
    everything = [sig(c, "critical", "strong", name=f"{c}_{i}", quote=f"unique {c} quote {i}")
                  for c in CATEGORIES for i in range(2)]
    assert score_of(everything) <= 100 and score_of(everything) >= 95


# ------------------------------------------------------------------ missing evidence is never "safe by default"
def test_no_evidence_with_full_coverage_is_low_risk_without_a_claimed_confidence():
    result = assess(make_report([], "full"))
    assert (result.verdict, result.risk_score, result.confidence) == (Verdict.LOW_RISK, 0, None)
    assert result.signals == ()


def test_no_evidence_with_partial_coverage_cannot_be_cleared():
    result = assess(make_report([], "partial"))
    assert result.status == AnalysisStatus.UNABLE_TO_ASSESS and result.risk_score is None
    assert result.reason_code == ReasonCode.INSUFFICIENT_EVIDENCE and result.verdict == Verdict.UNABLE_TO_ASSESS


def test_weak_evidence_with_partial_coverage_cannot_be_cleared_either():
    assert assess(make_report([sig("urgency_pressure", "low", "weak")], "partial")).status == AnalysisStatus.UNABLE_TO_ASSESS


def test_insufficient_coverage_is_unable_to_assess_even_with_signals():
    result = assess(make_report([sig("credential_request", "critical", "strong")], "insufficient"))
    assert result.status == AnalysisStatus.UNABLE_TO_ASSESS and result.risk_score is None and result.signals == ()


def test_partial_coverage_can_still_raise_a_warning_with_reduced_confidence():
    signals = [sig("credential_request", "critical", "strong")]
    full, partial = assess(make_report(signals, "full")), assess(make_report(signals, "partial"))
    assert partial.verdict == full.verdict == Verdict.HIGH_RISK and partial.risk_score == full.risk_score
    assert partial.confidence == pytest.approx(full.confidence * 0.5, abs=0.01)


# ------------------------------------------------------------------ weak vs strong, severity, reliability
def test_weak_evidence_stays_low_and_strong_evidence_reaches_high():
    weak = score_of([sig("authority_impersonation", "low", "weak")])
    strong = score_of([sig("credential_request", "critical", "strong")])
    assert weak < 25 <= 60 <= strong


@pytest.mark.parametrize("category", CATEGORIES)
def test_impact_rises_with_severity_and_with_reliability(category):
    by_severity = [engine.signal_impact(make_report([sig(category, s, "strong")]).signals[0]) for s in ("low", "medium", "high", "critical")]
    by_reliability = [engine.signal_impact(make_report([sig(category, "high", r)]).signals[0]) for r in ("weak", "moderate", "strong")]
    assert by_severity == sorted(by_severity) and len(set(by_severity)) == 4
    assert by_reliability == sorted(by_reliability) and len(set(by_reliability)) == 3


def test_reliability_changes_the_score():
    assert score_of([sig("payment_request", "high", "weak")]) < score_of([sig("payment_request", "high", "moderate")]) \
        < score_of([sig("payment_request", "high", "strong")])


def test_unreliable_evidence_alone_never_reaches_high_risk():
    for category in CATEGORIES:
        assert score_of([sig(category, "critical", "weak")]) < 60, category


def test_a_lone_synthetic_voice_or_scripted_indicator_never_reaches_suspicious():
    for category in ("synthetic_voice_indicator", "scripted_delivery"):
        assert score_of([sig(category, "critical", "strong", source="audio_acoustic")]) < 25


def test_different_kinds_of_evidence_compound():
    urgency = score_of([sig("urgency_pressure", "medium", "moderate")])
    payment = score_of([sig("payment_request", "medium", "moderate")])
    both = score_of([sig("urgency_pressure", "medium", "moderate"), sig("payment_request", "medium", "moderate")])
    assert both > max(urgency, payment) and both < urgency + payment + 1


# ------------------------------------------------------------------ duplicates vs independent evidence
def test_exact_duplicates_count_once():
    one = assess(make_report([sig("payment_request")]))
    five = assess(make_report([sig("payment_request")] * 5))
    assert five.risk_score == one.risk_score and len(five.signals) == 1 and five.signals[0].corroborating == 4


def test_different_descriptions_of_the_same_quoted_request_count_once():
    quote = "send fifty thousand rupees right now"
    signals = [sig("payment_request", name=f"n{i}", evidence=f"described differently {i}", quote=quote) for i in range(4)]
    result = assess(make_report(signals))
    assert result.risk_score == score_of([signals[0]]) and len(result.signals) == 1 and result.signals[0].corroborating == 3


def test_contained_quotes_are_the_same_event():
    a = sig("credential_request", name="otp_ask_one", evidence="x", quote="please read out the otp now")
    b = sig("credential_request", name="otp_ask_two", evidence="y", quote="Please read out the OTP now, quickly!")
    assert len(assess(make_report([a, b])).signals) == 1


def test_short_generic_quotes_do_not_prove_the_same_event():
    a = sig("urgency_pressure", name="urgent_one", evidence="x", quote="now")
    b = sig("urgency_pressure", name="urgent_two", evidence="y", quote="now")
    assert len(assess(make_report([a, b])).signals) == 2


def test_signals_at_the_same_moment_are_one_event_but_distant_ones_are_independent():
    def at(t, name):
        return sig("payment_request", name=name, evidence=name, source="audio_speech", timestamp_start=t, timestamp_end=t + 1.0)
    assert len(assess(make_report([at(10.0, "pay_a"), at(11.5, "pay_b"), at(12.9, "pay_c")])).signals) == 1   # chained within 2 s
    assert len(assess(make_report([at(10.0, "pay_a"), at(30.0, "pay_b")])).signals) == 2


def test_same_name_without_any_location_is_one_event_but_different_names_are_independent():
    same = [sig("payment_request", name="bank_transfer", evidence="asked once"), sig("payment_request", name="bank_transfer", evidence="asked again")]
    different = [sig("payment_request", name="bank_transfer", evidence="a"), sig("payment_request", name="gift_cards", evidence="b")]
    assert len(assess(make_report(same)).signals) == 1
    assert len(assess(make_report(different)).signals) == 2


def test_independent_events_in_one_category_add_with_diminishing_returns():
    one, two, three = (score_of(many(n, "payment_request", "high", "strong")) for n in (1, 2, 3))
    assert one < two < three
    assert two - one > three - two                      # each further event adds less
    assert two < 2 * one


def test_repetition_cannot_inflate_a_score_indefinitely():
    s10, s20, s40 = (score_of(many(n)) for n in (10, 20, 40))
    cap = int(engine.REPEAT_CAP_MULTIPLIER * engine.CATEGORY_WEIGHTS["urgency_pressure"] * 100 + 0.5)
    assert s10 == s20 == s40 == cap == 45


def test_the_same_quote_in_different_categories_is_not_merged():
    quote = "transfer the money and tell no one"
    result = assess(make_report([sig("payment_request", quote=quote), sig("isolation_secrecy", quote=quote)]))
    assert len(result.signals) == 2


def test_genuinely_independent_evidence_is_kept_distinguishable():
    result = assess(make_report(many(3, "payment_request") + [sig("credential_request", quote="read the otp")]))
    assert len(result.signals) == 4 and all(s.corroborating == 0 for s in result.signals)


def test_scored_signals_are_sorted_strongest_first():
    result = assess(make_report([sig("urgency_pressure", "low", "weak"), sig("credential_request", "critical", "strong")]))
    assert [s.impact for s in result.signals] == sorted((s.impact for s in result.signals), reverse=True)
    assert result.signals[0].signal.category == "credential_request"
    assert result.dominant_categories[0] == "credential_request"


# ------------------------------------------------------------------ confidence
def test_repeating_one_kind_of_evidence_does_not_raise_confidence():
    assert assess(make_report(many(1))).confidence == assess(make_report(many(10))).confidence


def test_more_kinds_of_evidence_raise_confidence_up_to_the_cap():
    one = assess(make_report([sig("credential_request")])).confidence
    two = assess(make_report([sig("credential_request"), sig("payment_request")])).confidence
    three = assess(make_report([sig("credential_request"), sig("payment_request"), sig("threat_coercion")])).confidence
    assert one < two < three <= 0.95


def test_weak_evidence_gives_lower_confidence_than_strong():
    assert assess(make_report([sig("payment_request", "high", "weak")])).confidence < \
           assess(make_report([sig("payment_request", "high", "strong")])).confidence


@pytest.mark.parametrize("confidence,level", [(None, None), (0.0, "Low"), (0.49, "Low"), (0.5, "Medium"), (0.74, "Medium"), (0.75, "High"), (0.95, "High")])
def test_confidence_levels(confidence, level):
    assert confidence_level_for(confidence) == level


# ------------------------------------------------------------------ the provider cannot influence the verdict
def test_authoritative_fields_in_provider_output_are_ignored():
    report = EvidenceReport(**{"coverage": "full", "risk_score": 0, "verdict": "low_risk", "confidence": 1.0,
                               "status": "completed", "recommended_action": "totally safe",
                               "signals": [sig("credential_request", "critical", "strong", risk_score=0, verdict="low_risk")]})
    assert not any(hasattr(report, f) for f in ("risk_score", "verdict", "confidence", "status", "recommended_action"))
    assert assess(report).verdict == Verdict.HIGH_RISK


def test_vectors_are_display_only_and_other_suspicious_still_counts_toward_the_score():
    result = assess(make_report([sig("other_suspicious", "critical", "strong")]))
    assert result.risk_score == 20 and set(result.signal_scores) == set(engine.VECTOR_CATEGORIES)
    assert all(v == 0 for v in result.signal_scores.values())     # not part of any dashboard vector
    assert all(0 <= v <= 100 for v in assess(make_report(many(5, "credential_request"))).signal_scores.values())
