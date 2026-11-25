// audio.js - the audio tab: file selection and validation, preview playback, sample clips, spectrum visualisation.
//
// The spectrum monitor is DECORATIVE (an animation, not an analysis of the audio). Selecting a file only keeps a
// reference to the File object: it is not copied or read into memory here, and its preview object URL is revoked
// as soon as it is replaced or the page is left. No new audio-analysis capability lives here.

import { el } from './dom.js';
import { state, MAX_AUDIO_BYTES, ALLOWED_EXTENSIONS } from './state.js';
import { showToast, formatMegabytes } from './utils.js';
import { AUDIO_SAMPLES } from './sim.js';

let previewUrl = null;
let waveStarted = false;

function releasePreviewUrl() {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
  el.audioPlayer.removeAttribute('src');
}

/** Returns an error message for an unacceptable file, or null. The backend validates again (content included). */
export function validateAudioFile(file) {
  const lowerName = file.name.toLowerCase();
  if (!ALLOWED_EXTENSIONS.some((extension) => lowerName.endsWith(extension))) {
    return 'Unsupported audio format. Use MP3, WAV, M4A, OGG, or WEBM.';
  }
  if (file.size > MAX_AUDIO_BYTES) return 'Audio file exceeds the 20 MB limit.';
  if (file.size === 0) return 'Audio file is empty.';
  return null;
}

export function handleAudioFile(file) {
  if (!file) return false;
  const problem = validateAudioFile(file);
  if (problem) {
    showToast(problem, 'error');
    return false;
  }
  state.selectedAudioFile = file;
  state.selectedAudioSampleKey = null;
  el.selectedAudioName.textContent = file.name;
  el.selectedAudioSize.textContent = `(${formatMegabytes(file.size)} MB)`;
  el.selectedAudioBadge.classList.remove('hidden');

  releasePreviewUrl();
  try {
    previewUrl = URL.createObjectURL(file);
    el.audioPlayer.src = previewUrl;
  } catch (error) {
    previewUrl = null; // preview is optional
  }
  showToast(`Loaded audio: ${file.name}`, 'success');
  return true;
}

/** Selects a demo sample clip (simulated data, not a real recording). Returns the sample or null. */
export function selectAudioSample(key) {
  const sample = AUDIO_SAMPLES[key];
  if (!sample) return null;
  state.selectedAudioSampleKey = key;
  state.selectedAudioFile = null;
  releasePreviewUrl();
  el.selectedAudioName.textContent = sample.name;
  el.selectedAudioSize.textContent = `(${sample.size})`;
  el.selectedAudioBadge.classList.remove('hidden');
  return sample;
}

export function setWavePulse(active) {
  state.waveActive = active;
  el.playPreviewLabel.textContent = active ? 'Pause Wave' : 'Pulse Wave';
}

export function bindAudioEvents() {
  const dropzone = el.audioDropzone;
  const openPicker = () => el.audioFileInput.click();

  dropzone.addEventListener('click', openPicker);
  dropzone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      openPicker();
    }
  });
  el.audioFileInput.addEventListener('change', (event) => {
    if (event.target.files && event.target.files[0]) handleAudioFile(event.target.files[0]);
  });
  dropzone.addEventListener('dragover', (event) => {
    event.preventDefault();
    dropzone.classList.add('dropzone-active');
  });
  dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dropzone-active'));
  dropzone.addEventListener('drop', (event) => {
    event.preventDefault();
    dropzone.classList.remove('dropzone-active');
    if (event.dataTransfer.files && event.dataTransfer.files[0]) handleAudioFile(event.dataTransfer.files[0]);
  });

  el.btnPlayPreview.addEventListener('click', () => {
    setWavePulse(!state.waveActive);
    if (el.audioPlayer.src) {
      if (state.waveActive) el.audioPlayer.play().catch(() => {});
      else el.audioPlayer.pause();
    }
  });
  el.audioPlayer.addEventListener('ended', () => setWavePulse(false));
  window.addEventListener('pagehide', releasePreviewUrl);
}

/** Starts the decorative spectrum animation. Returns a function that stops it. */
export function initAudioWaveCanvas() {
  const canvas = el.audioWaveCanvas;
  const ctx = canvas && canvas.getContext('2d');
  if (!ctx || waveStarted) return () => {};
  waveStarted = true;

  let phase = 0;
  let frame = 0;

  function renderWave() {
    const rect = canvas.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      if (canvas.width !== Math.floor(rect.width) || canvas.height !== Math.floor(rect.height)) {
        canvas.width = Math.floor(rect.width);
        canvas.height = Math.floor(rect.height);
      }
    }
    const w = canvas.width || 300;
    const h = canvas.height || 96;
    ctx.clearRect(0, 0, w, h);

    const bars = 44;
    const gap = 3;
    const barW = Math.max(2, (w - gap * (bars - 1)) / bars);
    const active = state.waveActive || state.isScanning;
    phase += active ? 0.12 : 0.03;

    const [r, g, b] = state.bgThemeRgb.map((value) => Math.round(value));

    for (let i = 0; i < bars; i++) {
      const norm = i / bars;
      const envelope = Math.sin(norm * Math.PI);
      const wave1 = Math.sin(phase + i * 0.42);
      const wave2 = Math.cos(phase * 1.4 - i * 0.25);
      const amp = active
        ? (0.25 + 0.72 * Math.abs(wave1 * 0.6 + wave2 * 0.4)) * envelope
        : (0.1 + 0.14 * Math.abs(wave1)) * (0.4 + 0.6 * envelope);
      const barH = Math.max(4, amp * (h - 16));
      const x = i * (barW + gap);
      const y = (h - barH) / 2;

      const gradient = ctx.createLinearGradient(x, y, x, y + barH);
      gradient.addColorStop(0, `rgba(${r}, ${g}, ${b}, 0.95)`);
      gradient.addColorStop(1, 'rgba(59, 130, 246, 0.55)');
      ctx.fillStyle = gradient;

      ctx.beginPath();
      ctx.roundRect(x, y, barW, barH, 3);
      ctx.fill();
    }

    frame = requestAnimationFrame(renderWave);
  }

  frame = requestAnimationFrame(renderWave);
  return function stopWave() {
    cancelAnimationFrame(frame);
    waveStarted = false;
  };
}
