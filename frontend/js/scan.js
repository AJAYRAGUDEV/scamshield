// scan.js - runs one analysis: the scanner overlay, the request (Live) or the demo (Smart Sim), and the result.
//
// Live mode talks to the backend and NEVER falls back to demo data: every failure becomes an explicit
// "Unable to Assess" result with the server's own message. Demo mode never calls the backend. Each scan gets a
// request id; a result that arrives after a newer scan started is ignored, and the loading state is always
// cleared in a `finally` block, so it cannot stay on screen.

import { el } from './dom.js';
import { state, isLiveMode, beginRequest, isCurrentRequest } from './state.js';
import { showToast, sleep } from './utils.js';
import { makeUnableResult } from './result.js';
import { analyzeText, analyzeAudio, canCallSameOriginApi, ApiError } from './api.js';
import { runLocalTextEngine, simulateAudio } from './sim.js';
import { renderResult, announce } from './render.js';

const STAGES = {
  audio: [
    { pct: 24, text: 'Extracting acoustic waveform features...' },
    { pct: 55, text: 'Scanning for coercion & impersonation cues...' },
    { pct: 84, text: 'Evaluating prosody & script uniformity...' },
    { pct: 100, text: 'Compiling threat intelligence report...' },
  ],
  text: [
    { pct: 22, text: 'Tokenizing message & entity claims...' },
    { pct: 52, text: 'Scoring authority & urgency pressure...' },
    { pct: 82, text: 'Inspecting OTP, UPI & credential traps...' },
    { pct: 100, text: 'Finalizing calibrated risk verdict...' },
  ],
};

let activeController = null;

/** Shows the scanner overlay and marks an analysis as running (this is what prevents duplicate submissions). */
export function startScanner() {
  state.isScanning = true;
  el.shieldCore.classList.add('scanning');
  el.scannerOverlay.classList.remove('hidden');
  el.inputCard.setAttribute('aria-busy', 'true');
  announce('Analysis in progress.');
}

/**
 * Updates the overlay text and bar. `percent` is a number the BACKEND reported, or null when no value exists yet
 * (then the bar pulses as an indeterminate indicator instead of showing an invented percentage).
 */
export function setScannerStatus(text, percent) {
  el.scanStageText.textContent = text;
  const known = typeof percent === 'number' && Number.isFinite(percent);
  el.scanProgressBar.style.width = known ? `${Math.max(0, Math.min(100, percent))}%` : '35%';
  el.scanProgressBar.classList.toggle('animate-pulse', !known);
}

async function runScanAnimation(mode) {
  startScanner();
  for (const step of STAGES[mode]) {
    el.scanStageText.textContent = step.text;
    el.scanProgressBar.style.width = `${step.pct}%`;
    await sleep(280);
  }
}

export function stopScanAnimation() {
  state.isScanning = false;
  el.scanProgressBar.classList.remove('animate-pulse');
  el.shieldCore.classList.remove('scanning');
  el.scannerOverlay.classList.add('hidden');
  el.scanProgressBar.style.width = '12%';
  el.inputCard.setAttribute('aria-busy', 'false');
}

/** Cancels the request in flight (if any). Used when the page is left. */
export function cancelActiveScan() {
  if (activeController) activeController.abort();
}

function liveFailure(mode, error) {
  if (error instanceof ApiError && error.code === 'CANCELLED') return null;
  const message = error instanceof ApiError ? error.message : 'Unexpected error';
  const code = error instanceof ApiError ? error.code : 'LIVE_API_ERROR';
  showToast(`Live analysis failed (${message}). No assessment was made.`, 'error');
  return makeUnableResult(mode, code, `The request to the analysis backend failed (${message}).`);
}

async function liveText(text, signal) {
  if (!canCallSameOriginApi()) {
    showToast('Live mode needs this page to be served by the ScamShield backend. No analysis was run.', 'warn');
    return makeUnableResult('text', 'LIVE_API_UNREACHABLE', 'Live mode is selected, but this page is not served by the ScamShield backend, so nothing was analysed.');
  }
  try {
    return await analyzeText(text, { signal });
  } catch (error) {
    return liveFailure('text', error);
  }
}

async function liveAudio(file, signal) {
  if (!canCallSameOriginApi()) {
    showToast('Live mode needs this page to be served by the ScamShield backend. No analysis was run.', 'warn');
    return makeUnableResult('audio', 'LIVE_API_UNREACHABLE', 'Live mode is selected, but this page is not served by the ScamShield backend, so nothing was analysed.');
  }
  try {
    return await analyzeAudio(file, { signal });
  } catch (error) {
    return liveFailure('audio', error);
  }
}

async function runAnalysis(mode, label, produce) {
  const requestId = beginRequest();
  activeController = new AbortController();
  const { signal } = activeController;
  try {
    await runScanAnimation(mode);
    const result = await produce(signal);
    if (result && isCurrentRequest(requestId)) renderResult(result, label);
  } finally {
    stopScanAnimation();
    activeController = null;
  }
}

/** Analyses the text in the message box. Live: backend. Demo: the labelled local simulation. */
export function triggerTextScan(label) {
  const text = el.messageInput.value.trim();
  return runAnalysis('text', label, (signal) => (isLiveMode() ? liveText(text, signal) : runLocalTextEngine(text)));
}

/** Analyses the selected audio file (Live) or the selected sample / demo preview (Smart Sim and samples). */
export function triggerAudioScan(label) {
  return runAnalysis('audio', label, (signal) => {
    // Live mode analyses a real uploaded file. A demo sample is simulated data and is labelled as such.
    if (isLiveMode() && state.selectedAudioFile) return liveAudio(state.selectedAudioFile, signal);
    return simulateAudio();
  });
}
