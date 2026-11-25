# ScamShield security notes

> **This is a hardened development build, not a production-ready or "completely secure" system.** It
> reduces specific, listed risks. It has no authentication, no TLS of its own, no shared rate-limit
> store and no independent security review. See [Production deployment requirements](#9-production-deployment-requirements)
> and [Known limitations](#10-known-limitations).

## 1. Threat model

**Assets:** the Gemini API key and its quota/cost; the server's availability; the privacy of
submitted messages and audio; the host's disk and memory.

**Actors:** (a) anonymous internet users who can reach the server; (b) a malicious uploader trying to
smuggle a file, exhaust resources or reach the filesystem; (c) a malicious *message* (prompt injection
aimed at the AI); (d) a curious operator reading logs; (e) a website that tries to make a visitor's
browser call the API cross-origin.

| Threat | Mitigation | Residual risk |
|---|---|---|
| Key theft via the browser | The key exists only in the server environment; no request channel can supply one; never returned, logged or stored in the page | Anyone with server/OS access can read it |
| Quota/cost abuse | Per-client + global rate limits, bounded concurrency and queue, provider quota cooldown, bounded retries | Many addresses can still exhaust the *global* limit (a denial of service for others) |
| Oversized / slow uploads | 3-layer size limits, early rejection, streamed cut-off | Slow trickling of a body within the limit holds a connection (needs proxy timeouts) |
| Disguised or malicious files | Allowlist + extension + declared type + magic bytes; file never executed or served | A valid container can still be malformed or carry unexpected streams |
| Path traversal / predictable paths | Client filename never used; random names; private directory; traversal-shaped names rejected | Windows ignores POSIX permission bits |
| Information leaks | One generic error shape, no echo of input, no traces/paths/keys | Debug logging (opt-in) records redacted tracebacks |
| Cross-origin use of the API | Explicit CORS origins, no credentials, no wildcard | Plain cross-site form POSTs are possible (no auth to steal; rate limited) |
| Script injection into the page | Escaping of model output; CSP limits sources | `style-src` still allows `'unsafe-inline'` (Tailwind Play CDN); CDN scripts have no SRI |
| Prompt injection in submitted text | Prompt tells the model to treat content as data; the model returns only validated *evidence*; the server computes the verdict | A manipulated model can still under-report evidence |

## 2. API key handling

- Read only from the `GEMINI_API_KEY` environment variable (or a local `.env`, excluded from Git by
  `.gitignore` entries `.env` / `.env.*`; `.env.example` ships a blank placeholder).
- **No client channel is read:** not form fields, query strings, headers (`Authorization`,
  `X-Goog-Api-Key`, `X-API-Key`) nor cookies. Tests send a key through each channel and assert Gemini is
  only ever called with the server's own key.
- The key is excluded from `repr(Settings)`, from API responses and from the OpenAPI schema (which has
  no credential parameters or security schemes), and is registered with the log redactor.
- The dashboard has no key field. Its "Backend" drawer shows `ready` / `not ready` from
  `GET /api/v1/status`, which returns only `{"status": "ready"|"not_ready"}`; it says nothing about
  why and never names credentials. The page uses no `localStorage`, `sessionStorage` or cookies.
- A missing key does not crash the app: analyses return `unable_to_assess` / `CONFIGURATION_ERROR`.

## 3. Rate limiting (slowapi)

| Setting (env) | Default | Applies to |
|---|---|---|
| `RATE_LIMIT_TEXT` | 30/minute | text analysis, per client (both URL paths share one counter) |
| `RATE_LIMIT_AUDIO` | 10/minute | audio analysis, per client |
| `RATE_LIMIT_VIDEO` | 3/minute | video submissions, per client (both URL paths share one counter) |
| `RATE_LIMIT_GLOBAL` | 120/minute | ALL clients combined, text + audio (quota protection) |
| `RATE_LIMIT_DEFAULT` | 120/minute | `/health`, `/api/v1/status`, the web page |
| `RATE_LIMIT_ENABLED` | true | master switch |

Exceeding a limit returns **HTTP 429** `{"detail": "...", "code": "RATE_LIMITED"}` with `Retry-After`.
A limited request never reaches the provider or the upload handler.

**Client identity.** The limiter keys on the TCP peer address, which a client cannot change. Request
headers (`X-Forwarded-For`, `X-Real-IP`, `Forwarded`, `User-Agent`, ...) are ignored; tests rotate every
common header and confirm no fresh allowance. `X-Forwarded-For` is honoured **only** when the direct peer
is in `TRUSTED_PROXY_IPS` (IPs/CIDRs); then the right-most address that is not itself a trusted proxy is
used, so a client cannot pick its own identity by prefixing the header. Leave `TRUSTED_PROXY_IPS` blank
unless a reverse proxy really sits in front. **Run uvicorn with `--no-proxy-headers`** (the documented
command does): otherwise uvicorn applies its own, separate forwarded-header trust to local peers.

**Storage is in memory.** Counters live in one process and reset on restart. That is fine for a
single-process development or small deployment. With several workers or servers each process counts
separately (the effective limit multiplies), and a shared store such as Redis would be required.

## 4. Concurrency and capacity

- `GEMINI_MAX_CONCURRENCY` (4) analyses run at once; `GEMINI_MAX_QUEUE` (8) more may wait. Anything
  beyond is refused immediately with **HTTP 503** `SERVICE_BUSY` and `Retry-After: 5`, *before* an upload
  is written to disk. Waiting uses a counter and a semaphore: no threads, no unbounded queue.
- Capacity is released in `finally` blocks, so provider errors, timeouts, validation failures and
  cancellations cannot leak it (each path is tested).
- Single process only: the counters are not shared across workers. No Redis or database is used.

## 5. Upload validation

Implemented in `app/services/media_validation.py`, applied before and while the file is stored:

1. **Name:** path separators, control characters, NUL and over-long names are rejected (400).
2. **Extension** allowlist: audio `.mp3 .wav .m4a .ogg .webm` (415). Video (Phase 8): `.mp4 .mov .webm`
   (see [video.md](video.md)).
3. **Declared MIME type** must be plausible for the extension; `application/octet-stream`/empty is
   tolerated only because step 4 decides.
4. **Magic bytes** on the first bytes, and the detected container must be the one the extension claims:
   WAV (`RIFF…WAVE` + `fmt `), MP3 (ID3v2 or a valid frame header), OGG (`OggS` page header), WebM (EBML +
   `webm` doctype; Matroska is refused), MP4/M4A/MOV (`ftyp` with an allowlisted brand; HEIC and other
   brands refused). Executables, archives, scripts, HTML, PDFs and images fail here (tested).
5. **Size and emptiness** are enforced while streaming to disk; the file is deleted on any failure.

Limits: a signature proves the *container*, not that the file decodes, nor that an `.m4a` holds audio
rather than video. Video additionally goes through ffprobe (duration, streams, integrity; see video.md); audio has no ffprobe check.

## 6. Request-body size protection

Starlette parses a whole multipart body *before* a route handler runs, so a handler-level check alone is
too late. `BodyLimitMiddleware` (pure ASGI, outermost of the request-handling layers) therefore adds:

1. `Content-Length` present and over the route's limit → **413** at once; the application never runs and no
   bytes are read. A malformed `Content-Length` → 400.
2. Absent or understated `Content-Length` (chunked or lying client) → bytes are **counted as they arrive**;
   once the limit is crossed the application is told the client disconnected (it stops reading), and any
   response it produces is replaced by 413. Tests show a 25 MB chunked stream is cut off after a few MB.
3. The accepted file is size-checked again while copied to disk.

Limits per route: audio = `MAX_AUDIO_SIZE_MB` (20) + 64 KiB multipart overhead; video routes = 100 MB + 64 KiB
(both URL spellings); text = `MAX_TEXT_LENGTH` × 12 bytes (worst-case URL-encoding) + 16 KiB; any other
route 64 KiB. Limitations: bytes already received by the server before the cut-off still pass through
uvicorn/Starlette buffers and Starlette's multipart parser spools file parts into the system temp directory
(not the dedicated upload directory) until the request ends; set `TMPDIR`/`TEMP` for the process to the
dedicated directory to keep all upload data in one place. The limit counts bytes, not time: use a reverse
proxy with request-read timeouts against slow-body (slowloris) attacks.

## 7. Temporary files

- Dedicated directory `TEMP_UPLOAD_DIR` (default `<system temp>/scamshield-uploads`), mode 0700 where supported.
- Each upload gets its own `mkdtemp` directory (random name, 0700) holding one file with a **random**
  name (`secrets.token_hex(16)` + validated extension), created exclusively (`O_EXCL`) with mode 0600. The
  client filename never reaches a path.
- Removed on success, validation failure, provider failure, timeout and cancellation, including partial files;
  cleanup errors are contained, retried once and logged without paths. A start-up sweep removes `up-*`
  directories older than an hour (crash leftovers).
- Never served: the frontend is returned by the explicit `/` route plus two fixed static mounts, `/css` and `/js`
  (nothing else under `frontend/`, and never the upload directory, the source tree or `.env`).
- Uploaded audio and text are not retained, not cached, and not logged. The copy uploaded to Gemini's Files
  API is deleted in a `finally` block even after errors and timeouts.
- Windows ignores POSIX permission bits; there the per-user temp directory's access control applies.

## 8. Other controls

**CORS.** `CORS_ALLOW_ORIGINS` is an explicit list (default: the four local development origins). A `*`
or malformed origin stops startup. Credentials are never allowed; methods are `GET`/`POST`; allowed
request headers are `Content-Type` and `X-Request-ID` (no `Authorization`). The bundled page is
same-origin and needs no CORS.

**Security headers.** On every response: `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
`X-Frame-Options: DENY`, `Permissions-Policy` (camera, microphone, geolocation, payment, usb disabled);
API responses add `Cache-Control: no-store`; `Strict-Transport-Security` only when `ENABLE_HSTS=true` *and*
the request is HTTPS.

**Content-Security-Policy** (HTML page only; directives used: `default-src`, `script-src`, `style-src`, `font-src`,
`img-src`, `media-src`, `connect-src`, `object-src`, `base-uri`, `form-action`, `frame-ancestors`): `default-src 'self'`; scripts from `self` + `cdn.tailwindcss.com`,
`cdn.jsdelivr.net`, `unpkg.com` and **no** `'unsafe-inline'`/`'unsafe-eval'`; styles from `self` + Google Fonts + `'unsafe-inline'`;
fonts from `fonts.gstatic.com`; `img-src 'self' data: blob:` and `media-src 'self' blob:` (the audio preview plays a
local `blob:` URL); `connect-src 'self'`; `object-src 'none'`; `base-uri 'none'`;
`form-action 'self'`; `frame-ancestors 'none'`. It was verified in a browser against the existing page
(Tailwind, Chart.js, Lucide, fonts, canvases and fetch calls all work, no violations), and a test checks that
every external resource the page loads is allowed by the policy.
*Since Phase 6* the page has no inline `<script>`, `<style>` or event-handler attribute: its code lives in external ES
modules under `/js` and its CSS in `/css/styles.css`, so inline script execution is blocked outright (a test asserts this, and
the policy was checked in a browser). *Why `'unsafe-inline'` remains in `style-src`:* the Tailwind Play CDN injects `<style>`
elements at runtime and the markup/JavaScript use `style=""` attributes. **To remove it:** replace the Tailwind Play CDN with a
compiled stylesheet (a build step, which the project has decided against for now) and move the few runtime `style` assignments
to CSS classes. **Also still open:** the three CDN scripts have no Subresource Integrity hashes (pin them with SRI, or self-host
them under `/js`), and Tailwind documents its Play CDN as not meant for production. See [frontend.md](frontend.md).

**Error handling.** Client errors use one shape `{"detail", "code"}` with stable codes `INVALID_INPUT` (400),
`PAYLOAD_TOO_LARGE` (413), `UNSUPPORTED_MEDIA_TYPE` (415), `INVALID_REQUEST` (422, names the fields, never echoes
values), `RATE_LIMITED` (429), `SERVICE_BUSY` (503), `NOT_FOUND`, `METHOD_NOT_ALLOWED`. Analysis failures keep
the Phase 4 contract (HTTP 200, `status`/`verdict` `unable_to_assess`, `risk_score: null`, `reason_code`) and are
never Low Risk. Unhandled exceptions return `{"detail": "Internal server error."}`; tracebacks are logged only at
DEBUG and only after redaction. `/docs`, `/redoc` and `/openapi.json` are disabled when `APP_ENV=production`.

**Gemini protection.** Request timeout plus an overall deadline (`GEMINI_TIMEOUT_SECONDS`); the SDK's own retries
are disabled; at most `GEMINI_MAX_RETRIES` (1) extra attempts for transient 5xx/connection failures only, with
capped, jittered backoff inside the deadline; authentication, invalid-request, timeout and quota errors are
never retried; after a provider quota error new analyses are refused locally for
`GEMINI_QUOTA_COOLDOWN_SECONDS` (30) instead of hammering the quota; input is validated before any provider call;
no caching, no deduplication of content.

**Logging.** Structured `event key=value` lines with a request ID. Never logged: message text, transcripts,
evidence text, audio, filenames, keys. Redacted if they appear: the configured key and Google-key shapes,
`Authorization`/`Cookie`/API-key headers, bearer tokens, and secret-looking query parameters; uvicorn's access
log has its query string removed.

## 9. Production deployment requirements

Not done by this project; required before any real exposure:

1. **TLS** terminated by a reverse proxy or platform; set `ENABLE_HSTS=true`.
2. **Reverse proxy** with request-body, header and **read timeouts**, connection limits, and a `client_max_body_size`
   at or below the limits above. Set `TRUSTED_PROXY_IPS` to it and run uvicorn with `--no-proxy-headers`.
3. **`APP_ENV=production`** (disables API docs) and a locked-down `CORS_ALLOW_ORIGINS`.
4. **Authentication/authorization** appropriate to the audience; right now any client that can reach the server can
   use the analysis within the rate limits.
5. **A shared rate-limit and capacity store** (e.g. Redis) if more than one process or server runs.
6. A **dedicated low-privilege service account**, secrets from a secret manager, `TMPDIR`/`TEMP` on a quota-limited
   volume, log shipping with retention, and monitoring/alerting on 429/503/5xx and Gemini spend.
7. **Dependency and image scanning** (none was performed here), pinned versions, and a security review/pen test.

## 10. Known limitations

- No authentication; per-IP limits are weak against distributed abuse and the global limit can be used to deny
  others service.
- In-memory limiter and capacity counters: single process, reset on restart.
- Magic-byte checks identify containers only; corrupt or hostile media inside a valid container is not detected.
- CSP: `style-src` allows inline styles; CDN scripts have no SRI hashes and the Tailwind Play CDN is not meant for production.
- Slow-body attacks are not mitigated in the application.
- Windows: POSIX file modes are not enforced.
- Nothing here was exercised against the real Gemini API; error mapping and retry behaviour are tested with mocks.
- No penetration test, fuzzing campaign or dependency audit has been performed.
