"""POST /api/analyze-video, /api/v1/analyze/video and GET /api/video-status/{job_id}: validation, jobs, limits, cleanup.
ffprobe is a fake runner and the provider a FakeProvider; nothing touches Gemini or decodes media."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.core.errors import AnalysisError, ReasonCode
from tests import media_samples as ms
from tests.fakes import FakeProvider
from tests.video_helpers import (MP4_FORMAT, SUBMIT_PATH, WEBM_FORMAT, fake_runner, ffprobe_json, leftovers, post_video, raising_timeout,
                                 video_app, wait_for_job)

SUBMIT_PATHS = ["/api/v1/analyze/video", "/api/analyze-video"]
STATUS_PATHS = ["/api/v1/video-status/", "/api/video-status/"]


def open_client(app):
    return TestClient(app, raise_server_exceptions=False)


# ------------------------------------------------------------------ accepted uploads
@pytest.mark.parametrize("path", SUBMIT_PATHS)
def test_a_valid_upload_returns_202_with_a_job_and_location(upload_dir, path):
    with open_client(video_app(upload_dir)) as client:
        response = post_video(client, path=path)
        body = response.json()
        assert response.status_code == 202
        assert set(body) == {"job_id", "status", "stage", "progress", "created_at", "status_url"}
        assert body["status"] == "queued" and body["progress"] == 0 and len(body["job_id"]) >= 32
        assert response.headers["location"] == body["status_url"] == "/api/v1/video-status/" + body["job_id"]
        assert wait_for_job(client, body["job_id"])["status"] == "completed"


@pytest.mark.parametrize("name, mime, content, fmt", [
    ("clip.mp4", "video/mp4", ms.mp4(), MP4_FORMAT),
    ("clip.mov", "video/quicktime", ms.mov(), MP4_FORMAT),
    ("clip.webm", "video/webm", ms.webm(), WEBM_FORMAT),
])
def test_mp4_mov_and_webm_are_accepted_end_to_end(upload_dir, name, mime, content, fmt):
    provider = FakeProvider()
    with open_client(video_app(upload_dir, provider=provider, runner=fake_runner(ffprobe_json(format_name=fmt)))) as client:
        accepted = post_video(client, content, name, mime)
        assert accepted.status_code == 202
        done = wait_for_job(client, accepted.json()["job_id"])
    assert done["status"] == "completed" and done["result"]["mode"] == "video"
    assert provider.video_existed == [True] and leftovers(upload_dir) == []


def test_a_generic_declared_type_is_tolerated_because_the_content_decides(upload_dir):
    with open_client(video_app(upload_dir)) as client:
        assert post_video(client, mime="application/octet-stream").status_code == 202
        assert post_video(client, mime="").status_code == 202


# ------------------------------------------------------------------ rejected uploads (nothing reaches Gemini, nothing is left behind)
def rejected(upload_dir, *, expect_status, expect_code, content=None, name="clip.mp4", mime="video/mp4", runner=None, **settings):
    provider = FakeProvider()
    with open_client(video_app(upload_dir, provider=provider, runner=runner, **settings)) as client:
        response = post_video(client, content, name, mime)
        body = response.json()
        assert response.status_code == expect_status and body["code"] == expect_code, body
        assert set(body) == {"detail", "code"}
        assert client.app.state.jobs.active_count() == 0
    assert provider.video_paths == [] and leftovers(upload_dir) == []
    return body


def test_empty_file(upload_dir):
    rejected(upload_dir, expect_status=400, expect_code="INVALID_INPUT", content=b"")


def test_oversized_file_is_stopped_while_streaming(upload_dir):
    big = ms.mp4() + b"\x00" * (1024 * 1024 + 10)
    rejected(upload_dir, expect_status=413, expect_code="PAYLOAD_TOO_LARGE", content=big, max_video_size_mb=1)


def test_the_approved_size_limit_is_100_mb_and_configurable(upload_dir):
    from app.core.config import Settings
    assert Settings().max_video_size_mb == 100 and Settings().max_video_bytes == 100 * 1024 * 1024
    assert Settings(max_video_size_mb=7).max_video_bytes == 7 * 1024 * 1024


@pytest.mark.parametrize("path", SUBMIT_PATHS)
def test_the_body_limit_middleware_stops_a_huge_request_before_the_app_runs(upload_dir, path):
    provider = FakeProvider()
    with open_client(video_app(upload_dir, provider=provider, max_video_size_mb=1)) as client:
        response = client.post(path, files={"file": ("c.mp4", ms.mp4() + b"\x00" * (2 * 1024 * 1024), "video/mp4")})
    assert response.status_code == 413 and response.json()["code"] == "PAYLOAD_TOO_LARGE" and provider.video_paths == []


def test_video_longer_than_300_seconds(upload_dir):
    body = rejected(upload_dir, expect_status=422, expect_code="VIDEO_TOO_LONG", runner=fake_runner(ffprobe_json(duration="300.5")))
    assert "300" in body["detail"]


def test_exactly_300_seconds_is_accepted(upload_dir):
    with open_client(video_app(upload_dir, runner=fake_runner(ffprobe_json(duration="300")))) as client:
        assert post_video(client).status_code == 202


def test_corrupted_video(upload_dir):
    rejected(upload_dir, expect_status=422, expect_code="INVALID_VIDEO", runner=fake_runner(b"", returncode=1))


def test_video_without_a_video_stream(upload_dir):
    rejected(upload_dir, expect_status=422, expect_code="INVALID_VIDEO", runner=fake_runner(ffprobe_json(video=False)))


@pytest.mark.parametrize("output", [b"not json", b"{}", ffprobe_json(duration="N/A"), ffprobe_json(width=0)])
def test_malformed_metadata(upload_dir, output):
    rejected(upload_dir, expect_status=422, expect_code="INVALID_VIDEO", runner=fake_runner(output))


def test_ffprobe_timeout_is_a_rejection(upload_dir):
    rejected(upload_dir, expect_status=422, expect_code="INVALID_VIDEO", runner=fake_runner(raises=raising_timeout()))


def test_missing_ffprobe_fails_closed_with_503(upload_dir):
    provider = FakeProvider()
    with open_client(video_app(upload_dir, provider=provider, runner=fake_runner(raises=FileNotFoundError()))) as client:
        response = post_video(client)
    assert response.status_code == 503 and response.json()["code"] == "VIDEO_VALIDATION_UNAVAILABLE"
    assert response.headers["retry-after"] and provider.video_paths == [] and leftovers(upload_dir) == []


@pytest.mark.parametrize("name, mime", [("clip.avi", "video/x-msvideo"), ("clip.mkv", "video/x-matroska"), ("clip.flv", "video/x-flv"),
                                        ("clip.exe", "application/octet-stream"), ("clip", "video/mp4"), ("clip.mp4.exe", "video/mp4"),
                                        ("clip.mp3", "audio/mpeg"), ("clip.html", "text/html")])
def test_unsupported_formats_are_never_silently_accepted(upload_dir, name, mime):
    rejected(upload_dir, expect_status=415, expect_code="UNSUPPORTED_MEDIA_TYPE", name=name, mime=mime)


def test_incorrect_mime_type_for_the_extension(upload_dir):
    rejected(upload_dir, expect_status=415, expect_code="UNSUPPORTED_MEDIA_TYPE", mime="audio/mpeg")
    rejected(upload_dir, expect_status=415, expect_code="UNSUPPORTED_MEDIA_TYPE", name="clip.webm", mime="video/mp4", content=ms.webm())


@pytest.mark.parametrize("label", ["windows_executable", "linux_executable", "zip_archive", "shell_script", "html_page", "png_image",
                                   "pdf_document", "plain_text", "heic_image"])
def test_disguised_files_fail_the_signature_check(upload_dir, label):
    rejected(upload_dir, expect_status=415, expect_code="UNSUPPORTED_MEDIA_TYPE", content=ms.DISGUISED[label])


def test_container_that_contradicts_the_extension(upload_dir):
    rejected(upload_dir, expect_status=415, expect_code="UNSUPPORTED_MEDIA_TYPE", content=ms.mp4(), name="clip.webm", mime="video/webm")
    rejected(upload_dir, expect_status=415, expect_code="UNSUPPORTED_MEDIA_TYPE", content=ms.webm(), name="clip.mp4")


def test_ffprobe_reporting_a_different_container_is_rejected(upload_dir):
    rejected(upload_dir, expect_status=422, expect_code="INVALID_VIDEO", runner=fake_runner(ffprobe_json(format_name="avi")))


@pytest.mark.parametrize("name", ["../../etc/passwd.mp4", "..\\..\\evil.mp4", "a/b.mp4", "x" * 300 + ".mp4"])   # NUL/control characters are covered by media_validation's own tests (HTTP clients re-encode them)
def test_path_traversal_and_odd_filenames_are_refused(upload_dir, name):
    rejected(upload_dir, expect_status=400, expect_code="INVALID_INPUT", name=name)


def test_the_clients_filename_never_reaches_a_path_or_the_response(upload_dir):
    calls = []
    runner = fake_runner(calls=calls)
    with open_client(video_app(upload_dir, runner=runner)) as client:
        response = post_video(client, name="my-secret-holiday-video.mp4")
        done = wait_for_job(client, response.json()["job_id"])
    (args, _), = calls
    assert "my-secret-holiday-video" not in args[-1] and args[-1].endswith(".mp4")        # generated name, validated extension
    assert "my-secret-holiday-video" not in json.dumps(done) and "my-secret-holiday-video" not in response.text


def test_missing_file_field_is_422(upload_dir):
    with open_client(video_app(upload_dir)) as client:
        response = client.post(SUBMIT_PATH, data={})
    assert response.status_code == 422 and response.json()["code"] == "INVALID_REQUEST"


# ------------------------------------------------------------------ status endpoint
@pytest.mark.parametrize("prefix", STATUS_PATHS)
def test_unknown_and_malformed_ids_are_404_with_a_safe_body(upload_dir, prefix):
    with open_client(video_app(upload_dir)) as client:
        for job_id in ("a" * 32, "short", "../../etc/passwd", "x" * 200, "id with spaces", "%00"):
            response = client.get(prefix + job_id)
            assert response.status_code == 404 and response.json()["code"] in ("JOB_NOT_FOUND", "NOT_FOUND") and set(response.json()) == {"detail", "code"}


@pytest.mark.parametrize("prefix", STATUS_PATHS)
def test_completed_job_status_contains_the_validated_result(upload_dir, prefix):
    with open_client(video_app(upload_dir)) as client:
        job_id = post_video(client).json()["job_id"]
        wait_for_job(client, job_id)
        body = client.get(prefix + job_id).json()
    assert body["status"] == "completed" and body["stage"] == "finished" and body["progress"] == 100 and body["error"] is None
    assert body["result"]["status"] == "completed" and body["result"]["verdict"] == "high_risk" and body["expires_at"]
    assert {"job_id", "status", "stage", "progress", "created_at", "updated_at", "expires_at", "result", "error"} == set(body)
    assert {s["source"] for s in body["result"]["signals"]} <= {"video_visual", "video_text", "video_audio"}


def test_a_running_job_shows_safe_progress_without_a_result(upload_dir):
    provider = FakeProvider(delay=0.4)
    with open_client(video_app(upload_dir, provider=provider)) as client:
        job_id = post_video(client).json()["job_id"]
        seen = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            body = client.get("/api/v1/video-status/" + job_id).json()
            seen.append((body["status"], body["stage"], body["progress"]))
            if body["status"] in ("completed", "failed", "unable_to_assess"):
                break
            assert body["result"] is None and body["error"] is None and body["expires_at"] is None
            time.sleep(0.02)
    states = {s for s, _, _ in seen}
    assert "processing" in states and seen[-1][0] == "completed"
    progress = [p for _, _, p in seen]
    assert progress == sorted(progress)                                  # progress never goes backwards
    assert all(stage in {"queued", "starting", "uploading", "processing_media", "analysing", "scoring", "finished"} for _, stage, _ in seen)


def test_a_provider_failure_is_an_unable_to_assess_job_never_low_risk(upload_dir):
    provider = FakeProvider(error=AnalysisError(ReasonCode.PROVIDER_UNAVAILABLE, "down"))
    with open_client(video_app(upload_dir, provider=provider)) as client:
        body = wait_for_job(client, post_video(client).json()["job_id"])
    assert body["status"] == "unable_to_assess" and body["error"] is None
    result = body["result"]
    assert (result["status"], result["verdict"], result["risk_score"], result["reason_code"]) == ("unable_to_assess", "unable_to_assess", None, "PROVIDER_UNAVAILABLE")
    assert result["signals"] == [] and result["signal_scores"] is None and leftovers(upload_dir) == []


def test_an_unexpected_exception_fails_the_job_without_leaking_details(upload_dir):
    class Exploding(FakeProvider):
        async def analyze_video(self, path, on_stage=None):
            raise RuntimeError("secret /var/tmp/leak sk-abc")

    with open_client(video_app(upload_dir, provider=Exploding())) as client:
        body = wait_for_job(client, post_video(client).json()["job_id"])
        text = client.get("/api/v1/video-status/" + body["job_id"]).text
    assert body["status"] == "unable_to_assess" and body["result"]["reason_code"] == "INTERNAL_ANALYSIS_ERROR"
    assert "secret" not in text and "leak" not in text and "Traceback" not in text and leftovers(upload_dir) == []


def test_a_job_timeout_is_unable_to_assess_and_the_file_is_deleted(upload_dir):
    provider = FakeProvider(delay=5.0)
    with open_client(video_app(upload_dir, provider=provider, video_job_timeout_seconds=0.2)) as client:
        body = wait_for_job(client, post_video(client).json()["job_id"])
    assert body["status"] == "unable_to_assess" and body["result"]["reason_code"] == "PROVIDER_TIMEOUT" and body["result"]["risk_score"] is None
    assert leftovers(upload_dir) == []


def test_finished_jobs_expire(upload_dir):
    with open_client(video_app(upload_dir, video_job_ttl_seconds=0.3)) as client:
        job_id = post_video(client).json()["job_id"]
        wait_for_job(client, job_id)
        assert client.get("/api/v1/video-status/" + job_id).status_code in (200, 404)
        time.sleep(0.5)
        gone = client.get("/api/v1/video-status/" + job_id)
    assert gone.status_code == 404 and gone.json()["code"] == "JOB_NOT_FOUND"


# ------------------------------------------------------------------ limits
def test_queue_capacity_returns_503_and_cleans_up_the_refused_upload(upload_dir):
    provider = FakeProvider(delay=0.6)
    with open_client(video_app(upload_dir, provider=provider, video_max_active_jobs=1, video_max_active_jobs_per_client=5)) as client:
        first = post_video(client)
        second = post_video(client)
        assert first.status_code == 202
        assert second.status_code == 503 and second.json()["code"] == "SERVICE_BUSY" and second.headers["retry-after"]
        wait_for_job(client, first.json()["job_id"])
        assert post_video(client).status_code == 202                      # capacity returned
    assert len(provider.video_paths) <= 2


def test_one_client_cannot_queue_unlimited_jobs(upload_dir):
    provider = FakeProvider(delay=0.6)
    with open_client(video_app(upload_dir, provider=provider, video_max_active_jobs=10, video_max_active_jobs_per_client=2)) as client:
        codes = [post_video(client).status_code for _ in range(4)]
        bodies = client.get("/health").status_code
    assert codes[:2] == [202, 202] and codes[2:] == [429, 429] and bodies == 200


def test_video_submissions_are_rate_limited_per_client_across_both_paths(upload_dir):
    with open_client(video_app(upload_dir, rate_limit_video="2/minute", video_max_active_jobs=50, video_max_active_jobs_per_client=50)) as client:
        codes = [post_video(client, path=SUBMIT_PATHS[i % 2]).status_code for i in range(4)]
    assert codes == [202, 202, 429, 429]


def test_a_single_worker_processes_one_video_at_a_time(upload_dir):
    provider = FakeProvider(delay=0.15)
    with open_client(video_app(upload_dir, provider=provider, video_max_active_jobs=5, video_max_active_jobs_per_client=5)) as client:
        ids = [post_video(client).json()["job_id"] for _ in range(3)]
        for job_id in ids:
            wait_for_job(client, job_id)
    assert provider.max_in_flight == 1 and len(provider.video_paths) == 3


def test_a_refused_submission_is_rejected_before_the_upload_is_even_read(upload_dir):
    """At capacity the server must not spend bandwidth and disk on receiving up to 100 MB first."""
    from app.services.video_service import submit_video

    class Counting:
        filename, content_type = "clip.mp4", "video/mp4"
        reads = 0

        def __init__(self):
            self.sent = False

        async def read(self, size):
            Counting.reads += 1
            if self.sent:
                return b""
            self.sent = True
            return ms.mp4()

        async def close(self):
            pass

    provider = FakeProvider(delay=5.0)
    app = video_app(upload_dir, provider=provider, video_max_active_jobs=1)

    async def go():
        app.state.jobs.start()
        await submit_video(Counting(), jobs=app.state.jobs, settings=app.state.settings, owner="a", runner=fake_runner())
        reads_after_first = Counting.reads
        from app.core.errors import ServiceBusy
        with pytest.raises(ServiceBusy):
            await submit_video(Counting(), jobs=app.state.jobs, settings=app.state.settings, owner="b", runner=fake_runner())
        await app.state.jobs.aclose()
        return reads_after_first

    first = asyncio.run(go())
    assert Counting.reads == first


# ------------------------------------------------------------------ cleanup
def test_local_files_are_removed_after_every_outcome(upload_dir):
    for provider in (FakeProvider(), FakeProvider(error=AnalysisError(ReasonCode.PROVIDER_TIMEOUT)), FakeProvider(delay=5.0)):
        extra = {"video_job_timeout_seconds": 0.2} if provider.delay else {}
        with open_client(video_app(upload_dir, provider=provider, **extra)) as client:
            wait_for_job(client, post_video(client).json()["job_id"])
            assert leftovers(upload_dir) == []                  # deleted by the job itself, before any shutdown cleanup
        assert leftovers(upload_dir) == []


def test_shutdown_with_a_running_job_deletes_its_video(upload_dir):
    provider = FakeProvider(delay=30.0)
    app = video_app(upload_dir, provider=provider)
    with open_client(app) as client:
        post_video(client)
        deadline = time.monotonic() + 5
        while not provider.video_paths and time.monotonic() < deadline:
            time.sleep(0.01)
        assert provider.video_existed == [True]
    assert leftovers(upload_dir) == []                                    # lifespan shutdown removed it


def test_client_disconnect_while_uploading_removes_the_partial_file(upload_dir):
    from app.services.video_service import submit_video

    class Dropping:
        filename, content_type = "clip.mp4", "video/mp4"

        def __init__(self):
            self.n = 0

        async def read(self, size):
            self.n += 1
            if self.n == 1:
                return ms.mp4()
            raise asyncio.CancelledError()                               # the connection went away mid-upload

        async def close(self):
            pass

    app = video_app(upload_dir)

    async def go():
        with pytest.raises(asyncio.CancelledError):
            await submit_video(Dropping(), jobs=app.state.jobs, settings=app.state.settings, owner="c", runner=fake_runner())

    asyncio.run(go())
    assert leftovers(upload_dir) == [] and app.state.jobs.active_count() == 0


def test_an_error_after_the_file_was_stored_still_deletes_it(upload_dir):
    def boom(args, timeout):
        raise ValueError("unexpected")

    with open_client(video_app(upload_dir, runner=boom)) as client:
        response = post_video(client)
    assert response.status_code == 500 and leftovers(upload_dir) == []
    assert "unexpected" not in response.text and "Traceback" not in response.text


# ------------------------------------------------------------------ safety of responses and existing endpoints
def test_responses_never_expose_paths_keys_or_internals(upload_dir, tmp_path):
    with open_client(video_app(upload_dir)) as client:
        accepted = post_video(client)
        done = wait_for_job(client, accepted.json()["job_id"])
    blob = accepted.text + json.dumps(done)
    for forbidden in (str(upload_dir), str(tmp_path), "scamshield-uploads", "test-key-not-real", "Traceback", "up-", ".mp4\\", "files/"):
        assert forbidden not in blob, forbidden


def test_responses_are_not_cacheable_and_carry_the_security_headers(upload_dir):
    with open_client(video_app(upload_dir)) as client:
        response = post_video(client)
        status = client.get("/api/v1/video-status/" + response.json()["job_id"])
    for r in (response, status):
        assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


def test_text_and_audio_endpoints_still_work_alongside_video(upload_dir):
    with open_client(video_app(upload_dir)) as client:
        assert client.post("/api/v1/analyze/text", data={"text": "send otp now"}).json()["mode"] == "text"
        audio = client.post("/api/v1/analyze/audio", files={"file": ("a.wav", ms.wav(), "audio/wav")}).json()
        assert audio["mode"] == "audio" and client.get("/health").status_code == 200
        assert client.post("/api/analyze-text", data={"text": "hello"}).status_code == 200


def test_no_job_means_no_background_tasks_and_the_app_starts_cleanly(upload_dir):
    with open_client(video_app(upload_dir)) as client:
        assert client.get("/health").status_code == 200 and client.app.state.jobs.active_count() == 0
