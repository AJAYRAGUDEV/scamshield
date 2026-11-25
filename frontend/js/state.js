// state.js - the application's mutable state and the constants shared by several modules.
//
// One plain object, mutated only through the helpers below or by the module that owns the field (noted
// beside each field). No state-management library.

/** Same limits the backend enforces (the backend remains the authority). */
export const MAX_TEXT_CHARS = 20000;
export const MAX_AUDIO_BYTES = 20 * 1024 * 1024;
export const ALLOWED_EXTENSIONS = ['.mp3', '.wav', '.m4a', '.ogg', '.webm'];
export const VIDEO_EXTENSIONS = ['.mp4', '.mov', '.webm'];
export const MAX_VIDEO_BYTES = 100 * 1024 * 1024; // the backend's default; the backend stays the authority (it is configurable)
export const MAX_VIDEO_SECONDS = 300; // 5 minutes, same note
export const MAX_HISTORY = 10;

export const state = {
  inputTab: 'text', // 'text' | 'audio' | 'video'              (main.js)
  engineMode: 'sim', // 'sim' = Demo | 'api' = Live backend   (main.js)
  isScanning: false, // an analysis is running                (scan.js)
  selectedAudioFile: null, // File chosen by the user          (audio.js)
  selectedAudioSampleKey: null, // demo sample id              (audio.js)
  selectedVideoFile: null, // File chosen by the user               (video.js)
  selectedVideoSeconds: null, // duration read locally, or null    (video.js)
  videoJob: null, // { controller, jobId } while a job is watched   (video.js)
  waveActive: false, // spectrum animation boost               (audio.js)
  currentResult: null, // normalised result on screen          (render.js)
  history: [], // newest first, at most MAX_HISTORY            (this file)
  bgThemeRgb: [6, 182, 212], // current background colour      (background.js)
  targetThemeRgb: [6, 182, 212], // colour it fades towards    (render.js)
  requestSeq: 0, // id of the most recent analysis request     (scan.js)
};

export function isLiveMode() {
  return state.engineMode === 'api';
}

/** Starts a new analysis request and returns its id; older requests become stale. */
export function beginRequest() {
  state.requestSeq += 1;
  return state.requestSeq;
}

export function isCurrentRequest(requestId) {
  return requestId === state.requestSeq;
}

export function addHistory(entry) {
  state.history.unshift(entry);
  if (state.history.length > MAX_HISTORY) state.history.length = MAX_HISTORY;
}

export function clearHistory() {
  state.history = [];
}
