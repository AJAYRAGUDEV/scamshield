"""Video validation with ffprobe (Phase 8). Runs BEFORE any job is created or Gemini is contacted.

What is checked, in order: ffprobe can read the file at all (integrity), the container is one of the approved
formats and matches the extension, there is a real video stream (cover art does not count), the metadata is sane
(finite positive duration, plausible dimensions and stream count) and the duration is within the approved limit.

Fail CLOSED: if ffprobe is missing, times out or produces nothing usable, the upload is refused (HTTP 503 / 422).
It is never accepted unchecked. ffprobe is executed WITHOUT a shell, with a fixed argument list, a timeout and a
generated path (never the client's filename), in a worker thread so the event loop is not blocked.

Known limits: ffprobe confirms the container and stream headers, not that every frame decodes. Some WebM files
recorded in a browser carry no duration in their header; they are refused because the 300-second limit cannot be
verified for them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.core.config import Settings
from app.core.errors import InvalidVideo, VideoTooLong, VideoValidationUnavailable
from app.core.logging import log_event

logger = logging.getLogger("scamshield.video")

MAX_FFPROBE_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_STREAMS = 32
MAX_DIMENSION = 16_384
MAX_PLAUSIBLE_DURATION = 365 * 24 * 3600.0

# ffprobe `format_name` values that are acceptable for each extension. MP4 and MOV share ffprobe's "mov,mp4,..." name,
# so the extension/brand match is enforced by the signature check in ``media_validation``.
CONTAINERS: dict[str, frozenset[str]] = {
    ".mp4": frozenset({"mov", "mp4"}),
    ".mov": frozenset({"mov", "mp4"}),
    ".webm": frozenset({"webm", "matroska"}),
}

# A runner takes the argument list and returns (returncode, stdout bytes). Tests inject a fake; production uses ffprobe.
Runner = Callable[[list[str], float], tuple[int, bytes]]


@dataclass(frozen=True)
class VideoFacts:
    duration_seconds: float
    width: int
    height: int
    container: str
    video_codec: str | None
    has_audio: bool


def ffprobe_command(ffprobe_path: str, path: Path) -> list[str]:
    return [ffprobe_path, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-i", str(path)]


def subprocess_runner(args: list[str], timeout: float) -> tuple[int, bytes]:
    """Run ffprobe without a shell. Raises FileNotFoundError / OSError / TimeoutExpired for the caller to translate."""
    completed = subprocess.run(args, capture_output=True, timeout=timeout, shell=False, check=False,
                               stdin=subprocess.DEVNULL)
    return completed.returncode, completed.stdout[: MAX_FFPROBE_OUTPUT_BYTES + 1]


def _number(value: Any) -> float | None:
    """ffprobe reports numbers as strings ("12.5") or "N/A". Returns a finite float or None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)       # True is an int in Python


def parse_ffprobe(output: bytes, suffix: str, *, max_duration: float) -> VideoFacts:
    """Turn ffprobe's JSON into ``VideoFacts`` or raise ``InvalidVideo`` / ``VideoTooLong``. Treats it as untrusted."""
    if len(output) > MAX_FFPROBE_OUTPUT_BYTES:
        raise InvalidVideo("The video metadata is malformed.")
    try:
        data = json.loads(output)
    except (ValueError, UnicodeDecodeError):
        raise InvalidVideo("The video could not be read; it may be corrupt.") from None
    if not isinstance(data, dict) or not isinstance(data.get("format"), dict) or not isinstance(data.get("streams"), list):
        raise InvalidVideo("The video metadata is malformed.")

    fmt, streams = data["format"], data["streams"]
    if len(streams) == 0 or len(streams) > MAX_STREAMS or not all(isinstance(s, dict) for s in streams):
        raise InvalidVideo("The video has no usable streams.")

    names = {part.strip().lower() for part in str(fmt.get("format_name", "")).split(",") if part.strip()}
    if not names & CONTAINERS.get(suffix, frozenset()):
        raise InvalidVideo("The video container does not match the file type.")

    video = next((s for s in streams
                  if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    if video is None:
        raise InvalidVideo("The file has no video stream.")
    width, height = video.get("width"), video.get("height")
    if not (_is_int(width) and _is_int(height) and 0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION):
        raise InvalidVideo("The video metadata is malformed.")

    duration = _number(fmt.get("duration"))
    if duration is None:
        duration = _number(video.get("duration"))
    if duration is None or duration <= 0 or duration > MAX_PLAUSIBLE_DURATION:
        raise InvalidVideo("The video's length could not be determined, so it cannot be accepted.")
    if duration > max_duration:
        raise VideoTooLong(f"The video is longer than the {int(max_duration)}-second limit.")

    return VideoFacts(
        duration_seconds=duration, width=width, height=height, container=sorted(names)[0],
        video_codec=str(video.get("codec_name"))[:32] if video.get("codec_name") else None,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


async def inspect_video(path: Path, suffix: str, settings: Settings, runner: Runner | None = None) -> VideoFacts:
    """Validate the stored video with ffprobe. Raises ``InputError`` subclasses (never returns for a bad video)."""
    run = runner or subprocess_runner
    args = ffprobe_command(settings.ffprobe_path, path)
    try:
        returncode, output = await asyncio.to_thread(run, args, settings.ffprobe_timeout_seconds)
    except (FileNotFoundError, PermissionError):
        log_event(logger, logging.ERROR, "ffprobe_missing", path=settings.ffprobe_path)
        raise VideoValidationUnavailable() from None
    except subprocess.TimeoutExpired:
        log_event(logger, logging.WARNING, "ffprobe_timeout")
        raise InvalidVideo("The video could not be checked in time; it may be corrupt or malformed.") from None
    except OSError as exc:
        log_event(logger, logging.ERROR, "ffprobe_failed", error=type(exc).__name__)
        raise VideoValidationUnavailable() from None

    if returncode != 0:
        log_event(logger, logging.INFO, "video_rejected", reason="ffprobe_nonzero", returncode=returncode)
        raise InvalidVideo("The video could not be read; it may be corrupt.")
    return parse_ffprobe(output, suffix, max_duration=float(settings.max_video_duration_seconds))
