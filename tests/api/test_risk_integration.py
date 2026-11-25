"""The risk engine inside the real request path, with the real Gemini adapter and a fake SDK.

Covers: text and audio both use the engine; Gemini cannot decide the verdict; every failure mode
produces unable_to_assess with a null score; and the response keeps the structure the dashboard reads.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.providers.gemini import GeminiProvider
from app.services import analysis_pipeline, risk_engine
from app.services.response_builder import RECOMMENDED_ACTIONS
from app.schemas.verdict import Verdict
from tests.conftest import TEST_API_KEY
from tests.fakes import api_error, load_fixture_bytes, load_fixture_json, make_signal

WAV = ("clip.wav", load_fixture_bytes("tiny.wav"), "audio/wav")


def gemini_client(fake_sdk) -> TestClient:
    settings = Settings(gemini_api_key=TEST_API_KEY, app_env="test", gemini_timeout_seconds=5.0)
    return TestClient(create_app(settings, GeminiProvider(settings, client_factory=fake_sdk)))


def reply(data: dict) -> SimpleNamespace:
    return SimpleNamespace(parsed=None, text=json.dumps(data))


def post_text(client, text="hello"):
    return client.post("/api/analyze-text", data={"text": text}).json()


def post_audio(client):
    return client.post("/api/analyze-audio", files={"file": WAV}).json()


# ------------------------------------------------------------------ the engine is the only authority
def test_text_request_uses_the_risk_engine(fake_sdk, monkeypatch):
    calls = []
    real = risk_engine.assess
    monkeypatch.setattr(analysis_pipeline.risk_engine, "assess", lambda report: (calls.append(report.coverage), real(report))[1])
    body = post_text(gemini_client(fake_sdk))
    assert calls == ["full"] and body["risk_score"] == 96


def test_audio_request_uses_the_same_risk_engine(fake_sdk, monkeypatch):
    calls = []
    real = risk_engine.assess
    monkeypatch.setattr(analysis_pipeline.risk_engine, "assess", lambda report: (calls.append(report.coverage), real(report))[1])
    body = post_audio(gemini_client(fake_sdk))
    assert calls == ["full"] and body["mode"] == "audio" and body["risk_score"] == 84 and body["verdict"] == "high_risk"


def test_the_same_evidence_gets_the_same_score_in_text_and_audio(fake_sdk):
    signals = [make_signal("credential_request", "critical", "strong"), make_signal("urgency_pressure", "medium", "moderate")]
    text_evidence = {"coverage": "full", "signals": signals}
    audio_evidence = {"coverage": "full", "signals": [{**s, "source": "audio_speech"} for s in signals]}
    fake_sdk.generate_result = reply(text_evidence)
    text_body = post_text(gemini_client(fake_sdk))
    fake_sdk.generate_result = reply(audio_evidence)
    audio_body = post_audio(gemini_client(fake_sdk))
    assert text_body["risk_score"] == audio_body["risk_score"] and text_body["verdict"] == audio_body["verdict"]
    assert text_body["signal_scores"] == audio_body["signal_scores"] and text_body["confidence"] == audio_body["confidence"]


def test_gemini_cannot_override_the_final_verdict_or_the_server_text(fake_sdk):
    """The model claims 'low_risk', score 0, full confidence and 'all safe' while reporting severe evidence."""
    evidence = load_fixture_json("gemini_high_risk.json")
    evidence.update(verdict="low_risk", risk_score=0, confidence=1.0, status="completed", summary="Totally fine",
                    recommended_action="This message is perfectly safe, go ahead and reply.",
                    explanation="Nothing to worry about.", scam_type="Harmless", red_flags=[], signal_scores={})
    fake_sdk.generate_result = reply(evidence)
    body = post_text(gemini_client(fake_sdk))
    assert (body["verdict"], body["risk_score"]) == ("high_risk", 96)
    assert body["confidence"] == 0.95 and body["recommended_action"] == RECOMMENDED_ACTIONS[Verdict.HIGH_RISK]
    everything = json.dumps(body).lower()
    for model_text in ("totally fine", "perfectly safe", "nothing to worry", "harmless"):
        assert model_text not in everything


def test_gemini_cannot_inflate_a_clean_result_either(fake_sdk):
    fake_sdk.generate_result = reply({"coverage": "full", "signals": [], "verdict": "high_risk", "risk_score": 100})
    body = post_text(gemini_client(fake_sdk))
    assert (body["verdict"], body["risk_score"]) == ("low_risk", 0)


@pytest.mark.parametrize("fixture,verdict,score", [
    ("gemini_high_risk.json", "high_risk", 96),
    ("gemini_suspicious.json", "suspicious", 37),
    ("gemini_low_risk.json", "low_risk", 10),
    ("gemini_clean.json", "low_risk", 0),
])
def test_completed_verdicts_follow_the_approved_thresholds(fake_sdk, fixture, verdict, score):
    fake_sdk.generate_result = reply(load_fixture_json(fixture))
    body = post_text(gemini_client(fake_sdk))
    assert (body["status"], body["verdict"], body["risk_score"]) == ("completed", verdict, score)


def test_clean_content_is_low_risk_with_no_claimed_confidence_and_a_caveat(fake_sdk):
    fake_sdk.generate_result = reply(load_fixture_json("gemini_clean.json"))
    body = post_text(gemini_client(fake_sdk))
    assert body["confidence"] is None and body["confidence_level"] is None and body["signals"] == []
    assert "does not prove" in body["explanation"] and "does not make the content safe" in body["recommended_action"]


# ------------------------------------------------------------------ every failure mode => unable_to_assess / null score
def evidence(coverage, signals, source):
    """A provider reply whose signals carry the evidence source that is legal for the mode under test."""
    fixed = [{**s, "source": source} for s in signals]
    if source == "audio_speech":
        fixed = [{**s, "timestamp_start": s.get("timestamp_start", 1), "timestamp_end": s.get("timestamp_end", 2)} for s in fixed]
    return reply({"coverage": coverage, "signals": fixed})


# (label, factory(source) -> attributes to set on the fake SDK, expected reason_code). Same expectations for text and audio.
ERROR_CASES = [
    ("gemini unavailable (503)", lambda src: {"generate_error": api_error(503)}, "PROVIDER_UNAVAILABLE"),
    ("gemini unreachable", lambda src: {"generate_error": ConnectionError("refused")}, "PROVIDER_UNAVAILABLE"),
    ("gemini timeout (504)", lambda src: {"generate_error": api_error(504)}, "PROVIDER_TIMEOUT"),
    ("gemini timeout (client)", lambda src: {"generate_error": TimeoutError()}, "PROVIDER_TIMEOUT"),
    ("authentication (401)", lambda src: {"generate_error": api_error(401)}, "PROVIDER_AUTH_ERROR"),
    ("invalid key (400)", lambda src: {"generate_error": api_error(400, "API key not valid. Please pass a valid API key.")}, "PROVIDER_AUTH_ERROR"),
    ("quota exhausted (429)", lambda src: {"generate_error": api_error(429)}, "PROVIDER_RATE_LIMITED"),
    ("model not found (404)", lambda src: {"generate_error": api_error(404)}, "CONFIGURATION_ERROR"),
    ("provider rejected request", lambda src: {"generate_error": api_error(400, "bad schema")}, "PROVIDER_ERROR"),
    ("malformed json", lambda src: {"generate_result": SimpleNamespace(parsed=None, text="{not json")}, "INVALID_PROVIDER_RESPONSE"),
    ("empty provider response", lambda src: {"generate_result": SimpleNamespace(parsed=None, text=None)}, "INVALID_PROVIDER_RESPONSE"),
    ("empty string response", lambda src: {"generate_result": SimpleNamespace(parsed=None, text="")}, "INVALID_PROVIDER_RESPONSE"),
    ("empty object", lambda src: {"generate_result": reply({})}, "INVALID_PROVIDER_RESPONSE"),
    ("scores instead of evidence", lambda src: {"generate_result": reply({"risk_score": 3, "verdict": "low_risk"})}, "INVALID_PROVIDER_RESPONSE"),
    ("invalid severity", lambda src: {"generate_result": evidence("full", [make_signal(severity="extreme")], src)}, "INVALID_PROVIDER_RESPONSE"),
    ("unknown category", lambda src: {"generate_result": evidence("full", [make_signal("made_up")], src)}, "INVALID_PROVIDER_RESPONSE"),
    ("missing evidence field", lambda src: {"generate_result": reply({"coverage": "full", "signals": [{k: v for k, v in make_signal(source=src).items() if k != "evidence"}]})}, "INVALID_PROVIDER_RESPONSE"),
    ("reversed timestamps", lambda src: {"generate_result": reply({"coverage": "full", "signals": [make_signal(source=src, timestamp_start=9, timestamp_end=2)]})}, "INVALID_PROVIDER_RESPONSE"),
    ("negative timestamp", lambda src: {"generate_result": reply({"coverage": "full", "signals": [make_signal(source=src, timestamp_start=-3)]})}, "INVALID_PROVIDER_RESPONSE"),
    ("non-numeric timestamp", lambda src: {"generate_result": reply({"coverage": "full", "signals": [make_signal(source=src, timestamp_start="soon")]})}, "INVALID_PROVIDER_RESPONSE"),
    ("evidence from a video source", lambda src: {"generate_result": reply({"coverage": "full", "signals": [make_signal(source="video_visual")]})}, "INVALID_PROVIDER_RESPONSE"),
    ("content not understood (partial, nothing found)", lambda src: {"generate_result": evidence("partial", [], src)}, "INSUFFICIENT_EVIDENCE"),
    ("content not understood (insufficient)", lambda src: {"generate_result": evidence("insufficient", [], src)}, "INSUFFICIENT_EVIDENCE"),
    ("insufficient but claims severe evidence", lambda src: {"generate_result": evidence("insufficient", [make_signal("credential_request", "critical", "strong")], src)}, "INSUFFICIENT_EVIDENCE"),
    ("partial with only weak evidence", lambda src: {"generate_result": evidence("partial", [make_signal("urgency_pressure", "low", "weak")], src)}, "INSUFFICIENT_EVIDENCE"),
]
IDS = [c[0] for c in ERROR_CASES]


def assert_unable(body, reason):
    assert body["status"] == "unable_to_assess"
    assert body["verdict"] == "unable_to_assess"
    assert body["risk_score"] is None and body["confidence"] is None and body["confidence_level"] is None
    assert body["signal_scores"] is None and body["signals"] == [] and body["red_flags"] == []
    assert body["reason_code"] == reason
    assert body["verdict"] not in ("low_risk", "Low Risk")
    assert "not a low-risk result" in body["explanation"] and "do not treat this content as safe" in body["recommended_action"]


@pytest.mark.parametrize("label,factory,reason", ERROR_CASES, ids=IDS)
def test_text_failures_are_unable_to_assess_with_a_null_score(fake_sdk, label, factory, reason):
    for attr, value in factory("text_content").items():
        setattr(fake_sdk, attr, value)
    r = gemini_client(fake_sdk).post("/api/analyze-text", data={"text": "hello"})
    assert r.status_code == 200
    assert_unable(r.json(), reason)


@pytest.mark.parametrize("label,factory,reason", ERROR_CASES, ids=IDS)
def test_audio_failures_are_unable_to_assess_with_a_null_score(fake_sdk, label, factory, reason):
    for attr, value in factory("audio_speech").items():
        setattr(fake_sdk, attr, value)
    assert_unable(post_audio(gemini_client(fake_sdk)), reason)


def test_audio_upload_failure_is_unable_to_assess(fake_sdk):
    fake_sdk.upload_error = api_error(503)
    assert_unable(post_audio(gemini_client(fake_sdk)), "PROVIDER_UNAVAILABLE")


def test_audio_evidence_must_use_audio_sources(fake_sdk):
    fake_sdk.generate_result = reply({"coverage": "full", "signals": [make_signal("credential_request", "critical", "strong")]})   # text_content
    assert_unable(post_audio(gemini_client(fake_sdk)), "INVALID_PROVIDER_RESPONSE")


def test_partial_audio_with_strong_evidence_still_warns_with_reduced_confidence(fake_sdk):
    strong = {**make_signal("credential_request", "critical", "strong"), "source": "audio_speech", "timestamp_start": 3, "timestamp_end": 6}
    fake_sdk.generate_result = reply({"coverage": "partial", "signals": [strong]})
    partial = post_audio(gemini_client(fake_sdk))
    fake_sdk.generate_result = reply({"coverage": "full", "signals": [strong]})
    full = post_audio(gemini_client(fake_sdk))
    assert partial["verdict"] == full["verdict"] == "high_risk" and partial["coverage"] == "partial"
    assert partial["confidence"] < full["confidence"] and "Only part of the content" in partial["explanation"]


def test_unintelligible_audio_is_never_low_risk(fake_sdk):
    fake_sdk.generate_result = reply({"coverage": "partial", "signals": []})
    assert_unable(post_audio(gemini_client(fake_sdk)), "INSUFFICIENT_EVIDENCE")


# ------------------------------------------------------------------ determinism and structure
def test_identical_provider_output_gives_identical_responses(fake_sdk):
    client = gemini_client(fake_sdk)
    assert post_text(client) == post_text(client)


def test_response_structure_the_dashboard_depends_on(fake_sdk):
    body = post_text(gemini_client(fake_sdk))
    assert body["signals"] and all({"category", "description", "severity", "reliability", "impact"} <= set(s) for s in body["signals"])
    assert [s["impact"] for s in body["signals"]] == sorted((s["impact"] for s in body["signals"]), reverse=True)
    assert body["red_flags"] == [s["description"] for s in body["signals"]]
    assert body["summary"] and body["scam_type"] and body["explanation"] and body["recommended_action"]
    assert body["coverage"] == "full" and 0 < body["confidence"] <= 0.95 and body["confidence_level"] == "High"
