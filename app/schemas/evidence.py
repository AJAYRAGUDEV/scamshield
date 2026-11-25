"""Evidence schema: what the AI provider reports and what the risk engine consumes.

The provider reports OBSERVATIONS (signals), the language it read, its own limitations and a self-assessed
coverage. It never supplies a risk score, a verdict, a probability or a recommendation: those are computed by the
server (``app/services/risk_engine.py``). Extra keys a provider might add (``risk_score``, ``verdict``...) are
ignored and can never reach the engine.

Context (Phase 7). Every signal states how the indicator appears in the content (``Context``): an instruction or
request aimed at the recipient, mere discussion/description of a scam (news, education, a question about what an
OTP is), or a fictional/example conversation. The engine counts only a small fraction of non-directed signals, so a
news article about OTP scams is not scored like an OTP demand. The model decides the context; the server applies it.

Video (Phase 8) reuses this schema unchanged apart from one extra source (``video_text``: text visible on screen).
``validate_report_for_mode`` decides which sources/categories are legal for each analysis mode.

This module is also the ``response_schema`` sent to Gemini, so it sticks to constructs the SDK converts to Gemini
schemas (Literal -> enum, Optional -> nullable, bounded numbers) and does not use ``maxItems`` / ``maxLength``, which
caused INVALID_ARGUMENT before; limits are enforced by validators. Fields that have a safe default for Python
validation (``context``, ``language``...) are still marked REQUIRED in the schema Gemini sees, so the model always
fills them in, while a response that omits one is tolerated rather than discarded.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SignalCategory = Literal[
    "credential_request",         # asks for OTP / password / PIN / card or account details
    "payment_request",            # asks for money, gift cards, crypto, a transfer
    "payment_redirection",        # new/changed bank details, "send it to this other account"
    "advance_fee_or_prize",       # advance fee, unexpected prize/lottery, fake refund, too-good investment promise
    "remote_access_request",      # asks to install a remote-access app / share screen / grant device or account access
    "authority_impersonation",    # claims to be police, a bank officer, a government body...
    "organization_impersonation",  # claims to be a delivery service, customer support, a company or its employee
    "impersonation_relationship",  # claims to be a relative, boss, colleague, known contact
    "threat_coercion",            # threats: arrest, account closure, harm, legal action
    "suspicious_link",            # link/URL the recipient is pushed to open
    "isolation_secrecy",          # "don't tell anyone", "stay on the call", "don't contact the bank"
    "urgency_pressure",           # artificial deadline / time pressure
    "synthetic_voice_indicator",  # audible cues possibly consistent with synthetic speech (never proof)
    "scripted_delivery",          # reads like a script / robocall
    "visual_manipulation",        # (video) observable editing/sync inconsistencies; a possible indicator, never proof
    "fake_interface",             # (video) fake payment/login/bank/government screen
    "other_suspicious",
]
EvidenceSource = Literal["text_content", "audio_speech", "audio_acoustic", "video_visual", "video_text", "video_audio"]
Severity = Literal["low", "medium", "high", "critical"]
Reliability = Literal["weak", "moderate", "strong"]
Coverage = Literal["full", "partial", "insufficient"]
Context = Literal["directed_at_recipient", "discussed_or_described", "fictional_or_example", "unclear"]
Mode = Literal["text", "audio", "video"]

DIRECTED: Context = "directed_at_recipient"

MAX_SIGNALS = 40
MAX_EVIDENCE_CHARS = 500
MAX_QUOTE_CHARS = 300
MAX_TIMESTAMP_SECONDS = 86_400.0
MAX_LANGUAGE_CHARS = 40
MAX_LIMITATIONS = 8
MAX_LIMITATION_CHARS = 200

# Strict numbers: "12" (a string) and True are rejected rather than coerced; NaN/inf are rejected.
Timestamp = Annotated[float, Field(strict=True, ge=0, le=MAX_TIMESTAMP_SECONDS, allow_inf_nan=False)]

_NAME_CLEAN = re.compile(r"[^a-z0-9]+")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _require(*names: str):
    """JSON-schema hook: mark fields as required for Gemini although Python validation has a default."""

    def hook(schema: dict[str, Any]) -> None:
        required = schema.setdefault("required", [])
        for name in names:
            if name not in required:
                required.append(name)

    return hook


class EvidenceSignal(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True, json_schema_extra=_require("context", "quote"))

    category: SignalCategory = Field(description="The kind of scam indicator.")
    name: str = Field(description="Short snake_case identifier, e.g. otp_request.")
    evidence: str = Field(description="One plain-English sentence: what the content does and why that is a concern.")
    source: EvidenceSource = Field(description="Where the evidence comes from.")
    context: Context = Field(
        default=DIRECTED,
        description="directed_at_recipient = an instruction/request/claim aimed at the reader or listener; "
        "discussed_or_described = a scam is only talked ABOUT (news, education, a question, a warning); "
        "fictional_or_example = a story, script or example; unclear = cannot tell.",
    )
    quote: str | None = Field(default=None, description="Short verbatim excerpt in the ORIGINAL language; null if none.")
    timestamp_start: Timestamp | None = Field(default=None, description="Seconds from the start. Only when reliably known; never guess.")
    timestamp_end: Timestamp | None = Field(default=None, description="Seconds from the start. Only when reliably known; never guess.")
    severity: Severity = Field(description="How serious the behaviour would be if genuine.")
    reliability: Reliability = Field(description="How clearly the input supports it: weak = ambiguous or inferred.")

    @field_validator("name", mode="before")
    @classmethod
    def _normalise_name(cls, value: object) -> object:
        if isinstance(value, str):
            value = _NAME_CLEAN.sub("_", value.strip().lower()).strip("_")
        return value

    @field_validator("name")
    @classmethod
    def _name_shape(cls, value: str) -> str:
        if not 2 <= len(value) <= 64:
            raise ValueError("name must be 2-64 characters after normalisation")
        return value

    @field_validator("evidence")
    @classmethod
    def _evidence_text(cls, value: str) -> str:
        value = _CONTROL.sub(" ", value).strip()
        if not value:
            raise ValueError("evidence description must not be empty")
        return _clip(value, MAX_EVIDENCE_CHARS)

    @field_validator("quote")
    @classmethod
    def _quote_text(cls, value: str | None) -> str | None:
        if not value:
            return None
        value = _CONTROL.sub(" ", value).strip()
        return _clip(value, MAX_QUOTE_CHARS) if value else None

    @model_validator(mode="after")
    def _timestamps_are_consistent(self) -> "EvidenceSignal":
        if self.timestamp_end is not None:
            if self.timestamp_start is None:
                raise ValueError("timestamp_end requires timestamp_start")
            if self.timestamp_end < self.timestamp_start:
                raise ValueError("timestamp_end must not be before timestamp_start")
        return self


class EvidenceReport(BaseModel):
    model_config = ConfigDict(extra="ignore", json_schema_extra=_require("language", "language_uncertain", "limitations"))

    coverage: Coverage = Field(
        description="full = the whole content was understood; partial = part of it was unintelligible, "
        "truncated or unassessable; insufficient = too little understood to judge (including silence or no speech)."
    )
    language: str | None = Field(
        default=None,
        description="Main language(s) of the content, in English, e.g. 'English', 'Tamil', 'Hindi', 'Tamil and English (mixed)'. "
        "null if you cannot tell.",
    )
    language_uncertain: bool = Field(
        default=False, description="true if you are not confident you interpreted the language(s) correctly."
    )
    limitations: list[str] = Field(
        default_factory=list,
        description="Short plain-English notes about what limited the analysis (unclear audio, unfamiliar dialect, "
        "cut-off text...). Empty if nothing.",
    )
    signals: list[EvidenceSignal]

    @field_validator("language")
    @classmethod
    def _language_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = _CONTROL.sub(" ", value).strip()
        return _clip(value, MAX_LANGUAGE_CHARS) if value else None

    @field_validator("limitations", mode="before")
    @classmethod
    def _limitations_text(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, list):
            cleaned = []
            for item in value:
                if isinstance(item, str):
                    text = _CONTROL.sub(" ", item).strip()
                    if text:
                        cleaned.append(_clip(text, MAX_LIMITATION_CHARS))
            return cleaned[:MAX_LIMITATIONS]
        return value

    @field_validator("signals")
    @classmethod
    def _bounded(cls, signals: list[EvidenceSignal]) -> list[EvidenceSignal]:
        if len(signals) > MAX_SIGNALS:
            raise ValueError(f"no more than {MAX_SIGNALS} signals are accepted")
        return signals


# Which evidence sources / categories are legal per analysis mode. Video will add its own row.
_LEGAL_SOURCES: dict[str, frozenset[str]] = {
    "text": frozenset({"text_content"}),
    "audio": frozenset({"audio_speech", "audio_acoustic"}),
    # video_visual = what is SEEN, video_text = text visible on screen, video_audio = what is HEARD (speech and sound)
    "video": frozenset({"video_visual", "video_text", "video_audio"}),
}
_VIDEO_ONLY_CATEGORIES = frozenset({"visual_manipulation", "fake_interface"})


def validate_report_for_mode(report: EvidenceReport, mode: str) -> None:
    """Raise ``ValueError`` if the evidence is impossible for this analysis mode."""
    legal = _LEGAL_SOURCES[mode]
    for sig in report.signals:
        if sig.source not in legal:
            raise ValueError(f"source {sig.source!r} is not valid for {mode} analysis")
        if sig.category in _VIDEO_ONLY_CATEGORIES and mode != "video":
            raise ValueError(f"category {sig.category!r} is not valid for {mode} analysis")
        if mode == "text" and (sig.timestamp_start is not None or sig.timestamp_end is not None):
            raise ValueError("text evidence cannot carry timestamps")
