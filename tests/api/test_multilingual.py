"""Phase 7 multilingual scenarios. Original-language excerpts must survive untouched; findings are explained in
English; uncertainty is reported. No test here claims equal accuracy across languages."""
from __future__ import annotations

from tests.fakes import load_fixture_json
from tests.scenario_helpers import analyze_audio, analyze_text, make_wav, scenario_client


def text_result(fake_sdk, upload_dir, fixture, text="message"):
    return analyze_text(scenario_client(fake_sdk, upload_dir, fixture), text)


def test_tamil_scam_keeps_tamil_excerpts_and_english_explanations(fake_sdk, upload_dir):
    body = text_result(fake_sdk, upload_dir, "gemini_tamil_scam.json", "உடனே OTP பகிருங்கள்")
    assert body["verdict"] == "high_risk" and body["language"] == "Tamil"
    quotes = {s["quote"] for s in body["signals"]}
    assert "உடனே OTP பகிருங்கள்" in quotes and "உங்கள் வங்கி கணக்கு முடக்கப்படும்" in quotes     # exact, not normalised
    assert all(s["description"].isascii() for s in body["signals"])                              # explained in English


def test_mixed_tamil_english_is_handled(fake_sdk, upload_dir):
    body = text_result(fake_sdk, upload_dir, "gemini_tamil_english_mixed.json")
    assert body["verdict"] == "high_risk" and "mixed" in body["language"]
    assert "OTP share பண்ணுங்க" in {s["quote"] for s in body["signals"]}


def test_hindi_scam(fake_sdk, upload_dir):
    body = text_result(fake_sdk, upload_dir, "gemini_hindi_scam.json")
    assert body["verdict"] == "high_risk" and body["language"] == "Hindi"
    assert "तुरंत OTP बताइए" in {s["quote"] for s in body["signals"]}


def test_harmless_multilingual_message_is_low_risk(fake_sdk, upload_dir):
    body = text_result(fake_sdk, upload_dir, "gemini_multilingual_harmless.json", "நாளை சந்திப்போம்")
    assert (body["verdict"], body["risk_score"], body["signals"]) == ("low_risk", 0, [])


def test_non_english_results_carry_a_reliability_caveat(fake_sdk, upload_dir):
    body = text_result(fake_sdk, upload_dir, "gemini_hindi_scam.json")
    assert body["limitations"], "a non-English analysis must say it may be less reliable than an English one"
    assert any("language" in note.lower() or "english" in note.lower() for note in body["limitations"])


def test_uncertain_language_is_reported_and_never_treated_as_safe(fake_sdk, upload_dir):
    body = text_result(fake_sdk, upload_dir, "gemini_language_uncertain.json")
    assert body["verdict"] != "low_risk"
    if body["verdict"] == "unable_to_assess":
        assert body["risk_score"] is None
    else:
        assert body["language_uncertain"] is True and body["limitations"]


def test_tamil_audio_keeps_original_script_and_time(fake_sdk, upload_dir):
    client = scenario_client(fake_sdk, upload_dir, "gemini_audio_tamil_scam.json")
    body = analyze_audio(client, make_wav(20.0)).json()
    otp = next(s for s in body["signals"] if s["category"] == "credential_request")
    assert otp["quote"] == "உங்கள் OTP சொல்லுங்கள்" and otp["timestamp_start"] == 6.0 and body["language"] == "Tamil"


def test_duplicate_tamil_quotes_are_grouped_not_double_counted(fake_sdk, upload_dir):
    once = load_fixture_json("gemini_tamil_scam.json")
    twice = {**once, "signals": [once["signals"][0], {**once["signals"][0], "name": "otp_again"}]}
    single = {**once, "signals": [once["signals"][0]]}
    a = analyze_text(scenario_client(fake_sdk, upload_dir, twice), "x")
    b = analyze_text(scenario_client(fake_sdk, upload_dir, single), "x")
    assert a["risk_score"] == b["risk_score"]
