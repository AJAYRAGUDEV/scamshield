from __future__ import annotations

import os

import pytest

from app.core import config
from app.core.config import Settings, SettingsError, load_settings


def test_approved_defaults():
    s = load_settings({})
    assert s.gemini_api_key == "" and not s.gemini_configured
    assert s.gemini_model == "gemini-3.5-flash"
    assert s.app_env == "development"
    assert (s.max_text_length, s.max_audio_size_mb, s.max_video_size_mb, s.max_video_duration_seconds) == (20000, 20, 100, 300)
    assert s.max_audio_bytes == 20 * 1024 * 1024 and s.max_video_bytes == 100 * 1024 * 1024


def test_env_example_parses_and_matches_the_approved_defaults():
    from pathlib import Path

    lines = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8").splitlines()
    env = dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))
    assert env["GEMINI_API_KEY"] == ""                      # placeholder only, never a real credential
    assert load_settings(env) == load_settings({})          # every documented value equals the default


def test_all_values_can_be_overridden():
    s = load_settings({
        "GEMINI_API_KEY": " k ", "GEMINI_MODEL": "m-1", "APP_ENV": "Production", "LOG_LEVEL": "debug",
        "MAX_TEXT_LENGTH": "10", "MAX_AUDIO_SIZE_MB": "2", "MAX_VIDEO_SIZE_MB": "3",
        "MAX_VIDEO_DURATION_SECONDS": "4", "GEMINI_TIMEOUT_SECONDS": "1.5", "GEMINI_MAX_CONCURRENCY": "2",
    })
    assert (s.gemini_api_key, s.gemini_model, s.app_env, s.log_level) == ("k", "m-1", "production", "DEBUG")
    assert (s.max_text_length, s.max_audio_size_mb, s.max_video_size_mb, s.max_video_duration_seconds) == (10, 2, 3, 4)
    assert (s.gemini_timeout_seconds, s.gemini_max_concurrency) == (1.5, 2)
    assert s.gemini_configured


def test_blank_values_count_as_unset():
    """.env.example ships blank placeholders."""
    s = load_settings({"GEMINI_API_KEY": "  ", "GEMINI_MODEL": "", "MAX_TEXT_LENGTH": ""})
    assert not s.gemini_configured and s.gemini_model == "gemini-3.5-flash" and s.max_text_length == 20000


def test_missing_api_key_is_not_a_startup_error():
    assert load_settings({"APP_ENV": "production"}).gemini_configured is False


@pytest.mark.parametrize("env,needle", [
    ({"MAX_TEXT_LENGTH": "abc"}, "MAX_TEXT_LENGTH"),
    ({"MAX_AUDIO_SIZE_MB": "0"}, "MAX_AUDIO_SIZE_MB"),
    ({"MAX_VIDEO_SIZE_MB": "-5"}, "MAX_VIDEO_SIZE_MB"),
    ({"MAX_VIDEO_DURATION_SECONDS": "1.5"}, "MAX_VIDEO_DURATION_SECONDS"),
    ({"GEMINI_TIMEOUT_SECONDS": "soon"}, "GEMINI_TIMEOUT_SECONDS"),
    ({"APP_ENV": "staging"}, "APP_ENV"),
    ({"LOG_LEVEL": "LOUD"}, "LOG_LEVEL"),
])
def test_malformed_settings_fail_fast_with_a_clear_message(env, needle):
    with pytest.raises(SettingsError, match=needle):
        load_settings(env)


def test_all_problems_are_reported_together():
    with pytest.raises(SettingsError) as exc:
        load_settings({"MAX_TEXT_LENGTH": "x", "APP_ENV": "nope"})
    assert "MAX_TEXT_LENGTH" in str(exc.value) and "APP_ENV" in str(exc.value)


def test_secret_never_appears_in_repr():
    assert "super-secret" not in repr(Settings(gemini_api_key="super-secret"))


def test_dotenv_is_skipped_in_test_env_and_never_overrides(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("DOTENV_PROBE=from-file\nDOTENV_EXISTING=from-file\n", encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    for name in ("DOTENV_PROBE", "DOTENV_EXISTING"):
        monkeypatch.setenv(name, "x")      # registers restore-to-absent at teardown
        monkeypatch.delenv(name)

    config.load_dotenv_file()               # APP_ENV=test -> skipped
    assert "DOTENV_PROBE" not in os.environ

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DOTENV_EXISTING", "from-process")
    config.load_dotenv_file()
    assert os.environ["DOTENV_PROBE"] == "from-file"
    assert os.environ["DOTENV_EXISTING"] == "from-process"   # existing env wins


# ------------------------------------------------------------------ Phase 5: security configuration
def test_security_defaults():
    s = load_settings({})
    assert (s.rate_limit_enabled, s.rate_limit_text, s.rate_limit_audio, s.rate_limit_video) == (True, "30/minute", "10/minute", "3/minute")
    assert (s.rate_limit_default, s.rate_limit_global) == ("120/minute", "120/minute")
    assert (s.gemini_max_concurrency, s.gemini_max_queue, s.gemini_max_retries, s.gemini_quota_cooldown_seconds) == (4, 8, 1, 30.0)
    assert s.trusted_proxy_ips == () and s.enable_hsts is False and s.docs_enabled is True
    assert s.cors_allow_origins == ("http://localhost", "http://127.0.0.1", "http://localhost:8000", "http://127.0.0.1:8000")
    assert s.upload_dir.name == "scamshield-uploads"
    assert load_settings({"APP_ENV": "production"}).docs_enabled is False


def test_security_settings_can_be_overridden():
    s = load_settings({
        "RATE_LIMIT_TEXT": "5/second", "RATE_LIMIT_ENABLED": "false", "TRUSTED_PROXY_IPS": "10.0.0.0/8, 192.168.1.1",
        "GEMINI_MAX_QUEUE": "0", "GEMINI_MAX_RETRIES": "0", "GEMINI_QUOTA_COOLDOWN_SECONDS": "0", "ENABLE_HSTS": "yes",
        "TEMP_UPLOAD_DIR": "/var/tmp/scam",
    })
    assert s.rate_limit_text == "5/second" and s.rate_limit_enabled is False
    assert s.trusted_proxy_ips == ("10.0.0.0/8", "192.168.1.1")
    assert (s.gemini_max_queue, s.gemini_max_retries, s.gemini_quota_cooldown_seconds, s.enable_hsts) == (0, 0, 0.0, True)
    assert str(s.upload_dir).replace("\\", "/").endswith("/var/tmp/scam")


@pytest.mark.parametrize("env,needle", [
    ({"RATE_LIMIT_TEXT": "fast"}, "RATE_LIMIT_TEXT"),
    ({"RATE_LIMIT_AUDIO": "10 per"}, "RATE_LIMIT_AUDIO"),
    ({"RATE_LIMIT_GLOBAL": ""}, None),                      # blank = default, not an error
    ({"RATE_LIMIT_ENABLED": "maybe"}, "RATE_LIMIT_ENABLED"),
    ({"TRUSTED_PROXY_IPS": "not-an-ip"}, "TRUSTED_PROXY_IPS"),
    ({"TRUSTED_PROXY_IPS": "10.0.0.1, 999.1.1.1"}, "TRUSTED_PROXY_IPS"),
    ({"GEMINI_MAX_QUEUE": "-1"}, "GEMINI_MAX_QUEUE"),
    ({"GEMINI_MAX_RETRIES": "-1"}, "GEMINI_MAX_RETRIES"),
    ({"GEMINI_MAX_CONCURRENCY": "0"}, "GEMINI_MAX_CONCURRENCY"),
    ({"GEMINI_QUOTA_COOLDOWN_SECONDS": "-3"}, "GEMINI_QUOTA_COOLDOWN_SECONDS"),
    ({"ENABLE_HSTS": "2"}, "ENABLE_HSTS"),
])
def test_unsafe_or_malformed_security_settings_fail_fast(env, needle):
    if needle is None:
        assert load_settings(env).rate_limit_global == "120/minute"
        return
    with pytest.raises(SettingsError, match=needle):
        load_settings(env)


def test_the_example_env_file_lists_every_setting_and_holds_no_credentials():
    from pathlib import Path
    import re

    root = Path(__file__).resolve().parents[2]
    example = (root / ".env.example").read_text(encoding="utf-8")
    names = set(re.findall(r"^([A-Z_]+)=", example, re.M))
    expected = {"GEMINI_API_KEY", "GEMINI_MODEL", "APP_ENV", "LOG_LEVEL", "MAX_TEXT_LENGTH", "MAX_AUDIO_SIZE_MB", "MAX_VIDEO_SIZE_MB",
                "MAX_VIDEO_DURATION_SECONDS", "GEMINI_TIMEOUT_SECONDS", "GEMINI_MAX_CONCURRENCY", "GEMINI_MAX_QUEUE",
                "GEMINI_MAX_RETRIES", "GEMINI_QUOTA_COOLDOWN_SECONDS", "RATE_LIMIT_ENABLED", "RATE_LIMIT_TEXT", "RATE_LIMIT_AUDIO",
                "RATE_LIMIT_VIDEO", "RATE_LIMIT_DEFAULT", "RATE_LIMIT_GLOBAL", "TRUSTED_PROXY_IPS", "CORS_ALLOW_ORIGINS",
                "ENABLE_HSTS", "TEMP_UPLOAD_DIR",
                # Phase 8 (video)
                "FFPROBE_PATH", "FFPROBE_TIMEOUT_SECONDS", "GEMINI_VIDEO_TIMEOUT_SECONDS", "GEMINI_FILE_POLL_INTERVAL_SECONDS",
                "GEMINI_FILE_READY_TIMEOUT_SECONDS", "VIDEO_WORKERS", "VIDEO_MAX_ACTIVE_JOBS", "VIDEO_MAX_ACTIVE_JOBS_PER_CLIENT",
                "VIDEO_JOB_TIMEOUT_SECONDS", "VIDEO_JOB_TTL_SECONDS"}
    assert names == expected
    assert "AIza" not in example and re.search(r"^GEMINI_API_KEY=$", example, re.M)


def test_secrets_and_generated_files_are_excluded_from_git():
    from pathlib import Path

    lines = {l.strip() for l in (Path(__file__).resolve().parents[2] / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert {".env", ".env.*", "!.env.example", ".venv/", "__pycache__/", ".pytest_cache/"} <= lines
