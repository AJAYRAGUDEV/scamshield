// sim.js - Demo mode ("Smart Sim"): everything here is SIMULATED and is flagged `simulated: true`.
//
// Nothing in this file calls the backend or Gemini. Results produced here are shown with a "Simulated Demo"
// badge and a SIMULATED tag in the history, so they cannot be mistaken for a real detection. They are kept
// strictly separate from live results (api.js / result.js).

import { state } from './state.js';

export const HEURISTIC_RULES = {
  authority_impersonation: {
    weight: 30,
    phrases: ['police', 'court', 'customs', 'government', 'official', 'cbi', 'rbi', 'bank officer', 'digital arrest', 'inspector', 'cyber crime'],
  },
  urgency_isolation: {
    weight: 25,
    phrases: ['urgent', 'immediately', 'today', 'within 24 hours', "don't tell", 'do not tell', 'keep this secret', 'arrest warrant', 'frozen', 'suspension'],
  },
  credential_payment_trap: {
    weight: 35,
    phrases: ['otp', 'one-time password', 'password', 'pin', 'upi', 'transfer money', 'send money', 'bank details', 'gift card', 'crypto', 'click this link'],
  },
  synthetic_or_script_signals: {
    weight: 22,
    phrases: ['voice clone', 'ai generated', 'synthetic voice', 'deepfake', 'robotic', 'scripted'],
  },
};

export const AUDIO_SAMPLES = {
  impersonation_call: {
    name: 'urgent_bank_impersonation_demo.wav',
    size: '1.4 MB',
    result: {
      risk_score: 88,
      verdict: 'High Risk',
      scam_type: 'Bank Officer Impersonation & OTP Coercion',
      confidence_level: 'High',
      signal_scores: {
        authority_impersonation: 86,
        urgency_isolation: 84,
        credential_payment_trap: 94,
        synthetic_or_script_signals: 62,
      },
      red_flags: [
        'Caller claims to be a senior bank fraud officer threatening immediate account lockout',
        'Pressures listener to read back a 6-digit OTP without hanging up',
        'Repetitive, tightly scripted phrasing with unusually uniform cadence (cannot confirm synthetic origin)',
      ],
      explanation: 'Audible content shows strong coercion and credential-harvesting patterns typical of bank impersonation calls. While cadence appears unusually uniform, voice synthesis cannot be proven from audio alone.',
      recommended_action: 'Hang up immediately and never share OTPs or PINs. Call the official number printed on the back of your bank card to verify your account status.',
      mode: 'audio',
      offline_fallback_used: false,
      simulated: true,
    },
  },
  benign_voicemail: {
    name: 'clinic_appointment_reminder_demo.wav',
    size: '620 KB',
    result: {
      risk_score: 8,
      verdict: 'Low Risk',
      scam_type: 'No strong scam indicators detected',
      confidence_level: 'Medium',
      signal_scores: {
        authority_impersonation: 5,
        urgency_isolation: 10,
        credential_payment_trap: 0,
        synthetic_or_script_signals: 14,
      },
      red_flags: [],
      explanation: 'The clip resembles a routine appointment reminder with natural pacing and no requests for credentials, money, or secrecy. Standard verification still applies if unexpected.',
      recommended_action: 'If you have an upcoming visit, confirm the appointment time directly through your clinic\'s known phone number or patient portal.',
      mode: 'audio',
      offline_fallback_used: false,
      simulated: true,
    },
  },
};

/** Phrases from the demo rule set found in `text` (used for the live trigger pills and the demo engine). */
export function findKeywordHits(text) {
  const lower = text.toLowerCase();
  const hits = [];
  Object.values(HEURISTIC_RULES).forEach((rule) => {
    rule.phrases.forEach((phrase) => {
      if (lower.includes(phrase) && !hits.includes(phrase)) hits.push(phrase);
    });
  });
  return hits;
}

/** The demo text engine: a simple keyword scorer. NOT an analysis: its result is always `simulated: true`. */
export function runLocalTextEngine(text) {
  const lower = text.toLowerCase();
  const scores = {};
  const flags = [];

  Object.entries(HEURISTIC_RULES).forEach(([key, rule]) => {
    const found = rule.phrases.filter((phrase) => lower.includes(phrase));
    scores[key] = Math.min(100, rule.weight * found.length);
    found.forEach((phrase) => {
      flags.push(`Detected high-signal phrase "${phrase}" (${key.replace(/_/g, ' ')})`);
    });
  });

  // Additional subtle pattern boosts for realistic simulation
  if (/https?:\/\/|bit\.ly|tinyurl/i.test(text)) {
    scores.credential_payment_trap = Math.min(100, scores.credential_payment_trap + 20);
    flags.push('Contains an external link prompting urgent action');
  }
  if (/\b\d{4,6}\b/.test(text) && (lower.includes('otp') || lower.includes('code') || lower.includes('pin'))) {
    scores.credential_payment_trap = Math.min(100, scores.credential_payment_trap + 15);
  }

  const totalScore = Math.min(
    100,
    Math.round(
      scores.authority_impersonation * 0.35 +
      scores.urgency_isolation * 0.3 +
      scores.credential_payment_trap * 0.4 +
      scores.synthetic_or_script_signals * 0.25 +
      (flags.length >= 4 ? 18 : 0)
    )
  );

  const verdict = totalScore >= 60 ? 'High Risk' : totalScore >= 25 ? 'Suspicious' : 'Low Risk';
  const confidence = flags.length >= 3 ? 'High' : 'Medium';

  let scamType = 'No common scam pattern detected';
  if (scores.authority_impersonation >= 50 && scores.urgency_isolation >= 40) {
    scamType = 'Authority Impersonation / Digital Arrest Coercion';
  } else if (scores.credential_payment_trap >= 50) {
    scamType = 'Credential Phishing & Payment Trap';
  } else if (scores.synthetic_or_script_signals >= 20 && scores.credential_payment_trap >= 25) {
    scamType = 'Synthetic / Scripted Executive Wire Fraud';
  } else if (flags.length > 0) {
    scamType = 'Potential Social Engineering';
  }

  const explanation =
    verdict === 'High Risk'
      ? 'Multiple converging coercion indicators were identified—including authority pressure, time-sensitive isolation, or payment/credential demands. Treat this message as a high-probability scam attempt.'
      : verdict === 'Suspicious'
        ? 'Meaningful social-engineering signals appear in this message, though they are not conclusive on their own. Avoid acting until verified through an independent channel.'
        : 'Little evidence of common impersonation, urgency, or payment-trap tactics was detected in this text. Still exercise caution if the sender is unexpected.';

  const recommendedAction =
    verdict === 'High Risk'
      ? 'Do not transfer funds, click links, or share OTPs/PINs. End contact immediately and report the sender via your official bank or cybercrime portal.'
      : verdict === 'Suspicious'
        ? 'Pause and verify the claim independently using the organization\'s official app or phone number rather than replying directly.'
        : 'No urgent threat indicators found. Confirm any logistics directly inside the official service app if needed.';

  return {
    risk_score: totalScore,
    verdict,
    scam_type: scamType,
    confidence_level: confidence,
    signal_scores: {
      authority_impersonation: scores.authority_impersonation,
      urgency_isolation: scores.urgency_isolation,
      credential_payment_trap: scores.credential_payment_trap,
      synthetic_or_script_signals: scores.synthetic_or_script_signals,
    },
    red_flags: flags.slice(0, 20),
    explanation,
    recommended_action: recommendedAction,
    mode: 'text',
    offline_fallback_used: false,
    simulated: true,
  };
}

/** The demo audio "analysis": a chosen sample's canned result, or a fixed preview result for an uploaded clip. */
export function simulateAudio() {
  const sample = state.selectedAudioSampleKey && AUDIO_SAMPLES[state.selectedAudioSampleKey];
  if (sample) return JSON.parse(JSON.stringify(sample.result));

  const name = state.selectedAudioFile ? state.selectedAudioFile.name : 'audio_sample';
  return {
    risk_score: 54,
    verdict: 'Suspicious',
    scam_type: 'Simulated Audio Inspection (Preview Mode)',
    confidence_level: 'Medium',
    signal_scores: {
      authority_impersonation: 48,
      urgency_isolation: 64,
      credential_payment_trap: 42,
      synthetic_or_script_signals: 38,
    },
    red_flags: [
      `Loaded audio clip "${name}" for inspection`,
      'Demo mode does not analyse audio; switch to FastAPI Live for real Gemini audio analysis',
    ],
    explanation: 'Demo mode: this result is simulated and the audio was not analysed. Your file was only checked locally (format and size under 20 MB). Use FastAPI Live to have the backend analyse it.',
    recommended_action: 'Do not act on urgent requests in the recording until independently verified through a trusted contact.',
    mode: 'audio',
    offline_fallback_used: false,
    simulated: true,
  };
}
