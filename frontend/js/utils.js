// utils.js - shared helpers: safe DOM construction, escaping, toasts, clipboard, download, URL checks.
//
// Rule for the whole frontend: untrusted text (user input, backend/Gemini output) is only ever placed in the
// page with textContent or createTextNode (via makeEl). Nothing in the modules assigns innerHTML.

import { el } from './dom.js';

/** Escapes text for use inside an HTML string. Kept for completeness; the modules build DOM nodes instead. */
export function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function formatMegabytes(bytes) {
  return (bytes / (1024 * 1024)).toFixed(2);
}

/**
 * Returns an absolute http(s) URL string, or null for anything else (javascript:, data:, vbscript:, malformed).
 * Use before putting any URL that did not come from this codebase into an href/src.
 */
export function safeUrl(value) {
  try {
    const url = new URL(String(value), window.location.origin);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
  } catch (error) {
    return null;
  }
}

/** Creates an element; `text` is set with textContent, `attrs` with setAttribute. */
export function makeEl(tag, { className = '', text = null, attrs = null } = {}) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== null) node.textContent = text;
  if (attrs) Object.entries(attrs).forEach(([name, value]) => node.setAttribute(name, value));
  return node;
}

const TOAST_COLORS = {
  info: 'border-cyan-500/40 bg-slate-900/95 text-cyan-200',
  success: 'border-emerald-500/40 bg-slate-900/95 text-emerald-200',
  warn: 'border-amber-500/40 bg-slate-900/95 text-amber-200',
  error: 'border-rose-500/40 bg-slate-900/95 text-rose-200',
};

export function showToast(message, type = 'info') {
  const toast = makeEl('div', {
    className: `pointer-events-auto px-4 py-2.5 rounded-xl border text-xs font-medium shadow-xl backdrop-blur-md animate-in-item ${TOAST_COLORS[type] || TOAST_COLORS.info}`,
    text: message,
    attrs: { role: type === 'error' ? 'alert' : 'status' },
  });
  el.toastContainer.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(8px)';
    toast.style.transition = 'all 0.3s ease';
    setTimeout(() => toast.remove(), 320);
  }, 3200);
}

function fallbackCopy(text) {
  const area = makeEl('textarea');
  area.value = text;
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.select();
  let copied = false;
  try {
    copied = document.execCommand('copy');
  } catch (error) {
    copied = false;
  }
  area.remove();
  return copied;
}

/** Copies text to the clipboard and tells the user what happened. */
export async function copyText(text, successMessage) {
  let copied = false;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch (error) {
      copied = fallbackCopy(text);
    }
  } else {
    copied = fallbackCopy(text);
  }
  showToast(copied ? successMessage : 'Unable to copy text automatically', copied ? 'success' : 'error');
}

/** Downloads `data` as a JSON file. The object URL is released straight away. */
export function downloadJson(data, filename) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = makeEl('a', { attrs: { href: url, download: filename } });
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
