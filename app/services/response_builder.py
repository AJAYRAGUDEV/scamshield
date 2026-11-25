"""Builds the public response for a COMPLETED assessment.

All user-facing wording here is deterministic and derived from the risk engine's result, so it can
never contradict the score or verdict (provider-written prose could). Wording is deliberately
calibrated: it describes indicators, never certainty, and never claims safety or proof.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas.analysis import AnalysisResponse, ResponseSignal, SignalScores, UrlFinding
from app.schemas.evidence import Mode
from app.schemas.verdict import AnalysisStatus, Verdict
from app.services.risk_engine import RiskAssessment, confidence_level_for

CAVEAT = ("This is a risk assessment of the supplied content only; it does not prove that the content "
          "or its sender is genuine or fraudulent.")

CATEGORY_PHRASES: dict[str, str] = {
    "credential_request": "a request for codes, passwords or account details",
    "payment_request": "a request for money",
    "payment_redirection": "a request to redirect a payment",
    "authority_impersonation": "a claim of authority",
    "organization_impersonation": "a claim to be a company, delivery or support service",
    "remote_access_request": "a request for remote access to your device or account",
    "advance_fee_or_prize": "a prize, refund or fee-based promise",
    "impersonation_relationship": "a claim to be someone the recipient knows",
    "threat_coercion": "threats or coercion",
    "suspicious_link": "a suspicious link",
    "isolation_secrecy": "pressure to keep it secret",
    "urgency_pressure": "urgency pressure",
    "synthetic_voice_indicator": "audible cues that may indicate synthetic speech (not proof)",
    "scripted_delivery": "scripted delivery",
    "visual_manipulation": "signs of visual manipulation",
    "fake_interface": "an apparently fake interface",
    "other_suspicious": "other suspicious indicators",
}

# One server-written sentence per category: WHY the indicator matters. Shown with each piece of evidence.
WHY_IT_MATTERS: dict[str, str] = {
    "credential_request": "Legitimate banks and services do not ask you to share codes, passwords or PINs.",
    "payment_request": "Unexpected requests for money, gift cards or crypto are a common way scammers get paid.",
    "payment_redirection": "Being told to pay a different account than expected is a common sign of payment fraud.",
    "advance_fee_or_prize": "Real prizes and refunds do not require you to pay a fee or hand over payment details first.",
    "remote_access_request": "Giving someone remote access lets them see and control your device and your accounts.",
    "authority_impersonation": "Scammers pretend to be officials so that you comply without checking.",
    "organization_impersonation": "Scammers pose as delivery, support or company staff to gain your trust.",
    "impersonation_relationship": "Scammers pose as someone you know to ask for help or money.",
    "threat_coercion": "Threats are used to frighten people into acting before they can stop and verify.",
    "suspicious_link": "Links in scam messages often lead to fake sites that steal logins or install harmful software.",
    "isolation_secrecy": "Scammers ask for secrecy so that family, friends or your bank cannot warn you.",
    "urgency_pressure": "Artificial deadlines are used to stop you from checking whether the request is real.",
    "synthetic_voice_indicator": "This is only a weak hint: it does not prove that the voice is synthetic.",
    "scripted_delivery": "Scripted delivery alone is not suspicious; many genuine calls are scripted.",
    "visual_manipulation": "Signs of manipulation are never proof on their own.",
    "fake_interface": "Fake payment or login screens are used to capture money or credentials.",
    "other_suspicious": "This behaviour is unusual enough to be worth verifying through an official channel.",
}
_NOT_DIRECTED_NOTE = " Counted only lightly: the content seems to discuss or illustrate this rather than ask for it."


@dataclass(frozen=True)
class ResponseExtras:
    """Everything besides the risk assessment that the response reports (all produced or checked by the server)."""
    language: str | None = None
    language_uncertain: bool = False
    limitations: list[str] = field(default_factory=list)
    urls: list[UrlFinding] = field(default_factory=list)


def _why(signal) -> str:
    text = WHY_IT_MATTERS.get(signal.category, "")
    if signal.context in ("discussed_or_described", "fictional_or_example"):
        text += _NOT_DIRECTED_NOTE
    return text


RECOMMENDED_ACTIONS: dict[Verdict, str] = {
    Verdict.HIGH_RISK: (
        "Do not send money, share codes or passwords, or open links. End the contact and verify "
        "independently, using an official number or app that you look up yourself."
    ),
    Verdict.SUSPICIOUS: (
        "Pause and verify the request independently through an official channel before taking any action."
    ),
    Verdict.LOW_RISK: (
        "No strong scam indicators were found, but that does not make the content safe. Stay cautious with "
        "unexpected requests and verify through an official channel if anything feels wrong."
    ),
}

_LEAD = {
    Verdict.HIGH_RISK: "High risk: strong scam indicators were detected",
    Verdict.SUSPICIOUS: "Suspicious: some scam indicators were detected, but they are not conclusive",
    Verdict.LOW_RISK: "Low risk: no strong scam indicators were detected",
}


def _top_phrases(assessment: RiskAssessment, limit: int = 3) -> list[str]:
    return [CATEGORY_PHRASES[c] for c in assessment.dominant_categories[:limit]]


def _summary(assessment: RiskAssessment) -> str:
    lead = _LEAD[assessment.verdict]
    phrases = _top_phrases(assessment)
    if phrases and assessment.verdict == Verdict.LOW_RISK:
        return f"{lead} (minor indicators noted: {'; '.join(phrases)})."
    if phrases:
        return f"{lead} ({'; '.join(phrases)})."
    return f"{lead}; no scam indicators were identified in the content that was analysed."


def _scam_type(assessment: RiskAssessment) -> str:
    if assessment.verdict == Verdict.LOW_RISK:
        return "No strong scam indicators identified" if assessment.signals else "No scam indicators identified"
    phrase = CATEGORY_PHRASES[assessment.dominant_categories[0]]
    return f"Possible scam pattern: {phrase}"


def _explanation(assessment: RiskAssessment, summary: str) -> str:
    parts = [summary]
    evidence = [s.signal.evidence.rstrip(".") for s in assessment.signals[:3]]
    if evidence:
        parts.append("Key evidence: " + "; ".join(evidence) + ".")
    if assessment.coverage == "partial":
        parts.append("Only part of the content could be understood, so this assessment is less certain.")
    parts.append(CAVEAT)
    return " ".join(parts)


def build_completed_response(assessment: RiskAssessment, mode: Mode, extras: ResponseExtras | None = None) -> AnalysisResponse:
    if assessment.status != AnalysisStatus.COMPLETED:
        raise ValueError("only a completed assessment can be turned into a completed response")
    summary = _summary(assessment)
    extras = extras or ResponseExtras()
    return AnalysisResponse(
        status=AnalysisStatus.COMPLETED,
        verdict=assessment.verdict,
        risk_score=assessment.risk_score,
        confidence=assessment.confidence,
        confidence_level=confidence_level_for(assessment.confidence),
        reason_code=None,
        coverage=assessment.coverage,
        summary=summary,
        scam_type=_scam_type(assessment),
        explanation=_explanation(assessment, summary),
        recommended_action=RECOMMENDED_ACTIONS[assessment.verdict],
        signals=[
            ResponseSignal(
                category=s.signal.category, name=s.signal.name, description=s.signal.evidence,
                why_it_matters=_why(s.signal), context=s.signal.context,
                source=s.signal.source, quote=s.signal.quote,
                timestamp_start=s.signal.timestamp_start, timestamp_end=s.signal.timestamp_end,
                severity=s.signal.severity, reliability=s.signal.reliability,
                impact=s.impact, corroborating_signals=s.corroborating,
            )
            for s in assessment.signals
        ],
        red_flags=[s.signal.evidence for s in assessment.signals[:20]],
        signal_scores=SignalScores(**assessment.signal_scores),
        mode=mode,
        offline_fallback_used=False,
        language=extras.language,
        language_uncertain=extras.language_uncertain,
        limitations=list(extras.limitations),
        urls=list(extras.urls),
    )
