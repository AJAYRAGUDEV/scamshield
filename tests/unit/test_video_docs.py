"""docs/video.md must agree with the code (defaults, states, stages, error codes)."""
from __future__ import annotations

import re
from pathlib import Path

from app.core import errors
from app.core.config import Settings, load_settings
from app.schemas.jobs import STAGE_PROGRESS, JobStatus
from app.services.video_jobs import TooManyJobs

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "docs" / "video.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")

ENV_DEFAULTS = {
    "MAX_VIDEO_SIZE_MB": "max_video_size_mb", "MAX_VIDEO_DURATION_SECONDS": "max_video_duration_seconds",
    "FFPROBE_TIMEOUT_SECONDS": "ffprobe_timeout_seconds", "GEMINI_VIDEO_TIMEOUT_SECONDS": "gemini_video_timeout_seconds",
    "GEMINI_FILE_POLL_INTERVAL_SECONDS": "gemini_file_poll_interval_seconds",
    "GEMINI_FILE_READY_TIMEOUT_SECONDS": "gemini_file_ready_timeout_seconds", "VIDEO_WORKERS": "video_workers",
    "VIDEO_MAX_ACTIVE_JOBS": "video_max_active_jobs", "VIDEO_MAX_ACTIVE_JOBS_PER_CLIENT": "video_max_active_jobs_per_client",
    "VIDEO_JOB_TIMEOUT_SECONDS": "video_job_timeout_seconds", "VIDEO_JOB_TTL_SECONDS": "video_job_ttl_seconds",
}


def fmt(value) -> str:
    return str(int(value)) if float(value) == int(value) else str(value)


def test_readme_defaults_for_video_settings_match_the_code():
    defaults = Settings()
    for env, attr in ENV_DEFAULTS.items():
        match = re.search(rf"\| `{env}` \| `([^`]+)` \|", README)
        assert match, env
        assert match.group(1) == fmt(getattr(defaults, attr)), env


def test_approved_defaults_are_unchanged():
    s = Settings()
    assert (s.max_video_size_mb, s.max_video_duration_seconds, s.video_workers, s.rate_limit_video) == (100, 300, 1, "3/minute")


def test_every_video_setting_can_be_set_from_the_environment_and_bad_values_stop_startup():
    import pytest
    from app.core.config import SettingsError

    loaded = load_settings({"VIDEO_WORKERS": "2", "MAX_VIDEO_SIZE_MB": "50", "FFPROBE_PATH": "/opt/ffprobe", "VIDEO_JOB_TTL_SECONDS": "60"})
    assert (loaded.video_workers, loaded.max_video_size_mb, loaded.ffprobe_path, loaded.video_job_ttl_seconds) == (2, 50, "/opt/ffprobe", 60.0)
    for bad in ({"VIDEO_WORKERS": "0"}, {"VIDEO_WORKERS": "x"}, {"MAX_VIDEO_DURATION_SECONDS": "-1"}, {"GEMINI_VIDEO_TIMEOUT_SECONDS": "0"}):
        with pytest.raises(SettingsError):
            load_settings(bad)


def test_the_doc_lists_every_job_state_and_stage():
    for status in JobStatus:
        assert f"`{status.value}`" in DOC or status.value in DOC, status
    for stage, progress in STAGE_PROGRESS.items():
        assert re.search(rf"`{stage}` {progress}\b", DOC), stage


def test_the_doc_lists_every_video_error_code():
    for code in ("INVALID_INPUT", "PAYLOAD_TOO_LARGE", "UNSUPPORTED_MEDIA_TYPE", "INVALID_VIDEO", "VIDEO_TOO_LONG", "INVALID_REQUEST",
                 "RATE_LIMITED", "TOO_MANY_JOBS", "SERVICE_BUSY", "VIDEO_VALIDATION_UNAVAILABLE", "JOB_NOT_FOUND"):
        assert code in DOC, code
    assert errors.InvalidVideo.code == "INVALID_VIDEO" and errors.VideoTooLong.code == "VIDEO_TOO_LONG"
    assert errors.VideoValidationUnavailable.code == "VIDEO_VALIDATION_UNAVAILABLE" and errors.NotFound.code == "JOB_NOT_FOUND"
    assert TooManyJobs.code == "TOO_MANY_JOBS" and errors.ServiceBusy.code == "SERVICE_BUSY"


def test_the_doc_makes_no_forbidden_claims():
    lowered = DOC.lower()
    assert "not proof" in lowered and "does **not** detect deepfakes" in lowered and "in-memory" in lowered
    for claim in ("guarantee", "detects deepfakes", "verifies identity", "authenticates the speaker"):
        assert claim not in lowered.replace("does **not** detect deepfakes", ""), claim
