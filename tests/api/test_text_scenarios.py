"""Phase 7 text scenarios through the REAL Gemini adapter + risk engine, with the model's answer controlled by a
fixture. These prove the SERVER's handling (validation, scoring, context, URL merging); whether the real model
produces these answers cannot be tested offline."""
from __future__ import annotations

import pytest

from tests.scenario_helpers import analyze_text, scenario_client


def run(fake_sdk, upload_dir, fixture, text="a message", **kw):
    return analyze_text(scenario_client(fake_sdk, upload_dir, fixture, **kw), text)


def categories(body):
    return {s["category"] for s in body["signals"]}


def test_bank_impersonation_is_high_risk_with_excerpts(fake_sdk, upload_dir):
    body = run(fake_sdk, upload_dir, "gemini_text_bank_impersonation.json")
    assert (body["verdict"], body["status"]) == ("high_risk", "completed") and body["risk_score"] == 88
    assert {"authority_impersonation", "credential_request", "urgency_pressure"} <= categories(body)
    assert any(s["quote"] == "reply with the OTP you just received" for s in body["signals"])
    assert all(s["why_it_matters"] and s["context"] == "directed_at_recipient" for s in body["signals"])
    assert body["language"] == "English" and body["limitations"] == []


def test_government_impersonation_with_threat_and_payment(fake_sdk, upload_dir):
    body = run(fake_sdk, upload_dir, "gemini_text_government_impersonation.json")
    assert body["verdict"] == "high_risk" and {"threat_coercion", "payment_request", "authority_impersonation"} <= categories(body)


def test_otp_request_alone_is_high_risk_but_not_maximal(fake_sdk, upload_dir):
    body = run(fake_sdk, upload_dir, "gemini_text_otp_request.json")
    assert body["verdict"] == "high_risk" and 60 <= body["risk_score"] < 100


def test_fake_prize_is_suspicious_not_high(fake_sdk, upload_dir):
    body = run(fake_sdk, upload_dir, "gemini_text_fake_prize.json")
    assert body["verdict"] == "suspicious" and "advance_fee_or_prize" in categories(body)


def test_payment_redirection_is_flagged(fake_sdk, upload_dir):
    body = run(fake_sdk, upload_dir, "gemini_text_payment_redirection.json")
    assert body["verdict"] == "high_risk" and "payment_redirection" in categories(body)


def test_remote_access_request_is_flagged(fake_sdk, upload_dir):
    body = run(fake_sdk, upload_dir, "gemini_text_remote_access.json")
    assert body["verdict"] == "high_risk" and {"remote_access_request", "organization_impersonation"} <= categories(body)


# ---- context-aware: describing a scam is not the same as being sent one -------------------------------------
@pytest.mark.parametrize("fixture", ["gemini_text_otp_discussion.json", "gemini_text_legit_notification.json"])
def test_harmless_otp_discussion_and_legitimate_notification_are_low_risk(fake_sdk, upload_dir, fixture):
    body = run(fake_sdk, upload_dir, fixture, "Your OTP is 123456. Never share it with anyone.")
    assert body["verdict"] == "low_risk" and body["risk_score"] == 0 and body["signals"] == []


def test_news_article_describing_scams_is_not_flagged_as_a_scam(fake_sdk, upload_dir):
    body = run(fake_sdk, upload_dir, "gemini_text_news_article.json")
    assert body["verdict"] == "low_risk" and body["risk_score"] < 25
    assert {s["context"] for s in body["signals"]} == {"discussed_or_described"}
    assert body["confidence"] is None                       # no directed evidence, so no confidence is claimed


def test_fictional_scam_scene_is_not_flagged(fake_sdk, upload_dir):
    body = run(fake_sdk, upload_dir, "gemini_text_fictional_scam.json")
    assert body["verdict"] == "low_risk" and {s["context"] for s in body["signals"]} == {"fictional_or_example"}


def test_the_same_evidence_scores_far_higher_when_directed_at_the_reader(fake_sdk, upload_dir):
    described = run(fake_sdk, upload_dir, "gemini_text_news_article.json")
    directed = run(fake_sdk, upload_dir, "gemini_text_bank_impersonation.json")
    assert directed["risk_score"] >= described["risk_score"] + 50


# ---- URLs: structure only, merged into the risk engine ---------------------------------------------------------
def test_lookalike_link_adds_server_evidence_and_findings(fake_sdk, upload_dir):
    text = "Verify now: http://paypa1-secure-login.example/verify?next=https://evil.example"
    body = run(fake_sdk, upload_dir, "gemini_text_suspicious_url.json", text)
    assert body["urls"] and all(u["reputation_verified"] is False for u in body["urls"])
    assert any("not been verified" in u["note"] for u in body["urls"])
    assert "suspicious_link" in categories(body)
    assert any("link" in note.lower() and "verified" in note.lower() for note in body["limitations"])


def test_link_facts_reach_the_model_and_the_link_is_never_fetched(fake_sdk, upload_dir):
    run(fake_sdk, upload_dir, "gemini_text_suspicious_url.json", "see http://192.168.0.1/login now")
    prompt = fake_sdk.instances[-1].generate_calls[0]["contents"][0]
    assert "192.168.0.1" in prompt and "structure" in prompt.lower()


def test_a_plain_shortened_link_is_informational_not_scored(fake_sdk, upload_dir):
    body = run(fake_sdk, upload_dir, "gemini_text_otp_discussion.json", "Details here: https://bit.ly/abc123")
    assert body["urls"] and "shortened_url" in body["urls"][0]["indicators"]
    assert body["verdict"] == "low_risk" and body["signals"] == []


def test_a_message_without_links_has_no_url_findings(fake_sdk, upload_dir):
    assert run(fake_sdk, upload_dir, "gemini_text_bank_impersonation.json")["urls"] == []


def test_link_evidence_inside_discussion_is_discounted_but_not_ignored(fake_sdk, upload_dir):
    """A look-alike link is still shown and scored, but at 'unclear' strength when the model found nothing directed."""
    text = "Scammers use http://paypa1-secure-login.example links."
    news = run(fake_sdk, upload_dir, "gemini_text_news_article.json", text)
    directed = run(fake_sdk, upload_dir, "gemini_text_suspicious_url.json", text)
    link = next(s for s in news["signals"] if s["category"] == "suspicious_link")
    assert link["context"] == "unclear" and news["urls"]
    assert news["verdict"] != "high_risk" and news["risk_score"] < directed["risk_score"]
