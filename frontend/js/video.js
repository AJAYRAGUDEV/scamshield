// video.js - the video tab: file selection and local checks, upload, and watching the background job until it finishes.
//
// Contract (Phase 8 backend): POST /api/analyze-video (multipart field "file") answers HTTP 202 with a job id; the job is
// then read with GET /api/video-status/{job_id} until its status is completed, unable_to_assess or failed. Progress shown
// to the user is ONLY what the server reports (stage + progress); nothing is estimated or invented. The server stays the
// authority on validation: the checks here (type, 100 MB, 5 minutes) only save a pointless upload.
//
// Polling is strictly sequential (one request at a time), bounded (a maximum total wait and a maximum number of
// consecutive failures), cancellable (Stop waiting, leaving the page) and stops at the first terminal status.
// The video file is never read into memory and never stored: only a File reference and a short-lived object URL used to
// read the length locally (revoked immediately).

import { el } from './dom.js';
import { state, isLiveMode, beginRequest, isCurrentRequest, MAX_VIDEO_BYTES, MAX_VIDEO_SECONDS, VIDEO_EXTENSIONS } from './state.js';
import { showToast, formatMegabytes, makeEl } from './utils.js';
import { makeUnableResult, VIDEO_TERMINAL_STATUSES } from './result.js';
import { submitVideo, fetchVideoJob, canCallSameOriginApi, ApiError } from './api.js';
import { renderResult, announce } from './render.js';
import { startScanner, setScannerStatus, stopScanAnimation } from './scan.js';

/** Mutable so tests can shorten the waits. The defaults are the real behaviour. */
export const polling = {
  intervalMs: 2000,
  maxWaitMs: 6 * 60 * 1000, // longer than the server's own 330 s job limit
  maxConsecutiveErrors: 4,
  maxBackoffMs: 10000,
  metadataTimeoutMs: 4000,
};

const STAGE_TEXT = {
  queued: 'Waiting in the analysis queue...',
  starting: 'Starting the analysis...',
  uploading: 'Sending the video to the analysis service...',
  processing_media: 'The analysis service is preparing the video...',
  analysing: 'Analysing visuals, on-screen text and speech...',
  scoring: 'Calculating the risk assessment...',
  finished: 'Finalising the report...',
};

let selectionToken = 0; // a newer selection supersedes an older one that is still reading its length
let polling_active = false;

// ---- local checks ------------------------------------------------------------------------------------------------
/** Returns a message for an unacceptable file, or null. The server validates again (content, duration, integrity). */
export function validateVideoFile(file) {
  const lowerName = file.name.toLowerCase();
  if (!VIDEO_EXTENSIONS.some((extension) => lowerName.endsWith(extension))) return 'Unsupported video format. Use MP4, MOV, or WebM.';
  if (file.size === 0) return 'The video file is empty.';
  if (file.size > MAX_VIDEO_BYTES) return `The video is larger than the ${MAX_VIDEO_BYTES / (1024 * 1024)} MB limit.`;
  return null;
}

/** Reads the length with a hidden <video> element and a short-lived object URL. Resolves to seconds, or null if unknown. */
function readVideoDuration(file) {
  return new Promise((resolve) => {
    const probe = makeEl('video');
    let url = null;
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      probe.removeAttribute('src');
      if (url) URL.revokeObjectURL(url);
      resolve(value);
    };
    const timer = setTimeout(() => finish(null), polling.metadataTimeoutMs);
    probe.preload = 'metadata';
    probe.muted = true;
    probe.addEventListener('loadedmetadata', () => finish(Number.isFinite(probe.duration) && probe.duration > 0 ? probe.duration : null), { once: true });
    probe.addEventListener('error', () => finish(null), { once: true });
    try {
      url = URL.createObjectURL(file);
      probe.src = url;
    } catch (error) {
      finish(null);
    }
  });
}

export function formatClock(seconds) {
  const total = Math.max(0, Math.round(seconds));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

// ---- selection UI ------------------------------------------------------------------------------------------------
function showVideoError(message) {
  el.videoError.textContent = message;
  el.videoError.classList.toggle('hidden', !message);
}

export function clearVideo() {
  selectionToken += 1;
  state.selectedVideoFile = null;
  state.selectedVideoSeconds = null;
  el.videoFileInput.value = '';
  el.selectedVideoBadge.classList.add('hidden');
  el.selectedVideoName.textContent = '';
  el.selectedVideoSize.textContent = '';
  el.selectedVideoLength.textContent = '';
  showVideoError('');
}

/** Validates and selects a file. Resolves true if it was accepted. Never uploads anything. */
export async function handleVideoFile(file) {
  if (!file) return false;
  if (state.videoJob) {
    showToast('A video analysis is in progress. Wait for it to finish before choosing another video.', 'warn');
    return false;
  }
  const token = ++selectionToken;
  showVideoError('');
  const problem = validateVideoFile(file);
  if (problem) {
    showVideoError(problem);
    showToast(problem, 'error');
    return false;
  }
  const seconds = await readVideoDuration(file);
  if (token !== selectionToken) return false; // the user chose something else meanwhile
  if (seconds !== null && seconds > MAX_VIDEO_SECONDS) {
    const message = `The video is ${formatClock(seconds)} long; the limit is ${formatClock(MAX_VIDEO_SECONDS)}.`;
    showVideoError(message);
    showToast(message, 'error');
    return false;
  }
  state.selectedVideoFile = file;
  state.selectedVideoSeconds = seconds;
  el.selectedVideoName.textContent = file.name;
  el.selectedVideoSize.textContent = `(${formatMegabytes(file.size)} MB)`;
  el.selectedVideoLength.textContent = seconds === null ? '- length will be checked by the server' : `- ${formatClock(seconds)}`;
  el.selectedVideoBadge.classList.remove('hidden');
  showToast(`Selected video: ${file.name}`, 'success');
  return true;
}

/** Shows or hides the note explaining that video analysis needs the live backend (there is no simulated video analysis). */
export function refreshVideoModeNote() {
  el.videoModeNote.classList.toggle('hidden', isLiveMode());
}

// ---- watching a job ----------------------------------------------------------------------------------------------
function cancelled() {
  return new ApiError('The request was cancelled', { code: 'CANCELLED' });
}

function pause(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(cancelled());
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(cancelled());
    };
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

function stageText(stage) {
  return STAGE_TEXT[stage] || 'Working on the analysis...';
}

/**
 * Polls until the job reaches a terminal status. Resolves with that job. Rejects with an ApiError for: cancellation,
 * an unknown/expired job (404), an invalid response, too many consecutive failures, or the overall wait running out.
 * Requests never overlap: each starts only after the previous one finished and the pause elapsed.
 */
export async function waitForJob(jobId, { signal, onUpdate = () => {} }) {
  if (polling_active) throw new ApiError('Already waiting for a job', { code: 'ALREADY_POLLING' });
  polling_active = true;
  const started = Date.now();
  let failures = 0;
  try {
    for (;;) {
      if (signal.aborted) throw cancelled();
      let delay = polling.intervalMs;
      try {
        const job = await fetchVideoJob(jobId, { signal });
        failures = 0;
        onUpdate(job);
        if (VIDEO_TERMINAL_STATUSES.includes(job.status)) return job;
      } catch (error) {
        if (!(error instanceof ApiError)) throw error;
        if (error.code === 'CANCELLED' || error.code === 'INVALID_RESPONSE' || error.status === 404) throw error;
        failures += 1; // network trouble, a timeout, 429 or a 5xx: retry a few times with growing pauses
        if (failures > polling.maxConsecutiveErrors) {
          throw new ApiError(`Lost contact with the analysis service (${error.message})`, { code: 'POLLING_FAILED', status: error.status });
        }
        const retryAfterMs = Number(error.retryAfter) > 0 ? Number(error.retryAfter) * 1000 : 0;
        delay = Math.min(polling.maxBackoffMs, Math.max(retryAfterMs, polling.intervalMs * 2 ** failures));
      }
      if (Date.now() - started + delay > polling.maxWaitMs) {
        throw new ApiError('The analysis is taking longer than expected', { code: 'POLL_TIMEOUT' });
      }
      await pause(delay, signal);
    }
  } finally {
    polling_active = false;
  }
}

/** Stops watching the current job (Stop waiting, leaving the page). The server may still finish it. */
export function cancelVideoWatch() {
  if (state.videoJob) state.videoJob.controller.abort();
}

function jobToResult(job) {
  if (job.status === 'failed') {
    return makeUnableResult('video', job.error.code, `The video analysis job failed (${job.error.message || 'no details were given'}).`);
  }
  return job.result;
}

function watchFailure(error) {
  if (error instanceof ApiError && error.code === 'CANCELLED') {
    showToast('Stopped waiting. The server may still finish the analysis, but this page will not show its result.', 'info');
    return null;
  }
  const message = error instanceof ApiError ? error.message : 'Unexpected error';
  const code = error instanceof ApiError ? error.code : 'LIVE_API_ERROR';
  if (error instanceof ApiError && error.status === 404) {
    showToast('The analysis job was not found. No assessment was made.', 'error');
    return makeUnableResult('video', code, 'The analysis job could not be found. It may have expired or the server may have restarted, so no assessment was made. Please submit the video again.');
  }
  showToast(`Video analysis did not finish (${message}). No assessment was made.`, 'error');
  return makeUnableResult('video', code, `The video analysis did not finish (${message}).`);
}

function reportSubmitError(error) {
  if (error instanceof ApiError && error.code === 'CANCELLED') {
    showToast('Upload cancelled.', 'info');
    return;
  }
  const detail = error instanceof ApiError ? error.message : 'Unexpected error';
  const message = `The video was not analysed: ${detail}`;
  showVideoError(message);
  showToast(message, 'error');
}

/** Uploads the selected video and follows its job to the end. Renders the result and adds it to the history. */
export async function triggerVideoScan() {
  if (state.isScanning || state.videoJob) return; // duplicate submissions are impossible while a job is active
  const file = state.selectedVideoFile;
  if (!file) {
    showToast('Choose a video file first.', 'warn');
    return;
  }
  if (!isLiveMode()) {
    refreshVideoModeNote();
    showToast('Video analysis needs FastAPI Live mode: there is no simulated video analysis.', 'warn');
    return;
  }
  if (!canCallSameOriginApi()) {
    showToast('Live mode needs this page to be served by the ScamShield backend. No analysis was run.', 'warn');
    return;
  }

  showVideoError('');
  const requestId = beginRequest();
  const controller = new AbortController();
  state.videoJob = { controller, jobId: null };
  el.btnAnalyzeVideo.disabled = true;
  el.btnStopVideoWait.classList.remove('hidden');
  startScanner();
  setScannerStatus('Uploading the video to the ScamShield server...', null);

  let result = null;
  try {
    let accepted;
    try {
      accepted = await submitVideo(file, { signal: controller.signal });
    } catch (error) {
      reportSubmitError(error); // a rejected or unreachable upload is not an analysis: no result, no history entry
      return;
    }
    state.videoJob.jobId = accepted.jobId;
    let lastStage = null;
    setScannerStatus(stageText(accepted.stage), accepted.progress);
    try {
      const job = await waitForJob(accepted.jobId, {
        signal: controller.signal,
        onUpdate: (update) => {
          setScannerStatus(stageText(update.stage), update.progress);
          if (update.stage && update.stage !== lastStage) {
            lastStage = update.stage;
            announce(stageText(update.stage));
          }
        },
      });
      result = jobToResult(job);
    } catch (error) {
      result = watchFailure(error);
    }
  } finally {
    stopScanAnimation();
    el.btnStopVideoWait.classList.add('hidden');
    el.btnAnalyzeVideo.disabled = false;
    state.videoJob = null;
  }
  if (result && isCurrentRequest(requestId)) renderResult(result, file.name);
}

// ---- events (called once from main.js) -----------------------------------------------------------------------------
export function bindVideoEvents() {
  const dropzone = el.videoDropzone;
  const openPicker = () => {
    if (!state.videoJob) el.videoFileInput.click();
  };

  dropzone.addEventListener('click', (event) => {
    if (event.target.closest('button')) return; // the Replace / Remove buttons inside the zone do their own thing
    openPicker();
  });
  dropzone.addEventListener('keydown', (event) => {
    if (event.target !== dropzone) return;
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      openPicker();
    }
  });
  el.videoFileInput.addEventListener('change', (event) => {
    const file = event.target.files && event.target.files[0];
    event.target.value = ''; // so choosing the same file again still fires 'change'; the File reference stays valid
    if (file) handleVideoFile(file);
  });
  dropzone.addEventListener('dragover', (event) => {
    event.preventDefault();
    dropzone.classList.add('dropzone-active');
  });
  dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dropzone-active'));
  dropzone.addEventListener('drop', (event) => {
    event.preventDefault();
    dropzone.classList.remove('dropzone-active');
    const file = event.dataTransfer.files && event.dataTransfer.files[0];
    if (file) handleVideoFile(file);
  });

  el.btnReplaceVideo.addEventListener('click', openPicker);
  el.btnRemoveVideo.addEventListener('click', () => {
    if (state.videoJob) return;
    clearVideo();
    showToast('Video removed.', 'info');
  });
  el.btnAnalyzeVideo.addEventListener('click', () => triggerVideoScan());
  el.btnStopVideoWait.addEventListener('click', cancelVideoWatch);
  refreshVideoModeNote();
}
