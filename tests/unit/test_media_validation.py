"""Upload validation: name, extension, declared type and CONTENT. Nothing the client says is trusted."""
from __future__ import annotations

import pytest

from app.core.errors import InvalidInput, UnsupportedMedia
from app.services import media_validation as mv
from tests import media_samples as ms


# ------------------------------------------------------------------ content detection
@pytest.mark.parametrize("sample,kind", [
    (ms.wav, "wav"), (ms.mp3_id3, "mp3"), (ms.mp3_frame, "mp3"), (ms.ogg, "ogg"), (ms.webm, "webm"),
    (ms.m4a, "mp4"), (ms.mp4, "mp4"), (ms.mov, "mov"),
])
def test_supported_containers_are_recognised(sample, kind):
    assert mv.detect_kind(sample()) == kind


@pytest.mark.parametrize("name", sorted(ms.DISGUISED))
def test_disguised_and_corrupt_files_are_never_recognised(name):
    assert mv.detect_kind(ms.DISGUISED[name]) is None


def test_matroska_is_not_webm():
    assert mv.detect_kind(ms.matroska()) is None


@pytest.mark.parametrize("head", [b"", b"R", b"RIFF", b"\xff\xfb", b"\x00" * 11, b"OggS"])
def test_truncated_input_is_not_recognised_and_never_raises(head):
    assert mv.detect_kind(head) is None


def test_adts_aac_and_reserved_mpeg_headers_are_not_mp3():
    assert mv.detect_kind(b"\xff\xf1\x50\x80" + ms.PAD) is None          # ADTS AAC (layer bits 00)
    assert mv.detect_kind(b"\xff\xe9\x90\x00" + ms.PAD) is None          # reserved MPEG version
    assert mv.detect_kind(b"\xff\xfb\xf0\x00" + ms.PAD) is None          # invalid bitrate index


# ------------------------------------------------------------------ audio: extension / declared type / content must agree
@pytest.mark.parametrize("ext", sorted(ms.AUDIO_SAMPLES))
def test_each_audio_type_passes_with_matching_name_type_and_content(ext):
    suffix = mv.validate_audio_metadata(f"clip{ext}", ms.AUDIO_MIME[ext])
    assert suffix == ext and mv.check_audio_content(ms.AUDIO_SAMPLES[ext](), suffix)


@pytest.mark.parametrize("ext", sorted(ms.AUDIO_SAMPLES))
def test_unknown_declared_type_is_tolerated_only_because_content_decides(ext):
    for declared in ("application/octet-stream", "", None):
        assert mv.validate_audio_metadata(f"clip{ext}", declared) == ext


@pytest.mark.parametrize("filename", ["a.exe", "a.zip", "a.html", "a.js", "a.pdf", "a.png", "a.wav.exe", "noext", "", None, ".wav", "a.mp4", "a.mov", "a.aac", "a.mkv"])
def test_unsupported_extensions_are_rejected(filename):
    with pytest.raises(UnsupportedMedia):
        mv.validate_audio_metadata(filename, "audio/wav")


@pytest.mark.parametrize("filename,declared", [
    ("a.wav", "text/plain"), ("a.wav", "audio/mpeg"), ("a.mp3", "audio/wav"), ("a.mp3", "application/x-msdownload"),
    ("a.ogg", "video/mp4"), ("a.m4a", "image/png"), ("a.webm", "text/html"),
])
def test_spoofed_or_mismatched_declared_types_are_rejected(filename, declared):
    with pytest.raises(UnsupportedMedia):
        mv.validate_audio_metadata(filename, declared)


def test_declared_type_parameters_and_case_are_tolerated():
    assert mv.validate_audio_metadata("a.wav", "Audio/WAV; charset=binary") == ".wav"


@pytest.mark.parametrize("ext,wrong_sample", [
    (".wav", ms.mp3_id3), (".mp3", ms.wav), (".ogg", ms.webm), (".webm", ms.ogg), (".m4a", ms.wav), (".mp3", ms.m4a),
])
def test_content_must_match_the_extension(ext, wrong_sample):
    with pytest.raises(UnsupportedMedia):
        mv.check_audio_content(wrong_sample(), ext)


@pytest.mark.parametrize("name", sorted(ms.DISGUISED))
@pytest.mark.parametrize("ext", sorted(ms.AUDIO_SAMPLES))
def test_disguised_files_fail_the_content_check_under_every_audio_extension(name, ext):
    with pytest.raises(UnsupportedMedia):
        mv.check_audio_content(ms.DISGUISED[name], ext)


def test_an_m4a_with_a_video_container_still_passes_known_limit():
    """DOCUMENTED LIMITATION: an ISO-BMFF signature proves the container, not that it holds audio."""
    assert mv.check_audio_content(ms.mp4(), ".m4a") == "mp4"


# ------------------------------------------------------------------ filenames
@pytest.mark.parametrize("filename", [
    "../../etc/passwd.wav", "..\\..\\windows\\system32\\a.wav", "dir/clip.wav", "C:\\temp\\clip.wav", "/abs/clip.wav",
    "clip\x00.wav", "clip\n.wav", "clip\r\n.wav", "clip\x1b[31m.wav", "..", ".", "x" * 300 + ".wav",
])
def test_traversal_and_control_character_filenames_are_rejected(filename):
    with pytest.raises(InvalidInput):
        mv.validate_audio_metadata(filename, "audio/wav")


@pytest.mark.parametrize("filename", ["my recording (1).wav", "résumé voicemail.mp3", "a.b.c.wav", "UPPER.WAV", "语音.m4a"])
def test_ordinary_filenames_are_accepted(filename):
    assert mv.validate_audio_metadata(filename, None).startswith(".")


# ------------------------------------------------------------------ video allowlist (defined now, used by the video phase)
@pytest.mark.parametrize("ext,sample,declared", [
    (".mp4", ms.mp4, "video/mp4"), (".mov", ms.mov, "video/quicktime"), (".webm", ms.webm, "video/webm"),
])
def test_video_allowlist_accepts_mp4_mov_and_webm(ext, sample, declared):
    suffix = mv.validate_video_metadata(f"clip{ext}", declared)
    assert mv.check_video_content(sample(), suffix)


@pytest.mark.parametrize("filename", ["a.avi", "a.mkv", "a.exe", "a.wav", "a.flv", "a.zip", "a"])
def test_video_allowlist_rejects_everything_else(filename):
    with pytest.raises(UnsupportedMedia):
        mv.validate_video_metadata(filename, "video/mp4")


@pytest.mark.parametrize("ext,wrong", [(".mp4", ms.mov), (".mov", ms.mp4), (".mp4", ms.webm), (".webm", ms.mp4), (".mp4", ms.matroska)])
def test_video_content_must_match_the_extension(ext, wrong):
    with pytest.raises(UnsupportedMedia):
        mv.check_video_content(wrong(), ext)
