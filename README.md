# ScamShield AI

ScamShield is a scam-risk assessment assistant. A user pastes a suspicious message or uploads an
audio clip or a short video; Google Gemini extracts **evidence** of scam indicators, and the ScamShield **server**
turns that evidence into a risk score, a verdict and a safe next step.

> **ScamShield is a risk-assessment tool.** A *Low Risk* result does not prove that content, a person
> or a communication is authentic or safe, and ScamShield does not guarantee that scams are detected.
> It does not prove that audio or video is synthetic or manipulated.

> **Status: Phase 10 of 10 (release-readiness verification): suitable for a controlled demonstration, not production-ready.** The project has no
> authentication, keeps video jobs in memory and has not been load-tested; see [docs/security.md](docs/security.md) and
> [Known issues and limitations](#known-issues-and-limitations).

## Current functionality

| Feature | Status | Notes |
|---|---|---|
| Text / transcript analysis | **Real** (Gemini evidence + server risk engine) | `POST /api/v1/analyze/text`, legacy `POST /api/analyze-text`, up to 20,000 characters |
| Audio clip analysis | **Real** (same engine) | `POST /api/v1/analyze/audio`, legacy `POST /api/analyze-audio`, MP3/WAV/M4A/OGG/WEBM, max 20 MB, content-checked |
| Context-aware evidence, link structure, languages (EN/TA/HI/mixed) | **Real, not verified against live Gemini** | Discussion/fiction is discounted; links are examined by structure only and never visited. See [docs/analysis.md](docs/analysis.md) |
| Score, verdict, confidence | **Server-computed** | Gemini never decides them. See [docs/risk-engine.md](docs/risk-engine.md) |
| Failed or incomplete analysis | **Real** | `status: "unable_to_assess"`, `risk_score: null`, with a `reason_code`. Never Low Risk |
| Rate limiting, bounded concurrency, upload validation, security headers | **Real** | See [docs/security.md](docs/security.md) |
| Backend status indicator | **Real** | `GET /api/v1/status` returns only `ready` / `not_ready` |
| Dashboard (gauge, radar, meters, red flags, history, JSON export) | Real UI, native ES modules, no build step | Shows a dash and a neutral state when there is no score; see [docs/frontend.md](docs/frontend.md) |
| **Smart Sim** mode (UI default) | **Simulated** | Local keyword scorer in the browser, labelled "Simulated Demo". Does not call the backend or Gemini |
| Audio sample buttons and uploaded audio in Smart Sim | **Simulated** | Hard-coded results, labelled "Simulated Demo" |
| Acoustic Spectrum Monitor | **Decorative** | Animation only; does not read the audio |
| Scam Example Lab (replaced "GET PROMPT") | Local | Modal of 8 fictional training messages (incl. one legitimate); loading only fills the text box, nothing is submitted; no AI call |
| Video analysis | **Real backend and a Video Clip tab (Live mode only; Smart Sim has no simulated video).** Verified once live with synthetic videos; the new tab was tested with mocked responses and once against the real backend with a stub provider, not against live Gemini | `POST /api/analyze-video` returns 202 + job id, poll `GET /api/video-status/{id}`; MP4/MOV/WebM, max 100 MB and 300 s, ffprobe-validated, background job, same risk engine. See [docs/video.md](docs/video.md) |

To use the real backend, open the page served by the FastAPI server and click **FastAPI Live**.
In **Smart Sim** nothing is sent to Gemini. **The Gemini API key lives only on the server**: the page has
no key field, and the backend ignores any key sent in a form field, query string, header or cookie.

## Verdicts and thresholds (fixed)

| Score | Verdict (canonical value) |
|---|---|
| 0-24 | `low_risk` |
| 25-59 | `suspicious` |
| 60-100 | `high_risk` |

`status` is separate: `completed` (one of the three verdicts) or `unable_to_assess` (verdict
`unable_to_assess`, `risk_score: null`, `confidence: null`, no signals, plus `reason_code`).

```json
{ "status": "completed", "verdict": "suspicious", "risk_score": 37, "confidence": 0.64,
  "confidence_level": "Medium", "reason_code": null, "coverage": "full",
  "summary": "Suspicious: some scam indicators were detected, but they are not conclusive (...).",
  "signals": [ { "category": "payment_request", "description": "...", "severity": "medium",
                 "reliability": "moderate", "impact": 0.27, "corroborating_signals": 0 } ],
  "recommended_action": "Pause and verify the request independently ...",
  "scam_type": "...", "explanation": "...", "red_flags": ["..."], "signal_scores": {"...": 0},
  "mode": "text", "offline_fallback_used": false }
```

## Errors

Requests rejected before analysis use one shape, `{"detail": "...", "code": "..."}`:

| HTTP | `code` | Meaning |
|---|---|---|
| 400 | `INVALID_INPUT` | Blank text, empty file, unsafe file name |
| 413 | `PAYLOAD_TOO_LARGE` | Text, file or request body over the limit |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | Extension, declared type or file content not supported |
| 422 | `INVALID_REQUEST` | A required field is missing or malformed (names the field, never echoes values) |
| 429 | `RATE_LIMITED` | Too many requests; see the `Retry-After` header |
| 503 | `SERVICE_BUSY` | Too many analyses running; retry in a few seconds |

If the request was fine but the analysis could not be completed, the response is HTTP 200 with
`unable_to_assess` and a `reason_code`: `CONFIGURATION_ERROR`, `PROVIDER_AUTH_ERROR`, `PROVIDER_RATE_LIMITED`,
`PROVIDER_TIMEOUT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `INVALID_PROVIDER_RESPONSE`,
`INSUFFICIENT_EVIDENCE`, `MEDIA_PROCESSING_FAILED`, `INTERNAL_ANALYSIS_ERROR`.

## Technology

Python 3.12, FastAPI, Pydantic, slowapi (rate limiting), Google Gemini (`google-genai`, native async
client), plain HTML/CSS and native JavaScript ES modules with no build step (Tailwind, Chart.js and Lucide loaded from CDNs), pytest. Video validation needs **ffprobe** (part of
FFmpeg), installed separately.

## Setup (Python 3.12)

Windows PowerShell:

```powershell
python --version                       # should print 3.12.x
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt        # runtime
pip install -r requirements-dev.txt    # runtime + pytest + httpx
copy .env.example .env                 # then set GEMINI_API_KEY
```

macOS / Linux: `source .venv/bin/activate` instead of the activation line.

### Video analysis needs FFmpeg (ffprobe)

ffprobe is **not** a Python package. Install FFmpeg and check it:

```powershell
winget install Gyan.FFmpeg             # Windows (macOS: brew install ffmpeg; Debian/Ubuntu: sudo apt install ffmpeg)
ffprobe -version                       # open a NEW terminal after installing so PATH is refreshed
```

If `ffprobe` is not on `PATH`, set `FFPROBE_PATH` in `.env` to the full path of `ffprobe.exe`. Without ffprobe the server still
starts (with a warning) but video uploads are refused with `VIDEO_VALIDATION_UNAVAILABLE`; text and audio are unaffected. Details,
limits (100 MB, 300 s, MP4/MOV/WebM), the API and the job lifecycle: [docs/video.md](docs/video.md).

## Configuration

All settings are read from environment variables in one place (`app/core/config.py`). A `.env` file in
the project root is loaded automatically (existing environment variables win); `.env` is excluded from Git.
Malformed or unsafe values (for example `MAX_TEXT_LENGTH=abc` or a wildcard CORS origin) stop startup with a
clear message.

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | _(none)_ | Gemini API key. **Backend only.** If unset, the app starts and analysis returns `unable_to_assess` / `CONFIGURATION_ERROR` |
| `GEMINI_MODEL` | `gemini-3.5-flash` | Gemini model ID |
| `APP_ENV` | `development` | `development`, `test` or `production` (`production` disables `/docs`; `test` skips loading `.env`) |
| `LOG_LEVEL` | `INFO` | Python log level |
| `MAX_TEXT_LENGTH` | `20000` | Max characters of text |
| `MAX_AUDIO_SIZE_MB` | `20` | Max audio upload size |
| `MAX_VIDEO_SIZE_MB` | `100` | Max video upload size |
| `MAX_VIDEO_DURATION_SECONDS` | `300` | Max video duration (checked with ffprobe) |
| `FFPROBE_PATH` | `ffprobe` | ffprobe executable used to validate videos (must be installed) |
| `FFPROBE_TIMEOUT_SECONDS` | `20` | Time limit for one ffprobe check |
| `GEMINI_VIDEO_TIMEOUT_SECONDS` | `240` | Overall deadline for one Gemini video analysis |
| `GEMINI_FILE_POLL_INTERVAL_SECONDS` | `2` | How often to check whether Gemini has finished processing the video |
| `GEMINI_FILE_READY_TIMEOUT_SECONDS` | `120` | Give up waiting for Gemini to process the video |
| `VIDEO_WORKERS` | `1` | Videos analysed at once |
| `VIDEO_MAX_ACTIVE_JOBS` | `4` | Queued + running video jobs; more get HTTP 503 |
| `VIDEO_MAX_ACTIVE_JOBS_PER_CLIENT` | `2` | Per client; more get HTTP 429 |
| `VIDEO_JOB_TIMEOUT_SECONDS` | `330` | Hard limit for one job |
| `VIDEO_JOB_TTL_SECONDS` | `900` | Finished jobs are forgotten after this; jobs are in memory and lost on restart |
| `GEMINI_TIMEOUT_SECONDS` | `60` | Overall deadline for one Gemini analysis |
| `GEMINI_MAX_CONCURRENCY` | `4` | Analyses running at once |
| `GEMINI_MAX_QUEUE` | `8` | Analyses allowed to wait; more get HTTP 503 |
| `GEMINI_MAX_RETRIES` | `1` | Extra attempts for transient provider failures only |
| `GEMINI_QUOTA_COOLDOWN_SECONDS` | `30` | Pause Gemini calls after a provider quota error (0 = off) |
| `RATE_LIMIT_ENABLED` | `true` | Master switch for rate limiting |
| `RATE_LIMIT_TEXT` | `30/minute` | Text analyses per client |
| `RATE_LIMIT_AUDIO` | `10/minute` | Audio analyses per client |
| `RATE_LIMIT_VIDEO` | `3/minute` | Video submissions per client |
| `RATE_LIMIT_DEFAULT` | `120/minute` | Health, status and the web page |
| `RATE_LIMIT_GLOBAL` | `120/minute` | All clients combined, text + audio |
| `TRUSTED_PROXY_IPS` | _(blank)_ | IPs/CIDRs of a reverse proxy allowed to set `X-Forwarded-For` |
| `CORS_ALLOW_ORIGINS` | local dev origins | Explicit comma-separated origins; no wildcard |
| `ENABLE_HSTS` | `false` | Send `Strict-Transport-Security` (HTTPS only) |
| `TEMP_UPLOAD_DIR` | _(blank)_ | Upload scratch directory; blank = `<system temp>/scamshield-uploads` |

Rate limiting and capacity counters are **in memory and single-process**.

## Run

```powershell
uvicorn app.main:app --no-proxy-headers
```

Do **not** add `--reload` for demonstrations: a reload (any saved file change) restarts the process, which drops every in-memory
video job and can cut an upload in progress (the page then reports it could not reach the backend). Use it only while editing code.

Open <http://127.0.0.1:8000/>. Health check: <http://127.0.0.1:8000/health>. `--no-proxy-headers` stops
uvicorn from trusting a client-supplied `X-Forwarded-For`; see [docs/security.md](docs/security.md). `uvicorn
server:app` still works (`server.py` is a thin shim).

## Deploying on Render

`Dockerfile` (Python 3.12 + FFmpeg) and `render.yaml` (one web service, health check `/health`, a single instance) are provided. The
Blueprint uses Render's **free** plan, which needs no payment method but sleeps after about 15 minutes idle (and loses in-memory video
jobs when it does); switch the instance type to `starter` (paid) to keep it awake. Step-by-step setup, the post-deploy checklist and the honest limits (not yet deployed or Docker-built by the
developer, per-visitor rate limiting and large uploads still to be verified on Render, no login so your Gemini quota is exposed):
[docs/deployment.md](docs/deployment.md).

## Tests

```powershell
pytest
```

Tests use a fake provider and a fake Gemini SDK client, need no API key, and make no calls to Gemini or the analysis
service (DNS and outbound connections are blocked inside the Python test run). Two groups depend on your machine and are skipped
when it cannot run them: the browser UI tests in `tests/frontend/` need Chrome or Edge **and internet access** (the page loads Tailwind,
Chart.js and Lucide from CDNs), and two real-ffprobe tests need `ffprobe` on `PATH`. One POSIX-permission test is skipped on Windows. Tests marked `legacy_bug` document
known gaps that a later phase fixes deliberately.

## Project layout

```
app/
  main.py                    create_app() factory; `app` for uvicorn
  core/                      config.py, errors.py, logging.py, security.py (body limit, headers, client key), rate_limit.py
  api/                       deps.py, routes_health/text/audio/video.py, routes_legacy.py (old paths)
  schemas/                   verdict.py, evidence.py, provider_output.py, analysis.py, jobs.py
  providers/                 base.py (interface); gemini/ (client, prompts, errors)
  services/                  risk_engine.py, response_builder.py, analysis_pipeline.py (capacity gate),
                             text_service.py, audio_service.py, media_validation.py,
                             url_analysis.py, audio_inspection.py, evidence_enrichment.py,
                             video_service.py, video_inspection.py, video_jobs.py
  utils/                     temp_files.py
docs/                        analysis.md, video.md, risk-engine.md, security.md, frontend.md, deployment.md
frontend/                    index.html, css/styles.css, js/ (15 ES modules + tailwind-config.js)
tests/                       api/, unit/, frontend/ (headless-browser UI tests), fixtures/, conftest.py, fakes.py, media_samples.py
legacy/                      byte-for-byte backups of the original files
server.py                    compatibility shim -> app.main
Dockerfile, render.yaml      Render deployment (see docs/deployment.md)
test.py                      original manual Gemini smoke script (not a pytest test)
```

## Known issues and limitations

- No authentication; no TLS (use a reverse proxy); in-memory rate limiting. The CSP forbids inline scripts but `style-src` still allows inline styles (Tailwind Play CDN); the CDN scripts have no SRI hashes. Full list: [docs/security.md](docs/security.md).
- Magic-byte checks identify containers only, not whether media decodes.
- Context judgement (is a scam being described or aimed at you?), language handling and audio timestamps depend on the model and are unverified against live Gemini; accuracy differs by language and is not measured. Links are checked by structure only (no reputation check). See [docs/analysis.md](docs/analysis.md).
- Weights are expert judgement, not calibrated on data. The automated tests never call Gemini; a handful of manual live checks (text, audio and synthetic videos) worked, but real-model behaviour across languages, noisy media and real scams is unmeasured, and the model's answers vary between runs (see docs/risk-engine.md, docs/video.md).
- Video jobs are in memory: a server restart (including `--reload`) loses them. One server process only; no authentication; anyone holding a job id can read that job's status. Video analysis can take a minute or more.
- Smart Sim has no video analysis, and its keyword scorer can rate a scam sample (including the Scam Example Lab's) as Low Risk. Use FastAPI Live for real results.
- The Smart Sim demo keyword scorer can mislabel benign text (it is labelled "Simulated Demo").
- The page needs internet access for its CDN libraries and fonts. Browser behaviour was verified in one browser engine only; see the manual checks listed in docs/frontend.md.

## Roadmap

1. Audit (done) 2. Foundations (done) 3. Backend modularization (done) 4. Risk engine (done)
5. Security hardening (done) 6. Frontend ES modules (done) 7. Audio and text v2 (done) 8. Video backend (done) 9. Video frontend (done) 10. **Release readiness (this state)**
