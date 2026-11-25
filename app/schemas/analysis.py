"""Public API response schema (one structure for text, audio and, later, video).

``status`` separates "was an analysis completed?" from "how risky is the content?":

* ``completed``        -> ``verdict`` is low_risk / suspicious / high_risk, ``risk_score`` is an int 0-100
                          and the verdict is exactly ``verdict_for_score(risk_score)``.
* ``unable_to_assess`` -> ``verdict`` is unable_to_assess, ``risk_score`` / ``confidence`` /
                          ``signal_scores`` are ``null``, ``signals`` is empty and ``reason_code`` says why.
                          This is NEVER a low-risk result.

Every authoritative field (status, verdict, score, confidence, text) is produced by server code
(risk engine + response builder), never copied from the AI provider.

Compatibility: the field names the existing dashboard reads (``scam_type``, ``confidence_level``,
``signal_scores``, ``red_flags``, ``explanation``, ``recommended_action``, ``mode``,
``offline_fallback_used``) are kept. ``offline_fallback_used`` is always false; the server has no
offline fallback any more.
"""
from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.errors import REASON_MESSAGES, SAFE_NEXT_STEP, ReasonCode
from app.schemas.evidence import Context, EvidenceSource, Mode, Reliability, Severity, SignalCategory
from app.schemas.verdict import RISK_VERDICTS, AnalysisStatus, Verdict, verdict_for_score

ConfidenceLevel = Literal["Low", "Medium", "High"]
ResponseCoverage = Literal["full", "partial"]


class SignalScores(BaseModel):
    """The four dashboard vectors (0-100). Display only: they never influence the verdict."""
    authority_impersonation: int = Field(ge=0, le=100)
    urgency_isolation: int = Field(ge=0, le=100)
    credential_payment_trap: int = Field(ge=0, le=100)
    synthetic_or_script_signals: int = Field(ge=0, le=100)


class UrlFinding(BaseModel):
    """What ScamShield could tell about one link from its STRUCTURE alone. The link was never visited, its content
    was never downloaded and its reputation was never checked, which ``reputation_verified`` states explicitly."""
    display: str                      # scheme://host (path, query and any credentials in the URL are NOT shown)
    domain: str
    indicators: list[str]
    note: str
    reputation_verified: Literal[False] = False


class ResponseSignal(BaseModel):
    """One counted piece of evidence (events merged by the engine are reported as corroborating)."""
    category: SignalCategory
    name: str
    description: str
    why_it_matters: str = ""          # server-written, one sentence
    context: Context = "directed_at_recipient"
    source: EvidenceSource
    quote: str | None = None
    timestamp_start: float | None = None
    timestamp_end: float | None = None
    severity: Severity
    reliability: Reliability
    impact: float = Field(ge=0, le=1)
    corroborating_signals: int = Field(ge=0)


class AnalysisResponse(BaseModel):
    status: AnalysisStatus
    verdict: Verdict
    risk_score: int | None = Field(ge=0, le=100)
    confidence: float | None = Field(ge=0, le=1)
    confidence_level: ConfidenceLevel | None
    reason_code: ReasonCode | None
    coverage: ResponseCoverage | None
    summary: str
    scam_type: str
    explanation: str
    recommended_action: str
    signals: list[ResponseSignal]
    red_flags: list[str]
    signal_scores: SignalScores | None
    mode: Mode
    offline_fallback_used: bool = False
    language: str | None = None
    language_uncertain: bool = False
    limitations: list[str] = Field(default_factory=list)
    urls: list[UrlFinding] = Field(default_factory=list)

    @field_validator("red_flags")
    @classmethod
    def _cap_red_flags(cls, flags: list[str]) -> list[str]:
        if len(flags) > 20:
            raise ValueError("red_flags must contain no more than 20 items")
        return flags

    @model_validator(mode="after")
    def _status_invariants(self) -> Self:
        if self.status == AnalysisStatus.COMPLETED:
            if self.verdict not in RISK_VERDICTS:
                raise ValueError("a completed analysis needs a low_risk / suspicious / high_risk verdict")
            if self.risk_score is None or self.reason_code is not None or self.signal_scores is None:
                raise ValueError("a completed analysis needs a score and signal_scores, and no reason_code")
            if self.verdict != verdict_for_score(self.risk_score):
                raise ValueError("verdict does not match the approved score thresholds")
            if self.coverage is None:
                raise ValueError("a completed analysis must state its coverage")
        else:
            if self.verdict != Verdict.UNABLE_TO_ASSESS:
                raise ValueError("an analysis that is unable_to_assess must have the unable_to_assess verdict")
            if (self.risk_score is not None or self.confidence is not None or self.confidence_level is not None
                    or self.signal_scores is not None or self.signals or self.red_flags or self.urls or self.limitations):
                raise ValueError("unable_to_assess must not carry a score, confidence, signals or findings")
            if self.reason_code is None:
                raise ValueError("unable_to_assess requires a reason_code")
        return self

    @classmethod
    def unable_to_assess(cls, mode: Mode, reason_code: ReasonCode) -> Self:
        """The ONLY representation of an analysis that could not be completed."""
        return cls(
            status=AnalysisStatus.UNABLE_TO_ASSESS,
            verdict=Verdict.UNABLE_TO_ASSESS,
            risk_score=None,
            confidence=None,
            confidence_level=None,
            reason_code=reason_code,
            coverage=None,
            summary="The content could not be assessed.",
            scam_type="Unable to assess",
            explanation=f"{REASON_MESSAGES[reason_code]} No risk score was produced; this is not a low-risk result.",
            recommended_action=SAFE_NEXT_STEP,
            signals=[],
            red_flags=[],
            signal_scores=None,
            mode=mode,
            offline_fallback_used=False,
        )
