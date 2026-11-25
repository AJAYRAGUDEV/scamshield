// main.js - application entry point. Loaded by index.html as <script type="module" src="/js/main.js">.
//
// Initialises the visuals once, registers every event listener once, and connects UI events to the modules that
// do the work. It holds no analysis, rendering or request logic of its own.

import { el, findMissingElements } from './dom.js';
import { state, clearHistory, isLiveMode } from './state.js';
import { showToast, copyText, downloadJson } from './utils.js';
import { fetchBackendStatus } from './api.js';
import { initBackgroundCanvas } from './background.js';
import { initRadarChart } from './charts.js';
import { initAudioWaveCanvas, bindAudioEvents, selectAudioSample, setWavePulse } from './audio.js';
import { PRESETS } from './prompts.js';
import { bindExampleLabEvents } from './examples.js';
import { findKeywordHits, runLocalTextEngine } from './sim.js';
import { renderResult, renderHistory, renderTextPreview, renderBackendStatus } from './render.js';
import { triggerTextScan, triggerAudioScan, cancelActiveScan } from './scan.js';
import { bindVideoEvents, refreshVideoModeNote, cancelVideoWatch } from './video.js';

const MODE_BUTTON_ACTIVE = 'px-2.5 py-1 rounded-lg text-xs font-semibold transition-all bg-cyan-500/20 text-cyan-300 border border-cyan-500/30';
const MODE_BUTTON_IDLE = 'px-2.5 py-1 rounded-lg text-xs font-semibold transition-all text-slate-400 hover:text-slate-200 border border-transparent';
const TAB_ACTIVE = 'flex-1 flex items-center justify-center gap-2 py-2 px-3 rounded-lg text-xs sm:text-sm font-semibold transition-all bg-cyan-500/20 text-cyan-300 border border-cyan-500/35 shadow-sm';
const TAB_IDLE = 'flex-1 flex items-center justify-center gap-2 py-2 px-3 rounded-lg text-xs sm:text-sm font-semibold transition-all text-slate-400 hover:text-slate-200 border border-transparent';

let initialized = false;

// ---- backend status ------------------------------------------------------------------------------------------
async function checkBackend() {
  renderBackendStatus('checking');
  const status = await fetchBackendStatus();
  renderBackendStatus(status);
  return status;
}

// ---- mode and tab switching ----------------------------------------------------------------------------------
function setEngineMode(mode) {
  const live = mode === 'api';
  state.engineMode = mode;
  el.btnModeApi.className = live ? MODE_BUTTON_ACTIVE : MODE_BUTTON_IDLE;
  el.btnModeSim.className = live ? MODE_BUTTON_IDLE : MODE_BUTTON_ACTIVE;
  el.btnModeApi.setAttribute('aria-pressed', String(live));
  el.btnModeSim.setAttribute('aria-pressed', String(!live));
  el.statusLabel.textContent = live ? 'FastAPI Live Mode' : 'Interactive Engine';
  refreshVideoModeNote();
  el.statusDot.className = live ? 'w-2 h-2 rounded-full bg-cyan-400 animate-pulse' : 'w-2 h-2 rounded-full bg-emerald-400 animate-pulse';

  if (!live) {
    showToast('Switched to Smart Sim (Demo): results are simulated', 'info');
    return;
  }
  showToast('Live Mode enabled: analyses are sent to the ScamShield backend', 'info');
  checkBackend().then((status) => {
    if (status !== 'ready') showToast('The backend is not ready, so analyses will report "Unable to Assess".', 'warn');
  });
}

const TABS = [
  { name: 'text', tab: 'tabText', panel: 'panelText' },
  { name: 'audio', tab: 'tabAudio', panel: 'panelAudio' },
  { name: 'video', tab: 'tabVideo', panel: 'panelVideo' },
];

function setInputTab(tab) {
  state.inputTab = tab;
  TABS.forEach((entry) => {
    const active = entry.name === tab;
    el[entry.tab].className = active ? TAB_ACTIVE : TAB_IDLE;
    el[entry.tab].setAttribute('aria-selected', String(active));
    el[entry.tab].tabIndex = active ? 0 : -1;
    el[entry.panel].classList.toggle('hidden', !active);
  });
}

function refreshTextPreview() {
  const value = el.messageInput.value;
  renderTextPreview(value, findKeywordHits(value));
}

// ---- event registration (called exactly once) -----------------------------------------------------------------
function bindEvents() {
  // Engine mode
  el.btnModeSim.addEventListener('click', () => setEngineMode('sim'));
  el.btnModeApi.addEventListener('click', () => setEngineMode('api'));

  // Backend status drawer (no credentials are handled in the browser)
  el.btnToggleConfig.addEventListener('click', () => {
    el.configDrawer.classList.toggle('hidden');
    const open = !el.configDrawer.classList.contains('hidden');
    el.btnToggleConfig.setAttribute('aria-expanded', String(open));
    if (open) checkBackend();
  });
  el.btnCloseConfig.addEventListener('click', () => {
    el.configDrawer.classList.add('hidden');
    el.btnToggleConfig.setAttribute('aria-expanded', 'false');
  });
  el.btnRecheckBackend.addEventListener('click', () => checkBackend());

  // Input tabs (click, and Left/Right arrows between tabs)
  el.tabText.addEventListener('click', () => setInputTab('text'));
  el.tabAudio.addEventListener('click', () => setInputTab('audio'));
  el.tabVideo.addEventListener('click', () => setInputTab('video'));
  TABS.forEach((entry, index) => {
    el[entry.tab].addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const step = event.key === 'ArrowRight' ? 1 : -1;
      const next = TABS[(index + step + TABS.length) % TABS.length];
      setInputTab(next.name);
      el[next.tab].focus();
    });
  });

  // Text input, presets and analysis
  el.messageInput.addEventListener('input', refreshTextPreview);
  el.btnClearText.addEventListener('click', () => {
    el.messageInput.value = '';
    refreshTextPreview();
  });
  document.querySelectorAll('.preset-btn').forEach((button) => {
    button.addEventListener('click', async () => {
      const preset = PRESETS[button.getAttribute('data-preset')];
      if (!preset || state.isScanning) return;
      el.messageInput.value = preset.text;
      refreshTextPreview();
      await triggerTextScan(preset.label);
    });
  });
  el.btnAnalyzeText.addEventListener('click', async () => {
    if (state.isScanning) return;
    const text = el.messageInput.value.trim();
    if (!text) {
      showToast('Enter a suspicious message or select a quick scenario first.', 'warn');
      return;
    }
    await triggerTextScan(text.slice(0, 34) + (text.length > 34 ? '...' : ''));
  });

  // Audio: file selection lives in audio.js; samples and analysis are connected here
  bindAudioEvents();
  document.querySelectorAll('.audio-sample-btn').forEach((button) => {
    button.addEventListener('click', async () => {
      if (state.isScanning) return;
      const sample = selectAudioSample(button.getAttribute('data-audio-sample'));
      if (!sample) return;
      setWavePulse(true);
      await triggerAudioScan(sample.name);
      setWavePulse(false);
    });
  });
  el.btnAnalyzeAudio.addEventListener('click', async () => {
    if (state.isScanning) return;
    if (!state.selectedAudioFile && !state.selectedAudioSampleKey) {
      showToast('Upload an audio file or pick a sample voice clip first.', 'warn');
      return;
    }
    const label = state.selectedAudioFile ? state.selectedAudioFile.name : el.selectedAudioName.textContent;
    await triggerAudioScan(label);
  });

  // Result utilities
  el.btnCopyAction.addEventListener('click', () => copyText(el.recommendedActionText.textContent || '', 'Recommended action copied to clipboard'));
  el.btnExportJson.addEventListener('click', () => {
    if (!state.currentResult) {
      showToast('Run an analysis first to export JSON.', 'warn');
      return;
    }
    downloadJson(state.currentResult, `scamshield-report-${Date.now()}.json`);
    showToast('Exported analysis JSON', 'success');
  });
  el.btnClearHistory.addEventListener('click', () => {
    clearHistory();
    renderHistory();
    showToast('Inspection history cleared', 'info');
  });

  // Scam Example Lab: loading only fills the text box. It never submits; the user clicks Analyze.
  bindExampleLabEvents({
    onLoad: (example) => {
      setInputTab('text');
      el.messageInput.value = example.text;
      refreshTextPreview();
      el.messageInput.focus();
      // Smart Sim is a rough keyword demo that can rate a scam sample as Low Risk, so say so before the user analyses it.
      const demoNote = isLiveMode() ? '' : ' Smart Sim is only a rough keyword demo: switch to FastAPI Live for the real analysis.';
      showToast(`Fictional training sample loaded: ${example.title}. Click Analyze when you are ready.${demoNote}`, 'info');
    },
  });

  // Video tab: selection, upload and job watching live in video.js
  bindVideoEvents();

  window.addEventListener('pagehide', () => {
    cancelActiveScan();
    cancelVideoWatch(); // stop polling when the page goes away
  });
}

// ---- start-up -------------------------------------------------------------------------------------------------
function init() {
  if (initialized) return;
  initialized = true;

  const missing = findMissingElements();
  if (missing.length) {
    console.error(`ScamShield: index.html is missing required element ids: ${missing.join(', ')}`);
    return;
  }

  if (window.lucide && window.lucide.createIcons) window.lucide.createIcons();
  initBackgroundCanvas();
  initAudioWaveCanvas();
  initRadarChart();
  bindEvents();
  setEngineMode('sim');
  el.toastContainer.replaceChildren(); // drop the toast from the initial mode set-up

  // Open with a rich DEMO scenario so the dashboard is not empty (labelled "Simulated Demo").
  el.messageInput.value = PRESETS.digital_arrest.text;
  refreshTextPreview();
  renderResult(runLocalTextEngine(PRESETS.digital_arrest.text), PRESETS.digital_arrest.label);
  checkBackend();
}

init();
