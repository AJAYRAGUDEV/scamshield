// api.js - every request to the ScamShield backend, and nothing else.
//
// Same-origin relative URLs only. No credentials of any kind are sent, stored or read: the Gemini API key lives
// on the server. Each request has a timeout and can be cancelled; every failure becomes an ApiError with a stable
// `code`, and every success is validated (result.js) before the rest of the app sees it.

import { validateLiveResult, validateVideoSubmission, validateVideoJob, isValidJobId, ResultShapeError } from './result.js';

export const ENDPOINTS = {
  text: '/api/v1/analyze/text',
  audio: '/api/v1/analyze/audio',
  status: '/api/v1/status',
  video: '/api/analyze-video',
  videoStatus: '/api/video-status/', // + the job id
};

export const REQUEST_TIMEOUT_MS = 90000; // the server's own Gemini deadline is 60 s
const STATUS_TIMEOUT_MS = 5000;
export const VIDEO_UPLOAD_TIMEOUT_MS = 5 * 60 * 1000; // up to 100 MB on a slow connection
const JOB_STATUS_TIMEOUT_MS = 10000;

export class ApiError extends Error {
  constructor(message, { code = 'LIVE_API_ERROR', status = null, retryAfter = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

/** True when the page can reach a backend: it was loaded over http(s), i.e. it is served by FastAPI. */
export function canCallSameOriginApi() {
  const protocol = window.location.protocol;
  return (protocol === 'https:' || protocol === 'http:') && window.location.host !== '';
}

function describeHttpError(data, response) {
  if (data && typeof data.detail === 'string') {
    const retry = response.headers.get('Retry-After');
    const wait = retry && (response.status === 429 || response.status === 503) ? ` (retry in about ${retry}s)` : '';
    return `${data.detail}${wait}`;
  }
  return `Server error (${response.status})`;
}

// Combines the caller's cancel signal with a timeout. `timedOut()` tells the two apart afterwards.
function withTimeout(signal, ms) {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, ms);
  const onAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener('abort', onAbort, { once: true });
  }
  return {
    signal: controller.signal,
    timedOut: () => timedOut,
    done: () => {
      clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', onAbort);
    },
  };
}

async function request(url, options, { signal, timeoutMs }) {
  const guard = withTimeout(signal, timeoutMs);
  try {
    let response;
    try {
      response = await fetch(url, { ...options, signal: guard.signal, cache: 'no-store', credentials: 'omit' });
    } catch (error) {
      if (guard.timedOut()) throw new ApiError('The request took too long', { code: 'REQUEST_TIMEOUT' });
      if (error && error.name === 'AbortError') throw new ApiError('The request was cancelled', { code: 'CANCELLED' });
      throw new ApiError('Could not reach the analysis backend', { code: 'NETWORK_ERROR' });
    }

    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null; // not JSON (e.g. an HTML error page from a proxy)
    }

    if (!response.ok) {
      throw new ApiError(describeHttpError(data, response), {
        code: (data && typeof data.code === 'string' && data.code) || `HTTP_${response.status}`,
        status: response.status,
        retryAfter: response.headers.get('Retry-After'),
      });
    }
    if (data === null) throw new ApiError('The server returned an unreadable response', { code: 'INVALID_RESPONSE', status: response.status });
    return data;
  } finally {
    guard.done();
  }
}

async function analyze(mode, url, formData, signal) {
  const data = await request(url, { method: 'POST', body: formData }, { signal, timeoutMs: REQUEST_TIMEOUT_MS });
  try {
    return validateLiveResult(data, mode);
  } catch (error) {
    if (error instanceof ResultShapeError) throw new ApiError(`The server returned an unexpected response (${error.message})`, { code: 'INVALID_RESPONSE' });
    throw error;
  }
}

/** Analyses text. Resolves with a validated result or rejects with an ApiError. */
export function analyzeText(text, { signal } = {}) {
  const form = new FormData();
  form.append('text', text);
  return analyze('text', ENDPOINTS.text, form, signal);
}

/** Uploads an audio File for analysis. Resolves with a validated result or rejects with an ApiError. */
export function analyzeAudio(file, { signal } = {}) {
  const form = new FormData();
  form.append('file', file);
  return analyze('audio', ENDPOINTS.audio, form, signal);
}

/** 'ready' | 'not_ready' | 'unavailable' (server answered with an error) | 'unreachable'. Never throws. */
export async function fetchBackendStatus({ signal } = {}) {
  try {
    const data = await request(ENDPOINTS.status, { method: 'GET' }, { signal, timeoutMs: STATUS_TIMEOUT_MS });
    return data && data.status === 'ready' ? 'ready' : 'not_ready';
  } catch (error) {
    return error instanceof ApiError && error.status ? 'unavailable' : 'unreachable';
  }
}

function invalid(error) {
  if (error instanceof ResultShapeError) return new ApiError(`The server returned an unexpected response (${error.message})`, { code: 'INVALID_RESPONSE' });
  return error;
}

/**
 * Uploads a video File. Resolves with the new job ({ jobId, status, stage, progress }) when the server answers
 * HTTP 202, or rejects with an ApiError (the server's own `detail` and `code` for validation errors).
 */
export async function submitVideo(file, { signal } = {}) {
  const form = new FormData();
  form.append('file', file);
  const data = await request(ENDPOINTS.video, { method: 'POST', body: form }, { signal, timeoutMs: VIDEO_UPLOAD_TIMEOUT_MS });
  try {
    return validateVideoSubmission(data);
  } catch (error) {
    throw invalid(error);
  }
}

/** Reads one job's status. Resolves with a validated job or rejects with an ApiError (404 = unknown or expired). */
export async function fetchVideoJob(jobId, { signal } = {}) {
  if (!isValidJobId(jobId)) throw new ApiError('Invalid job id', { code: 'INVALID_RESPONSE' });
  const data = await request(ENDPOINTS.videoStatus + encodeURIComponent(jobId), { method: 'GET' }, { signal, timeoutMs: JOB_STATUS_TIMEOUT_MS });
  try {
    const job = validateVideoJob(data);
    if (job.jobId !== jobId) throw new ResultShapeError('the response belongs to a different job');
    return job;
  } catch (error) {
    throw invalid(error);
  }
}
