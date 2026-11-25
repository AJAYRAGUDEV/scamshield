"""Helpers for Phase 8 video tests. ffprobe is replaced by a controlled fake runner (ffprobe is not required to run
the tests), and Gemini by the fake SDK client. No live Gemini call and no real media decoding happens anywhere."""
from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.providers.gemini import GeminiProvider
from tests import media_samples as ms
from tests.fakes import FakeProvider, load_fixture_json

SUBMIT_PATH = "/api/v1/analyze/video"
STATUS_PATH = "/api/v1/video-status/"
MP4_FORMAT = "mov,mp4,m4a,3gp,3g2,mj2"
WEBM_FORMAT = "matroska,webm"
TERMINAL = {"completed", "unable_to_assess", "failed"}


def ffprobe_json(*, duration="30.5", format_name=MP4_FORMAT, video=True, audio=True, width=1280, height=720,
                 attached_pic=False, extra_streams=None, fmt_extra=None) -> bytes:
    streams = []
    if video:
        stream = {"index": 0, "codec_type": "video", "codec_name": "h264", "width": width, "height": height}
        if attached_pic:
            stream["disposition"] = {"attached_pic": 1}
        streams.append(stream)
    if audio:
        streams.append({"index": len(streams), "codec_type": "audio", "codec_name": "aac"})
    streams.extend(extra_streams or [])
    fmt = {"format_name": format_name}
    if duration is not None:
        fmt["duration"] = duration
    fmt.update(fmt_extra or {})
    return json.dumps({"streams": streams, "format": fmt}).encode()


def fake_runner(output: bytes | None = None, returncode: int = 0, raises: BaseException | None = None, calls: list | None = None):
    """A stand-in for ffprobe: returns ``output`` (default: a valid 30 s MP4 description) or raises."""
    payload = ffprobe_json() if output is None else output

    def run(args, timeout):
        if calls is not None:
            calls.append((list(args), timeout))
        if raises is not None:
            raise raises
        return returncode, payload

    return run


def raising_timeout(timeout: float = 1.0):
    return subprocess.TimeoutExpired(cmd="ffprobe", timeout=timeout)


def video_settings(upload_dir, **overrides) -> Settings:
    base = dict(
        gemini_api_key="test-key-not-real", app_env="test", gemini_max_retries=0, temp_upload_dir=str(upload_dir),
        rate_limit_text="100000/minute", rate_limit_audio="100000/minute", rate_limit_video="100000/minute",
        rate_limit_default="100000/minute", rate_limit_global="100000/minute",
        gemini_file_poll_interval_seconds=0.01, gemini_file_ready_timeout_seconds=2.0, gemini_video_timeout_seconds=5.0,
        video_job_timeout_seconds=10.0,
    )
    base.update(overrides)
    return Settings(**base)


def video_app(upload_dir, *, provider=None, runner=None, **settings_overrides):
    """An app with a FakeProvider (default) and a fake ffprobe. Use ``with TestClient(app) as client`` so the workers run."""
    settings = video_settings(upload_dir, **settings_overrides)
    return create_app(settings, provider if provider is not None else FakeProvider(), runner if runner is not None else fake_runner())


def gemini_video_app(fake_sdk, upload_dir, fixture: str | dict, *, runner=None, **settings_overrides):
    """An app whose REAL Gemini adapter talks to the fake SDK, which answers with ``fixture``."""
    data = load_fixture_json(fixture) if isinstance(fixture, str) else fixture
    fake_sdk.generate_result = SimpleNamespace(parsed=None, text=json.dumps(data, ensure_ascii=False))
    settings = video_settings(upload_dir, **settings_overrides)
    return create_app(settings, GeminiProvider(settings, client_factory=fake_sdk), runner if runner is not None else fake_runner())


def post_video(client: TestClient, content: bytes | None = None, name: str = "clip.mp4", mime: str = "video/mp4", path: str = SUBMIT_PATH):
    return client.post(path, files={"file": (name, content if content is not None else ms.mp4(), mime)})


def wait_for_job(client: TestClient, job_id: str, timeout: float = 10.0) -> dict:
    """Poll the status endpoint until the job is finished."""
    deadline = time.monotonic() + timeout
    while True:
        body = client.get(STATUS_PATH + job_id).json()
        if body.get("status") in TERMINAL:
            return body
        assert time.monotonic() < deadline, f"job did not finish: {body}"
        time.sleep(0.02)


def leftovers(upload_dir) -> list:
    return list(upload_dir.rglob("*")) if upload_dir.exists() else []
