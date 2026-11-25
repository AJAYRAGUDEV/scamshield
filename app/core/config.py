"""Centralized configuration, read from environment variables.

* The Gemini API key lives ONLY here (backend environment). No request may supply one.
* A missing key is NOT a startup error: the app still serves the frontend and /health,
  and analysis requests report ``unable_to_assess`` / ``CONFIGURATION_ERROR``.
* Malformed settings ARE a startup error (fail fast with a clear message), including unsafe
  security configuration such as a wildcard CORS origin.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = PROJECT_ROOT / "frontend"

APP_ENVS = ("development", "test", "production")
DEFAULT_CORS_ORIGINS = ("http://localhost", "http://127.0.0.1", "http://localhost:8000", "http://127.0.0.1:8000")
_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?$")


class SettingsError(ValueError):
    """Raised at startup when environment configuration is malformed."""


def default_upload_dir() -> Path:
    return Path(tempfile.gettempdir()) / "scamshield-uploads"


@dataclass(frozen=True)
class Settings:
    # Secret: excluded from repr so it can never leak through logging of the object.
    gemini_api_key: str = field(default="", repr=False)
    gemini_model: str = "gemini-3.5-flash"
    app_env: str = "development"
    log_level: str = "INFO"

    # Approved limits.
    max_text_length: int = 20_000
    max_audio_size_mb: int = 20
    max_video_size_mb: int = 100
    max_video_duration_seconds: int = 300

    # Provider behaviour.
    gemini_timeout_seconds: float = 60.0      # overall deadline per analysis call
    gemini_max_concurrency: int = 4           # analyses running at once
    gemini_max_queue: int = 8                 # analyses allowed to WAIT; beyond this -> HTTP 503
    gemini_max_retries: int = 1               # extra attempts, transient provider failures only
    gemini_quota_cooldown_seconds: float = 30.0   # after a provider 429, stop calling Gemini for this long (0 = off)

    # Video (Phase 8). Jobs live in memory in this one process and are lost on restart.
    ffprobe_path: str = "ffprobe"             # executable used to validate uploaded videos
    ffprobe_timeout_seconds: float = 20.0
    gemini_video_timeout_seconds: float = 240.0       # overall deadline for one video analysis (upload + processing + generation)
    gemini_file_poll_interval_seconds: float = 2.0    # how often to ask Gemini whether the uploaded video is ready
    gemini_file_ready_timeout_seconds: float = 120.0  # give up waiting for Gemini to finish processing the video
    video_workers: int = 1                    # jobs processed at once (single worker initially)
    video_max_active_jobs: int = 4            # queued + running jobs allowed; more get HTTP 503
    video_max_active_jobs_per_client: int = 2
    video_job_timeout_seconds: float = 330.0  # hard limit for one job, including cleanup-safe cancellation
    video_job_ttl_seconds: float = 900.0      # finished jobs are forgotten this long after they finish

    # Rate limits (slowapi syntax, per client address unless noted).
    rate_limit_enabled: bool = True
    rate_limit_text: str = "30/minute"
    rate_limit_audio: str = "10/minute"
    rate_limit_video: str = "3/minute"        # video submissions per client
    rate_limit_default: str = "120/minute"    # status, health and the web page
    rate_limit_global: str = "120/minute"     # ALL clients combined, analysis routes (quota protection)
    trusted_proxy_ips: tuple[str, ...] = ()   # only these peers may supply X-Forwarded-For

    # Web security.
    cors_allow_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS
    enable_hsts: bool = False                 # enable only when served over HTTPS
    temp_upload_dir: str = ""                 # empty = <system temp>/scamshield-uploads

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def max_audio_bytes(self) -> int:
        return self.max_audio_size_mb * 1024 * 1024

    @property
    def max_video_bytes(self) -> int:
        return self.max_video_size_mb * 1024 * 1024

    @property
    def upload_dir(self) -> Path:
        return Path(self.temp_upload_dir) if self.temp_upload_dir else default_upload_dir()

    @property
    def docs_enabled(self) -> bool:
        return self.app_env != "production"


def _read(environ: Mapping[str, str], name: str) -> str | None:
    """Blank values count as unset (``.env.example`` ships blank placeholders)."""
    value = environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _split(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from a mapping (defaults to ``os.environ``). Pure and testable."""
    env = os.environ if environ is None else environ
    defaults = Settings()
    errors: list[str] = []

    def number(name: str, default, cast, *, minimum=1):
        raw = _read(env, name)
        if raw is None:
            return default
        try:
            value = cast(raw)
        except ValueError:
            errors.append(f"{name} must be a number, got {raw!r}")
            return default
        if value < minimum:
            errors.append(f"{name} must be at least {minimum}, got {raw!r}")
            return default
        return value

    def boolean(name: str, default: bool) -> bool:
        raw = _read(env, name)
        if raw is None:
            return default
        if raw.lower() in ("1", "true", "yes", "on"):
            return True
        if raw.lower() in ("0", "false", "no", "off"):
            return False
        errors.append(f"{name} must be true or false, got {raw!r}")
        return default

    def rate(name: str, default: str) -> str:
        raw = _read(env, name)
        if raw is None:
            return default
        from limits import parse

        try:
            parse(raw)
        except Exception:
            errors.append(f"{name} must look like '10/minute', got {raw!r}")
            return default
        return raw

    app_env = (_read(env, "APP_ENV") or defaults.app_env).lower()
    if app_env not in APP_ENVS:
        errors.append(f"APP_ENV must be one of {', '.join(APP_ENVS)}, got {app_env!r}")
        app_env = defaults.app_env

    log_level = (_read(env, "LOG_LEVEL") or defaults.log_level).upper()
    if not isinstance(logging.getLevelName(log_level), int):
        errors.append(f"LOG_LEVEL must be a standard logging level name, got {log_level!r}")
        log_level = defaults.log_level

    origins = defaults.cors_allow_origins
    raw_origins = _read(env, "CORS_ALLOW_ORIGINS")
    if raw_origins is not None:
        origins = tuple(_split(raw_origins))
        for origin in origins:
            if origin == "*" or not _ORIGIN_RE.match(origin):
                errors.append(f"CORS_ALLOW_ORIGINS must list explicit origins like http://localhost:8000 (no wildcards), got {origin!r}")

    proxies: tuple[str, ...] = ()
    raw_proxies = _read(env, "TRUSTED_PROXY_IPS")
    if raw_proxies is not None:
        proxies = tuple(_split(raw_proxies))
        for item in proxies:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError:
                errors.append(f"TRUSTED_PROXY_IPS must list IP addresses or CIDR ranges, got {item!r}")

    settings = Settings(
        gemini_api_key=_read(env, "GEMINI_API_KEY") or "",
        gemini_model=_read(env, "GEMINI_MODEL") or defaults.gemini_model,
        app_env=app_env,
        log_level=log_level,
        max_text_length=number("MAX_TEXT_LENGTH", defaults.max_text_length, int),
        max_audio_size_mb=number("MAX_AUDIO_SIZE_MB", defaults.max_audio_size_mb, int),
        max_video_size_mb=number("MAX_VIDEO_SIZE_MB", defaults.max_video_size_mb, int),
        max_video_duration_seconds=number("MAX_VIDEO_DURATION_SECONDS", defaults.max_video_duration_seconds, int),
        gemini_timeout_seconds=number("GEMINI_TIMEOUT_SECONDS", defaults.gemini_timeout_seconds, float, minimum=0.001),
        gemini_max_concurrency=number("GEMINI_MAX_CONCURRENCY", defaults.gemini_max_concurrency, int),
        gemini_max_queue=number("GEMINI_MAX_QUEUE", defaults.gemini_max_queue, int, minimum=0),
        gemini_max_retries=number("GEMINI_MAX_RETRIES", defaults.gemini_max_retries, int, minimum=0),
        gemini_quota_cooldown_seconds=number("GEMINI_QUOTA_COOLDOWN_SECONDS", defaults.gemini_quota_cooldown_seconds, float, minimum=0),
        ffprobe_path=_read(env, "FFPROBE_PATH") or defaults.ffprobe_path,
        ffprobe_timeout_seconds=number("FFPROBE_TIMEOUT_SECONDS", defaults.ffprobe_timeout_seconds, float, minimum=0.001),
        gemini_video_timeout_seconds=number("GEMINI_VIDEO_TIMEOUT_SECONDS", defaults.gemini_video_timeout_seconds, float, minimum=0.001),
        gemini_file_poll_interval_seconds=number("GEMINI_FILE_POLL_INTERVAL_SECONDS", defaults.gemini_file_poll_interval_seconds, float, minimum=0.001),
        gemini_file_ready_timeout_seconds=number("GEMINI_FILE_READY_TIMEOUT_SECONDS", defaults.gemini_file_ready_timeout_seconds, float, minimum=0.001),
        video_workers=number("VIDEO_WORKERS", defaults.video_workers, int),
        video_max_active_jobs=number("VIDEO_MAX_ACTIVE_JOBS", defaults.video_max_active_jobs, int),
        video_max_active_jobs_per_client=number("VIDEO_MAX_ACTIVE_JOBS_PER_CLIENT", defaults.video_max_active_jobs_per_client, int),
        video_job_timeout_seconds=number("VIDEO_JOB_TIMEOUT_SECONDS", defaults.video_job_timeout_seconds, float, minimum=0.001),
        video_job_ttl_seconds=number("VIDEO_JOB_TTL_SECONDS", defaults.video_job_ttl_seconds, float, minimum=0.001),
        rate_limit_enabled=boolean("RATE_LIMIT_ENABLED", defaults.rate_limit_enabled),
        rate_limit_text=rate("RATE_LIMIT_TEXT", defaults.rate_limit_text),
        rate_limit_audio=rate("RATE_LIMIT_AUDIO", defaults.rate_limit_audio),
        rate_limit_video=rate("RATE_LIMIT_VIDEO", defaults.rate_limit_video),
        rate_limit_default=rate("RATE_LIMIT_DEFAULT", defaults.rate_limit_default),
        rate_limit_global=rate("RATE_LIMIT_GLOBAL", defaults.rate_limit_global),
        trusted_proxy_ips=proxies,
        cors_allow_origins=origins,
        enable_hsts=boolean("ENABLE_HSTS", defaults.enable_hsts),
        temp_upload_dir=_read(env, "TEMP_UPLOAD_DIR") or "",
    )
    if errors:
        raise SettingsError("Invalid configuration: " + "; ".join(errors))
    return settings


def load_dotenv_file() -> None:
    """Load ``.env`` into the process environment without overriding existing variables.

    Skipped when APP_ENV=test so a developer's real ``.env`` can never leak into the test run.
    """
    if os.environ.get("APP_ENV", "").strip().lower() == "test":
        return
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv_file()
    return load_settings()
