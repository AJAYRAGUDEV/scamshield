// Test harness (NOT part of the product). Loads the real page in an iframe, replaces fetch with a mock, drives the real UI
// and records pass/fail for each scenario into <pre id="results"> as JSON. Run by tests/frontend/test_video_ui.py.

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const JID = 'AbCdEfGhIjKlMnOpQrStUvWxYz012345';
const results = [];

class Failure extends Error {}
function ok(condition, message) {
  if (!condition) throw new Failure(message);
}
function eq(actual, expected, message) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new Failure(`${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}

// ---- building blocks ----------------------------------------------------------------------------------------------
async function freshApp() {
  const holder = document.getElementById('frames');
  holder.replaceChildren();
  const frame = document.createElement('iframe');
  frame.style.cssText = 'width:1100px;height:900px';
  frame.src = '/';
  holder.append(frame);
  await new Promise((resolve) => frame.addEventListener('load', resolve, { once: true }));
  const w = frame.contentWindow;
  const d = w.document;
  for (let i = 0; i < 400; i++) {
    const badge = d.getElementById('verdictBadge');
    if (badge && badge.textContent.trim() && d.getElementById('btnModeApi')) break;
    await sleep(50);
  }
  if (!d.getElementById('verdictBadge') || !d.getElementById('verdictBadge').textContent.trim()) throw new Error('INIT_FAILED');
  const video = await w.eval("import('/js/video.js')");
  video.polling.intervalMs = 20;
  video.polling.maxBackoffMs = 60;
  video.polling.maxWaitMs = 5000;
  video.polling.metadataTimeoutMs = 300;
  const app = { w, d, video, q: (id) => d.getElementById(id) };
  app.useFakeDuration = (seconds) => {
    // Test-only: a hidden <video> cannot read a fake file, so make createElement('video') report a chosen length.
    const real = d.createElement.bind(d);
    d.createElement = (tag, ...rest) => {
      if (String(tag).toLowerCase() !== 'video') return real(tag, ...rest);
      const listeners = {};
      const fake = {
        preload: '', muted: false, duration: seconds,
        addEventListener: (name, fn) => { listeners[name] = fn; },
        removeAttribute: () => {},
        set src(value) { setTimeout(() => (seconds === null ? listeners.error && listeners.error() : listeners.loadedmetadata && listeners.loadedmetadata()), 0); },
      };
      return fake;
    };
  };
  return app;
}

function bodyOf(status, stage, progress, extra = {}) {
  return { job_id: JID, status, stage, progress, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:01Z', expires_at: null, result: null, error: null, ...extra };
}

const SCORES = { authority_impersonation: 27, urgency_isolation: 47, credential_payment_trap: 70, synthetic_or_script_signals: 0 };
function signal(category, source, extra = {}) {
  return {
    category, name: `${category}_signal`, description: `Description of ${category}`, why_it_matters: 'It matters because of the pattern.', context: 'directed_at_recipient',
    source, quote: `Quote for ${source}`, timestamp_start: null, timestamp_end: null, severity: 'high', reliability: 'strong', impact: 0.5, corroborating_signals: 0, ...extra,
  };
}
function completedResult(extra = {}) {
  const signals = [
    signal('fake_interface', 'video_visual', { timestamp_start: 3, timestamp_end: 9, severity: 'high', reliability: 'moderate', quote: null }),
    signal('credential_request', 'video_text', { timestamp_start: 10, timestamp_end: 14, severity: 'critical', quote: 'ENTER THE OTP SENT TO YOUR PHONE' }),
    signal('authority_impersonation', 'video_audio', { timestamp_start: 65, timestamp_end: 70, quote: 'I am calling from the bank security team' }),
  ];
  return {
    status: 'completed', verdict: 'high_risk', risk_score: 88, confidence: 0.95, confidence_level: 'High', reason_code: null, coverage: 'full',
    summary: 'High risk: strong scam indicators were detected.', scam_type: 'Possible scam pattern: a request for codes', explanation: 'Server explanation text.',
    recommended_action: 'Do not share any code.', signals, red_flags: signals.map((s) => s.description), signal_scores: SCORES, mode: 'video',
    offline_fallback_used: false, language: 'English', language_uncertain: false,
    limitations: ['Timestamps are approximate and come from the AI model.', 'It does not identify people.'], urls: [], ...extra,
  };
}
function unableResult(reason = 'INSUFFICIENT_EVIDENCE') {
  return {
    status: 'unable_to_assess', verdict: 'unable_to_assess', risk_score: null, confidence: null, confidence_level: null, reason_code: reason, coverage: null,
    summary: 'The content could not be assessed.', scam_type: 'Unable to assess',
    explanation: 'The content could not be understood well enough to judge it, so no assessment was made. No risk score was produced; this is not a low-risk result.',
    recommended_action: 'No assessment was made, so do not treat this content as safe.', signals: [], red_flags: [], signal_scores: null, mode: 'video',
    offline_fallback_used: false, language: null, language_uncertain: false, limitations: [], urls: [],
  };
}

/** Replaces fetch in the page. `cfg.submit` / `cfg.statuses` describe the answers; every call is recorded. */
function installMock(app, cfg) {
  const { w } = app;
  const log = { calls: [], statusInflight: 0, maxStatusInflight: 0, snapshots: [], statusCount: 0 };
  const abortable = (ms, signal) => new Promise((resolve, reject) => {
    if (signal && signal.aborted) { reject(new w.DOMException('Aborted', 'AbortError')); return; }
    const timer = setTimeout(resolve, ms);
    if (signal) signal.addEventListener('abort', () => { clearTimeout(timer); reject(new w.DOMException('Aborted', 'AbortError')); }, { once: true });
  });
  const reply = (spec) => new w.Response(JSON.stringify(spec.body === undefined ? {} : spec.body), {
    status: spec.status || 200, headers: { 'Content-Type': 'application/json', ...(spec.headers || {}) },
  });
  w.fetch = async (url, options = {}) => {
    const u = String(url);
    const method = options.method || 'GET';
    const record = { url: u, method, credentials: options.credentials, hasSignal: Boolean(options.signal) };
    if (options.body instanceof w.FormData) {
      record.fields = [...options.body.keys()];
      const file = options.body.get('file');
      record.fileName = file && file.name;
    }
    log.calls.push(record);
    if (u === '/api/v1/status') return reply({ body: { status: 'ready' } });
    if (u === '/api/analyze-video' && method === 'POST') {
      const spec = typeof cfg.submit === 'function' ? cfg.submit() : cfg.submit;
      await abortable(spec.delay || 0, options.signal);
      if (spec.network) throw new TypeError('Failed to fetch');
      return reply(spec);
    }
    if (u.startsWith('/api/video-status/')) {
      log.statusCount += 1;
      log.statusInflight += 1;
      log.maxStatusInflight = Math.max(log.maxStatusInflight, log.statusInflight);
      try {
        log.snapshots.push({ text: app.q('scanStageText').textContent, width: app.q('scanProgressBar').style.width });
        const list = cfg.statuses;
        const spec = typeof list === 'function' ? list(log.statusCount - 1) : list[Math.min(log.statusCount - 1, list.length - 1)];
        await abortable(spec.delay === undefined ? 5 : spec.delay, options.signal);
        if (spec.network) throw new TypeError('Failed to fetch');
        return reply(spec);
      } finally {
        log.statusInflight -= 1;
      }
    }
    if ((u === '/api/v1/analyze/text' || u === '/api/v1/analyze/audio') && method === 'POST') {
      const spec = u.endsWith('text') ? cfg.text : cfg.audio;
      if (!spec) return reply({ status: 404, body: { detail: 'not mocked', code: 'NOT_FOUND' } });
      return reply(spec);
    }
    return reply({ status: 404, body: { detail: 'Not Found', code: 'NOT_FOUND' } });
  };
  return log;
}

function makeFile(w, name = 'clip.mp4', type = 'video/mp4', bytes = 2048) {
  return new w.File([new Uint8Array(bytes)], name, { type });
}
async function pick(app, file) {
  const input = app.q('videoFileInput');
  const transfer = new app.w.DataTransfer();
  transfer.items.add(file);
  input.files = transfer.files;
  input.dispatchEvent(new app.w.Event('change', { bubbles: true }));
  await sleep(450); // the (fake or real) length check is asynchronous
}
async function waitFor(condition, label, ms = 8000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (condition()) return;
    await sleep(15);
  }
  throw new Failure(`timed out waiting for: ${label}`);
}
const visible = (node) => !node.classList.contains('hidden');
async function goLive(app) {
  app.q('btnModeApi').click();
  await sleep(50);
  app.q('tabVideo').click();
  await sleep(20);
}
const finished = (app) => () => !app.q('btnAnalyzeVideo').disabled && app.q('scannerOverlay').classList.contains('hidden');
const verdict = (app) => app.q('verdictBadge').textContent.trim();
const cards = (app) => [...app.q('redFlagsList').querySelectorAll('li')];

// ---- scenarios ----------------------------------------------------------------------------------------------------
const tests = {};
const test = (name, fn) => { tests[name] = fn; };

test('video tab is present, selectable and accessible', async () => {
  const app = await freshApp();
  eq(app.q('tabVideo').getAttribute('role'), 'tab', 'role');
  eq(app.q('tabVideo').getAttribute('aria-controls'), 'panelVideo', 'aria-controls');
  app.q('tabVideo').click();
  ok(visible(app.q('panelVideo')) && !visible(app.q('panelText')) && !visible(app.q('panelAudio')), 'only the video panel is shown');
  eq(app.q('tabVideo').getAttribute('aria-selected'), 'true', 'aria-selected');
  eq(app.q('tabText').getAttribute('aria-selected'), 'false', 'text tab deselected');
  app.q('tabVideo').dispatchEvent(new app.w.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
  ok(visible(app.q('panelText')), 'ArrowRight from the last tab wraps to the first');
  ok(app.q('videoFileInput').accept.includes('.mp4') && app.q('videoFileInput').accept.includes('.mov') && app.q('videoFileInput').accept.includes('.webm'), 'accepts mp4/mov/webm');
  ok(app.q('panelVideo').textContent.includes('authoritative validation'), 'the page says the server validates authoritatively');
});

test('valid MP4, MOV and WebM files are accepted and shown with name and size', async () => {
  const app = await freshApp();
  app.useFakeDuration(75);
  await goLive(app);
  for (const [name, type] of [['holiday.mp4', 'video/mp4'], ['clip.MOV', 'video/quicktime'], ['rec.webm', 'video/webm']]) {
    await pick(app, makeFile(app.w, name, type, 1572864));
    eq(app.q('selectedVideoName').textContent, name, 'filename');
    eq(app.q('selectedVideoSize').textContent, '(1.50 MB)', 'size');
    eq(app.q('selectedVideoLength').textContent, '- 1:15', 'length');
    ok(visible(app.q('selectedVideoBadge')), 'badge shown');
  }
});

test('unknown length is not blocked and says the server will check it', async () => {
  const app = await freshApp();
  app.useFakeDuration(null);
  await goLive(app);
  await pick(app, makeFile(app.w));
  ok(app.q('selectedVideoLength').textContent.includes('checked by the server'), 'note shown');
  ok(visible(app.q('selectedVideoBadge')), 'file kept');
});

test('unsupported, empty, oversized and too-long videos are rejected on the client', async () => {
  const app = await freshApp();
  app.useFakeDuration(60);
  await goLive(app);
  for (const [file, text] of [
    [makeFile(app.w, 'movie.avi', 'video/x-msvideo'), 'Unsupported video format'],
    [makeFile(app.w, 'movie.mp4.exe', 'application/octet-stream'), 'Unsupported video format'],
    [makeFile(app.w, 'empty.mp4', 'video/mp4', 0), 'empty'],
  ]) {
    await pick(app, file);
    ok(visible(app.q('videoError')) && app.q('videoError').textContent.includes(text), `error for ${file.name}: ${app.q('videoError').textContent}`);
    ok(!visible(app.q('selectedVideoBadge')), 'nothing selected');
  }
  const big = makeFile(app.w, 'big.mp4', 'video/mp4', 10);
  Object.defineProperty(big, 'size', { value: 100 * 1024 * 1024 + 1 });
  await pick(app, big);
  ok(app.q('videoError').textContent.includes('100 MB'), 'oversize message');
  const exactly = makeFile(app.w, 'limit.mp4', 'video/mp4', 10);
  Object.defineProperty(exactly, 'size', { value: 100 * 1024 * 1024 });
  await pick(app, exactly);
  ok(visible(app.q('selectedVideoBadge')), 'exactly 100 MB is allowed');
  app.q('btnRemoveVideo').click();
  app.useFakeDuration(301);
  await pick(app, makeFile(app.w, 'long.mp4'));
  ok(app.q('videoError').textContent.includes('limit'), 'too-long message: ' + app.q('videoError').textContent);
  ok(!visible(app.q('selectedVideoBadge')), 'too-long video not selected');
  app.useFakeDuration(300);
  await pick(app, makeFile(app.w, 'edge.mp4'));
  ok(visible(app.q('selectedVideoBadge')), 'exactly 5 minutes is allowed');
});

test('remove and replace work and nothing is uploaded by selecting', async () => {
  const app = await freshApp();
  app.useFakeDuration(30);
  const log = installMock(app, { submit: { status: 202, body: bodyOf('queued', 'queued', 0) }, statuses: [{ body: bodyOf('processing', 'analysing', 70) }] });
  await goLive(app);
  await pick(app, makeFile(app.w, 'first.mp4'));
  await pick(app, makeFile(app.w, 'second.webm', 'video/webm'));
  eq(app.q('selectedVideoName').textContent, 'second.webm', 'replaced');
  let pickerOpened = 0;
  app.q('videoFileInput').click = () => { pickerOpened += 1; };
  app.q('btnReplaceVideo').click();
  eq(pickerOpened, 1, 'Replace opens the picker');
  app.q('btnRemoveVideo').click();
  ok(!visible(app.q('selectedVideoBadge')) && app.q('selectedVideoName').textContent === '', 'removed');
  eq(log.calls.filter((c) => c.url.includes('video')).length, 0, 'selecting, replacing and removing made no video requests');
});

test('Smart Sim (demo) mode never uploads and says video needs live mode', async () => {
  const app = await freshApp();
  app.useFakeDuration(30);
  const log = installMock(app, { submit: { status: 202, body: bodyOf('queued', 'queued', 0) }, statuses: [{ body: bodyOf('processing', 'analysing', 70) }] });
  app.q('tabVideo').click();
  ok(visible(app.q('videoModeNote')), 'note shown in demo mode');
  await pick(app, makeFile(app.w));
  const before = verdict(app);
  app.q('btnAnalyzeVideo').click();
  await sleep(100);
  eq(log.calls.filter((c) => c.url.includes('video')).length, 0, 'no video request in demo mode');
  eq(verdict(app), before, 'no fabricated demo result');
  app.q('btnModeApi').click();
  ok(!visible(app.q('videoModeNote')), 'note hidden in live mode');
});

test('analyse without a file shows a warning and sends nothing', async () => {
  const app = await freshApp();
  const log = installMock(app, { submit: { status: 202, body: bodyOf('queued', 'queued', 0) }, statuses: [{ body: bodyOf('processing', 'analysing', 70) }] });
  await goLive(app);
  app.q('btnAnalyzeVideo').click();
  await sleep(60);
  eq(log.calls.filter((c) => c.url.includes('video')).length, 0, 'no request');
  ok(app.q('toastContainer').textContent.includes('Choose a video file'), 'toast shown');
});

test('successful job: 202, job id, polling, real progress, completed result', async () => {
  const app = await freshApp();
  app.useFakeDuration(30);
  const statuses = [
    { body: bodyOf('queued', 'queued', 0) },
    { body: bodyOf('processing', 'uploading', 20) },
    { body: bodyOf('processing', 'processing_media', 45) },
    { body: bodyOf('processing', 'analysing', 70) },
    { body: bodyOf('completed', 'finished', 100, { result: completedResult(), expires_at: '2026-01-01T00:15:00Z' }) },
  ];
  const log = installMock(app, { submit: { status: 202, body: bodyOf('queued', 'queued', 0), headers: { Location: `/api/v1/video-status/${JID}` }, delay: 20 }, statuses });
  await goLive(app);
  await pick(app, makeFile(app.w, 'scam.mp4'));
  const before = app.q('historyList').children.length;
  app.q('btnAnalyzeVideo').click();
  await sleep(5);
  ok(app.q('btnAnalyzeVideo').disabled, 'button disabled during the job');
  ok(!app.q('scannerOverlay').classList.contains('hidden') && visible(app.q('btnStopVideoWait')), 'overlay and stop button shown');
  ok(app.q('scanStageText').textContent.toLowerCase().includes('upload'), 'upload text while the POST is pending');
  await waitFor(finished(app), 'job to finish');

  const posts = log.calls.filter((c) => c.method === 'POST' && c.url === '/api/analyze-video');
  eq(posts.length, 1, 'exactly one POST');
  eq(posts[0].fields, ['file'], 'multipart field name');
  eq(posts[0].fileName, 'scam.mp4', 'file sent');
  eq(posts[0].credentials, 'omit', 'no credentials');
  const gets = log.calls.filter((c) => c.method === 'GET' && c.url.startsWith('/api/video-status/'));
  ok(gets.length === 5 && gets.every((c) => c.url === `/api/video-status/${JID}`), `polled the real status URL 5 times, got ${gets.length}`);
  eq(log.maxStatusInflight, 1, 'polling requests never overlap');
  const widths = log.snapshots.map((s) => s.width);
  ok(widths.slice(1).join(',') === '0%,20%,45%,70%'.split(',').slice(0, widths.length - 1).join(',') || widths.join(',').includes('20%,45%,70%'), `overlay shows the backend's progress values: ${widths.join(',')}`);
  ok(log.snapshots.some((s) => s.text.includes('preparing the video')) && log.snapshots.some((s) => s.text.includes('Analysing visuals')), 'stage names are translated into text');
  const count = log.calls.length;
  await sleep(300);
  eq(log.calls.length, count, 'polling stopped at the terminal status');

  eq(verdict(app), 'High Risk', 'verdict');
  // The numeric counter is animated with requestAnimationFrame, which headless virtual time does not drive; the gauge arc is set
  // synchronously from the score (414.69 x (1 - 0.88) = 49.76), so that is what is checked here.
  eq(app.q('gaugeArc').style.strokeDashoffset, '49.76', 'gauge shows the server score of 88');
  ok(app.q('confidenceBadge').textContent.includes('High (95%)'), 'confidence');
  eq(app.q('modeBadge').textContent, 'Mode: video', 'mode badge');
  ok(app.q('coverageBadge').textContent === 'Coverage: Full' && visible(app.q('coverageBadge')), 'coverage badge');
  ok(app.q('limitationsList').textContent.includes('does not identify people'), 'limitations shown');
  eq(app.q('historyList').children.length, before + 1, 'history entry added');
  ok(app.q('historyList').textContent.includes('scam.mp4') && app.q('historyList').textContent.includes('VIDEO'), 'history labelled with the filename and VIDEO');
  const list = cards(app);
  eq(list.length, 3, 'three evidence cards');
  const text = list.map((li) => li.textContent);
  ok(text[0].includes('Visual (what is shown)') && text[0].includes('0:03–0:09') && text[0].includes('Severity: High') && text[0].includes('Reliability: Moderate'), 'visual card: ' + text[0]);
  ok(text[1].includes('On-screen text') && text[1].includes('ENTER THE OTP SENT TO YOUR PHONE') && text[1].includes('0:10–0:14') && text[1].includes('Severity: Critical'), 'text card: ' + text[1]);
  ok(text[2].includes('Speech / audio in the video') && text[2].includes('1:05–1:10') && text[2].includes('bank security team'), 'speech card: ' + text[2]);
  const chips = list.map((li) => li.querySelector('span.border') && li.querySelector('span.border').className);
  ok(new Set(chips).size === 3, 'three different tags for visual, on-screen text and speech');
  ok(!app.q('btnAnalyzeVideo').disabled, 'button enabled again');
});

test('unable_to_assess job is never shown as Low Risk and a null score is not zero', async () => {
  const app = await freshApp();
  app.useFakeDuration(30);
  installMock(app, {
    submit: { status: 202, body: bodyOf('queued', 'queued', 0) },
    statuses: [{ body: bodyOf('processing', 'analysing', 70) }, { body: bodyOf('unable_to_assess', 'finished', 100, { result: unableResult(), expires_at: '2026-01-01T00:15:00Z' }) }],
  });
  await goLive(app);
  await pick(app, makeFile(app.w));
  app.q('btnAnalyzeVideo').click();
  await sleep(10);
  await waitFor(finished(app), 'finish');
  eq(verdict(app), 'Unable to Assess', 'verdict label');
  ok(!verdict(app).includes('Low'), 'not Low Risk');
  ok(app.q('riskScoreValue').textContent.trim() !== '0', 'score is not displayed as 0: ' + app.q('riskScoreValue').textContent);
  ok(app.q('explanationText').textContent.includes('no assessment was made') || app.q('explanationText').textContent.includes('No risk score'), 'explanation: ' + app.q('explanationText').textContent);
  ok(app.q('redFlagCount').textContent.trim() === '—', 'findings count unavailable');
  ok(app.q('actionTitle').textContent === 'Analysis Incomplete', 'incomplete card');
  ok(!visible(app.q('coverageBadge')) && !visible(app.q('limitationsBlock')), 'no coverage or limitations claimed');
  ok(app.q('historyList').textContent.includes('N/A'), 'history shows N/A for the null score');
});

test('failed job becomes an explicit unable-to-assess result with the server message', async () => {
  const app = await freshApp();
  app.useFakeDuration(30);
  installMock(app, {
    submit: { status: 202, body: bodyOf('queued', 'queued', 0) },
    statuses: [{ body: bodyOf('failed', 'finished', 100, { error: { code: 'SERVER_SHUTDOWN', message: 'The server was shutting down. Please submit the video again.' }, expires_at: '2026-01-01T00:15:00Z' }) }],
  });
  await goLive(app);
  await pick(app, makeFile(app.w));
  app.q('btnAnalyzeVideo').click();
  await sleep(10);
  await waitFor(finished(app), 'finish');
  eq(verdict(app), 'Unable to Assess', 'verdict');
  ok(app.q('explanationText').textContent.includes('shutting down'), 'server message shown: ' + app.q('explanationText').textContent);
  ok(app.q('riskScoreValue').textContent.trim() !== '0', 'no zero score');
});

test('each kind of "no assessment" has its own heading and message, and none looks like a result', async () => {
  const cases = [
    ['completed analysis, not enough evidence', [{ body: bodyOf('unable_to_assess', 'finished', 100, { result: unableResult('INSUFFICIENT_EVIDENCE'), expires_at: 'x' }) }], 'Not enough evidence to assess', 'could not be understood well enough'],
    ['analysis ran but the provider failed', [{ body: bodyOf('unable_to_assess', 'finished', 100, { result: unableResult('PROVIDER_TIMEOUT'), expires_at: 'x' }) }], 'Analysis could not be completed', 'No risk score'],
    ['failed job', [{ body: bodyOf('failed', 'finished', 100, { error: { code: 'INTERNAL_ERROR', message: 'An unexpected internal error stopped the analysis.' }, expires_at: 'x' }) }], 'Analysis could not be completed', 'job failed'],
    ['expired or unknown job (404)', [{ status: 404, body: { detail: 'gone', code: 'JOB_NOT_FOUND' } }], 'Result could not be retrieved', 'could not be found'],
    ['polling interrupted for good', [{ network: true }], 'Result could not be retrieved', 'Lost contact'],
  ];
  const headings = new Set();
  for (const [label, statuses, heading, message] of cases) {
    const app = await freshApp();
    app.useFakeDuration(30);
    installMock(app, { submit: { status: 202, body: bodyOf('queued', 'queued', 0) }, statuses });
    await goLive(app);
    await pick(app, makeFile(app.w));
    app.q('btnAnalyzeVideo').click();
    await sleep(10);
    await waitFor(finished(app), 'finish');
    eq(verdict(app), 'Unable to Assess', `${label}: badge`);
    eq(app.q('scamTypeHeading').textContent, heading, `${label}: heading`);
    ok(app.q('explanationText').textContent.includes(message), `${label}: message: ${app.q('explanationText').textContent}`);
    ok(!/low/i.test(verdict(app) + app.q('scamTypeHeading').textContent), `${label}: never Low Risk`);
    ok(app.q('riskScoreValue').textContent.trim() !== '0' && app.q('gaugeArc').style.strokeDashoffset === '414.69', `${label}: no score on the gauge`);
    ok(app.q('confidenceBadge').textContent.includes('--'), `${label}: no confidence`);
    ok(app.q('redFlagCount').textContent.trim() === '—' && !visible(app.q('coverageBadge')), `${label}: no findings or coverage`);
    headings.add(heading);
  }
  eq(headings.size, 3, 'three distinct situations are told apart');
});

test('server validation errors on submit are shown inline and nothing is polled', async () => {
  for (const [spec, expected] of [
    [{ status: 422, body: { detail: 'The video is longer than the 300-second limit.', code: 'VIDEO_TOO_LONG' } }, '300-second limit'],
    [{ status: 415, body: { detail: 'Unsupported video format. Use MP4, MOV, or WEBM.', code: 'UNSUPPORTED_MEDIA_TYPE' } }, 'Unsupported video format'],
    [{ status: 413, body: { detail: 'Video file exceeds the 100 MB limit.', code: 'PAYLOAD_TOO_LARGE' } }, '100 MB'],
    [{ status: 503, body: { detail: 'The service is at capacity right now.', code: 'SERVICE_BUSY' }, headers: { 'Retry-After': '15' } }, 'retry in about 15s'],
    [{ status: 429, body: { detail: 'Too many requests.', code: 'RATE_LIMITED' }, headers: { 'Retry-After': '30' } }, 'Too many requests'],
    [{ status: 500, body: 'not an object' }, 'Server error (500)'],
    [{ network: true }, 'Could not reach the analysis backend'],
  ]) {
    const app = await freshApp();
    app.useFakeDuration(30);
    const log = installMock(app, { submit: spec, statuses: [{ body: bodyOf('processing', 'analysing', 70) }] });
    await goLive(app);
    await pick(app, makeFile(app.w));
    const before = verdict(app);
    const historyBefore = app.q('historyList').children.length;
    app.q('btnAnalyzeVideo').click();
    await sleep(10);
    await waitFor(finished(app), 'finish');
    ok(visible(app.q('videoError')) && app.q('videoError').textContent.includes(expected), `inline error for ${JSON.stringify(spec).slice(0, 60)}: ${app.q('videoError').textContent}`);
    eq(log.calls.filter((c) => c.url.startsWith('/api/video-status/')).length, 0, 'no polling after a failed submit');
    eq(verdict(app), before, 'a rejected upload does not replace the shown result');
    eq(app.q('historyList').children.length, historyBefore, 'no history entry for a rejected upload');
    ok(!app.q('btnAnalyzeVideo').disabled && !app.q('scannerOverlay').classList.contains('hidden') === false, 'UI usable again');
    ok(visible(app.q('selectedVideoBadge')), 'the selected video is kept so the user can retry');
  }
});

test('unexpected 202 bodies are rejected (no invented job)', async () => {
  for (const body of [{}, { job_id: 'short', status: 'queued' }, { job_id: JID, status: 'completed' }, { job_id: '../../etc/passwd/../../xx', status: 'queued' }, 'text', null, []]) {
    const app = await freshApp();
    app.useFakeDuration(30);
    const log = installMock(app, { submit: { status: 202, body }, statuses: [{ body: bodyOf('processing', 'analysing', 70) }] });
    await goLive(app);
    await pick(app, makeFile(app.w));
    app.q('btnAnalyzeVideo').click();
    await sleep(10);
    await waitFor(finished(app), 'finish');
    ok(visible(app.q('videoError')), `error shown for ${JSON.stringify(body)}`);
    eq(log.calls.filter((c) => c.url.startsWith('/api/video-status/')).length, 0, 'never polled with an invalid id');
  }
});

test('transient polling failures are retried and the job still completes', async () => {
  const app = await freshApp();
  app.useFakeDuration(30);
  const log = installMock(app, {
    submit: { status: 202, body: bodyOf('queued', 'queued', 0) },
    statuses: [{ network: true }, { status: 503, body: { detail: 'busy', code: 'SERVICE_BUSY' } }, { body: bodyOf('processing', 'analysing', 70) },
      { body: bodyOf('completed', 'finished', 100, { result: completedResult(), expires_at: 'x' }) }],
  });
  await goLive(app);
  await pick(app, makeFile(app.w));
  app.q('btnAnalyzeVideo').click();
  await sleep(10);
  await waitFor(finished(app), 'finish');
  eq(verdict(app), 'High Risk', 'completed after retries');
  eq(log.statusCount, 4, 'four status requests');
  eq(log.maxStatusInflight, 1, 'still never overlapping');
});

test('persistent polling failures stop after a bounded number of attempts', async () => {
  const app = await freshApp();
  app.useFakeDuration(30);
  const log = installMock(app, { submit: { status: 202, body: bodyOf('queued', 'queued', 0) }, statuses: [{ network: true }] });
  await goLive(app);
  await pick(app, makeFile(app.w));
  app.q('btnAnalyzeVideo').click();
  await sleep(10);
  await waitFor(finished(app), 'give up');
  eq(verdict(app), 'Unable to Assess', 'explicit unable result');
  ok(app.q('explanationText').textContent.includes('Lost contact'), 'explains: ' + app.q('explanationText').textContent);
  ok(log.statusCount >= 4 && log.statusCount <= 6, `bounded attempts: ${log.statusCount}`);
  const count = log.calls.length;
  await sleep(300);
  eq(log.calls.length, count, 'no further polling');
});

test('an unknown or expired job (404) stops polling with a clear message', async () => {
  const app = await freshApp();
  app.useFakeDuration(30);
  const log = installMock(app, { submit: { status: 202, body: bodyOf('queued', 'queued', 0) }, statuses: [{ body: bodyOf('processing', 'analysing', 70) }, { status: 404, body: { detail: 'No video analysis was found', code: 'JOB_NOT_FOUND' } }] });
  await goLive(app);
  await pick(app, makeFile(app.w));
  app.q('btnAnalyzeVideo').click();
  await sleep(10);
  await waitFor(finished(app), 'finish');
  eq(verdict(app), 'Unable to Assess', 'verdict');
  ok(app.q('explanationText').textContent.includes('could not be found'), 'message: ' + app.q('explanationText').textContent);
  eq(log.statusCount, 2, 'no retry after 404');
});

test('inconsistent or malformed job responses are rejected, never displayed', async () => {
  for (const body of [
    bodyOf('completed', 'finished', 100),                                           // completed without a result
    bodyOf('processing', 'analysing', 70, { result: completedResult() }),           // unfinished with a result
    bodyOf('completed', 'finished', 100, { result: unableResult() }),               // status and result disagree
    bodyOf('mystery', 'x', 1),                                                      // unknown status
    { ...bodyOf('processing', 'analysing', 70), job_id: 'DifferentJobIdDifferentJobId99999' },
    bodyOf('failed', 'finished', 100),                                              // failed without an error
    'oops',
  ]) {
    const app = await freshApp();
    app.useFakeDuration(30);
    const log = installMock(app, { submit: { status: 202, body: bodyOf('queued', 'queued', 0) }, statuses: [{ body }] });
    await goLive(app);
    await pick(app, makeFile(app.w));
    app.q('btnAnalyzeVideo').click();
    await sleep(10);
    await waitFor(finished(app), 'finish');
    eq(verdict(app), 'Unable to Assess', `rejected: ${JSON.stringify(body).slice(0, 70)}`);
    ok(app.q('explanationText').textContent.includes('unexpected response'), 'says the response was unexpected: ' + app.q('explanationText').textContent);
    ok(log.statusCount === 1, 'no retry on an invalid response');
  }
});

test('missing progress is never invented', async () => {
  const app = await freshApp();
  app.useFakeDuration(30);
  const log = installMock(app, {
    submit: { status: 202, body: bodyOf('queued', 'queued', 0) },
    statuses: [{ body: { ...bodyOf('processing', 'analysing', 70), progress: null } }, { body: { ...bodyOf('processing', 'weird stage!', 'lots'), progress: 'lots' } },
      { body: bodyOf('completed', 'finished', 100, { result: completedResult(), expires_at: 'x' }) }],
  });
  await goLive(app);
  await pick(app, makeFile(app.w));
  app.q('btnAnalyzeVideo').click();
  await sleep(10);
  await waitFor(finished(app), 'finish');
  ok(log.snapshots.length === 3 && log.snapshots[1].width === '35%' && log.snapshots[2].width === '35%', `indeterminate bar when no number: ${JSON.stringify(log.snapshots)}`);
  ok(log.snapshots[2].text === 'Working on the analysis...', 'unknown stage text is generic: ' + log.snapshots[2].text);
});

test('polling gives up after the maximum wait', async () => {
  const app = await freshApp();
  app.useFakeDuration(30);
  app.video.polling.maxWaitMs = 400;
  const log = installMock(app, { submit: { status: 202, body: bodyOf('queued', 'queued', 0) }, statuses: [{ body: bodyOf('processing', 'analysing', 70) }] });
  await goLive(app);
  await pick(app, makeFile(app.w));
  app.q('btnAnalyzeVideo').click();
  await sleep(10);
  await waitFor(finished(app), 'give up', 15000);
  eq(verdict(app), 'Unable to Assess', 'explicit unable result, no infinite polling');
  ok(app.q('explanationText').textContent.includes('longer than expected'), 'message: ' + app.q('explanationText').textContent);
  const count = log.calls.length;
  await sleep(300);
  eq(log.calls.length, count, 'polling stopped');
});

test('duplicate submissions are prevented while a job is active', async () => {
  const app = await freshApp();
  app.useFakeDuration(30);
  const log = installMock(app, {
    submit: { status: 202, body: bodyOf('queued', 'queued', 0), delay: 60 },
    statuses: (i) => (i < 60 ? { body: bodyOf('processing', 'analysing', 70) } : { body: bodyOf('completed', 'finished', 100, { result: completedResult(), expires_at: 'x' }) }),
  });
  await goLive(app);
  await pick(app, makeFile(app.w));
  const button = app.q('btnAnalyzeVideo');
  button.click();
  button.click();
  button.dispatchEvent(new app.w.MouseEvent('click', { bubbles: true }));
  await app.video.triggerVideoScan();
  await sleep(10);
  ok(button.disabled, 'disabled while active');
  await pick(app, makeFile(app.w, 'other.mp4'));
  ok(app.q('selectedVideoName').textContent !== 'other.mp4', 'cannot swap the file mid-job');
  app.q('btnRemoveVideo').click();
  ok(visible(app.q('selectedVideoBadge')), 'cannot remove the file mid-job');
  await waitFor(finished(app), 'finish');
  eq(log.calls.filter((c) => c.method === 'POST' && c.url === '/api/analyze-video').length, 1, 'exactly one job was created');
  button.click();
  await sleep(10);
  await waitFor(finished(app), 'second job');
  eq(log.calls.filter((c) => c.method === 'POST' && c.url === '/api/analyze-video').length, 2, 'a new job is allowed after the first finished');
});

test('Stop waiting cancels polling and shows no result', async () => {
  const app = await freshApp();
  app.useFakeDuration(30);
  const log = installMock(app, { submit: { status: 202, body: bodyOf('queued', 'queued', 0) }, statuses: [{ body: bodyOf('processing', 'analysing', 70) }] });
  await goLive(app);
  await pick(app, makeFile(app.w));
  const before = verdict(app);
  const historyBefore = app.q('historyList').children.length;
  app.q('btnAnalyzeVideo').click();
  await waitFor(() => log.statusCount >= 2, 'polling to start');
  app.q('btnStopVideoWait').click();
  await waitFor(finished(app), 'UI to return');
  const count = log.calls.length;
  await sleep(300);
  eq(log.calls.length, count, 'no requests after Stop waiting');
  eq(verdict(app), before, 'no result rendered');
  eq(app.q('historyList').children.length, historyBefore, 'no history entry');
  ok(app.q('toastContainer').textContent.includes('Stopped waiting'), 'user told');
  ok(!visible(app.q('btnStopVideoWait')), 'stop button hidden');
});

test('leaving the page stops polling', async () => {
  const app = await freshApp();
  app.useFakeDuration(30);
  const log = installMock(app, { submit: { status: 202, body: bodyOf('queued', 'queued', 0) }, statuses: [{ body: bodyOf('processing', 'analysing', 70) }] });
  await goLive(app);
  await pick(app, makeFile(app.w));
  app.q('btnAnalyzeVideo').click();
  await waitFor(() => log.statusCount >= 2, 'polling to start');
  app.w.dispatchEvent(new app.w.Event('pagehide'));
  await sleep(150);
  const count = log.calls.length;
  await sleep(300);
  eq(log.calls.length, count, 'polling stopped on pagehide');
});

test('hostile text in results and filenames is rendered as plain text', async () => {
  const app = await freshApp();
  app.useFakeDuration(30);
  const evil = completedResult({
    summary: '<img src=x onerror="window.__xss=1">', explanation: '<script>window.__xss=2</script>', scam_type: '<b>bold</b>',
    limitations: ['<img src=x onerror="window.__xss=3">'],
    signals: [signal('credential_request', 'video_text', { description: '<img src=x onerror="window.__xss=4">desc', quote: '<script>window.__xss=5</script>', why_it_matters: '<b>w</b>', timestamp_start: 1, timestamp_end: 2 })],
  });
  installMock(app, { submit: { status: 202, body: bodyOf('queued', 'queued', 0) }, statuses: [{ body: bodyOf('completed', 'finished', 100, { result: evil, expires_at: 'x' }) }] });
  await goLive(app);
  await pick(app, makeFile(app.w, '<img src=x onerror="window.__xss=6">.mp4'));
  ok(app.q('selectedVideoName').textContent.includes('<img'), 'filename shown as text');
  app.q('btnAnalyzeVideo').click();
  await sleep(10);
  await waitFor(finished(app), 'finish');
  ok(cards(app)[0].textContent.includes('<img src=x'), 'markup visible as text');
  eq(app.d.querySelectorAll('#redFlagsList img, #redFlagsList script, #limitationsList img, #explanationText img, #explanationText script, #historyList img, #selectedVideoName img').length, 0, 'no element was created from untrusted text');
  eq(app.w.__xss, undefined, 'no script ran');
});

test('existing text and audio analysis still work (live mode)', async () => {
  const app = await freshApp();
  const textResult = { ...completedResult(), mode: 'text', signals: [], red_flags: [], limitations: [], summary: 'Text summary' };
  installMock(app, { text: { status: 200, body: textResult }, audio: { status: 200, body: { ...unableResult(), mode: 'audio' } } });
  app.q('btnModeApi').click();
  app.q('tabText').click();
  app.q('messageInput').value = 'Please share your OTP now';
  app.q('messageInput').dispatchEvent(new app.w.Event('input', { bubbles: true }));
  app.q('btnAnalyzeText').click();
  await waitFor(() => app.q('scannerOverlay').classList.contains('hidden') && app.q('modeBadge').textContent === 'Mode: text', 'text result');
  eq(verdict(app), 'High Risk', 'text verdict');
  ok(!visible(app.q('simBadge')), 'not marked as demo');
  app.q('tabAudio').click();
  const transfer = new app.w.DataTransfer();
  transfer.items.add(new app.w.File([new Uint8Array(64)], 'call.wav', { type: 'audio/wav' }));
  app.q('audioFileInput').files = transfer.files;
  app.q('audioFileInput').dispatchEvent(new app.w.Event('change', { bubbles: true }));
  await sleep(50);
  app.q('btnAnalyzeAudio').click();
  await waitFor(() => app.q('scannerOverlay').classList.contains('hidden') && app.q('modeBadge').textContent === 'Mode: audio', 'audio result');
  eq(verdict(app), 'Unable to Assess', 'audio unable result still renders');
  app.q('tabVideo').click();
  app.q('tabText').click();
  ok(visible(app.q('panelText')), 'tabs still switch');
});

test('demo mode text analysis and the example lab are unaffected', async () => {
  const app = await freshApp();
  app.q('tabText').click();
  app.q('messageInput').value = 'URGENT share your OTP';
  app.q('messageInput').dispatchEvent(new app.w.Event('input', { bubbles: true }));
  app.q('btnAnalyzeText').click();
  await waitFor(() => app.q('scannerOverlay').classList.contains('hidden') && visible(app.q('simBadge')), 'demo result');
  ok(visible(app.q('simBadge')), 'demo result is labelled Simulated Demo');
  app.q('btnOpenExampleLab').click();
  ok(visible(app.q('exampleLab')), 'example lab opens');
});

// ---- runner -------------------------------------------------------------------------------------------------------
async function run() {
  for (const [name, fn] of Object.entries(tests)) {
    const started = Date.now();
    try {
      await fn();
      results.push({ name, ok: true, ms: Date.now() - started });
    } catch (error) {
      if (error && error.message === 'INIT_FAILED') {
        results.push({ name: '*', ok: false, init_failed: true, detail: 'the page did not initialise (are the CDN scripts reachable?)' });
        break;
      }
      results.push({ name, ok: false, detail: String(error && error.message ? error.message : error).slice(0, 600) });
    }
  }
  document.getElementById('results').textContent = JSON.stringify({ done: true, results });
  document.title = 'DONE';
}
run();
