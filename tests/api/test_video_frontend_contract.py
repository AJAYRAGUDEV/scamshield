"""Static checks that the Phase 9 frontend matches the REAL Phase 8 backend contract (names, limits, endpoints) and keeps the
project's frontend safety rules. Behaviour is tested in a real browser by tests/frontend/test_video_ui.py."""
from __future__ import annotations

import re
from typing import get_args

from app.core.config import Settings
from app.schemas.evidence import EvidenceSource
from app.schemas.jobs import STAGE_PROGRESS, TERMINAL_STATUSES, JobStatus
from app.services.media_validation import VIDEO_MIME_TYPES
from tests.api.test_frontend_contract import ALL, CODE, HTML

VIDEO, RESULT, API, RENDER, MAIN, STATE = (ALL[n] for n in ("video", "result", "api", "render", "main", "state"))


def js_list(source: str, name: str) -> list[str]:
    match = re.search(rf"{name} = \[([^\]]*)\]", source)
    assert match, name
    return re.findall(r"'([^']+)'", match.group(1))


def test_the_frontend_knows_exactly_the_job_statuses_the_backend_has():
    assert set(js_list(RESULT, "VIDEO_JOB_STATUSES")) == {s.value for s in JobStatus}
    assert set(js_list(RESULT, "VIDEO_TERMINAL_STATUSES")) == {s.value for s in TERMINAL_STATUSES}


def test_every_backend_stage_has_a_user_facing_text():
    block = VIDEO.split("const STAGE_TEXT = {")[1].split("};")[0]
    assert set(re.findall(r"^\s+(\w+):", block, re.M)) == set(STAGE_PROGRESS)


def test_the_client_limits_and_formats_mirror_the_backend_defaults():
    s = Settings()
    assert set(js_list(STATE, "VIDEO_EXTENSIONS")) == set(VIDEO_MIME_TYPES)
    assert f"MAX_VIDEO_BYTES = {s.max_video_size_mb} * 1024 * 1024" in STATE and f"MAX_VIDEO_SECONDS = {s.max_video_duration_seconds}" in STATE
    accept = re.search(r'id="videoFileInput"[^>]*accept="([^"]+)"', HTML).group(1)
    assert all(ext in accept for ext in VIDEO_MIME_TYPES)
    assert "Max 100 MB" in HTML and "Max 5 minutes" in HTML


def test_the_page_states_the_server_is_the_authority_and_where_the_video_goes():
    text = re.sub(r"<[^>]+>", " ", HTML)
    assert "authoritative validation" in text and "Gemini" in text and "deletes its copies" in text


def test_requests_use_the_real_endpoints_and_the_real_field_name(client):
    paths = set(client.get("/openapi.json").json()["paths"])
    assert "ENDPOINTS" in API and "video: '/api/analyze-video'" in API and "videoStatus: '/api/video-status/'" in API
    assert "/api/analyze-video" in paths and "/api/video-status/{job_id}" in paths
    submit = API.split("export async function submitVideo")[1].split("export async function fetchVideoJob")[0]
    assert "form.append('file', file)" in submit and "method: 'POST'" in submit and "validateVideoSubmission(data)" in submit
    poll = API.split("export async function fetchVideoJob")[1]
    assert "encodeURIComponent(jobId)" in poll and "method: 'GET'" in poll and "isValidJobId(jobId)" in poll and "validateVideoJob(data)" in poll
    assert "job.jobId !== jobId" in poll                                   # a response for a different job is rejected


def test_the_job_id_pattern_matches_what_the_backend_generates():
    from app.services.video_service import JOB_ID
    assert JOB_ID.pattern in RESULT.replace("^", "").replace("$", "") or "{16,64}" in RESULT
    assert re.search(r"JOB_ID = /\^\[A-Za-z0-9_-\]\{16,64\}\$/", RESULT)
    import secrets
    assert JOB_ID.fullmatch(secrets.token_urlsafe(24))


def test_progress_and_completion_are_only_ever_taken_from_the_server():
    assert "Number.isInteger(value) && value >= 0 && value <= 100 ? value : null" in RESULT            # invalid progress becomes null, never a guess
    assert "setScannerStatus(" in VIDEO and "update.progress" in VIDEO and "accepted.progress" in VIDEO
    assert "Math.random" not in VIDEO and "setInterval" not in VIDEO                                   # nothing fabricated, no timer-driven bar
    scan = ALL["scan"]
    assert "known ? `${Math.max(0, Math.min(100, percent))}%` : '35%'" in scan and "animate-pulse" in scan   # no value -> indeterminate pulse


def test_polling_is_sequential_bounded_and_cancellable():
    wait = VIDEO.split("export async function waitForJob")[1].split("/** Stops watching")[0]
    assert wait.count("await fetchVideoJob(") == 1 and "for (;;)" in wait and "await pause(" in wait   # one request at a time
    for guard in ("polling_active", "polling.maxWaitMs", "polling.maxConsecutiveErrors", "signal.aborted", "VIDEO_TERMINAL_STATUSES.includes(job.status)",
                  "error.status === 404", "POLL_TIMEOUT", "POLLING_FAILED"):
        assert guard in wait, guard
    assert "finally {\n    polling_active = false;" in wait
    assert "setInterval" not in VIDEO and "export const polling = {" in VIDEO
    assert "intervalMs: 2000" in VIDEO and "maxWaitMs: 6 * 60 * 1000" in VIDEO
    assert "cancelVideoWatch" in MAIN and "'pagehide'" in MAIN.split("bindVideoEvents();")[1]


def test_a_second_job_cannot_start_while_one_is_active_and_everything_is_cleared_in_finally():
    trigger = VIDEO.split("export async function triggerVideoScan")[1].split("// ---- events")[0]
    assert "if (state.isScanning || state.videoJob) return;" in trigger and "el.btnAnalyzeVideo.disabled = true" in trigger
    final = trigger.split("finally {")[1].split("if (result &&")[0]
    for cleanup in ("stopScanAnimation();", "btnStopVideoWait.classList.add('hidden')", "btnAnalyzeVideo.disabled = false", "state.videoJob = null"):
        assert cleanup in final, cleanup
    assert "isCurrentRequest(requestId)" in trigger and "beginRequest()" in trigger


def test_demo_mode_never_produces_a_video_result():
    trigger = VIDEO.split("export async function triggerVideoScan")[1]
    gate = trigger.split("if (!isLiveMode()) {")[1].split("}")[0]
    assert "return;" in gate and "makeUnableResult" not in gate and "renderResult" not in gate
    assert "sim" not in {t for _, t in __import__("tests.api.test_frontend_contract", fromlist=["imports_of"]).imports_of(VIDEO)}
    assert 'id="videoModeNote"' in HTML and "no simulated video analysis" in HTML


def test_failures_are_never_displayed_as_a_safe_result():
    assert VIDEO.count("makeUnableResult('video'") >= 3 and "job.status === 'failed'" in VIDEO
    assert "Low Risk" not in VIDEO and "low_risk" not in VIDEO
    assert "reportSubmitError" in VIDEO and "showVideoError(message)" in VIDEO                         # a rejected upload is an inline error, not a result


def test_every_backend_evidence_source_has_a_label_and_video_sources_a_distinct_tag():
    labels = RENDER.split("const SOURCE_LABELS = {")[1].split("};")[0]
    assert set(re.findall(r"^\s+(\w+):", labels, re.M)) == set(get_args(EvidenceSource))
    chips = RENDER.split("const SOURCE_CHIPS = {")[1].split("};")[0]
    colours = re.findall(r"^\s+(video_\w+): '([^']+)'", chips, re.M)
    assert {name for name, _ in colours} == {"video_visual", "video_text", "video_audio"} and len({c for _, c in colours}) == 3
    assert "On-screen text" in labels and "Visual (what is shown)" in labels and "Speech / audio in the video" in labels


def test_evidence_cards_show_severity_reliability_timestamps_and_coverage_without_inventing_them():
    assert "Severity: " in RENDER and "Reliability: " in RENDER and "Coverage: " in RENDER
    assert "if (typeof start !== 'number') return '';" in RENDER                                         # timestamps only when the backend sent a number
    assert "timestamp_start: inRange(item.timestamp_start, 0, 86400)" in RESULT
    assert "res.coverage === 'full' || res.coverage === 'partial'" in RENDER


def test_video_text_reaches_the_page_only_through_textContent_and_nothing_is_stored_or_read_into_memory():
    for banned in ("innerHTML", "insertAdjacentHTML", "outerHTML", "localStorage", "sessionStorage", "indexedDB", "FileReader", "arrayBuffer(", "readAsDataURL",
                   "eval(", "document.write"):
        assert banned not in CODE["video"], banned
    assert "textContent" in VIDEO and re.search(r"\bfetch\(", VIDEO) is None
    assert "revokeObjectURL" in VIDEO and "createObjectURL" in VIDEO                                      # the only object URL is released at once
    assert "readVideoDuration" in VIDEO and "probe.removeAttribute('src')" in VIDEO


def test_the_video_panel_is_accessible_and_has_every_required_control():
    for needle in ('id="tabVideo"', 'role="tab"', 'aria-controls="panelVideo"', 'id="panelVideo" role="tabpanel" aria-labelledby="tabVideo"',
                   'id="videoDropzone" role="button" tabindex="0"', 'id="videoError" role="alert"', 'id="btnRemoveVideo"', 'id="btnReplaceVideo"',
                   'id="btnAnalyzeVideo"', 'id="btnStopVideoWait"', 'id="selectedVideoName"', 'id="selectedVideoSize"'):
        assert needle in HTML, needle
    assert "ArrowLeft" in MAIN and "ArrowRight" in MAIN and "'Enter'" in VIDEO and "' '" in VIDEO


def test_existing_text_and_audio_endpoints_and_fields_are_unchanged():
    assert "text: '/api/v1/analyze/text'" in API and "audio: '/api/v1/analyze/audio'" in API
    assert "form.append('text', text)" in API
    assert "analyzeText(" in ALL["scan"] and "analyzeAudio(" in ALL["scan"] and "runLocalTextEngine" in ALL["scan"]


def test_every_reason_the_backend_can_give_for_no_assessment_has_a_specific_heading():
    from app.core.errors import ReasonCode

    block = RESULT.split("const UNABLE_HEADINGS = {")[1].split("};")[0]
    mapped = set(re.findall(r"\b([A-Z][A-Z_]+):", block))
    assert {code.value for code in ReasonCode} <= mapped                                          # every ReasonCode
    assert {"SERVER_SHUTDOWN", "SERVER_RESTARTED", "JOB_CANCELLED", "INTERNAL_ERROR", "JOB_NOT_FOUND"} <= mapped      # failed-job and lost-job codes
    assert {"POLLING_FAILED", "POLL_TIMEOUT", "NETWORK_ERROR", "REQUEST_TIMEOUT", "INVALID_RESPONSE", "LIVE_API_UNREACHABLE"} <= mapped
    assert "scam_type: unableHeading(reasonCode)" in RESULT and "unableHeading(res.reason_code)" in RENDER
    assert "INSUFFICIENT_EVIDENCE: NOT_ENOUGH_EVIDENCE" in RESULT


def test_long_unbroken_text_wraps_instead_of_overflowing_the_result_cards():
    for element in ("scamTypeHeading", "explanationText", "recommendedActionText"):
        assert re.search(rf'id="{element}" class="[^"]*break-words', HTML), element
    card = RENDER.split("function evidenceCard")[1].split("function renderUrls")[0]
    assert card.count("break-words") >= 4
    assert "makeEl('li', { className: 'break-words', text })" in RENDER                                # limitations
