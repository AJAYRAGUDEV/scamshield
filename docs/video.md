# Video analysis backend (Phase 8)

The page has a **Video Clip** tab that uses these endpoints (Phase 9; see [frontend.md](frontend.md)). ScamShield provides
**suspicious-content indicators, not proof of fraud, authenticity or manipulation.**

## 1. Approved specification

| Item | Value | Setting (env) |
|---|---|---|
| Formats | MP4 (`.mp4`), MOV (`.mov`), WebM (`.webm`) | fixed allowlist |
| Maximum file size | 100 MB | `MAX_VIDEO_SIZE_MB` |
| Maximum duration | 300 seconds (inclusive) | `MAX_VIDEO_DURATION_SECONDS` |
| Provider | Gemini only, through the existing provider (`GEMINI_MODEL`) | |
| Validation | ffprobe | `FFPROBE_PATH`, `FFPROBE_TIMEOUT_SECONDS` |
| Processing | background jobs, in memory, one worker | `VIDEO_WORKERS` (1), `VIDEO_MAX_ACTIVE_JOBS` (4), `VIDEO_MAX_ACTIVE_JOBS_PER_CLIENT` (2) |
| Scoring | the existing Phase 4 risk engine, thresholds unchanged (0-24 / 25-59 / 60-100) | |
| Permanent media storage | none | |

Other settings: `RATE_LIMIT_VIDEO` (3/minute per client), `GEMINI_VIDEO_TIMEOUT_SECONDS` (240, whole Gemini step),
`GEMINI_FILE_POLL_INTERVAL_SECONDS` (2), `GEMINI_FILE_READY_TIMEOUT_SECONDS` (120), `VIDEO_JOB_TIMEOUT_SECONDS` (330),
`VIDEO_JOB_TTL_SECONDS` (900). All have safe defaults; malformed values stop start-up with a clear message.

## 2. Installing ffprobe

ffprobe ships with FFmpeg and must be installed on the server; it is **not** a Python package.

* Windows: `winget install Gyan.FFmpeg` (or download a build from ffmpeg.org) and make sure `ffprobe.exe` is on `PATH`,
  or set `FFPROBE_PATH` to its full path.
* macOS: `brew install ffmpeg`. Debian/Ubuntu: `sudo apt install ffmpeg`.
* Check with `ffprobe -version`.

If ffprobe is missing the server still starts (with a warning), but every video upload is refused with **HTTP 503
`VIDEO_VALIDATION_UNAVAILABLE`**. Validation fails closed: an unchecked video is never accepted. Text and audio are
unaffected. The automated tests use a fake ffprobe and do not need it installed; real ffprobe behaviour was therefore
exercised only through its documented JSON output format (see §9).

## 3. Upload validation (before any job or Gemini call)

In order, in `app/services/video_service.py`, `media_validation.py`, `temp_files.py` and `video_inspection.py`:

1. **Name** (no separators, control characters, over-long names), **extension** allowlist, **declared MIME type** plausible
   for the extension (`application/octet-stream`/empty tolerated because the content decides). The client's filename and
   type are never trusted and the filename never reaches a path: the file is stored as `<random hex>.<validated extension>`
   in a private random directory (mode 0600 where supported).
2. **Capacity pre-check**: if no job could be accepted, the upload is refused before its body is read.
3. **Streaming**: bytes are counted as they are copied to disk; over the limit stops the transfer (HTTP 413). The first bytes
   must identify the container the extension claims (MP4/MOV `ftyp` brand, WebM/EBML doctype); executables, archives, HTML,
   images and renamed files fail (415). Empty files are 400.
4. **ffprobe** (no shell, fixed arguments, timeout, worker thread): it must read the file (non-zero exit = corrupt), the
   container must match the extension, there must be a real video stream (cover art does not count), the metadata must be
   sane (finite positive duration, plausible dimensions and stream count, bounded output) and the duration must not exceed
   the limit (HTTP 422 `VIDEO_TOO_LONG`).

Errors are `{"detail": "<safe text>", "code": "<stable code>"}`:

| Status | `code` | Cause |
|---|---|---|
| 400 | `INVALID_INPUT` | empty file, bad filename |
| 413 | `PAYLOAD_TOO_LARGE` | over the size limit |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | wrong extension, MIME type or file signature |
| 422 | `INVALID_VIDEO` | corrupt, no video stream, unreadable or malformed metadata, duration unknown, container mismatch |
| 422 | `VIDEO_TOO_LONG` | longer than the limit |
| 422 | `INVALID_REQUEST` | missing `file` field |
| 429 | `RATE_LIMITED` / `TOO_MANY_JOBS` | rate limit, or this client already has too many jobs |
| 503 | `SERVICE_BUSY` | too many jobs in progress (`Retry-After`) |
| 503 | `VIDEO_VALIDATION_UNAVAILABLE` | ffprobe is missing or cannot run |

## 4. Endpoints

`POST /api/analyze-video` (also `/api/v1/analyze/video`): multipart field `file`. Returns **202**:

```json
{
  "job_id": "432uQvB07UQ7dD_GeJ8H7vd1ZtU1wuAM",
  "status": "queued",
  "stage": "queued",
  "progress": 0,
  "created_at": "2026-11-25T08:30:00.123456Z",
  "status_url": "/api/v1/video-status/432uQvB07UQ7dD_GeJ8H7vd1ZtU1wuAM"
}
```

(plus a `Location` header with the same URL).

`GET /api/video-status/{job_id}` (also `/api/v1/video-status/{job_id}`):

```json
{
  "job_id": "432uQvB07UQ7dD_GeJ8H7vd1ZtU1wuAM",
  "status": "completed",
  "stage": "finished",
  "progress": 100,
  "created_at": "2026-11-25T08:30:00.123456Z",
  "updated_at": "2026-11-25T08:30:41.502113Z",
  "expires_at": "2026-11-25T08:45:41.502113Z",
  "result": { "status": "completed", "verdict": "high_risk", "risk_score": 94, "mode": "video",
              "signals": [ { "category": "credential_request", "source": "video_text", "quote": "Enter the OTP...",
                             "timestamp_start": 10.0, "timestamp_end": 14.0 } ],
              "limitations": ["..."] },
  "error": null
}
```

`result` is the same `AnalysisResponse` as text and audio (`mode: "video"`). While the job runs `result`, `error` and
`expires_at` are `null`. A job whose analysis could not be completed:

```json
{ "status": "unable_to_assess", "stage": "finished", "progress": 100, "error": null,
  "result": { "status": "unable_to_assess", "verdict": "unable_to_assess", "risk_score": null,
              "reason_code": "PROVIDER_TIMEOUT", "signals": [], "...": "..." } }
```

Unknown, expired or malformed ids give **404** `JOB_NOT_FOUND`. Responses never contain paths, filenames, keys, stack traces or
provider internals.

## 5. Job lifecycle

```
queued -> processing -> completed          analysis finished: low_risk / suspicious / high_risk
                     -> unable_to_assess   analysis could NOT be completed (null score, reason_code); never Low Risk
queued | processing -> failed              internal problem or shutdown (no result; error.code, error.message)
```

* `stage` / `progress` (coarse, safe): `queued` 0, `starting` 5, `uploading` 20, `processing_media` 45, `analysing` 70,
  `scoring` 90, `finished` 100. They show where the job is, not how long it will take.
* Invalid transitions raise inside the manager (a test enforces the table). `validating` is not a job state: validation
  happens in the upload request, so an invalid video is an immediate HTTP error and never occupies a job.
* **Bounds**: one worker; at most 4 queued + running jobs (503 beyond that), 2 per client (429), 500 stored records; each job
  has a hard timeout; Gemini calls still pass through the shared Gemini concurrency limit.
* **Expiry**: a finished job is forgotten 15 minutes after it finishes (`VIDEO_JOB_TTL_SECONDS`), purged on access and by a
  janitor task. Active jobs never expire.
* **Job ids** are 192-bit random tokens, unguessable but **not authenticated**: anyone holding an id can read its status.
* **In-memory limitation**: jobs exist only in this process. A restart, crash or deployment **loses every job**; a clean
  shutdown marks running jobs `failed` (`SERVER_SHUTDOWN`) and deletes their videos; a status request for a lost id is 404.
  There is no sharing between processes: run exactly one server process.

## 6. Gemini processing flow

Through the existing `GeminiProvider` (same client, key, retry policy, quota cooldown and error translation; no second client):

1. Upload the file with its validated MIME type (`files.upload`, with a video-sized HTTP timeout).
2. **Wait for Gemini to finish processing** it: poll `files.get` every `GEMINI_FILE_POLL_INTERVAL_SECONDS` until the file
   state is `ACTIVE` (states verified against the installed SDK: `STATE_UNSPECIFIED`, `PROCESSING`, `ACTIVE`, `FAILED`).
   `FAILED` ends the analysis (`MEDIA_PROCESSING_FAILED`); an unknown or missing state is treated as *not ready*;
   not ready within `GEMINI_FILE_READY_TIMEOUT_SECONDS` is `PROVIDER_TIMEOUT`.
3. Generate structured evidence (`response_schema` = the evidence schema, low temperature, video system prompt).
4. **Always delete the remote file** (success, failure, timeout, cancellation); a failed delete is logged, never fatal.

One overall deadline (`GEMINI_VIDEO_TIMEOUT_SECONDS`) covers all steps. The only retry is a single extra generation
attempt for transient 5xx/connection errors; uploads are never retried and polling is bounded, so there are no retry storms.
After a provider quota error new analyses are refused locally for `GEMINI_QUOTA_COOLDOWN_SECONDS`.

## 7. Evidence and scoring

Gemini returns **evidence only**, in the same schema as text and audio (see [risk-engine.md](risk-engine.md)); extra fields
(verdicts, scores) are ignored. Video uses three sources:

| `source` | Meaning |
|---|---|
| `video_visual` | what is seen: fake bank/payment/government screens, authority claims, QR payment demands, fake support screens, misleading financial adverts |
| `video_text` | text visible on screen: payment instructions, OTP requests, fake warnings, URLs, misleading claims |
| `video_audio` | what is heard: OTP/credential requests, payment instructions, fake authority, threats, urgency, remote-access installs, social engineering |

Categories are the existing ones; `fake_interface` (0.55) and `visual_manipulation` (0.35) are legal for video only.
The illustrative schema in the brief (`credential_theft`, numeric `reliability`, `analysis_complete`) was adapted to the
existing one: `category`, enum `reliability` (weak/moderate/strong, not a number the model can inflate), `coverage`
(`full`/`partial`/`insufficient`) in place of `analysis_complete`, and `timestamp_start`/`timestamp_end` only when the model
gives them.

The same server pipeline then applies: mode validation (wrong sources are rejected), timestamp verification against the
ffprobe duration (a timestamp more than 1 s past the end is removed and the result says so; timestamps are never invented),
the Phase 4 engine (impact = weight × severity × reliability × context; duplicates grouped; diminishing returns), and the
standard response builder. Coverage `partial` or `insufficient` with no evidence is `unable_to_assess`, never Low Risk.
**Possible manipulation is not fraud:** `visual_manipulation` has weight 0.35 and, however many such signals there are, cannot
reach High Risk by itself (tested); the result also carries the limitation "observations only, not proof".

## 8. Temporary media and cleanup

The uploaded video lives only in a private random directory under `TEMP_UPLOAD_DIR`. It is deleted when the job ends in any
way (completed, unable_to_assess, failed, timeout, cancellation, shutdown), when an upload is rejected, when the client
disconnects mid-upload, and when a job expires; leftovers from a crashed process are swept at start-up. The Gemini copy is
deleted in a `finally` block. Neither the video nor a transcript is retained afterwards; results keep only short excerpts.

## 9. Known limitations and honesty notes

* **Not verified here:** ffprobe is not installed in the development environment used for Phase 8, so ffprobe validation was
  tested against a fake runner that returns ffprobe's documented JSON. Real MP4/MOV/WebM files, real ffprobe output
  quirks and real Gemini video processing (including how long it takes and what the model returns) have **not** been
  exercised.
* ffprobe checks headers and streams, not that every frame decodes. Some browser-recorded WebM files have no duration in the
  header and are refused because the 300-second limit cannot be verified.
* Gemini samples video at a low frame rate: brief on-screen text, fast flashes or small details can be missed.
* Quality is lower for noisy audio, small or blurred text, unusual scripts and dialects; accuracy is not measured per language.
* **False positives:** legitimate tutorials, bank or police awareness videos, screen recordings of real banking apps, and adverts
  can show the same screens and phrases (context labelling reduces, but does not remove, this). **False negatives:** polished
  scams, short or subtle cues, text shown briefly, or content the model misreads.
* Manipulation indicators are visual or audio **observations**. ScamShield does **not** detect deepfakes, identify people,
  authenticate voices, documents or video sources, or prove that a video is genuine or fake.
* Single process, in-memory jobs, no authentication; see §5 and [security.md](security.md).
