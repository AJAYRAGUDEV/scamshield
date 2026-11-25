// result.js - the analysis RESULT model: canonical verdicts, display metadata, normalisation, validation.
//
// The backend emits only: low_risk | suspicious | high_risk | unable_to_assess. This module is the only place
// that maps them to labels and colours, and the only place that decides what a usable response looks like.
// Principles: a missing score is null, never 0; an unknown verdict is NEVER shown as safe; nothing (confidence,
// evidence, scores) is invented when the response does not contain it.

export const VERDICT_META = {
  low_risk: { label: 'Low Risk', theme: 'low', rgb: [16, 185, 129], dot: 'bg-emerald-400',
    badge: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40', chartColor: '#10b981', chartFill: 'rgba(16, 185, 129, 0.22)' },
  suspicious: { label: 'Suspicious', theme: 'medium', rgb: [245, 158, 11], dot: 'bg-amber-400',
    badge: 'bg-amber-500/20 text-amber-300 border-amber-500/40', chartColor: '#f59e0b', chartFill: 'rgba(245, 158, 11, 0.24)' },
  high_risk: { label: 'High Risk', theme: 'high', rgb: [244, 63, 94], dot: 'bg-rose-400',
    badge: 'bg-rose-500/20 text-rose-300 border-rose-500/40', chartColor: '#f43f5e', chartFill: 'rgba(244, 63, 94, 0.24)' },
  unable_to_assess: { label: 'Unable to Assess', theme: 'unavailable', rgb: [148, 163, 184], dot: 'bg-slate-400',
    badge: 'bg-slate-500/20 text-slate-300 border-slate-500/40', chartColor: '#94a3b8', chartFill: 'rgba(148, 163, 184, 0.18)' },
};

// Labels used by the Smart Sim demo engine, mapped to the canonical values.
export const LEGACY_VERDICTS = { 'Low Risk': 'low_risk', 'Suspicious': 'suspicious', 'High Risk': 'high_risk' };

export const SIGNAL_SCORE_KEYS = ['authority_impersonation', 'urgency_isolation', 'credential_payment_trap', 'synthetic_or_script_signals'];

const FIELD_LIMIT = 4000;
const MAX_FLAGS = 20;
const MAX_SIGNALS = 40;
const CONFIDENCE_LEVELS = ['Low', 'Medium', 'High'];

export class ResultShapeError extends Error {}

// Headings for results that carry NO assessment. They tell the user WHICH situation they are in; all of them are
// "Unable to Assess" (no score, never Low Risk). Keys are the backend's ReasonCode values, the failed-job codes and the
// codes this frontend produces when a result could not be retrieved.
const NOT_ENOUGH_EVIDENCE = 'Not enough evidence to assess';
const ANALYSIS_FAILED = 'Analysis could not be completed';
const NOT_RETRIEVED = 'Result could not be retrieved';
const UNABLE_HEADINGS = {
  INSUFFICIENT_EVIDENCE: NOT_ENOUGH_EVIDENCE,
  CONFIGURATION_ERROR: ANALYSIS_FAILED, PROVIDER_AUTH_ERROR: ANALYSIS_FAILED, PROVIDER_RATE_LIMITED: ANALYSIS_FAILED,
  PROVIDER_TIMEOUT: ANALYSIS_FAILED, PROVIDER_UNAVAILABLE: ANALYSIS_FAILED, PROVIDER_ERROR: ANALYSIS_FAILED,
  INVALID_PROVIDER_RESPONSE: ANALYSIS_FAILED, MEDIA_PROCESSING_FAILED: ANALYSIS_FAILED, INTERNAL_ANALYSIS_ERROR: ANALYSIS_FAILED,
  SERVER_SHUTDOWN: ANALYSIS_FAILED, SERVER_RESTARTED: ANALYSIS_FAILED, JOB_CANCELLED: ANALYSIS_FAILED, INTERNAL_ERROR: ANALYSIS_FAILED,
  JOB_FAILED: ANALYSIS_FAILED,
  JOB_NOT_FOUND: NOT_RETRIEVED, POLLING_FAILED: NOT_RETRIEVED, POLL_TIMEOUT: NOT_RETRIEVED, NETWORK_ERROR: NOT_RETRIEVED,
  REQUEST_TIMEOUT: NOT_RETRIEVED, INVALID_RESPONSE: NOT_RETRIEVED, LIVE_API_ERROR: NOT_RETRIEVED, LIVE_API_UNREACHABLE: NOT_RETRIEVED,
};

/** The heading for a result without an assessment, chosen from its reason code. */
export function unableHeading(reasonCode) {
  if (typeof reasonCode === 'string' && Object.prototype.hasOwnProperty.call(UNABLE_HEADINGS, reasonCode)) return UNABLE_HEADINGS[reasonCode];
  if (typeof reasonCode === 'string' && reasonCode.startsWith('HTTP_')) return NOT_RETRIEVED;
  return 'Unable to assess';
}

/** A result for an analysis that did not happen. Never carries a score. */
export function makeUnableResult(mode, reasonCode, explanation) {
  return {
    status: 'unable_to_assess',
    verdict: 'unable_to_assess',
    risk_score: null,
    confidence: null,
    confidence_level: null,
    reason_code: reasonCode,
    coverage: null,
    summary: 'The content could not be assessed.',
    scam_type: unableHeading(reasonCode),
    explanation: `${explanation} No risk score was produced; this is not a low-risk result.`,
    recommended_action: 'No assessment was made, so do not treat this content as safe. Verify any urgent request through an official channel you trust, and try the analysis again later.',
    signals: [],
    red_flags: [],
    language: null,
    language_uncertain: false,
    limitations: [],
    urls: [],
    signal_scores: null,
    mode,
    offline_fallback_used: false,
    simulated: false,
  };
}

/** Maps legacy labels to canonical values and guarantees a safe shape. Idempotent. */
export function normalizeResult(res) {
  const out = Object.assign({}, res);
  if (LEGACY_VERDICTS[out.verdict]) out.verdict = LEGACY_VERDICTS[out.verdict];
  if (!VERDICT_META[out.verdict]) {
    out.verdict = 'unable_to_assess';
    out.reason_code = out.reason_code || 'UNKNOWN_VERDICT';
  }
  if (out.verdict === 'unable_to_assess') {
    out.risk_score = null;
    out.signal_scores = null;
    out.confidence = null;
    out.confidence_level = null;
  }
  if (!out.status) out.status = out.verdict === 'unable_to_assess' ? 'unable_to_assess' : 'completed';
  return out;
}

export function verdictLabel(res) {
  return VERDICT_META[res.verdict] ? VERDICT_META[res.verdict].label : 'Unable to Assess';
}

export function scoreText(res) {
  return typeof res.risk_score === 'number' ? `${res.risk_score}/100` : 'N/A';
}

// ---- validation of a LIVE backend response ---------------------------------------------------------------
const asString = (value) => (typeof value === 'string' ? value.slice(0, FIELD_LIMIT) : '');
const inRange = (value, low, high) => (typeof value === 'number' && Number.isFinite(value) && value >= low && value <= high ? value : null);

function cleanSignals(raw) {
  if (!Array.isArray(raw)) return [];
  return raw.slice(0, MAX_SIGNALS).filter((item) => item && typeof item === 'object').map((item) => ({
    category: asString(item.category),
    name: asString(item.name),
    description: asString(item.description),
    source: asString(item.source),
    why_it_matters: asString(item.why_it_matters),
    context: asString(item.context),
    quote: typeof item.quote === 'string' ? item.quote.slice(0, FIELD_LIMIT) : null,
    timestamp_start: inRange(item.timestamp_start, 0, 86400),
    timestamp_end: inRange(item.timestamp_end, 0, 86400),
    severity: asString(item.severity),
    reliability: asString(item.reliability),
    impact: inRange(item.impact, 0, 1),
    corroborating_signals: Number.isInteger(item.corroborating_signals) ? item.corroborating_signals : 0,
  }));
}

const MAX_LIMITATIONS = 8;
const MAX_URLS = 10;
const stringList = (raw, limit, max) => (Array.isArray(raw)
  ? raw.filter((s) => typeof s === 'string' && s.trim()).slice(0, max).map((s) => s.slice(0, limit))
  : []);

function cleanUrls(raw) {
  if (!Array.isArray(raw)) return [];
  return raw.slice(0, MAX_URLS).filter((item) => item && typeof item === 'object').map((item) => ({
    display: asString(item.display).slice(0, 300),
    domain: asString(item.domain).slice(0, 253),
    indicators: stringList(item.indicators, 80, 12),
    note: asString(item.note).slice(0, 500),
    reputation_verified: false, // the backend never verifies reputation; the UI never claims it
  }));
}

/**
 * Checks a parsed backend response and returns a CLEAN copy containing only known fields with safe types.
 * Throws ResultShapeError when the response cannot be trusted (the caller turns that into an unable_to_assess
 * result; it is never displayed as an assessment).
 */
export function validateLiveResult(data, fallbackMode) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new ResultShapeError('the response is not an object');

  const verdict = LEGACY_VERDICTS[data.verdict] || data.verdict;
  if (!VERDICT_META[verdict]) throw new ResultShapeError('the response has an unknown verdict');
  const unable = verdict === 'unable_to_assess';
  const status = unable ? 'unable_to_assess' : 'completed';
  if (data.status !== undefined && data.status !== status) throw new ResultShapeError('status and verdict disagree');

  const score = inRange(data.risk_score, 0, 100);
  let signalScores = null;
  if (!unable) {
    if (score === null) throw new ResultShapeError('a completed analysis has no valid risk_score');
    const raw = data.signal_scores;
    if (!raw || typeof raw !== 'object' || SIGNAL_SCORE_KEYS.some((key) => inRange(raw[key], 0, 100) === null)) {
      throw new ResultShapeError('a completed analysis has no valid signal_scores');
    }
    signalScores = Object.fromEntries(SIGNAL_SCORE_KEYS.map((key) => [key, raw[key]]));
  }

  return {
    status,
    verdict,
    risk_score: unable ? null : score,
    confidence: unable ? null : inRange(data.confidence, 0, 1),
    confidence_level: unable || !CONFIDENCE_LEVELS.includes(data.confidence_level) ? null : data.confidence_level,
    reason_code: unable ? (asString(data.reason_code).slice(0, 64) || 'UNKNOWN') : null,
    coverage: data.coverage === 'full' || data.coverage === 'partial' ? data.coverage : null,
    summary: asString(data.summary),
    scam_type: asString(data.scam_type) || (unable ? 'Unable to assess' : ''),
    explanation: asString(data.explanation),
    recommended_action: asString(data.recommended_action),
    signals: unable ? [] : cleanSignals(data.signals),
    red_flags: unable || !Array.isArray(data.red_flags) ? [] : data.red_flags.filter((flag) => typeof flag === 'string').slice(0, MAX_FLAGS).map((flag) => flag.slice(0, FIELD_LIMIT)),
    language: typeof data.language === 'string' && data.language.trim() ? data.language.slice(0, 40) : null,
    language_uncertain: data.language_uncertain === true,
    limitations: unable ? [] : stringList(data.limitations, 200, MAX_LIMITATIONS),
    urls: unable ? [] : cleanUrls(data.urls),
    signal_scores: signalScores,
    mode: data.mode === 'audio' || data.mode === 'text' || data.mode === 'video' ? data.mode : fallbackMode,
    offline_fallback_used: false,
    simulated: false,
  };
}

// ---- validation of the video JOB responses (Phase 8 contract) --------------------------------------------------
export const VIDEO_JOB_STATUSES = ['queued', 'processing', 'completed', 'unable_to_assess', 'failed'];
export const VIDEO_TERMINAL_STATUSES = ['completed', 'unable_to_assess', 'failed'];
const JOB_ID = /^[A-Za-z0-9_-]{16,64}$/;
const STAGE = /^[a-z_]{1,40}$/;

const jobProgress = (value) => (Number.isInteger(value) && value >= 0 && value <= 100 ? value : null); // never invented
const jobStage = (value) => (typeof value === 'string' && STAGE.test(value) ? value : null);

export function isValidJobId(value) {
  return typeof value === 'string' && JOB_ID.test(value);
}

/** The body of the HTTP 202 response. Throws ResultShapeError when it is not a usable job. */
export function validateVideoSubmission(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new ResultShapeError('the response is not an object');
  if (!isValidJobId(data.job_id)) throw new ResultShapeError('the response has no valid job id');
  if (data.status !== 'queued' && data.status !== 'processing') throw new ResultShapeError('the new job has an unexpected status');
  return { jobId: data.job_id, status: data.status, stage: jobStage(data.stage), progress: jobProgress(data.progress) };
}

/**
 * A job status response. `result` is a fully validated result for completed / unable_to_assess jobs (the same
 * validation as text and audio, with mode 'video'); `error` is {code, message} for failed jobs. Anything inconsistent
 * is rejected instead of being displayed.
 */
export function validateVideoJob(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new ResultShapeError('the response is not an object');
  if (!isValidJobId(data.job_id)) throw new ResultShapeError('the response has no valid job id');
  const status = data.status;
  if (!VIDEO_JOB_STATUSES.includes(status)) throw new ResultShapeError('the job has an unknown status');

  const job = { jobId: data.job_id, status, stage: jobStage(data.stage), progress: jobProgress(data.progress), result: null, error: null };
  if (status === 'completed' || status === 'unable_to_assess') {
    job.result = validateLiveResult(data.result, 'video');
    const unable = job.result.verdict === 'unable_to_assess';
    if (unable !== (status === 'unable_to_assess')) throw new ResultShapeError('the job status and its result disagree');
  } else if (status === 'failed') {
    const err = data.error;
    if (!err || typeof err !== 'object' || data.result) throw new ResultShapeError('a failed job has no valid error');
    job.error = { code: asString(err.code).slice(0, 64) || 'JOB_FAILED', message: asString(err.message).slice(0, 300) };
  } else if (data.result) {
    throw new ResultShapeError('an unfinished job must not carry a result');
  }
  return job;
}
