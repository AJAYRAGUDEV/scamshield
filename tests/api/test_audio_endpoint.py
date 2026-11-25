"""POST /api/analyze-audio (legacy path) and /api/v1/analyze/audio."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import AnalysisError, ReasonCode
from app.main import create_app
from app.providers.gemini import GeminiProvider
from app.services import audio_service
from tests import media_samples as ms
from tests.fakes import FakeProvider, FakeSDKClient, api_error

PATHS = ["/api/analyze-audio", "/api/v1/analyze/audio"]
WAV = ms.wav()
UNSUPPORTED = {"detail": "Unsupported audio format. Use MP3, WAV, M4A, OGG, or WEBM.", "code": "UNSUPPORTED_MEDIA_TYPE"}


def post_audio(client, path, name="clip.wav", content=WAV, mime="audio/wav", **data):
    return client.post(path, files={"file": (name, content, mime)}, data=data)


def leftovers(upload_dir: Path) -> list[Path]:
    return list(upload_dir.rglob("*")) if upload_dir.exists() else []


@pytest.mark.parametrize("path", PATHS)
def test_valid_mocked_analysis(client, provider, path):
    r = post_audio(client, path)
    body = r.json()
    assert r.status_code == 200 and body["mode"] == "audio" and body["status"] == "completed"
    assert (body["verdict"], body["risk_score"]) == ("high_risk", 84)
    assert body["offline_fallback_used"] is False and body["reason_code"] is None
    assert {sig["source"] for sig in body["signals"]} <= {"audio_speech", "audio_acoustic"}
    assert any(sig["timestamp_start"] is not None for sig in body["signals"])
    assert provider.audio_bytes == [WAV]


@pytest.mark.parametrize("path", PATHS)
def test_missing_file_is_422_with_a_stable_code(client, provider, path):
    r = client.post(path, data={})
    assert r.status_code == 422 and r.json()["code"] == "INVALID_REQUEST" and provider.audio_paths == []


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("name,mime", [("a.exe", "audio/wav"), ("a.wav", "text/plain"), ("noext", "audio/wav"),
                                       ("a.mp4", "video/mp4"), ("a.wav.exe", "audio/wav"), ("a.mp3", "audio/wav")])
def test_unsupported_or_spoofed_files_are_415_and_never_reach_the_provider(client, provider, upload_dir, path, name, mime):
    r = post_audio(client, path, name=name, mime=mime)
    assert r.status_code == 415 and r.json()["code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert provider.audio_paths == [] and leftovers(upload_dir) == []


def test_the_unsupported_format_message_is_unchanged(client):
    assert post_audio(client, PATHS[0], name="a.exe").json() == UNSUPPORTED


@pytest.mark.parametrize("ext", sorted(ms.AUDIO_SAMPLES))
@pytest.mark.parametrize("declared", ["matching", "application/octet-stream"])
def test_every_documented_type_is_accepted_with_valid_content(client, provider, ext, declared):
    mime = ms.AUDIO_MIME[ext] if declared == "matching" else declared
    assert post_audio(client, PATHS[0], name=f"x{ext}", content=ms.AUDIO_SAMPLES[ext](), mime=mime).status_code == 200


def test_mime_parameters_and_case_are_tolerated(client):
    assert post_audio(client, PATHS[0], mime="Audio/WAV; codecs=1").status_code == 200


@pytest.mark.parametrize("path", PATHS)
def test_empty_file_is_400(client, provider, upload_dir, path):
    r = post_audio(client, path, content=b"")
    assert r.status_code == 400 and r.json() == {"detail": "Audio file is empty.", "code": "INVALID_INPUT"}
    assert provider.audio_paths == [] and leftovers(upload_dir) == []


def test_oversize_is_413_and_uses_the_configured_limit(provider, upload_dir):
    app = create_app(Settings(gemini_api_key="k", max_audio_size_mb=1, temp_upload_dir=str(upload_dir)), provider)
    c = TestClient(app)
    ok = post_audio(c, PATHS[0], content=ms.wav() + b"\x00" * (1024 * 1024 - len(ms.wav())))
    big = post_audio(c, PATHS[0], content=ms.wav() + b"\x00" * (1024 * 1024))
    assert ok.status_code == 200
    assert big.status_code == 413 and big.json()["code"] == "PAYLOAD_TOO_LARGE"
    assert len(provider.audio_paths) == 1 and leftovers(upload_dir) == []


# ---- content, not claims
@pytest.mark.parametrize("name", sorted(ms.DISGUISED))
def test_disguised_files_are_rejected_whatever_they_claim_to_be(client, provider, upload_dir, name):
    r = post_audio(client, PATHS[0], name="voicemail.mp3", content=ms.DISGUISED[name], mime="audio/mpeg")
    assert r.status_code == 415 and r.json()["code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert provider.audio_paths == [] and leftovers(upload_dir) == []


def test_octet_stream_with_non_audio_content_is_now_rejected(client, provider):
    """Closes audit finding P6: the declared type can no longer stand in for a content check."""
    r = post_audio(client, PATHS[0], name="a.mp3", content=b"not audio at all, just text", mime="application/octet-stream")
    assert r.status_code == 415 and provider.audio_paths == []
    r = post_audio(client, PATHS[0], name="a.webm", content=b"not video either, really", mime="video/webm")
    assert r.status_code == 415 and provider.audio_paths == []


@pytest.mark.parametrize("ext,wrong", [(".wav", ms.mp3_id3), (".mp3", ms.wav), (".ogg", ms.webm), (".m4a", ms.ogg)])
def test_content_of_a_different_format_than_the_extension_is_rejected(client, provider, ext, wrong):
    assert post_audio(client, PATHS[0], name=f"x{ext}", content=wrong(), mime=ms.AUDIO_MIME[ext]).status_code == 415
    assert provider.audio_paths == []


@pytest.mark.parametrize("filename", ["../../etc/passwd.wav", "..\\..\\windows\\a.wav", "dir/clip.wav"])
def test_path_traversal_filenames_are_rejected(client, provider, upload_dir, filename):
    r = post_audio(client, PATHS[0], name=filename)
    assert r.status_code in (400, 415) and provider.audio_paths == [] and leftovers(upload_dir) == []


# ---- temporary files
def test_temp_file_exists_during_analysis_and_everything_is_removed_after(client, provider, upload_dir):
    post_audio(client, PATHS[0])
    path = provider.audio_paths[0]
    assert provider.audio_existed == [True] and not path.exists() and leftovers(upload_dir) == []
    assert upload_dir in path.parents


def test_client_filename_is_not_used_for_the_temp_path(client, provider):
    post_audio(client, PATHS[0], name="secret name.wav")
    path = provider.audio_paths[0]
    assert "secret" not in str(path) and path.name.endswith(".wav") and len(path.name) == 36


@pytest.mark.parametrize("error", [AnalysisError(ReasonCode.PROVIDER_UNAVAILABLE), RuntimeError("boom")])
def test_temp_file_removed_after_provider_failure(settings, upload_dir, error):
    provider = FakeProvider(error=error)
    r = post_audio(TestClient(create_app(settings, provider)), PATHS[0])
    assert r.json()["verdict"] == "unable_to_assess" and leftovers(upload_dir) == []


def test_temp_file_removed_after_timeout_through_the_real_adapter(fake_sdk, upload_dir):
    fake_sdk.generate_delay = 2.0
    settings = Settings(gemini_api_key="k", gemini_timeout_seconds=0.05, temp_upload_dir=str(upload_dir))
    seen: list[str] = []
    real_upload = FakeSDKClient._upload

    async def spy(self, *, file, config=None):
        seen.append(file)
        return await real_upload(self, file=file, config=config)

    FakeSDKClient._upload = spy
    try:
        app = create_app(settings, GeminiProvider(settings, client_factory=fake_sdk))
        body = post_audio(TestClient(app), PATHS[0]).json()
    finally:
        FakeSDKClient._upload = real_upload
    assert body["reason_code"] == "PROVIDER_TIMEOUT"
    assert seen and not Path(seen[0]).exists() and leftovers(upload_dir) == []
    assert fake_sdk.instances[-1].deleted == ["files/fake-123"]     # remote copy removed too


def test_upload_write_failure_is_media_processing_failed(client, monkeypatch, upload_dir):
    from app.utils import temp_files

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(temp_files, "_open_exclusive", broken)
    body = post_audio(client, PATHS[0]).json()
    assert (body["verdict"], body["reason_code"]) == ("unable_to_assess", "MEDIA_PROCESSING_FAILED")
    assert leftovers(upload_dir) == []


def test_documented_extension_list_matches_the_allowlist():
    assert audio_service.ALLOWED_EXTENSIONS == frozenset({".mp3", ".wav", ".m4a", ".ogg", ".webm"})


# ---- through the real adapter
def test_provider_failure_through_the_real_adapter(fake_sdk, upload_dir):
    fake_sdk.generate_error = api_error(503)
    settings = Settings(gemini_api_key="k", temp_upload_dir=str(upload_dir), gemini_max_retries=0)
    app = create_app(settings, GeminiProvider(settings, client_factory=fake_sdk))
    body = post_audio(TestClient(app), PATHS[0]).json()
    assert (body["status"], body["verdict"], body["risk_score"], body["reason_code"]) == ("unable_to_assess", "unable_to_assess", None, "PROVIDER_UNAVAILABLE")
    assert fake_sdk.instances[-1].deleted == ["files/fake-123"] and leftovers(upload_dir) == []


def test_missing_key_for_audio_is_a_configuration_result(fake_sdk, upload_dir):
    settings = Settings(gemini_api_key="", temp_upload_dir=str(upload_dir))
    app = create_app(settings, GeminiProvider(settings, client_factory=fake_sdk))
    body = post_audio(TestClient(app), PATHS[0]).json()
    assert body["reason_code"] == "CONFIGURATION_ERROR" and fake_sdk.instances == [] and leftovers(upload_dir) == []
