"""The in-memory job manager: lifecycle, valid transitions, bounds, expiry, cleanup. No HTTP and no Gemini."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.core.config import Settings
from app.core.errors import ReasonCode, ServiceBusy
from app.schemas.analysis import AnalysisResponse
from app.schemas.jobs import TERMINAL_STATUSES, TRANSITIONS, JobStatus, VideoJobStatus
from app.services import risk_engine
from app.services.response_builder import build_completed_response
from app.services.video_jobs import InvalidTransition, JobManager, TooManyJobs
from tests.fakes import make_report, make_signal


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def completed_response() -> AnalysisResponse:
    report = make_report([make_signal("credential_request", "critical", "strong", source="video_audio")])
    return build_completed_response(risk_engine.assess(report), "video")


def failed_response(reason=ReasonCode.PROVIDER_UNAVAILABLE) -> AnalysisResponse:
    return AnalysisResponse.unable_to_assess("video", reason)


def make_manager(tmp_path, analyze, **overrides):
    clock = Clock()
    settings = Settings(**{"video_job_ttl_seconds": 100.0, "video_job_timeout_seconds": 5.0, **overrides})
    return JobManager(settings, analyze, clock), clock


def new_work(tmp_path, name):
    work = tmp_path / name
    work.mkdir()
    path = work / "video.mp4"
    path.write_bytes(b"x" * 64)
    return path, work


async def settle(manager, job_id, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        job = manager.get(job_id)
        if job is None or not job.active:
            return job
        assert loop.time() < end, "job did not finish"
        await asyncio.sleep(0.005)


def run(coro):
    return asyncio.run(coro)


def submit(manager, tmp_path, name="a", owner="client-1"):
    path, work = new_work(tmp_path, name)
    return manager.submit(owner, path, work, size=64, duration=12.0), work


# ------------------------------------------------------------------ creation
def test_job_creation_is_queued_with_an_unpredictable_id_and_no_paths(tmp_path):
    async def go():
        gate = asyncio.Event()

        async def analyze(path, size, duration, on_stage):
            await gate.wait()
            return completed_response()

        manager, _ = make_manager(tmp_path, analyze)
        accepted, _ = submit(manager, tmp_path)
        other, _ = submit(manager, tmp_path, "b")
        status = manager.public_status(manager.get(accepted.job_id))
        gate.set()
        await manager.aclose()
        return accepted, other, status

    accepted, other, status = run(go())
    assert accepted.status == JobStatus.QUEUED and accepted.stage == "queued" and accepted.progress == 0
    assert len(accepted.job_id) >= 32 and accepted.job_id != other.job_id
    assert accepted.status_url == "/api/v1/video-status/" + accepted.job_id
    dumped = json.dumps(status.model_dump(mode="json"))
    assert str(tmp_path) not in dumped and "video.mp4" not in dumped and "path" not in dumped.replace("status_path", "")


def test_job_ids_do_not_repeat(tmp_path):
    async def go():
        async def analyze(path, size, duration, on_stage):
            return completed_response()

        manager, _ = make_manager(tmp_path, analyze, video_max_active_jobs=500, video_max_active_jobs_per_client=500)
        ids = {submit(manager, tmp_path, f"j{i}")[0].job_id for i in range(200)}
        await manager.aclose()
        return ids

    assert len(run(go())) == 200


# ------------------------------------------------------------------ lifecycle and cleanup
def test_completed_job_stores_the_result_and_deletes_the_video(tmp_path):
    async def go():
        stages = []

        async def analyze(path, size, duration, on_stage):
            assert path.exists() and (size, duration) == (64, 12.0)
            on_stage("uploading"); on_stage("analysing")
            stages.append(manager.get(accepted.job_id).stage)
            return completed_response()

        manager, _ = make_manager(tmp_path, analyze)
        accepted, work = submit(manager, tmp_path)
        job = await settle(manager, accepted.job_id)
        status = manager.public_status(job)
        work_exists = work.exists()                      # BEFORE aclose(): the job itself must have deleted the video
        await manager.aclose()
        return job, status, work_exists, stages

    job, status, work_exists, stages = run(go())
    assert job.status == JobStatus.COMPLETED and status.result.verdict == "high_risk" and status.progress == 100
    assert stages == ["analysing"] and status.expires_at is not None
    assert work_exists is False


def test_unable_to_assess_result_is_its_own_final_status_and_never_low_risk(tmp_path):
    async def go():
        async def analyze(path, size, duration, on_stage):
            return failed_response()

        manager, _ = make_manager(tmp_path, analyze)
        accepted, work = submit(manager, tmp_path)
        job = await settle(manager, accepted.job_id)
        exists = work.exists()
        await manager.aclose()
        return manager.public_status(job), exists

    status, exists = run(go())
    assert status.status == JobStatus.UNABLE_TO_ASSESS and status.result.risk_score is None
    assert status.result.verdict == "unable_to_assess" and status.result.reason_code == "PROVIDER_UNAVAILABLE"
    assert exists is False


def test_unexpected_exception_fails_the_job_safely_and_cleans_up(tmp_path):
    async def go():
        async def analyze(path, size, duration, on_stage):
            raise RuntimeError("secret internal detail /tmp/x")

        manager, _ = make_manager(tmp_path, analyze)
        accepted, work = submit(manager, tmp_path)
        job = await settle(manager, accepted.job_id)
        exists = work.exists()
        await manager.aclose()
        return manager.public_status(job), exists

    status, exists = run(go())
    assert status.status == JobStatus.FAILED and status.error.code == "INTERNAL_ERROR" and status.result is None
    assert "secret" not in json.dumps(status.model_dump(mode="json")) and exists is False


def test_processing_timeout_gives_unable_to_assess_and_cleans_up(tmp_path):
    async def go():
        async def analyze(path, size, duration, on_stage):
            await asyncio.sleep(30)

        manager, _ = make_manager(tmp_path, analyze, video_job_timeout_seconds=0.05)
        accepted, work = submit(manager, tmp_path)
        job = await settle(manager, accepted.job_id)
        exists = work.exists()
        await manager.aclose()
        return manager.public_status(job), exists

    status, exists = run(go())
    assert status.status == JobStatus.UNABLE_TO_ASSESS and status.result.reason_code == "PROVIDER_TIMEOUT"
    assert status.result.risk_score is None and exists is False


def test_shutdown_fails_running_and_queued_jobs_and_deletes_every_video(tmp_path):
    async def go():
        started = asyncio.Event()

        async def analyze(path, size, duration, on_stage):
            started.set()
            await asyncio.sleep(30)

        manager, _ = make_manager(tmp_path, analyze)
        first, w1 = submit(manager, tmp_path, "a")
        second, w2 = submit(manager, tmp_path, "b", owner="client-2")
        await asyncio.wait_for(started.wait(), 2)
        await manager.aclose()
        return [manager.public_status(manager.get(j.job_id)) for j in (first, second)], (w1, w2)

    statuses, works = run(go())
    assert all(s.status == JobStatus.FAILED and s.error.code in {"SERVER_SHUTDOWN", "JOB_CANCELLED"} for s in statuses)
    assert not any(w.exists() for w in works)


# ------------------------------------------------------------------ transitions
def test_transition_table_is_closed_and_final_states_have_no_exits():
    for status in TERMINAL_STATUSES:
        assert TRANSITIONS[status] == frozenset()
    assert TRANSITIONS[JobStatus.QUEUED] == {JobStatus.PROCESSING, JobStatus.FAILED}
    assert JobStatus.COMPLETED not in TRANSITIONS[JobStatus.QUEUED]        # a job cannot complete without processing


def test_invalid_transitions_raise(tmp_path):
    async def go():
        async def analyze(path, size, duration, on_stage):
            return completed_response()

        manager, _ = make_manager(tmp_path, analyze)
        accepted, _ = submit(manager, tmp_path)
        job = await settle(manager, accepted.job_id)
        with pytest.raises(InvalidTransition):
            manager._transition(job, JobStatus.PROCESSING)               # final -> anything
        queued_manager, _ = make_manager(tmp_path, analyze)
        path, work = new_work(tmp_path, "c")
        from app.services.video_jobs import VideoJob
        fresh = VideoJob(id="x" * 20, owner="o", created_at=0, updated_at=0)
        with pytest.raises(InvalidTransition):
            queued_manager._transition(fresh, JobStatus.COMPLETED)       # queued -> completed skips processing
        await manager.aclose()

    run(go())


def test_status_schema_rejects_inconsistent_combinations():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    base = dict(job_id="j", stage="queued", progress=0, created_at=now, updated_at=now)
    for bad in (dict(status="completed"),                                           # completed without a result
                dict(status="queued", result=completed_response()),                 # queued with a result
                dict(status="completed", result=failed_response(), expires_at=now),  # completed carrying a failure result
                dict(status="unable_to_assess", result=completed_response(), expires_at=now),
                dict(status="failed", expires_at=now),                              # failed without an error
                dict(status="completed", result=completed_response())):             # finished without an expiry
        with pytest.raises(ValueError):
            VideoJobStatus(**base, **bad)


# ------------------------------------------------------------------ expiry
def test_finished_jobs_expire_after_the_ttl_and_unknown_ids_return_none(tmp_path):
    async def go():
        async def analyze(path, size, duration, on_stage):
            return completed_response()

        manager, clock = make_manager(tmp_path, analyze)
        accepted, _ = submit(manager, tmp_path)
        await settle(manager, accepted.job_id)
        clock.now += 99
        still = manager.get(accepted.job_id) is not None
        clock.now += 2
        gone = manager.get(accepted.job_id) is None
        unknown = manager.get("no-such-job-id-1234567890") is None
        await manager.aclose()
        return still, gone, unknown

    assert run(go()) == (True, True, True)


def test_an_active_job_never_expires_and_expiry_deletes_leftover_files(tmp_path):
    async def go():
        gate = asyncio.Event()

        async def analyze(path, size, duration, on_stage):
            await gate.wait()
            return completed_response()

        manager, clock = make_manager(tmp_path, analyze)
        accepted, work = submit(manager, tmp_path)
        clock.now += 10_000
        alive = manager.get(accepted.job_id) is not None
        gate.set()
        await settle(manager, accepted.job_id)
        await manager.aclose()
        return alive, work.exists()

    assert run(go()) == (True, False)


# ------------------------------------------------------------------ bounds
def test_only_the_configured_number_of_jobs_run_at_once(tmp_path):
    async def go(workers):
        running = peak = 0

        async def analyze(path, size, duration, on_stage):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.03)
            running -= 1
            return completed_response()

        manager, _ = make_manager(tmp_path, analyze, video_workers=workers, video_max_active_jobs=10, video_max_active_jobs_per_client=10)
        ids = [submit(manager, tmp_path, f"j{i}", owner=f"c{i}")[0].job_id for i in range(5)]
        for job_id in ids:
            await settle(manager, job_id)
        await manager.aclose()
        return peak

    assert run(go(1)) == 1                  # the approved single worker
    assert run(go(2)) == 2


def test_capacity_limit_refuses_new_jobs_and_leaves_the_files_with_the_caller(tmp_path):
    async def go():
        gate = asyncio.Event()

        async def analyze(path, size, duration, on_stage):
            await gate.wait()
            return completed_response()

        manager, _ = make_manager(tmp_path, analyze, video_max_active_jobs=2, video_max_active_jobs_per_client=5)
        submit(manager, tmp_path, "a", owner="c1")
        submit(manager, tmp_path, "b", owner="c2")
        path, work = new_work(tmp_path, "refused")
        with pytest.raises(ServiceBusy):
            manager.submit("c3", path, work, size=1, duration=1.0)
        refused_exists = work.exists()                          # the manager did not take ownership of it
        gate.set()
        await manager.aclose()
        return refused_exists

    assert run(go()) is True


def test_a_single_client_cannot_fill_the_queue(tmp_path):
    async def go():
        gate = asyncio.Event()

        async def analyze(path, size, duration, on_stage):
            await gate.wait()
            return completed_response()

        manager, _ = make_manager(tmp_path, analyze, video_max_active_jobs=10, video_max_active_jobs_per_client=2)
        submit(manager, tmp_path, "a", owner="same")
        submit(manager, tmp_path, "b", owner="same")
        with pytest.raises(TooManyJobs) as info:
            submit(manager, tmp_path, "c", owner="same")
        submit(manager, tmp_path, "d", owner="someone-else")      # another client is unaffected
        gate.set()
        await manager.aclose()
        return info.value.status_code

    assert run(go()) == 429


def test_capacity_frees_up_when_jobs_finish(tmp_path):
    async def go():
        async def analyze(path, size, duration, on_stage):
            return completed_response()

        manager, _ = make_manager(tmp_path, analyze, video_max_active_jobs=1)
        first, _ = submit(manager, tmp_path, "a")
        await settle(manager, first.job_id)
        second, _ = submit(manager, tmp_path, "b")
        await settle(manager, second.job_id)
        await manager.aclose()
        return True

    assert run(go())


def test_a_crashing_analysis_does_not_kill_the_worker(tmp_path):
    async def go():
        calls = []

        async def analyze(path, size, duration, on_stage):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("first one explodes")
            return completed_response()

        manager, _ = make_manager(tmp_path, analyze, video_max_active_jobs=5, video_max_active_jobs_per_client=5)
        a, _ = submit(manager, tmp_path, "a")
        b, _ = submit(manager, tmp_path, "b")
        ja, jb = await settle(manager, a.job_id), await settle(manager, b.job_id)
        await manager.aclose()
        return ja.status, jb.status

    assert run(go()) == (JobStatus.FAILED, JobStatus.COMPLETED)
