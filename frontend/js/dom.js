// dom.js - the single place that looks up elements by id.
//
// Modules import `el` instead of calling document.getElementById themselves. The module script runs after
// the document has been parsed, so every element exists when this file is evaluated. `findMissingElements`
// lets main.js report (once, at start-up) any id that index.html no longer provides.

const $ = (id) => document.getElementById(id);

export const el = {
  // header, mode switch, backend status
  bgCanvas: $('bgCanvas'),
  shieldCore: $('shieldCore'),
  statusDot: $('statusDot'),
  statusLabel: $('statusLabel'),
  btnModeSim: $('btnModeSim'),
  btnModeApi: $('btnModeApi'),
  btnToggleConfig: $('btnToggleConfig'),
  configDrawer: $('configDrawer'),
  backendStatusDot: $('backendStatusDot'),
  backendStatusText: $('backendStatusText'),
  btnRecheckBackend: $('btnRecheckBackend'),
  btnCloseConfig: $('btnCloseConfig'),

  // input card
  inputCard: $('inputCard'),
  tabText: $('tabText'),
  tabAudio: $('tabAudio'),
  panelText: $('panelText'),
  panelAudio: $('panelAudio'),
  messageInput: $('messageInput'),
  charCounter: $('charCounter'),
  liveKeywordBar: $('liveKeywordBar'),
  liveKeywordPills: $('liveKeywordPills'),
  btnClearText: $('btnClearText'),
  btnAnalyzeText: $('btnAnalyzeText'),
  audioDropzone: $('audioDropzone'),
  audioFileInput: $('audioFileInput'),
  selectedAudioBadge: $('selectedAudioBadge'),
  selectedAudioName: $('selectedAudioName'),
  selectedAudioSize: $('selectedAudioSize'),
  audioWaveCanvas: $('audioWaveCanvas'),
  audioPlayer: $('audioPlayer'),
  btnPlayPreview: $('btnPlayPreview'),
  playPreviewLabel: $('playPreviewLabel'),
  btnAnalyzeAudio: $('btnAnalyzeAudio'),
  scannerOverlay: $('scannerOverlay'),
  scanStageText: $('scanStageText'),
  scanProgressBar: $('scanProgressBar'),

  // verdict card, charts, findings
  gaugeArc: $('gaugeArc'),
  riskScoreValue: $('riskScoreValue'),
  verdictBadge: $('verdictBadge'),
  confidenceBadge: $('confidenceBadge'),
  modeBadge: $('modeBadge'),
  fallbackBadge: $('fallbackBadge'),
  simBadge: $('simBadge'),
  coverageBadge: $('coverageBadge'),
  languageBadge: $('languageBadge'),
  limitationsBlock: $('limitationsBlock'),
  limitationsList: $('limitationsList'),
  urlFindings: $('urlFindings'),
  urlFindingsList: $('urlFindingsList'),
  scamTypeHeading: $('scamTypeHeading'),
  explanationText: $('explanationText'),
  recommendedActionText: $('recommendedActionText'),
  actionCard: $('actionCard'),
  actionTitle: $('actionTitle'),
  redFlagsList: $('redFlagsList'),
  redFlagCount: $('redFlagCount'),
  signalRadarChart: $('signalRadarChart'),
  valAuthority: $('valAuthority'),
  barAuthority: $('barAuthority'),
  valUrgency: $('valUrgency'),
  barUrgency: $('barUrgency'),
  valCredential: $('valCredential'),
  barCredential: $('barCredential'),
  valSynthetic: $('valSynthetic'),
  barSynthetic: $('barSynthetic'),
  btnCopyAction: $('btnCopyAction'),
  btnExportJson: $('btnExportJson'),

  // history, toasts, screen-reader announcements
  historyList: $('historyList'),
  btnClearHistory: $('btnClearHistory'),
  toastContainer: $('toastContainer'),
  srAnnouncer: $('srAnnouncer'),

  // video tab
  tabVideo: $('tabVideo'),
  panelVideo: $('panelVideo'),
  videoDropzone: $('videoDropzone'),
  videoFileInput: $('videoFileInput'),
  selectedVideoBadge: $('selectedVideoBadge'),
  selectedVideoName: $('selectedVideoName'),
  selectedVideoSize: $('selectedVideoSize'),
  selectedVideoLength: $('selectedVideoLength'),
  btnReplaceVideo: $('btnReplaceVideo'),
  btnRemoveVideo: $('btnRemoveVideo'),
  videoError: $('videoError'),
  videoModeNote: $('videoModeNote'),
  btnAnalyzeVideo: $('btnAnalyzeVideo'),
  btnStopVideoWait: $('btnStopVideoWait'),

  // Scam Example Lab
  btnOpenExampleLab: $('btnOpenExampleLab'),
  exampleLab: $('exampleLab'),
  exampleLabBackdrop: $('exampleLabBackdrop'),
  exampleLabPanel: $('exampleLabPanel'),
  btnCloseExampleLab: $('btnCloseExampleLab'),
  btnCancelExampleLab: $('btnCancelExampleLab'),
  exampleList: $('exampleList'),
  exampleEmpty: $('exampleEmpty'),
  exampleDetails: $('exampleDetails'),
  exampleTitle: $('exampleTitle'),
  exampleDescription: $('exampleDescription'),
  exampleLegitNote: $('exampleLegitNote'),
  exampleSampleText: $('exampleSampleText'),
  btnLoadExample: $('btnLoadExample'),
};

/** Names of `el` entries whose element is not in the page. Empty when index.html is intact. */
export function findMissingElements() {
  return Object.entries(el).filter(([, node]) => !node).map(([name]) => name);
}
