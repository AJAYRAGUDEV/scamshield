"""Strict validation of provider evidence (untrusted input)."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.schemas.evidence import (
    MAX_EVIDENCE_CHARS, MAX_QUOTE_CHARS, MAX_SIGNALS, EvidenceReport, EvidenceSignal, validate_report_for_mode,
)
from app.schemas.provider_output import ProviderAnalysis
from tests.fakes import make_report, make_signal


def sig(**over) -> dict:
    return {**make_signal("credential_request"), **over}


# ------------------------------------------------------------------ rejected input
@pytest.mark.parametrize("over", [
    {"category": "scammy"}, {"category": ""}, {"category": None},
    {"severity": "extreme"}, {"severity": "HIGH"}, {"severity": 3}, {"severity": None},
    {"reliability": "certain"}, {"reliability": 0.9}, {"reliability": None},
    {"source": "telepathy"}, {"source": None},
    {"evidence": ""}, {"evidence": "   "}, {"evidence": None}, {"evidence": 42},
    {"name": ""}, {"name": "!!"}, {"name": "x"}, {"name": "n" * 65}, {"name": None},
])
def test_malformed_signal_fields_are_rejected(over):
    with pytest.raises(ValidationError):
        EvidenceSignal(**sig(**over))


@pytest.mark.parametrize("missing", ["category", "name", "evidence", "source", "severity", "reliability"])
def test_missing_required_fields_are_rejected(missing):
    data = sig()
    del data[missing]
    with pytest.raises(ValidationError):
        EvidenceSignal(**data)


@pytest.mark.parametrize("over", [
    {"timestamp_start": -0.1},
    {"timestamp_start": 86_400.1},
    {"timestamp_start": "12"},                       # strings are not coerced
    {"timestamp_start": True},                       # nor booleans
    {"timestamp_start": float("nan")},
    {"timestamp_start": float("inf")},
    {"timestamp_start": 5.0, "timestamp_end": 4.9},  # end before start
    {"timestamp_end": 5.0},                          # end without start
    {"timestamp_start": 5.0, "timestamp_end": -1.0},
    {"timestamp_start": 5.0, "timestamp_end": "9"},
])
def test_invalid_timestamps_are_rejected(over):
    with pytest.raises(ValidationError):
        EvidenceSignal(**sig(**over))


@pytest.mark.parametrize("over", [{"coverage": "complete"}, {"coverage": ""}, {"coverage": None}, {"signals": None}, {"signals": "x"}])
def test_malformed_report_fields_are_rejected(over):
    with pytest.raises(ValidationError):
        EvidenceReport(**{"coverage": "full", "signals": [], **over})


def test_missing_coverage_is_rejected_so_empty_output_is_never_read_as_safe():
    with pytest.raises(ValidationError):
        EvidenceReport(signals=[])
    with pytest.raises(ValidationError):
        EvidenceReport.model_validate_json("{}")


def test_too_many_signals_are_rejected():
    make_report([make_signal(name=f"n{i}") for i in range(MAX_SIGNALS)])
    with pytest.raises(ValidationError):
        make_report([make_signal(name=f"n{i}") for i in range(MAX_SIGNALS + 1)])


# ------------------------------------------------------------------ accepted / normalised
def test_valid_timestamps_are_accepted_including_integers_and_points_in_time():
    assert EvidenceSignal(**sig(timestamp_start=12, timestamp_end=16)).timestamp_end == 16.0
    assert EvidenceSignal(**sig(timestamp_start=0.0)).timestamp_end is None
    assert EvidenceSignal(**sig(timestamp_start=5.0, timestamp_end=5.0)).timestamp_start == 5.0
    assert EvidenceSignal(**sig(timestamp_start=86_400.0)).timestamp_start == 86_400.0


def test_integer_timestamps_survive_json_parsing_too():
    report = EvidenceReport.model_validate_json(json.dumps({"coverage": "full", "signals": [sig(timestamp_start=3, timestamp_end=4)]}))
    assert report.signals[0].timestamp_start == 3.0


@pytest.mark.parametrize("raw,clean", [("OTP Request!", "otp_request"), ("  Bank-Officer Claim ", "bank_officer_claim"), ("already_ok", "already_ok")])
def test_names_are_normalised_to_snake_case(raw, clean):
    assert EvidenceSignal(**sig(name=raw)).name == clean


def test_long_text_is_clipped_and_empty_quotes_become_none():
    parsed = EvidenceSignal(**sig(evidence="e" * 5000, quote="q" * 5000))
    assert len(parsed.evidence) <= MAX_EVIDENCE_CHARS and parsed.evidence.endswith("…")
    assert len(parsed.quote) <= MAX_QUOTE_CHARS
    assert EvidenceSignal(**sig(quote="")).quote is None and EvidenceSignal(**sig(quote="   ")).quote is None


def test_unknown_and_authoritative_keys_are_ignored_not_trusted():
    report = ProviderAnalysis(**{"coverage": "full", "signals": [], "risk_score": 0, "verdict": "low_risk",
                                 "confidence": 1.0, "summary": "safe", "recommended_action": "ignore it"})
    assert set(report.model_dump()) == {"coverage", "signals", "language", "language_uncertain", "limitations"}


# ------------------------------------------------------------------ mode legality
def report_with(**over):
    return make_report([sig(**over)])


def test_text_mode_accepts_text_evidence_only():
    validate_report_for_mode(report_with(), "text")
    for over in ({"source": "audio_speech"}, {"source": "video_visual"}, {"timestamp_start": 1.0}):
        with pytest.raises(ValueError):
            validate_report_for_mode(report_with(**over), "text")


def test_audio_mode_accepts_audio_evidence_with_timestamps():
    validate_report_for_mode(report_with(source="audio_speech", timestamp_start=1.0, timestamp_end=2.0), "audio")
    validate_report_for_mode(report_with(source="audio_acoustic"), "audio")
    for over in ({"source": "text_content"}, {"source": "video_audio"}):
        with pytest.raises(ValueError):
            validate_report_for_mode(report_with(**over), "audio")


@pytest.mark.parametrize("mode,source", [("text", "text_content"), ("audio", "audio_speech")])
def test_future_video_categories_are_not_legal_yet(mode, source):
    for category in ("visual_manipulation", "fake_interface"):
        with pytest.raises(ValueError):
            validate_report_for_mode(make_report([make_signal(category, source=source)]), mode)


# ------------------------------------------------------------------ the schema that is sent to Gemini
def test_gemini_schema_has_enums_and_avoids_limits_that_broke_gemini_before():
    schema = ProviderAnalysis.model_json_schema()
    text = json.dumps(schema)
    for banned in ("maxItems", "minItems", "maxLength", "minLength"):
        assert banned not in text
    signal = schema["$defs"]["EvidenceSignal"]["properties"]
    assert "credential_request" in signal["category"]["enum"] and "critical" in signal["severity"]["enum"]
    assert set(schema["properties"]) == {"coverage", "signals", "language", "language_uncertain", "limitations"}   # evidence only
    assert not {"risk_score", "verdict", "confidence", "status"} & set(schema["properties"])
