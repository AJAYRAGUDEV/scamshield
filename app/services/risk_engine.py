"""The single authority for the final risk score, verdict and confidence.

The AI provider supplies EVIDENCE (``EvidenceReport``). Everything below is deterministic,
rule-based arithmetic on that evidence: the same evidence always yields the same result, whatever
order the signals arrive in. No random numbers, no model-generated scores.

Scoring formula
---------------
1. **Impact of one signal**   ``p = CATEGORY_WEIGHT x SEVERITY_FACTOR x RELIABILITY_FACTOR x CONTEXT_FACTOR``  (0..1)
   - CATEGORY_WEIGHT is the most that one signal of that kind can contribute on its own. It encodes
     how strongly the behaviour correlates with scams (asking for an OTP is worse than urgency).
   - SEVERITY_FACTOR scales it by how serious the described behaviour is.
   - RELIABILITY_FACTOR scales it by how clearly the input supports it (weak = ambiguous/inferred).
   - CONTEXT_FACTOR (Phase 7) scales it by HOW the indicator appears: 1.0 when it is an instruction, request or
     claim aimed at the recipient; 0.6 when the context is unclear; 0.1 when a scam is only discussed or described
     (news, education, a question) or appears in a fictional example. Discussion is therefore not scored like a demand,
     but it is never ignored: an unrecognised or adversarial "this is just an example" wrapper still counts a little,
     and a model that omits the context is treated as 'directed' (the cautious default).
2. **Same event is counted once.** Signals are grouped into one *event* when, within a category, they
   share a quote (equal, or one contains the other), overlap in time (or are within 2 s), are exact
   duplicates, or have the same name and no location. An event's impact is its strongest member's
   impact; the other members are reported as ``corroborating_signals``, not added.
3. **Repetition has diminishing returns.** Within one category, independent events are sorted by
   impact and the k-th is multiplied by ``0.5**k``; the category total is capped at
   ``1.5 x CATEGORY_WEIGHT``. Ten independent urgency statements cannot exceed 1.5 x one urgency.
4. **Categories combine as independent corroboration** (noisy-OR): ``raw = 1 - prod(1 - category_total)``.
   Several different kinds of evidence compound; the result can approach but never exceed 1.
5. **Score** = ``floor(raw x 100 + 0.5)``, clamped to 0..100.
6. **Verdict** comes only from ``verdict_for_score`` (0-24 low_risk, 25-59 suspicious, 60-100 high_risk).

Missing evidence is NOT safety
------------------------------
The provider also reports ``coverage``. ``insufficient`` -> unable_to_assess. ``partial`` can still
raise a warning (a scam found in the readable part is still a scam) but can never clear content: a
partial analysis whose score falls in the low-risk band is unable_to_assess / INSUFFICIENT_EVIDENCE,
because the part that was not understood might contain the scam. An empty signal list is a
``low_risk`` result only when coverage is ``full``, and even then no confidence is claimed.

Confidence (0..1, or ``None``)
------------------------------
How strongly the validated evidence supports the assessment; NOT a probability that content is a scam.
``confidence = weighted_mean_reliability x corroboration x coverage_factor``, capped at 0.95, where
the mean is weighted by event impact, corroboration is 0.70 / 0.85 / 1.0 for 1 / 2 / 3+ DIFFERENT
categories of evidence (repeating one kind never raises it), and coverage_factor is 1.0 (full) or 0.5 (partial). With no events it is ``None`` because the
absence of indicators cannot be turned into a reliable number.

Limitations: the engine is only as good as the evidence it is given; the provider can miss indicators
or over-report them, and weights are expert judgement, not fitted to data. A low score never proves
that content or a person is genuine or safe.
"""
from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from typing import Mapping

from app.core.errors import ReasonCode
from app.schemas.evidence import EvidenceReport, EvidenceSignal
from app.schemas.verdict import (
    LOW_RISK_MAX, MAX_SCORE, SUSPICIOUS_MAX, AnalysisStatus, Verdict, verdict_for_score,
)

__all__ = ["assess", "RiskAssessment", "ScoredSignal", "verdict_for_score", "confidence_level_for",
           "LOW_RISK_MAX", "SUSPICIOUS_MAX"]

# ----------------------------------------------------------------------------- weights (centralised)
# Maximum standalone impact of one signal of this category (severity critical + reliability strong).
CATEGORY_WEIGHTS: dict[str, float] = {
    "credential_request": 0.70,        # OTP/password/PIN requests are the strongest single indicator
    "payment_redirection": 0.65,       # changed bank details / "pay this other account"
    "payment_request": 0.60,           # money, gift cards, crypto
    "authority_impersonation": 0.55,
    "threat_coercion": 0.55,
    "suspicious_link": 0.50,
    "fake_interface": 0.55,            # (video) fake bank/payment/government screen
    "impersonation_relationship": 0.45,
    "isolation_secrecy": 0.40,         # "don't tell anyone" is a classic coercion marker
    "visual_manipulation": 0.35,       # (video) possible manipulation; weak alone, can never reach high_risk by itself
    "urgency_pressure": 0.30,          # common in legitimate messages too, so weak alone
    "synthetic_voice_indicator": 0.20,  # cannot be proven from audio; can never reach "suspicious" alone
    "other_suspicious": 0.20,
    "scripted_delivery": 0.15,         # many legitimate calls are scripted
    "remote_access_request": 0.60,     # install AnyDesk/TeamViewer, share the screen, grant device access
    "advance_fee_or_prize": 0.50,      # pay a fee to claim a prize/refund, "guaranteed" investment returns
    "organization_impersonation": 0.45,  # a "delivery company", "customer support" or "employee" asking for something
}
SEVERITY_FACTORS: dict[str, float] = {"low": 0.35, "medium": 0.60, "high": 0.85, "critical": 1.00}
CONTEXT_FACTORS: dict[str, float] = {
    "directed_at_recipient": 1.00,
    "unclear": 0.60,
    "discussed_or_described": 0.10,
    "fictional_or_example": 0.10,
}
RELIABILITY_FACTORS: dict[str, float] = {"weak": 0.50, "moderate": 0.75, "strong": 1.00}

DECAY = 0.5                      # k-th independent event in a category counts 0.5**k
REPEAT_CAP_MULTIPLIER = 1.5      # a category can never exceed 1.5 x its weight, however often repeated
EVENT_WINDOW_SECONDS = 2.0       # same category within this gap in time = same event
MIN_QUOTE_MATCH_CHARS = 8        # shorter quotes are too generic to prove "same event"

PARTIAL_COVERAGE_FACTOR = 0.5
CONFIDENCE_CAP = 0.95
CORROBORATION_FACTORS = {1: 0.70, 2: 0.85, 3: 1.00}
HIGH_CONFIDENCE = 0.75
MEDIUM_CONFIDENCE = 0.50

# The four radar/meter vectors the dashboard shows. Display only: they never influence the verdict.
VECTOR_CATEGORIES: dict[str, tuple[str, ...]] = {
    "authority_impersonation": ("authority_impersonation", "organization_impersonation", "impersonation_relationship"),
    "urgency_isolation": ("urgency_pressure", "isolation_secrecy", "threat_coercion"),
    "credential_payment_trap": ("credential_request", "payment_request", "payment_redirection", "advance_fee_or_prize",
                                "remote_access_request", "suspicious_link", "fake_interface"),
    "synthetic_or_script_signals": ("synthetic_voice_indicator", "scripted_delivery", "visual_manipulation"),
}


# ----------------------------------------------------------------------------- results
@dataclass(frozen=True)
class ScoredSignal:
    """One counted event: its strongest member, plus how many other signals were merged into it."""
    signal: EvidenceSignal
    impact: float
    corroborating: int


@dataclass(frozen=True)
class RiskAssessment:
    status: AnalysisStatus
    verdict: Verdict
    risk_score: int | None
    confidence: float | None
    reason_code: ReasonCode | None
    coverage: str
    signals: tuple[ScoredSignal, ...]
    signal_scores: Mapping[str, int] | None
    dominant_categories: tuple[str, ...]      # categories by contribution, strongest first


def confidence_level_for(confidence: float | None) -> str | None:
    if confidence is None:
        return None
    if confidence >= HIGH_CONFIDENCE:
        return "High"
    return "Medium" if confidence >= MEDIUM_CONFIDENCE else "Low"


# ----------------------------------------------------------------------------- grouping
@dataclass
class _Group:
    category: str
    representative: EvidenceSignal
    impact: float
    size: int


def signal_impact(signal: EvidenceSignal) -> float:
    return (CATEGORY_WEIGHTS[signal.category]
            * SEVERITY_FACTORS[signal.severity]
            * RELIABILITY_FACTORS[signal.reliability]
            * CONTEXT_FACTORS[signal.context])


def _norm(text: str | None) -> str:
    """Case-folded text with punctuation removed, for comparing quotes. Unicode-aware: letters, DIGITS and
    COMBINING MARKS are kept, so Tamil, Hindi and other Indic scripts (whose vowel signs are marks, not letters)
    are not split into fragments the way a plain word-character regex would split them."""
    folded = unicodedata.normalize("NFKC", (text or "")).casefold()
    kept = "".join(ch if (ch.isalnum() or unicodedata.category(ch).startswith("M")) else " " for ch in folded)
    return " ".join(kept.split())


def _has_location(sig: EvidenceSignal) -> bool:
    return bool(_norm(sig.quote)) or sig.timestamp_start is not None


def _same_event(a: EvidenceSignal, b: EvidenceSignal) -> bool:
    """Symmetric predicate; only ever called for two signals of the same category."""
    if a.name == b.name and _norm(a.evidence) == _norm(b.evidence):
        return True                                            # exact duplicate
    qa, qb = _norm(a.quote), _norm(b.quote)
    if len(qa) >= MIN_QUOTE_MATCH_CHARS and len(qb) >= MIN_QUOTE_MATCH_CHARS and (qa == qb or qa in qb or qb in qa):
        return True                                            # same words quoted
    if a.timestamp_start is not None and b.timestamp_start is not None:
        a_end = a.timestamp_end if a.timestamp_end is not None else a.timestamp_start
        b_end = b.timestamp_end if b.timestamp_end is not None else b.timestamp_start
        if max(a.timestamp_start, b.timestamp_start) - min(a_end, b_end) <= EVENT_WINDOW_SECONDS:
            return True                                        # same moment
    return a.name == b.name and not _has_location(a) and not _has_location(b)


def _sort_key(sig: EvidenceSignal):
    return (sig.category, sig.name, _norm(sig.evidence), _norm(sig.quote),
            -1.0 if sig.timestamp_start is None else sig.timestamp_start,
            sig.severity, sig.reliability)


def _group_signals(signals: list[EvidenceSignal]) -> list[_Group]:
    """Connected components of ``_same_event`` within each category (order-independent)."""
    ordered = sorted(signals, key=_sort_key)
    parent = list(range(len(ordered)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(ordered):
        for j in range(i + 1, len(ordered)):
            b = ordered[j]
            if a.category == b.category and _same_event(a, b):
                parent[find(j)] = find(i)

    members: dict[int, list[EvidenceSignal]] = {}
    for i, sig in enumerate(ordered):
        members.setdefault(find(i), []).append(sig)

    groups = []
    for sigs in members.values():
        best = max(sigs, key=signal_impact)                    # ties: first in sorted order
        groups.append(_Group(best.category, best, signal_impact(best), len(sigs)))
    return groups


# ----------------------------------------------------------------------------- aggregation
def _category_totals(groups: list[_Group]) -> dict[str, float]:
    by_category: dict[str, list[float]] = {}
    for g in groups:
        by_category.setdefault(g.category, []).append(g.impact)
    totals = {}
    for category in sorted(by_category):
        impacts = sorted(by_category[category], reverse=True)
        decayed = sum(impact * DECAY ** k for k, impact in enumerate(impacts))
        totals[category] = min(decayed, min(1.0, REPEAT_CAP_MULTIPLIER * CATEGORY_WEIGHTS[category]))
    return totals


def _combine(groups: list[_Group]) -> float:
    remaining = 1.0
    for total in _category_totals(groups).values():
        remaining *= 1.0 - total
    return 1.0 - remaining


def _to_score(raw: float) -> int:
    return max(0, min(MAX_SCORE, math.floor(raw * 100 + 0.5)))


def _confidence(groups: list[_Group], coverage: str) -> float | None:
    # Only evidence aimed at the recipient supports a risk assessment. Signals that merely discuss or illustrate a scam
    # must not raise confidence (a news article about OTP scams is not "95% confident" of anything).
    groups = [g for g in groups if CONTEXT_FACTORS[g.representative.context] >= 0.5]
    total = sum(g.impact for g in groups)
    if not groups or total <= 0:
        return None
    mean_reliability = sum(g.impact * RELIABILITY_FACTORS[g.representative.reliability] for g in groups) / total
    # Corroboration = DIFFERENT kinds of evidence. Repeating the same kind never raises confidence.
    corroboration = CORROBORATION_FACTORS[min(len({g.category for g in groups}), 3)]
    factor = 1.0 if coverage == "full" else PARTIAL_COVERAGE_FACTOR
    return round(min(CONFIDENCE_CAP, mean_reliability * corroboration * factor), 2)


def _unable(coverage: str) -> RiskAssessment:
    return RiskAssessment(
        status=AnalysisStatus.UNABLE_TO_ASSESS, verdict=Verdict.UNABLE_TO_ASSESS, risk_score=None,
        confidence=None, reason_code=ReasonCode.INSUFFICIENT_EVIDENCE, coverage=coverage,
        signals=(), signal_scores=None, dominant_categories=(),
    )


# ----------------------------------------------------------------------------- public entry point
def assess(report: EvidenceReport) -> RiskAssessment:
    """Turn validated evidence into the final assessment. Pure and deterministic."""
    coverage = report.coverage
    if coverage == "insufficient":
        return _unable(coverage)

    groups = _group_signals(list(report.signals))
    score = _to_score(_combine(groups))

    if coverage == "partial" and score <= LOW_RISK_MAX:
        return _unable(coverage)           # part of the content was not understood: cannot clear it

    totals = _category_totals(groups)
    scored = tuple(sorted(
        (ScoredSignal(g.representative, round(g.impact, 3), g.size - 1) for g in groups),
        key=lambda s: (-s.impact, s.signal.category, s.signal.name),
    ))
    return RiskAssessment(
        status=AnalysisStatus.COMPLETED,
        verdict=verdict_for_score(score),
        risk_score=score,
        confidence=_confidence(groups, coverage),
        reason_code=None,
        coverage=coverage,
        signals=scored,
        signal_scores={vector: _to_score(_combine([g for g in groups if g.category in cats]))
                       for vector, cats in VECTOR_CATEGORIES.items()},
        dominant_categories=tuple(sorted(totals, key=lambda c: (-totals[c], c))),
    )
