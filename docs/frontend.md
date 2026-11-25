# ScamShield frontend

Plain HTML, CSS and JavaScript **ES modules**, with **no build step**. The backend serves the page at `/` and two
static directories, `/css` and `/js`. The visual design is unchanged from the original single-file page.

## 1. Files and module responsibilities

```
frontend/
  index.html            page structure, element ids, ARIA attributes, the external libraries, the module entry point
  css/styles.css        the page's CSS (moved verbatim from the old <style> block)
  js/
    tailwind-config.js  Tailwind Play CDN configuration (a classic script; keeps index.html free of inline scripts)
    main.js             entry point: initialises once, registers every event listener once, connects events to modules
    dom.js              the one place that looks elements up by id (`el`); reports missing ids at start-up
    state.js            the mutable application state, shared constants (limits), request ids, history helpers
    result.js           the RESULT model: canonical verdicts and labels, normalisation, validation of live responses
    api.js              every request to the backend: timeout, cancellation, ApiError codes, response validation
    scan.js             runs one analysis: scanner overlay, Live request or Demo simulation, stale-result guard
    render.js           turns a result into DOM: verdict card, scores, red flags, recommendation, history, status
    charts.js           the risk gauge and the Chart.js radar chart (no duplicate instances)
    background.js       the neural-grid background canvas (decorative)
    audio.js            the audio tab: file validation, preview playback, samples, spectrum animation (decorative)
    sim.js              Demo mode ("Smart Sim"): keyword scorer, canned audio samples; everything is `simulated: true`
    prompts.js          the quick-test scenario texts (presets)
    examples.js         the Scam Example Lab: modal of fictional training messages that can be loaded into the Text Scanner
    video.js            the Video tab: file checks, upload, and watching the background job until it finishes
    utils.js            escaping, toasts, clipboard, download, safe DOM builder, URL check
```

Dependency direction (no cycles, enforced by a test): `main` → everything; `scan` → `api`, `sim`, `render`, `result`, `state`;
`render` → `charts`, `result`, `state`, `utils`; `api` → `result`; `utils`/`charts`/`prompts`/`audio` → `dom`, `state` and
small helpers. `render` and `sim` never import `api`: rendering and the demo cannot make network calls.

Old function → new module: background canvas → `background.js`; spectrum canvas, file handling → `audio.js`; radar chart,
gauge, counter → `charts.js`; local engine, samples, keyword rules → `sim.js`; presets → `prompts.js`; the Scam Example Lab → `examples.js`;
request functions → `api.js`; `renderResult`/history/preview → `render.js`; scan animation and `trigger*Scan` → `scan.js`;
verdict metadata, `normalizeResult`, `makeUnableResult` → `result.js`; `state`, limits → `state.js`; `el` → `dom.js`;
toast/copy/escape → `utils.js`; bootstrap and event wiring → `main.js`.

## 2. How a result gets on screen

1. **Live mode:** `scan.js` calls `api.js`, which sends a relative same-origin request (`/api/v1/analyze/text` or
   `/audio`, no credentials), applies a 90-second timeout and cancellation, and validates the JSON with `result.js`.
   A response that cannot be trusted (not an object, unknown verdict, completed without a valid score or signal scores,
   status/verdict disagreement) is rejected; unknown fields are dropped, text fields are length-limited, and nothing is
   invented (a missing confidence stays `null`).
2. **Demo mode:** `sim.js` produces a result flagged `simulated: true`. It never calls the backend.
3. `render.js` normalises the result and draws it. A missing score is an empty gauge and a dash, never `0`;
   `unable_to_assess` gets a neutral theme, the "Analysis Incomplete" card and no radar values; it is never shown as Low Risk.
4. **Evidence display (Phase 7).** When a live result has `signals`, the "Red flags" card lists each as an evidence card:
   description, the original-language excerpt (`dir="auto"`, shown exactly as received), source ("Message text",
   "Spoken audio"), approximate time `m:ss` **only when the response contains a number**, why it matters, and a note when
   the content is discussed/fictional/unclear. Demo results and older shapes fall back to the plain `red_flags` strings.
   A language badge (with "(uncertain)"), a "Limitations of this analysis" list and a "Links found" list (structure only,
   "reputation has not been verified") appear only when present and are always hidden for `unable_to_assess`.
5. Every piece of text, including backend and Gemini output, is placed with `textContent`. No module assigns `innerHTML`
   (a test scans the code).

## 2b. Video tab (Phase 9)

A third tab, **Video Clip**, uses the Phase 8 backend ([video.md](video.md)); the code is `video.js` plus small additions in
`api.js` (`submitVideo`, `fetchVideoJob`), `result.js` (`validateVideoSubmission`, `validateVideoJob`), `scan.js` (the overlay
helpers) and `render.js`.

* **Selection** (nothing is uploaded yet): MP4 / MOV / WebM, at most 100 MB and 5 minutes. The length is read locally with a
  hidden `<video>` element and a short-lived object URL (revoked at once); if the browser cannot read it, the file is still
  accepted and the page says the server will check the length. The filename and size are shown, with **Replace** and **Remove**.
  The page states that these checks are a convenience and the server is the authority. The file is never read into memory,
  never stored in the browser and never sent anywhere except `POST /api/analyze-video` (field `file`).
* **Submit**: HTTP 202 -> `{job_id, status, stage, progress}`; the id must match the backend's pattern or the response is rejected.
  A rejected upload (400/413/415/422/429/503, network failure, odd response) is shown **inline** with the server's own message:
  it is not an analysis, so no result is rendered and nothing is added to the history; the selection is kept for a retry.
* **Watching the job**: `GET /api/video-status/{job_id}` every 2 s, one request at a time (the next starts only after the previous
  one answered and the pause elapsed). It stops at `completed`, `unable_to_assess` or `failed`; after 4 consecutive failures
  (network, timeout, 429, 5xx; growing pauses, `Retry-After` honoured); on 404 (expired job or restarted server) or an invalid
  response at once; after a total of 6 minutes (the server's own job limit is 330 s); on **Stop waiting**; and when the page is left
  (`pagehide`). Stopping only stops *watching*: the server may still finish the job, and there is no cancel endpoint.
* **Progress** shown in the existing scanner overlay is only what the server reports (`stage` mapped to a sentence, `progress` as
  the bar width). While the upload itself is in flight, or when the server sent no usable number, the bar **pulses** instead of
  showing a made-up percentage.
* **Duplicates**: while a job is active the button is disabled, `state.isScanning`/`state.videoJob` block any second submit (and
  swapping or removing the file), and a stale result from an older request is ignored.
* **Results** use the same renderer as text and audio. For video, each evidence card shows a coloured tag (violet *Visual*, sky
  *On-screen text*, amber *Speech / audio*), the excerpt, approximate time `m:ss` (only when the server sent numbers),
  `Severity` and `Reliability` (now shown for every mode), why it matters, and a context note; the badge row also shows
  `Coverage: Full/Partial`. Everything without an assessment is shown as "Unable to Assess" with a dash for the score, never Low Risk, and the heading says
  which situation it is (chosen from the reason code in `result.js`): *Not enough evidence to assess* (the analysis ran, evidence
  was insufficient), *Analysis could not be completed* (provider, configuration or internal failure, or a `failed` job) and *Result
  could not be retrieved* (job expired or lost, polling gave up, network problem). The history stores the result object only (label = filename), so video results fit the
  existing history and JSON export unchanged; the video itself is never stored.
* **Demo mode**: there is no simulated video analysis. In Smart Sim the tab explains that video needs FastAPI Live and Analyze
  sends nothing.

## 3. Demo versus Live

| | Smart Sim (Demo) | FastAPI Live |
|---|---|---|
| Calls the backend / Gemini | never | yes |
| Results | `simulated: true`, "Simulated Demo" badge, SIMULATED tag in history | real, validated backend responses |
| On a failure | n/a | an explicit "Unable to Assess" result with the server's message; **never** a demo result |
| Page not served by the backend | works | "Unable to Assess" (`LIVE_API_UNREACHABLE`) |
| Audio sample buttons | canned results (labelled simulated), also usable in Live mode as clearly labelled demo data | same |

Switching modes never changes an existing result; the mode only decides how the next analysis is produced.

## 4. External dependencies (CDN) and CSP

| Resource | Used for | Notes |
|---|---|---|
| `cdn.tailwindcss.com/3.4.17` | styling utilities | the Play CDN; Tailwind documents it as not for production; injects `<style>` at runtime |
| `cdn.jsdelivr.net/.../chart.js@4.4.1` | radar chart | the page still works without it (no chart) |
| `unpkg.com/lucide@0.460.0` | icons | the page still works without it (no icons) |
| `fonts.googleapis.com`, `fonts.gstatic.com` | Inter and JetBrains Mono | falls back to system fonts |

The page needs internet access for these. Content-Security-Policy (set by the backend): scripts from `'self'` and the three
script hosts only, with **no** `'unsafe-inline'` and **no** `'unsafe-eval'`; styles allow `'unsafe-inline'` (Tailwind CDN and
`style=""` attributes); `connect-src 'self'`; `object-src 'none'`; `frame-ancestors 'none'`. Remaining work, none of it done yet:
pin the CDN scripts with SRI hashes or self-host them, and replace the Tailwind Play CDN with a compiled stylesheet (which
would let `style-src` drop `'unsafe-inline'`). See [security.md](security.md).

## 5. Accessibility

Tabs use `role="tablist"/"tab"/"tabpanel"` with `aria-selected`, roving `tabindex` and Left/Right arrow keys; the mode buttons
use `aria-pressed`; the backend drawer button uses `aria-expanded`; the audio dropzone is a keyboard-operable button
(Enter/Space); a visually hidden live region announces "Analysis in progress" and the outcome; the input card sets
`aria-busy` while scanning; toasts are `role="status"` (errors `role="alert"`); the radar chart has a text alternative that
is updated with each result; decorative canvases are `aria-hidden`. Visual appearance is unchanged.

## 6. Verification performed, and what still needs a human

Automated in a real headless browser (`tests/frontend/`, Chrome or Edge; skipped if none is installed or the CDN scripts cannot be loaded): the video tab driven against the real page with `fetch` mocked to return the Phase 8 shapes (selection and limits, 202, progress, polling and terminal states, completed / unable_to_assess / failed, network and server errors, null scores, hostile text, duplicate prevention, Stop waiting, leaving the page, and that text, audio and demo still work).

Automated (pytest, no browser): module structure, import resolution, no cycles, no duplicate functions, element ids,
no inline code or handlers, no unsafe DOM APIs, no credentials or browser storage, relative same-origin requests, canonical
verdict handling, null-score handling, listener registration once, ARIA presence, and that the backend serves each asset
with the right headers and refuses path traversal.

Verified once in the in-app browser against the real backend with a stub provider: a scripted run of nine scenarios gave
**identical results to the old single-file page across 130 compared fields**; layout rectangles and computed styles of all 79
id'd elements matched at 1024, 768 and 375 px (the single difference is the intended backend-status dot colour); 20 API
failure/shape cases, loading state, stale-result handling, hostile markup rendered as text, keyboard operation, object-URL
release, export, and CSP (no violations, no console errors).

Still requires a human or a real device: how the page looks and feels in your own browser (screenshots were not compared
pixel by pixel), screen-reader announcements with a real screen reader, touch behaviour on a phone, behaviour with the CDNs
blocked, Safari/Firefox, and anything involving the real Gemini API.
