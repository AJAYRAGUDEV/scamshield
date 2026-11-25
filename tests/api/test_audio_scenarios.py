"""Phase 7 audio scenarios: real adapter + risk engine + audio inspection, model answer controlled by fixtures."""
from __future__ import annotations

from app.core.errors import ReasonCode
from tests import media_samples as ms
from tests.fakes import api_error
from tests.scenario_helpers import analyze_audio, make_wav, provider_calls, scenario_client


def post(fake_sdk, upload_dir, fixture, content=None, **kw):
    client = scenario_client(fake_sdk, upload_dir, fixture, **kw)
    response = analyze_audio(client, content if content is not None else make_wav(40.0))
    return response.status_code, response.json()


def categories(body):
    return {s["category"] for s in body["signals"]}


def leftovers(upload_dir):
    return list(upload_dir.rglob("*")) if upload_dir.exists() else []


def test_spoken_otp_request_is_high_risk_with_transcript_excerpt_and_time(fake_sdk, upload_dir):
    code, body = post(fake_sdk, upload_dir, "gemini_audio_spoken_otp.json")
    assert code == 200 and (body["verdict"], body["risk_score"]) == ("high_risk", 84)
    otp = next(s for s in body["signals"] if s["category"] == "credential_request")
    assert otp["quote"] == "read me the six digit code" and (otp["timestamp_start"], otp["timestamp_end"]) == (12.0, 15.5)
    assert otp["source"] == "audio_speech"


def test_fake_authority_with_threat(fake_sdk, upload_dir):
    _, body = post(fake_sdk, upload_dir, "gemini_audio_fake_authority.json")
    assert body["verdict"] == "high_risk" and {"authority_impersonation", "threat_coercion"} <= categories(body)


def test_payment_pressure_with_secrecy(fake_sdk, upload_dir):
    _, body = post(fake_sdk, upload_dir, "gemini_audio_payment_pressure.json")
    assert body["verdict"] == "high_risk" and {"payment_request", "urgency_pressure", "isolation_secrecy"} <= categories(body)


def test_remote_access_install_request(fake_sdk, upload_dir):
    _, body = post(fake_sdk, upload_dir, "gemini_audio_remote_access.json")
    assert body["verdict"] == "high_risk" and categories(body) == {"remote_access_request"}


def test_harmless_call_is_low_risk(fake_sdk, upload_dir):
    _, body = post(fake_sdk, upload_dir, "gemini_audio_harmless.json")
    assert (body["verdict"], body["risk_score"], body["signals"]) == ("low_risk", 0, [])


def test_unclear_audio_is_unable_to_assess_never_low_risk(fake_sdk, upload_dir):
    _, body = post(fake_sdk, upload_dir, "gemini_audio_unclear.json")
    assert (body["status"], body["verdict"], body["risk_score"]) == ("unable_to_assess", "unable_to_assess", None)
    assert body["signals"] == []


def test_no_speech_from_the_model_is_unable_to_assess(fake_sdk, upload_dir):
    _, body = post(fake_sdk, upload_dir, "gemini_audio_no_speech.json")
    assert body["verdict"] == "unable_to_assess" and body["risk_score"] is None


def test_silent_recording_is_refused_without_calling_the_model(fake_sdk, upload_dir):
    """Silence is absence of evidence, not safety: it is never scored, and Gemini is not even asked."""
    code, body = post(fake_sdk, upload_dir, "gemini_audio_harmless.json", make_wav(3.0, silent=True))
    assert code == 200 and (body["verdict"], body["risk_score"], body["reason_code"]) == ("unable_to_assess", None, "INSUFFICIENT_EVIDENCE")
    assert provider_calls(fake_sdk) == 0 and leftovers(upload_dir) == []


def test_empty_upload_is_rejected_and_the_model_not_called(fake_sdk, upload_dir):
    client = scenario_client(fake_sdk, upload_dir, "gemini_audio_harmless.json")
    response = analyze_audio(client, b"")
    assert response.status_code in (400, 415, 422) and provider_calls(fake_sdk) == 0


def test_unsupported_format_is_rejected_and_the_model_not_called(fake_sdk, upload_dir):
    client = scenario_client(fake_sdk, upload_dir, "gemini_audio_harmless.json")
    response = analyze_audio(client, ms.DISGUISED["plain_text"], name="notes.wav", mime="audio/wav")
    assert response.status_code == 415 and response.json()["code"] == "UNSUPPORTED_MEDIA_TYPE" and provider_calls(fake_sdk) == 0


def test_provider_timeout_is_unable_to_assess(fake_sdk, upload_dir):
    fake_sdk.generate_delay = 0.5
    client = scenario_client(fake_sdk, upload_dir, "gemini_audio_spoken_otp.json", gemini_timeout_seconds=0.05)
    body = analyze_audio(client, make_wav(5.0)).json()
    assert (body["verdict"], body["risk_score"], body["reason_code"]) == ("unable_to_assess", None, ReasonCode.PROVIDER_TIMEOUT.value)
    assert leftovers(upload_dir) == []


def test_provider_failure_is_unable_to_assess(fake_sdk, upload_dir):
    client = scenario_client(fake_sdk, upload_dir, "gemini_audio_spoken_otp.json")
    fake_sdk.generate_error = api_error(503)
    body = analyze_audio(client, make_wav(5.0)).json()
    assert (body["verdict"], body["risk_score"], body["reason_code"]) == ("unable_to_assess", None, "PROVIDER_UNAVAILABLE")


# ---- timestamps are reported only when they can be trusted ------------------------------------------------------
def test_timestamps_beyond_the_recording_are_removed_and_disclosed(fake_sdk, upload_dir):
    _, body = post(fake_sdk, upload_dir, "gemini_audio_timestamps_outside_recording.json", make_wav(2.0))
    by_cat = {s["category"]: s for s in body["signals"]}
    assert by_cat["credential_request"]["timestamp_start"] is None and by_cat["credential_request"]["timestamp_end"] is None
    assert by_cat["urgency_pressure"]["timestamp_start"] == 0.05            # inside the recording: kept
    assert any("timestamps were removed" in n for n in body["limitations"])


def test_no_timestamps_are_invented(fake_sdk, upload_dir):
    _, body = post(fake_sdk, upload_dir, "gemini_audio_no_timestamps.json", make_wav(5.0))
    assert all(s["timestamp_start"] is None and s["timestamp_end"] is None for s in body["signals"])
    assert not any("imestamps are approximate" in n for n in body["limitations"])


def test_audio_results_never_claim_speaker_identity_or_synthetic_proof(fake_sdk, upload_dir):
    _, body = post(fake_sdk, upload_dir, "gemini_audio_spoken_otp.json")
    note = next(n for n in body["limitations"] if "does not identify" in n)
    assert "synthetic" in note and "who is speaking" in note
    text = " ".join([body["summary"], body["explanation"], body["recommended_action"]]).lower()
    assert "deepfake" not in text and "voice clone" not in text and "authentic" not in text


def test_the_upload_is_deleted_after_analysis(fake_sdk, upload_dir):
    post(fake_sdk, upload_dir, "gemini_audio_spoken_otp.json")
    assert leftovers(upload_dir) == [] and fake_sdk.instances[-1].deleted == ["files/fake-123"]
