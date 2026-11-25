"""The audio/text endpoints must not block the event loop while the provider works.

The Gemini adapter uses the SDK's native async client, so there is no synchronous provider call
to push to a thread. These tests verify the OBSERVABLE property: while a slow provider call is in
flight, other requests are served promptly and the loop keeps ticking. A negative control proves
the measurement would catch a blocking implementation.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from app.core.config import Settings
from app.main import create_app
from app.providers.gemini import GeminiProvider
from tests.fakes import FakeProvider, load_fixture_bytes

WAV = ("c.wav", load_fixture_bytes("tiny.wav"), "audio/wav")
SLOW = 0.5


async def measure(app, request_coro_factory) -> dict:
    """Run one slow request while a heartbeat task and a /health call observe the loop."""
    transport = httpx.ASGITransport(app=app)
    lags: list[float] = []
    stop = asyncio.Event()

    async def heartbeat():
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            lags.append(now - last - 0.01)
            last = now

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        beat = asyncio.create_task(heartbeat())
        slow = asyncio.create_task(request_coro_factory(http))
        await asyncio.sleep(0.1)                            # let the slow request reach the provider
        t0 = time.perf_counter()
        health = await http.get("/health")
        health_seconds = time.perf_counter() - t0
        slow_response = await slow
        stop.set()
        await beat
    return {"health_ok": health.status_code == 200, "health_seconds": health_seconds,
            "max_lag": max(lags), "slow_status": slow_response.status_code}


def audio_request(http):
    return http.post("/api/analyze-audio", files={"file": WAV})


def test_audio_endpoint_does_not_block_the_event_loop(fake_sdk):
    fake_sdk.generate_delay = SLOW                         # async sleep inside the (fake) Gemini call
    settings = Settings(gemini_api_key="k", gemini_timeout_seconds=5)
    app = create_app(settings, GeminiProvider(settings, client_factory=fake_sdk))
    result = asyncio.run(measure(app, audio_request))
    assert result["slow_status"] == 200 and result["health_ok"]
    assert result["health_seconds"] < 0.2, result           # served while the audio call is in flight
    assert result["max_lag"] < 0.15, result                 # loop kept ticking


def test_text_endpoint_does_not_block_the_event_loop(fake_sdk):
    fake_sdk.generate_delay = SLOW
    settings = Settings(gemini_api_key="k", gemini_timeout_seconds=5)
    app = create_app(settings, GeminiProvider(settings, client_factory=fake_sdk))
    result = asyncio.run(measure(app, lambda http: http.post("/api/analyze-text", data={"text": "hi"})))
    assert result["health_seconds"] < 0.2 and result["max_lag"] < 0.15, result


def test_negative_control_a_blocking_provider_IS_detected(settings):
    """If a provider blocked the loop (time.sleep), this harness must notice."""
    app = create_app(settings, FakeProvider(delay=SLOW, blocking=True))
    result = asyncio.run(measure(app, audio_request))
    assert result["max_lag"] > 0.3, result


def test_concurrent_requests_overlap_instead_of_queueing(fake_sdk):
    fake_sdk.generate_delay = 0.3
    settings = Settings(gemini_api_key="k", gemini_timeout_seconds=5, gemini_max_concurrency=4)
    app = create_app(settings, GeminiProvider(settings, client_factory=fake_sdk))

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
            t0 = time.perf_counter()
            rs = await asyncio.gather(*(http.post("/api/analyze-text", data={"text": f"m{i}"}) for i in range(4)))
            return time.perf_counter() - t0, [r.status_code for r in rs]

    elapsed, statuses = asyncio.run(go())
    assert statuses == [200] * 4 and elapsed < 0.9          # ~0.3s if overlapped, ~1.2s if serialized


def test_provider_concurrency_is_capped_by_settings():
    provider = FakeProvider(delay=0.1)
    app = create_app(Settings(gemini_api_key="k", gemini_max_concurrency=2), provider)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
            await asyncio.gather(*(http.post("/api/analyze-text", data={"text": f"m{i}"}) for i in range(6)))

    asyncio.run(go())
    assert provider.max_in_flight == 2 and len(provider.text_calls) == 6
