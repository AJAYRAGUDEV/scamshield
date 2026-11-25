"""Canonical verdict values, analysis status and the ONE definition of the risk thresholds.

Thresholds (approved, never to be changed without approval):

    0-24  -> low_risk
    25-59 -> suspicious
    60-100 -> high_risk

``unable_to_assess`` is not a risk level. It means no valid analysis was completed, and it never
carries a score. Display labels ("Low Risk", ...) belong to the frontend; the backend only ever emits
the canonical values below.
"""
from __future__ import annotations

from enum import StrEnum

LOW_RISK_MAX = 24
SUSPICIOUS_MAX = 59
MAX_SCORE = 100


class AnalysisStatus(StrEnum):
    COMPLETED = "completed"
    UNABLE_TO_ASSESS = "unable_to_assess"


class Verdict(StrEnum):
    LOW_RISK = "low_risk"
    SUSPICIOUS = "suspicious"
    HIGH_RISK = "high_risk"
    UNABLE_TO_ASSESS = "unable_to_assess"


RISK_VERDICTS = (Verdict.LOW_RISK, Verdict.SUSPICIOUS, Verdict.HIGH_RISK)


def verdict_for_score(score: int) -> Verdict:
    """Map a completed analysis' score to its verdict. Raises for anything that is not an int 0-100."""
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= MAX_SCORE:
        raise ValueError(f"risk score must be an integer from 0 to {MAX_SCORE}, got {score!r}")
    if score <= LOW_RISK_MAX:
        return Verdict.LOW_RISK
    if score <= SUSPICIOUS_MAX:
        return Verdict.SUSPICIOUS
    return Verdict.HIGH_RISK
