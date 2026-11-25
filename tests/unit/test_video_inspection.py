"""ffprobe validation: parsing, limits and failure modes. ffprobe itself is replaced by a controlled fake."""
from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from app.core.config import Settings
from app.core.errors import InvalidVideo, VideoTooLong, VideoValidationUnavailable
from app.services.video_inspection import ffprobe_command, inspect_video, parse_ffprobe, subprocess_runner
from tests.video_helpers import MP4_FORMAT, WEBM_FORMAT, fake_runner, ffprobe_json, raising_timeout

SETTINGS = Settings(ffprobe_path="ffprobe-test", ffprobe_timeout_seconds=3.0)


def inspect(output=None, suffix=".mp4", **kw):
    from pathlib import Path
    return asyncio.run(inspect_video(Path("clip.mp4"), suffix, SETTINGS, fake_runner(output, **kw)))


def test_a_valid_mp4_mov_and_webm_are_accepted():
    assert inspect(suffix=".mp4").duration_seconds == 30.5 and inspect(suffix=".mov").has_audio is True
    facts = inspect(ffprobe_json(format_name=WEBM_FORMAT, audio=False), ".webm")
    assert facts.container in {"webm", "matroska"} and facts.has_audio is False and (facts.width, facts.height) == (1280, 720)


def test_the_approved_duration_limit_is_300_seconds_inclusive():
    assert inspect(ffprobe_json(duration="300.0")).duration_seconds == 300.0
    with pytest.raises(VideoTooLong):
        inspect(ffprobe_json(duration="300.01"))


def test_the_duration_limit_is_configurable():
    from pathlib import Path
    settings = Settings(max_video_duration_seconds=10)
    with pytest.raises(VideoTooLong):
        asyncio.run(inspect_video(Path("a.mp4"), ".mp4", settings, fake_runner(ffprobe_json(duration="11"))))


def test_stream_duration_is_used_when_the_container_has_none():
    data = json.loads(ffprobe_json(duration=None))
    data["streams"][0]["duration"] = "12.0"
    assert inspect(json.dumps(data).encode()).duration_seconds == 12.0


@pytest.mark.parametrize("duration", [None, "N/A", "abc", "nan", "inf", "-5", "0", "1e99"])
def test_missing_or_malformed_durations_are_rejected_not_assumed(duration):
    with pytest.raises(InvalidVideo):
        inspect(ffprobe_json(duration=duration))


def test_a_file_without_a_video_stream_is_rejected():
    with pytest.raises(InvalidVideo, match="no video stream"):
        inspect(ffprobe_json(video=False))


def test_cover_art_is_not_a_video_stream():
    with pytest.raises(InvalidVideo, match="no video stream"):
        inspect(ffprobe_json(attached_pic=True, audio=True))


@pytest.mark.parametrize("output", [b"", b"not json", b"[]", b"{}", b'{"format": {}, "streams": []}',
                                    b'{"format": [], "streams": []}', b'{"format": {"format_name": "mp4"}, "streams": "x"}',
                                    json.dumps({"format": {"format_name": MP4_FORMAT, "duration": "5"}, "streams": [1, 2]}).encode()])
def test_malformed_metadata_is_rejected(output):
    with pytest.raises(InvalidVideo):
        inspect(output)


@pytest.mark.parametrize("w, h", [(0, 720), (1280, 0), (-1, 5), (99999, 10), ("1280", 720), (True, 720), (None, None)])
def test_implausible_dimensions_are_rejected(w, h):
    data = json.loads(ffprobe_json())
    data["streams"][0]["width"], data["streams"][0]["height"] = w, h
    with pytest.raises(InvalidVideo):
        inspect(json.dumps(data).encode())


def test_too_many_streams_is_rejected():
    extra = [{"index": i, "codec_type": "data"} for i in range(40)]
    with pytest.raises(InvalidVideo):
        inspect(ffprobe_json(extra_streams=extra))


def test_an_oversized_ffprobe_output_is_rejected():
    with pytest.raises(InvalidVideo):
        inspect(b"{" + b" " * (3 * 1024 * 1024) + b"}")


def test_container_must_match_the_extension():
    with pytest.raises(InvalidVideo, match="container"):
        inspect(ffprobe_json(format_name="avi"))
    with pytest.raises(InvalidVideo, match="container"):
        inspect(ffprobe_json(format_name=MP4_FORMAT), ".webm")
    with pytest.raises(InvalidVideo, match="container"):
        inspect(ffprobe_json(format_name=WEBM_FORMAT), ".mp4")


def test_ffprobe_nonzero_exit_means_corrupt():
    with pytest.raises(InvalidVideo, match="corrupt"):
        inspect(b"", returncode=1)


def test_a_failing_ffprobe_is_rejected_even_if_it_printed_plausible_output():
    with pytest.raises(InvalidVideo, match="corrupt"):
        inspect(ffprobe_json(), returncode=1)


def test_ffprobe_timeout_is_a_rejection_not_a_hang():
    with pytest.raises(InvalidVideo):
        inspect(raises=raising_timeout())


@pytest.mark.parametrize("error", [FileNotFoundError("x"), PermissionError("x"), OSError("boom")])
def test_missing_or_unusable_ffprobe_fails_closed_with_503(error):
    with pytest.raises(VideoValidationUnavailable) as info:
        inspect(raises=error)
    assert info.value.status_code == 503


def test_the_command_has_no_shell_and_uses_the_generated_path_after_dash_i(tmp_path):
    path = tmp_path / "abc123.mp4"
    args = ffprobe_command("ffprobe", path)
    assert args[0] == "ffprobe" and args[-2:] == ["-i", str(path)] and "-show_streams" in args
    assert all(isinstance(a, str) for a in args)


def test_the_real_runner_does_not_use_a_shell_and_reports_a_missing_binary(tmp_path):
    with pytest.raises(FileNotFoundError):
        subprocess_runner([str(tmp_path / "definitely-not-ffprobe-xyz"), "-version"], 2.0)


def test_runner_arguments_and_timeout_are_passed_through():
    calls = []
    from pathlib import Path
    asyncio.run(inspect_video(Path("clip.mp4"), ".mp4", SETTINGS, fake_runner(calls=calls)))
    (args, timeout), = calls
    assert args[0] == "ffprobe-test" and timeout == 3.0


def test_parse_is_pure_and_reports_codec_and_audio():
    facts = parse_ffprobe(ffprobe_json(), ".mp4", max_duration=300)
    assert facts.video_codec == "h264" and facts.has_audio is True


# ------------------------------------------------------------------ real ffprobe (skipped when it is not installed)
import shutil  # noqa: E402

REAL_FFPROBE = shutil.which("ffprobe")


@pytest.mark.skipif(REAL_FFPROBE is None, reason="ffprobe is not installed; install FFmpeg to run the real-ffprobe checks")
def test_real_ffprobe_rejects_a_file_that_is_not_media(tmp_path):
    from app.core.config import Settings as S

    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64)       # right signature, no real content
    with pytest.raises(InvalidVideo):
        asyncio.run(inspect_video(junk, ".mp4", S(ffprobe_path=REAL_FFPROBE)))


@pytest.mark.skipif(REAL_FFPROBE is None, reason="ffprobe is not installed; install FFmpeg to run the real-ffprobe checks")
def test_real_ffprobe_reports_a_clear_error_for_a_missing_file(tmp_path):
    from app.core.config import Settings as S

    with pytest.raises(InvalidVideo):
        asyncio.run(inspect_video(tmp_path / "nope.mp4", ".mp4", S(ffprobe_path=REAL_FFPROBE)))
