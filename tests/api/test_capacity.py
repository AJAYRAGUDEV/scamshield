"""Bounded concurrency: running + waiting analyses are capped; overload gets a clear 503; capacity is always released."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import AnalysisError, ReasonCode, ServiceBusy
from app.main import create_app
from app.providers.gemini import GeminiProvider
from app.services.analysis_pipeline import AnalysisPipeline
from tests import media_samples as ms
from tests.fakes import FakeProvider

WAV = ms.wav()


def build(provider, upload_dir=None, **over):
    values = dict(gemini_api_key="k", rate_limit_text="100000/minute", rate_limit_audio="100000/minute",
                  rate_limit_global="100000/minute", rate_limit_default="100000/minute")
    if upload_dir is not None:
        values["temp_upload_dir"] = str(upload_dir)
    values.update(over)
    return create_app(Settings(**values), provider)


async def many(app, n, kind="text"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
        def one(i):
            if kind == "text":
                return http.post("/api/analyze-text", data={"text": f"message {i}"})
            return http.post("/api/analyze-audio", files={"file": ("c.wav", WAV, "audio/wav")})
        return await asyncio.gather(*(one(i) for i in range(n)))


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ overload => 503, never an unbounded queue
def test_requests_beyond_running_plus_waiting_capacity_get_503():
    provider = FakeProvider(delay=0.4)
    app = build(provider, gemini_max_concurrency=1, gemini_max_queue=1)
    codes = sorted(r.status_code for r in run(many(app, 3)))
    assert codes == [200, 200, 503]                                  # one running, one waiting, one refused
    assert provider.max_in_flight == 1


def test_the_busy_response_is_clear_and_retryable():
    app = build(FakeProvider(delay=0.4), gemini_max_concurrency=1, gemini_max_queue=0)
    busy = [r for r in run(many(app, 2)) if r.status_code == 503]
    assert len(busy) == 1
    r = busy[0]
    assert r.json() == {"detail": "The service is at capacity right now. Please try again in a few seconds.", "code": "SERVICE_BUSY"}
    assert r.headers["retry-after"] == "5"


def test_a_large_burst_is_bounded_and_only_capacity_worth_of_work_is_accepted():
    provider = FakeProvider(delay=0.3)
    app = build(provider, gemini_max_concurrency=2, gemini_max_queue=3)
    responses = run(many(app, 50))
    ok = [r for r in responses if r.status_code == 200]
    busy = [r for r in responses if r.status_code == 503]
    assert len(ok) == 5 and len(busy) == 45                          # 2 running + 3 waiting; the rest refused at once
    assert provider.max_in_flight == 2 and len(provider.text_calls) == 5


def test_capacity_is_released_after_the_burst():
    provider = FakeProvider(delay=0.2)
    app = build(provider, gemini_max_concurrency=1, gemini_max_queue=0)
    run(many(app, 4))
    assert app.state.pipeline.admitted == 0
    assert [r.status_code for r in run(many(app, 1))] == [200]


def test_audio_that_is_refused_for_capacity_is_never_written_to_disk(upload_dir):
    provider = FakeProvider(delay=0.4)
    app = build(provider, upload_dir=upload_dir, gemini_max_concurrency=1, gemini_max_queue=0)
    responses = run(many(app, 3, kind="audio"))
    assert sorted(r.status_code for r in responses) == [200, 503, 503]
    assert len(provider.audio_paths) == 1 and not list(upload_dir.rglob("*"))


# ------------------------------------------------------------------ capacity is released on every exit path
@pytest.mark.parametrize("error", [AnalysisError(ReasonCode.PROVIDER_TIMEOUT), AnalysisError(ReasonCode.PROVIDER_UNAVAILABLE),
                                   RuntimeError("boom")])
def test_capacity_is_released_after_provider_failures(error):
    app = build(FakeProvider(error=error), gemini_max_concurrency=1, gemini_max_queue=0)
    for _ in range(5):                                               # a leak would turn these into 503s
        assert [r.status_code for r in run(many(app, 1))] == [200]
    assert app.state.pipeline.admitted == 0


def test_capacity_is_released_after_a_real_adapter_timeout(fake_sdk):
    fake_sdk.generate_delay = 2.0
    settings = Settings(gemini_api_key="k", gemini_timeout_seconds=0.05, gemini_max_concurrency=1, gemini_max_queue=0,
                        rate_limit_text="100000/minute", rate_limit_global="100000/minute")
    app = create_app(settings, GeminiProvider(settings, client_factory=fake_sdk))
    for _ in range(3):
        responses = run(many(app, 1))
        assert responses[0].status_code == 200 and responses[0].json()["reason_code"] == "PROVIDER_TIMEOUT"
    assert app.state.pipeline.admitted == 0


def test_capacity_is_released_when_validation_fails_after_admission(upload_dir):
    app = build(FakeProvider(), upload_dir=upload_dir, gemini_max_concurrency=1, gemini_max_queue=0)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
            for _ in range(3):
                r = await http.post("/api/analyze-audio", files={"file": ("c.wav", b"not audio at all, no way", "audio/wav")})
                assert r.status_code == 415                           # rejected by the content check
            return await http.post("/api/analyze-audio", files={"file": ("c.wav", WAV, "audio/wav")})

    assert run(go()).status_code == 200 and app.state.pipeline.admitted == 0


def test_capacity_is_released_when_a_request_is_cancelled():
    provider = FakeProvider(delay=5)
    pipeline = AnalysisPipeline(provider, max_concurrency=1, max_queue=0)

    async def go():
        async def work():
            async with pipeline.admit():
                await pipeline.run_text("x")

        task = asyncio.create_task(work())
        await asyncio.sleep(0.05)
        assert pipeline.admitted == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with pipeline.admit():                                  # would raise ServiceBusy if capacity leaked
            pass

    run(go())
    assert pipeline.admitted == 0


# ------------------------------------------------------------------ the gate itself
def test_admit_refuses_exactly_when_full_and_counts_correctly():
    pipeline = AnalysisPipeline(FakeProvider(), max_concurrency=2, max_queue=1)

    async def go():
        async with pipeline.admit():
            async with pipeline.admit():
                async with pipeline.admit():
                    assert pipeline.admitted == 3
                    with pytest.raises(ServiceBusy) as exc:
                        async with pipeline.admit():
                            pass
                    assert exc.value.status_code == 503 and exc.value.headers == {"Retry-After": "5"}
                assert pipeline.admitted == 2

    run(go())
    assert pipeline.admitted == 0


def test_a_refused_admission_does_not_consume_capacity():
    pipeline = AnalysisPipeline(FakeProvider(), max_concurrency=1, max_queue=0)

    async def go():
        async with pipeline.admit():
            for _ in range(10):
                with pytest.raises(ServiceBusy):
                    async with pipeline.admit():
                        pass
            assert pipeline.admitted == 1

    run(go())
    assert pipeline.admitted == 0


def test_no_threads_are_created_for_waiting_requests():
    """Waiting is a counter and a semaphore, not a thread pool: a burst does not grow the thread count."""
    import threading

    provider = FakeProvider(delay=0.2)
    app = build(provider, gemini_max_concurrency=2, gemini_max_queue=10)
    before = threading.active_count()
    run(many(app, 12))
    assert threading.active_count() <= before + 1
