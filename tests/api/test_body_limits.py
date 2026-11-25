"""Layered request-body protection: Content-Length, streamed byte counting, and the final on-disk check."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import (
    DEFAULT_BODY_LIMIT_BYTES, MULTIPART_OVERHEAD_BYTES, BodyLimitMiddleware, body_limits,
)
from app.main import create_app
from tests import media_samples as ms
from tests.fakes import FakeProvider

MB = 1024 * 1024


# ------------------------------------------------------------------ the configured limits
def test_limits_come_from_the_approved_maximum_sizes():
    s = Settings()
    limits = body_limits(s)
    assert limits["/api/analyze-audio"] == limits["/api/v1/analyze/audio"] == 20 * MB + MULTIPART_OVERHEAD_BYTES
    assert limits["/api/v1/analyze/video"] == 100 * MB + MULTIPART_OVERHEAD_BYTES
    assert limits["/api/analyze-text"] == limits["/api/v1/analyze/text"] == 20_000 * 12 + 16 * 1024
    assert BodyLimitMiddleware(lambda *a: None, s).limit_for("/anything/else") == DEFAULT_BODY_LIMIT_BYTES


def test_limits_follow_the_settings():
    limits = body_limits(Settings(max_audio_size_mb=1, max_video_size_mb=2, max_text_length=100))
    assert limits["/api/analyze-audio"] == 1 * MB + MULTIPART_OVERHEAD_BYTES
    assert limits["/api/v1/analyze/video"] == 2 * MB + MULTIPART_OVERHEAD_BYTES
    assert limits["/api/analyze-text"] == 100 * 12 + 16 * 1024


# ------------------------------------------------------------------ raw ASGI: the middleware itself
class Recorder:
    """A tiny ASGI app that reads the body to the end (or until it is told the client went away)."""

    def __init__(self, answer_after_disconnect: int | None = 400, raise_after_disconnect: bool = False):
        self.read = 0
        self.called = False
        self.answer_after_disconnect = answer_after_disconnect
        self.raise_after_disconnect = raise_after_disconnect

    async def __call__(self, scope, receive, send):
        self.called = True
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                if self.raise_after_disconnect:
                    raise RuntimeError("app crashed while reading")
                status = self.answer_after_disconnect or 200
                break
            self.read += len(message.get("body", b""))
            if not message.get("more_body"):
                status = 200
                break
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"from":"app"}'})


async def drive(app, headers, chunks, path="/api/analyze-text"):
    """Run the ASGI app with a body delivered in ``chunks``. Returns (responses, chunks_pulled)."""
    pulled = {"n": 0}
    sent = []
    queue = list(chunks)

    async def receive():
        if not queue:
            return {"type": "http.disconnect"}
        pulled["n"] += 1
        data = queue.pop(0)
        return {"type": "http.request", "body": data, "more_body": bool(queue)}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": path, "headers": headers}
    await app(scope, receive, send)
    starts = [m for m in sent if m["type"] == "http.response.start"]
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    return starts, bodies, pulled["n"]


def middleware(inner, limit_bytes=1000):
    s = Settings(max_text_length=1)         # irrelevant; limits are overridden below
    mw = BodyLimitMiddleware(inner, s)
    mw.limits = {"/api/analyze-text": limit_bytes}
    return mw


def run(coro):
    return asyncio.run(coro)


def test_oversized_content_length_is_rejected_without_running_the_application():
    inner = Recorder()
    starts, bodies, pulled = run(drive(middleware(inner), [(b"content-length", b"1001")], [b"x" * 1001]))
    assert starts[0]["status"] == 413 and json.loads(bodies[0]["body"])["code"] == "PAYLOAD_TOO_LARGE"
    assert inner.called is False and pulled == 0                      # nothing was read, the app never ran


def test_content_length_at_the_limit_is_accepted():
    inner = Recorder()
    starts, _, _ = run(drive(middleware(inner), [(b"content-length", b"1000")], [b"x" * 1000]))
    assert starts[0]["status"] == 200 and inner.read == 1000


@pytest.mark.parametrize("value", [b"abc", b"-5", b"1e3", b"", b"1 000"])
def test_malformed_content_length_is_rejected(value):
    inner = Recorder()
    starts, bodies, _ = run(drive(middleware(inner), [(b"content-length", value)], [b"x"]))
    assert starts[0]["status"] == 400 and json.loads(bodies[0]["body"])["code"] == "INVALID_REQUEST" and inner.called is False


def test_missing_content_length_is_handled_by_counting_the_stream():
    inner = Recorder()
    chunks = [b"x" * 300] * 10                                       # 3000 bytes, no Content-Length header
    starts, bodies, pulled = run(drive(middleware(inner), [], chunks))
    assert len(starts) == 1 and starts[0]["status"] == 413           # the app's own 400 reply was replaced
    assert json.loads(bodies[0]["body"])["code"] == "PAYLOAD_TOO_LARGE"
    assert pulled == 4 and inner.read == 900                         # stopped right after crossing 1000 bytes


def test_an_understated_content_length_is_not_trusted():
    inner = Recorder()
    starts, _, pulled = run(drive(middleware(inner), [(b"content-length", b"10")], [b"x" * 400] * 20))
    assert starts[0]["status"] == 413 and pulled < 20


def test_a_stream_that_stays_within_the_limit_passes_untouched():
    inner = Recorder()
    starts, bodies, _ = run(drive(middleware(inner), [], [b"x" * 300] * 3))
    assert starts[0]["status"] == 200 and inner.read == 900 and json.loads(bodies[0]["body"]) == {"from": "app"}


def test_an_application_that_crashes_after_the_limit_still_yields_413_not_500():
    inner = Recorder(raise_after_disconnect=True)
    starts, _, _ = run(drive(middleware(inner), [], [b"x" * 600] * 5))
    assert [s["status"] for s in starts] == [413]


def test_the_application_that_ignores_the_disconnect_cannot_send_a_second_response():
    class Chatty:
        async def __call__(self, scope, receive, send):
            await receive(); await receive(); await receive()
            for _ in range(2):
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"hi"})

    starts, bodies, _ = run(drive(middleware(Chatty()), [], [b"x" * 600] * 5))
    assert [s["status"] for s in starts] == [413] and len(bodies) == 1


def test_other_scopes_and_other_paths_are_handled():
    inner = Recorder()
    mw = middleware(inner)
    starts, _, _ = run(drive(mw, [(b"content-length", b"70000")], [b"x"], path="/other"))      # default 64 KiB limit
    assert starts[0]["status"] == 413 and inner.called is False

    async def websocket():
        called = []

        async def app(scope, receive, send):
            called.append(scope["type"])

        await BodyLimitMiddleware(app, Settings())({"type": "websocket", "path": "/x", "headers": []}, None, None)
        return called

    assert run(websocket()) == ["websocket"]


# ------------------------------------------------------------------ through the real application
def small_app(provider=None, upload_dir=None, **over):
    values = dict(gemini_api_key="k", max_audio_size_mb=1, rate_limit_audio="100000/minute", rate_limit_text="100000/minute",
                  rate_limit_global="100000/minute", rate_limit_default="100000/minute")
    if upload_dir is not None:
        values["temp_upload_dir"] = str(upload_dir)
    values.update(over)
    provider = provider or FakeProvider()
    return create_app(Settings(**values), provider), provider


def test_an_oversized_audio_request_is_413_before_any_processing(upload_dir):
    app, provider = small_app(upload_dir=upload_dir)
    big = ms.wav() + b"\x00" * (3 * MB)                            # ~3 MB multipart body against a ~1 MB limit
    r = TestClient(app).post("/api/analyze-audio", files={"file": ("a.wav", big, "audio/wav")})
    assert r.status_code == 413 and r.json()["code"] == "PAYLOAD_TOO_LARGE"
    assert provider.audio_paths == [] and not list(upload_dir.rglob("*"))


@pytest.mark.parametrize("path", ["/api/analyze-text", "/api/v1/analyze/text"])
def test_an_oversized_text_request_is_rejected_by_the_body_limit(path):
    app, provider = small_app()
    r = TestClient(app).post(path, data={"text": "a" * 400_000})
    assert r.status_code == 413 and r.json()["code"] == "PAYLOAD_TOO_LARGE" and provider.text_calls == []


def test_text_over_the_character_limit_but_under_the_body_limit_gets_the_service_message():
    app, _ = small_app()
    r = TestClient(app).post("/api/analyze-text", data={"text": "a" * 25_000})
    assert r.status_code == 413 and r.json()["detail"] == "Text exceeds the 20,000 character limit."


def test_every_other_route_has_a_small_default_limit():
    app, _ = small_app()
    r = TestClient(app).post("/health", content=b"x" * (DEFAULT_BODY_LIMIT_BYTES + 1))
    assert r.status_code == 413


def test_a_chunked_upload_without_content_length_is_cut_off_early(upload_dir):
    """The upload arrives as a stream with NO Content-Length; the server stops reading shortly after the limit."""
    app, provider = small_app(upload_dir=upload_dir)
    produced = {"n": 0}
    total_chunks = 400

    preamble = b'--bnd\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\nContent-Type: audio/wav\r\n\r\n'

    async def body():
        yield preamble + ms.wav()                                    # a real multipart stream: the parser keeps reading
        for _ in range(total_chunks):
            produced["n"] += 1
            yield b"\x00" * 65_536                                   # 400 x 64 KiB = 25 MB offered, never terminated

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            return await http.post("/api/analyze-audio", content=body(), headers={"Content-Type": "multipart/form-data; boundary=bnd"})

    r = asyncio.run(go())
    assert r.status_code == 413 and r.json()["code"] == "PAYLOAD_TOO_LARGE"
    assert produced["n"] < total_chunks // 4                         # nowhere near the whole 25 MB was consumed
    assert provider.audio_paths == [] and not list(upload_dir.rglob("*"))


def test_the_final_on_disk_check_still_applies_to_files_inside_the_body_limit(upload_dir):
    """Body limit = size + 64 KiB overhead, so a file a little over the limit passes layers 1-2 and is stopped by layer 3."""
    app, provider = small_app(upload_dir=upload_dir)
    just_over = ms.wav() + b"\x00" * (MB - len(ms.wav()) + 1000)
    r = TestClient(app).post("/api/analyze-audio", files={"file": ("a.wav", just_over, "audio/wav")})
    assert r.status_code == 413 and r.json()["detail"] == "Audio file exceeds the 1 MB limit."
    assert provider.audio_paths == [] and not list(upload_dir.rglob("*"))


def test_413_responses_carry_security_headers_and_a_request_id():
    app, _ = small_app()
    r = TestClient(app).post("/api/analyze-text", data={"text": "a" * 400_000})
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-request-id"]
