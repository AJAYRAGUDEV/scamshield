// render.js - turns a result into what is on screen: verdict card, scores, radar, red flags, recommendation,
// history, text preview, backend status.
//
// Rendering NEVER makes network calls and never assigns innerHTML: every piece of text (including backend and
// Gemini output) is placed with textContent via makeEl, so it cannot inject markup or script.

import { el } from './dom.js';
import { state, addHistory, MAX_TEXT_CHARS } from './state.js';
import { makeEl, showToast } from './utils.js';
import { VERDICT_META, normalizeResult, scoreText, unableHeading } from './result.js';
import { setGauge, updateRadar } from './charts.js';

const BADGE_BASE = 'inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold uppercase tracking-wider border';

/** Updates the visually hidden live region so screen readers hear status changes. */
export function announce(message) {
  el.srAnnouncer.textContent = message;
}

function renderVerdict(res, meta, unavailable) {
  state.targetThemeRgb = meta.rgb;
  document.body.setAttribute('data-theme-risk', meta.theme);

  setGauge(typeof res.risk_score === 'number' ? res.risk_score : null);

  el.verdictBadge.className = `${BADGE_BASE} ${meta.badge}`;
  el.verdictBadge.replaceChildren(
    makeEl('span', { className: `w-2 h-2 rounded-full bg-current ${unavailable ? '' : 'animate-pulse'}`.trim() }),
    document.createTextNode(meta.label)
  );

  const confidenceText = res.confidence_level
    ? `${res.confidence_level}${typeof res.confidence === 'number' ? ` (${Math.round(res.confidence * 100)}%)` : ''}`
    : '--';
  el.confidenceBadge.textContent = `Confidence: ${confidenceText}`;
  el.modeBadge.textContent = `Mode: ${res.mode}`;
  el.fallbackBadge.classList.toggle('hidden', !res.offline_fallback_used);
  el.simBadge.classList.toggle('hidden', !res.simulated);

  el.scamTypeHeading.textContent = unavailable ? unableHeading(res.reason_code) : res.scam_type; // says WHICH kind of 'no assessment'
  el.explanationText.textContent = res.explanation;
}

function renderRecommendation(res, unavailable) {
  el.recommendedActionText.textContent = res.recommended_action;
  // A failed analysis is visibly different from a completed one.
  el.actionTitle.textContent = unavailable ? 'Analysis Incomplete' : 'Recommended Action';
  el.actionCard.classList.toggle('border-emerald-500/20', !unavailable);
  el.actionCard.classList.toggle('border-slate-500/40', unavailable);
  el.actionTitle.parentElement.classList.toggle('text-emerald-300', !unavailable);
  el.actionTitle.parentElement.classList.toggle('text-slate-300', unavailable);
}

function updateMeter(valueEl, barEl, score) {
  if (score === null || score === undefined) {
    valueEl.textContent = '—';
    barEl.style.width = '0%';
    return;
  }
  const clamped = Math.max(0, Math.min(100, Number(score) || 0));
  valueEl.textContent = `${clamped}%`;
  barEl.style.width = `${clamped}%`;
}

function renderScores(res, meta) {
  const scores = res.signal_scores;
  const values = scores
    ? [scores.authority_impersonation, scores.urgency_isolation, scores.credential_payment_trap, scores.synthetic_or_script_signals]
    : [null, null, null, null];
  updateMeter(el.valAuthority, el.barAuthority, values[0]);
  updateMeter(el.valUrgency, el.barUrgency, values[1]);
  updateMeter(el.valCredential, el.barCredential, values[2]);
  updateMeter(el.valSynthetic, el.barSynthetic, values[3]);
  updateRadar(values, meta);
}

function statusItem(text, textClass, dotClass) {
  const item = makeEl('li', { className: `flex items-center gap-2 ${textClass} py-1` });
  item.append(makeEl('span', { className: `w-2 h-2 rounded-full ${dotClass}` }), document.createTextNode(text));
  return item;
}

const SOURCE_LABELS = {
  text_content: 'Message text',
  audio_speech: 'Spoken audio',
  audio_acoustic: 'Audio sound',
  video_visual: 'Visual (what is shown)',
  video_text: 'On-screen text',
  video_audio: 'Speech / audio in the video',
};
// Video evidence gets a coloured tag so visual, on-screen-text and spoken evidence cannot be confused.
const SOURCE_CHIPS = {
  video_visual: 'bg-violet-500/15 text-violet-300 border-violet-500/35',
  video_text: 'bg-sky-500/15 text-sky-300 border-sky-500/35',
  video_audio: 'bg-amber-500/15 text-amber-300 border-amber-500/35',
};
const capitalise = (word) => (typeof word === 'string' && word ? word.charAt(0).toUpperCase() + word.slice(1) : '');
const CONTEXT_NOTES = {
  discussed_or_described: 'Described or discussed here, not aimed at the reader.',
  fictional_or_example: 'Appears in fiction or as an example.',
  unclear: 'It is unclear whether this is aimed at the reader.',
};

/** Seconds -> m:ss. Only called with real numbers. */
function clock(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

function timeLabel(signal) {
  const start = signal.timestamp_start;
  const end = signal.timestamp_end;
  if (typeof start !== 'number') return '';
  const range = typeof end === 'number' && end > start ? `${clock(start)}–${clock(end)}` : clock(start);
  return `Approx. ${range}`;
}

function evidenceCard(signal, res, index) {
  const item = makeEl('li', { className: 'animate-in-item flex items-start gap-2.5 p-2.5 rounded-xl bg-slate-900/75 border border-slate-800/90' });
  item.style.animationDelay = `${index * 65}ms`;
  const body = makeEl('div', { className: 'min-w-0 space-y-1' });
  body.append(makeEl('p', { className: 'leading-snug break-words', text: signal.description || signal.name }));
  if (signal.quote) {
    // dir="auto" lets Tamil/Hindi/Arabic quotes lay out correctly; the text is shown exactly as received.
    body.append(makeEl('blockquote', {
      className: 'pl-2 border-l-2 border-slate-600 text-slate-200 italic break-words',
      text: signal.quote,
      attrs: { dir: 'auto' },
    }));
  }
  const chip = SOURCE_CHIPS[signal.source];
  if (chip) {
    body.append(makeEl('span', {
      className: `inline-flex items-center px-2 py-0.5 rounded-full border text-[10px] font-mono font-semibold ${chip}`,
      text: SOURCE_LABELS[signal.source],
    }));
  }
  const meta = [
    chip ? '' : SOURCE_LABELS[signal.source] || '',
    timeLabel(signal),
    signal.severity ? `Severity: ${capitalise(signal.severity)}` : '',
    signal.reliability ? `Reliability: ${capitalise(signal.reliability)}` : '',
  ].filter(Boolean).join(' • ');
  if (meta) body.append(makeEl('p', { className: 'text-[10px] font-mono text-slate-400 break-words', text: meta }));
  if (signal.why_it_matters) body.append(makeEl('p', { className: 'text-slate-400 break-words', text: signal.why_it_matters }));
  if (CONTEXT_NOTES[signal.context]) body.append(makeEl('p', { className: 'text-[11px] text-sky-300/90 break-words', text: CONTEXT_NOTES[signal.context] }));
  item.append(
    makeEl('span', { className: `mt-1 w-2 h-2 rounded-full shrink-0 ${res.verdict === 'high_risk' ? 'bg-rose-400' : 'bg-amber-400'}` }),
    body
  );
  return item;
}

function renderUrls(res, unavailable) {
  const urls = !unavailable && Array.isArray(res.urls) ? res.urls : [];
  el.urlFindings.classList.toggle('hidden', urls.length === 0);
  el.urlFindingsList.replaceChildren();
  urls.forEach((url) => {
    const item = makeEl('li', { className: 'p-2.5 rounded-xl bg-slate-900/75 border border-slate-800/90 space-y-1' });
    item.append(makeEl('p', { className: 'font-mono break-all text-slate-200', text: url.display || url.domain }));
    if (url.indicators.length) item.append(makeEl('p', { className: 'text-amber-300/90', text: url.indicators.join(' • ') }));
    item.append(makeEl('p', { className: 'text-[11px] text-slate-400', text: url.note || 'Only the link structure was examined; it was not visited and its reputation has not been verified.' }));
    el.urlFindingsList.append(item);
  });
}

function renderEvidenceExtras(res, unavailable) {
  const language = !unavailable && res.language ? `Language: ${res.language}${res.language_uncertain ? ' (uncertain)' : ''}` : '';
  el.languageBadge.textContent = language;
  el.languageBadge.setAttribute('dir', 'auto');
  el.languageBadge.classList.toggle('hidden', !language);

  const coverage = !unavailable && (res.coverage === 'full' || res.coverage === 'partial') ? `Coverage: ${capitalise(res.coverage)}` : '';
  el.coverageBadge.textContent = coverage;
  el.coverageBadge.classList.toggle('hidden', !coverage);

  const limits = !unavailable && Array.isArray(res.limitations) ? res.limitations : [];
  el.limitationsBlock.classList.toggle('hidden', limits.length === 0);
  el.limitationsList.replaceChildren(...limits.map((text) => makeEl('li', { className: 'break-words', text })));
  renderUrls(res, unavailable);
}

function renderFlags(res, unavailable) {
  const flags = Array.isArray(res.red_flags) ? res.red_flags : [];
  const signals = Array.isArray(res.signals) ? res.signals : [];
  const useEvidence = !unavailable && signals.length > 0;
  el.redFlagCount.textContent = unavailable ? '—' : String(useEvidence ? signals.length : flags.length);
  el.redFlagsList.replaceChildren();

  if (unavailable) {
    el.redFlagsList.append(statusItem('No assessment was made, so there are no findings to show. This is not a low-risk result.', 'text-slate-300', 'bg-slate-400'));
  } else if (useEvidence) {
    signals.forEach((signal, index) => el.redFlagsList.append(evidenceCard(signal, res, index)));
  } else if (flags.length === 0) {
    el.redFlagsList.append(statusItem('No specific scam red flags flagged in this input.', 'text-emerald-300/90', 'bg-emerald-400'));
  } else {
    flags.forEach((flag, index) => {
      const item = makeEl('li', { className: 'animate-in-item flex items-start gap-2.5 p-2.5 rounded-xl bg-slate-900/75 border border-slate-800/90' });
      item.style.animationDelay = `${index * 65}ms`;
      item.append(
        makeEl('span', { className: `mt-1 w-2 h-2 rounded-full shrink-0 ${res.verdict === 'high_risk' ? 'bg-rose-400' : 'bg-amber-400'}` }),
        makeEl('span', { className: 'leading-snug', text: String(flag) })
      );
      el.redFlagsList.append(item);
    });
  }
}

function announcementFor(res, unavailable) {
  const prefix = res.simulated ? 'Simulated demo result: ' : '';
  if (unavailable) return `${prefix}Analysis could not be completed. No risk score was produced.`;
  return `${prefix}Analysis complete: ${VERDICT_META[res.verdict].label}, risk score ${res.risk_score} out of 100.`;
}

/** Renders a result. `sourceLabel` (non-empty) also records it in the history. Returns the normalised result. */
export function renderResult(rawResult, sourceLabel = '') {
  const res = normalizeResult(rawResult);
  state.currentResult = res;
  const meta = VERDICT_META[res.verdict];
  const unavailable = res.verdict === 'unable_to_assess';

  renderVerdict(res, meta, unavailable);
  renderRecommendation(res, unavailable);
  renderScores(res, meta);
  renderFlags(res, unavailable);
  renderEvidenceExtras(res, unavailable);
  announce(announcementFor(res, unavailable));

  if (sourceLabel) {
    addHistory({
      id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
      label: sourceLabel,
      timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      result: res,
    });
    renderHistory();
  }
  return res;
}

export function renderHistory() {
  el.historyList.replaceChildren();
  if (state.history.length === 0) {
    el.historyList.append(makeEl('p', { className: 'text-xs text-slate-500 italic py-2 text-center', text: 'No inspections logged in this session.' }));
    return;
  }

  state.history.forEach((entry) => {
    const meta = VERDICT_META[entry.result.verdict] || VERDICT_META.unable_to_assess;
    const button = makeEl('button', {
      className: 'w-full text-left flex items-center justify-between gap-3 px-3 py-2.5 rounded-xl bg-slate-900/70 hover:bg-slate-800/80 border border-slate-800/90 transition-colors',
      attrs: { type: 'button' },
    });

    const detail = `${String(entry.result.mode).toUpperCase()} • ${entry.timestamp}${entry.result.simulated ? ' • SIMULATED' : ''}`;
    const text = makeEl('div', { className: 'min-w-0' });
    text.append(
      makeEl('div', { className: 'text-xs font-semibold text-slate-200 truncate', text: entry.label }),
      makeEl('div', { className: 'text-[10px] font-mono text-slate-400', text: detail })
    );
    const left = makeEl('div', { className: 'min-w-0 flex items-center gap-2.5' });
    left.append(makeEl('span', { className: `w-2 h-2 rounded-full shrink-0 ${meta.dot}` }), text);

    button.append(left, makeEl('span', { className: 'text-xs font-mono font-bold text-slate-200 shrink-0', text: scoreText(entry.result) }));
    button.addEventListener('click', () => {
      renderResult(entry.result);
      showToast(`Loaded inspection: ${entry.label}`, 'info');
    });
    el.historyList.append(button);
  });
}

/** Character counter and the live "trigger" pills for the text box. `hits` are matched demo phrases. */
export function renderTextPreview(value, hits) {
  el.charCounter.textContent = `${value.length.toLocaleString()} / ${MAX_TEXT_CHARS.toLocaleString()}`;
  el.liveKeywordPills.replaceChildren();
  const hasHits = hits.length > 0;
  el.liveKeywordBar.classList.toggle('hidden', !hasHits);
  el.liveKeywordBar.classList.toggle('flex', hasHits);
  hits.slice(0, 6).forEach((hit) => {
    el.liveKeywordPills.append(makeEl('span', { className: 'px-2 py-0.5 rounded-md bg-rose-500/15 border border-rose-500/35 text-rose-300 font-mono', text: hit }));
  });
}

const BACKEND_STATUS = {
  checking: { text: 'Checking…', dot: 'bg-slate-400 animate-pulse' },
  ready: { text: 'Ready — analysis is available', dot: 'bg-emerald-400' },
  not_ready: { text: 'Not ready — analysis is not available', dot: 'bg-amber-400' },
  unavailable: { text: 'Unavailable — the server returned an error', dot: 'bg-amber-400' },
  unreachable: { text: 'Unreachable — this page is not served by the backend', dot: 'bg-rose-400' },
};

export function renderBackendStatus(kind) {
  const status = BACKEND_STATUS[kind] || BACKEND_STATUS.unreachable;
  el.backendStatusText.textContent = status.text;
  el.backendStatusDot.className = `w-2 h-2 rounded-full ${status.dot} shrink-0`;
}
