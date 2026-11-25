// examples.js - the SCAM EXAMPLE LAB: a modal of fictional training messages that can be loaded into the Text Scanner.
//
// Replaces the former "GET PROMPT" task bar. It is entirely local: no network call, no analysis, no storage.
// Loading an example only fills the text box; the user decides whether to click Analyze. The samples deliberately
// contain NO scores or verdicts: whatever result appears comes from the normal analysis path.
// All text is placed with textContent via makeEl (no innerHTML).

import { el } from './dom.js';
import { state } from './state.js';
import { makeEl, showToast } from './utils.js';

/** Fictional training samples. Names, banks, numbers and domains are invented (".example" domains cannot resolve). */
export const EXAMPLES = [
  {
    id: 'banking_impersonation',
    title: 'Banking impersonation',
    pattern: 'The sender poses as a bank to look trustworthy, then threatens loss of access and sends you to a look-alike website.',
    text: 'Northfield Bank Alert: Unusual activity was detected on your account ending 4821. Your account will be restricted within 2 hours unless you confirm your identity at https://northfieldbank-secure-login.example/verify. Our fraud officer will call you; please read out the code you receive to complete verification.',
  },
  {
    id: 'digital_arrest',
    title: 'Digital arrest scam',
    pattern: 'A fake law-enforcement officer claims you are involved in a crime, demands secrecy and keeps you on the line while pressing you to pay.',
    text: 'This is Inspector R. Verma from the National Cyber Crime Cell. A parcel booked in your name contains illegal items and an arrest warrant has been issued. You are under digital arrest: do not disconnect this call and do not tell your family or the bank. To close the case today you must transfer Rs 60,000 to the verification account I will share.',
  },
  {
    id: 'fake_job_offer',
    title: 'Fake job offer',
    pattern: 'An unrealistic job offer for little work, followed by a request for an upfront "registration" or "training" fee.',
    text: 'Congratulations! You have been shortlisted for a work-from-home data entry role at BrightPath Solutions. Salary Rs 45,000 per month, only 2 hours a day, no interview needed. To confirm your seat, pay a refundable registration fee of Rs 2,500 to the account below today. Seats are limited and the offer expires tonight.',
  },
  {
    id: 'phishing_message',
    title: 'Phishing message',
    pattern: 'A routine-sounding account notice with a deceptive link that asks you to log in, so your password can be captured.',
    text: 'Your CloudBox storage is 98% full and your files will be deleted in 24 hours. Log in now to keep your data: http://cloudbox-account-recovery.example/login?next=https://cloudbox-verify.example. Sign in with your email and password to avoid permanent loss.',
  },
  {
    id: 'fake_delivery',
    title: 'Fake delivery notification',
    pattern: 'A small "redelivery fee" or address update is requested through a link, aiming to collect card details.',
    text: 'SwiftPost: We tried to deliver your parcel but the address was incomplete. Your package will be returned unless you pay a redelivery fee of Rs 29 and update your address at https://swiftpost-redelivery.example/pay within 12 hours. Please enter your card number to proceed.',
  },
  {
    id: 'investment_scam',
    title: 'Investment scam',
    pattern: 'Guaranteed or unusually high returns, pressure to act fast, and a request to move money to a personal account or an unknown platform.',
    text: 'Join our private trading group! Our expert team guarantees 30% returns every week with zero risk. Early members doubled their money in 10 days. Deposit a minimum of Rs 10,000 to this personal account and send the screenshot to get started. Only 5 spots left, do not tell others about this opportunity.',
  },
  {
    id: 'otp_credential_theft',
    title: 'OTP and credential theft',
    pattern: 'A caller or message creates a problem and asks you to share a one-time password, PIN or to install a remote-access app.',
    text: 'Hello, I am calling from Northfield Bank customer support. A payment of Rs 18,900 from your account is pending and will go through unless you stop it. To cancel it, please tell me the OTP just sent to your phone, and install the AnyDesk app so I can secure your account. Do it right now, there is no time to waste.',
  },
  {
    id: 'legitimate_message',
    title: 'Legitimate message (safe comparison)',
    pattern: 'An ordinary service notification. It mentions an OTP, an account and a link, which are words scam detectors watch for, so it shows how a genuine message differs and why false alarms can happen.',
    text: 'Northfield Bank: Your one-time password for the purchase of Rs 1,250 at BookStore is 482913. It is valid for 10 minutes. Never share this OTP with anyone, including bank staff. If you did not make this purchase, call the number printed on the back of your card.',
    legitimate: true,
  },
];

let opener = null;           // the element that had focus before the modal opened
let selectedId = null;

const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])';

function selectedExample() {
  return EXAMPLES.find((example) => example.id === selectedId) || null;
}

function renderDetails() {
  const example = selectedExample();
  const has = example !== null;
  el.exampleDetails.classList.toggle('hidden', !has);
  el.exampleEmpty.classList.toggle('hidden', has);
  el.btnLoadExample.disabled = !has;
  el.btnLoadExample.classList.toggle('opacity-50', !has);
  el.btnLoadExample.classList.toggle('cursor-not-allowed', !has);
  if (!has) return;
  el.exampleTitle.textContent = example.title;
  el.exampleDescription.textContent = example.pattern;
  el.exampleSampleText.textContent = example.text;
  el.exampleLegitNote.classList.toggle('hidden', !example.legitimate);
}

function selectExample(id) {
  selectedId = id;
  el.exampleList.querySelectorAll('[data-example-id]').forEach((item) => {
    const active = item.getAttribute('data-example-id') === id;
    item.setAttribute('aria-checked', String(active));
    item.tabIndex = active ? 0 : -1;
    item.classList.toggle('bg-cyan-500/15', active);
    item.classList.toggle('border-cyan-400/60', active);
    item.classList.toggle('text-cyan-100', active);
    item.classList.toggle('border-slate-700/80', !active);
  });
  renderDetails();
}

function buildList() {
  el.exampleList.replaceChildren();
  EXAMPLES.forEach((example) => {
    const item = makeEl('button', {
      className: 'w-full text-left px-3 py-2.5 rounded-xl border border-slate-700/80 bg-slate-900/80 hover:bg-slate-800/80 text-xs sm:text-sm font-semibold text-slate-200 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400',
      attrs: { type: 'button', role: 'radio', 'aria-checked': 'false', 'data-example-id': example.id, tabindex: '-1' },
      text: example.title,
    });
    item.addEventListener('click', () => selectExample(example.id));
    el.exampleList.append(item);
  });
}

/** Radio-group keyboard behaviour: arrows move the selection (and focus), Home/End jump. */
function onListKeydown(event) {
  const keys = ['ArrowDown', 'ArrowUp', 'ArrowRight', 'ArrowLeft', 'Home', 'End'];
  if (!keys.includes(event.key)) return;
  event.preventDefault();
  const items = [...el.exampleList.querySelectorAll('[data-example-id]')];
  let index = items.findIndex((item) => item.getAttribute('data-example-id') === selectedId);
  if (index < 0) index = 0;
  if (event.key === 'ArrowDown' || event.key === 'ArrowRight') index = (index + 1) % items.length;
  else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') index = (index - 1 + items.length) % items.length;
  else if (event.key === 'Home') index = 0;
  else index = items.length - 1;
  selectExample(items[index].getAttribute('data-example-id'));
  items[index].focus();
}

export function isLabOpen() {
  return !el.exampleLab.classList.contains('hidden');
}

export function openLab() {
  if (isLabOpen()) return;
  opener = document.activeElement;
  el.exampleLab.classList.remove('hidden');
  el.btnOpenExampleLab.setAttribute('aria-expanded', 'true');
  document.body.classList.add('overflow-hidden');
  if (!selectedId) selectExample(EXAMPLES[0].id);
  else selectExample(selectedId);
  const active = el.exampleList.querySelector('[aria-checked="true"]');
  (active || el.btnCloseExampleLab).focus();
}

export function closeLab() {
  if (!isLabOpen()) return;
  el.exampleLab.classList.add('hidden');
  el.btnOpenExampleLab.setAttribute('aria-expanded', 'false');
  document.body.classList.remove('overflow-hidden');
  if (opener && typeof opener.focus === 'function') opener.focus();
  opener = null;
}

/** Keeps Tab/Shift+Tab inside the open dialog and closes it on Escape. */
function onDialogKeydown(event) {
  if (event.key === 'Escape') {
    event.preventDefault();
    closeLab();
    return;
  }
  if (event.key !== 'Tab') return;
  const items = [...el.exampleLabPanel.querySelectorAll(FOCUSABLE)].filter((node) => node.offsetParent !== null);
  if (items.length === 0) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

/**
 * Wires the lab. `onLoad(text)` is supplied by main.js, which owns the tab and the text input; it must only fill the
 * input (never submit). The lab refuses to overwrite the input while a scan is running.
 */
export function bindExampleLabEvents({ onLoad }) {
  buildList();
  renderDetails();

  el.btnOpenExampleLab.addEventListener('click', openLab);
  el.btnCloseExampleLab.addEventListener('click', closeLab);
  el.btnCancelExampleLab.addEventListener('click', closeLab);
  el.exampleLabBackdrop.addEventListener('click', closeLab);
  el.exampleLab.addEventListener('keydown', onDialogKeydown);
  el.exampleList.addEventListener('keydown', onListKeydown);

  el.btnLoadExample.addEventListener('click', () => {
    const example = selectedExample();
    if (!example) return;
    if (state.isScanning) {
      showToast('Wait for the current analysis to finish before loading an example.', 'warn');
      return;
    }
    closeLab();
    onLoad(example);
  });
}
