# Text and audio analysis (Phase 7)

How ScamShield turns a message or recording into evidence, and what it can and cannot tell you.
Scoring itself is described in [risk-engine.md](risk-engine.md); nothing in this phase adds a second scoring
algorithm or changes the thresholds (0-24 Low, 25-59 Suspicious, 60-100 High).

## 1. Who does what

| Step | Done by | Notes |
|---|---|---|
| Find links, describe their **structure** | Server (`url_analysis.py`) | No network access of any kind |
| Check audio length and silence | Server (`audio_inspection.py`, WAV only) | Silent audio is refused before Gemini is called |
| Extract evidence (categories, quotes, context, language, limitations) | Gemini | Returns **evidence only**: never a score, verdict, status or recommendation |
| Validate the evidence | Server (Pydantic, strict) | Invalid output makes the whole analysis `unable_to_assess` |
| Add server-found link evidence, verify timestamps, build limitations | Server (`evidence_enrichment.py`) | |
| Score, verdict, confidence | Server (`risk_engine.py`) | Same engine as Phase 4 |
| Summary, explanation, recommended action | Server (`response_builder.py`) | Deterministic wording from validated evidence |

Everything Gemini returns is **untrusted input**. Text inside a message cannot instruct the server, and fields such as
`verdict` or `risk_score` in the model's JSON are ignored.

## 2. Indicator categories

| Category | What it looks for |
|---|---|
| `credential_request` | OTP, password, PIN, card or bank-login requests; "verify your account" through a link |
| `payment_request` | Money, gift cards, crypto, transfers |
| `payment_redirection` | New or personal account to pay, changed bank details |
| `advance_fee_or_prize` | Unexpected prize/lottery/refund that needs a fee first; investment promising guaranteed returns |
| `remote_access_request` | Install a remote-access/screen-sharing app, share a code, give control of a device |
| `authority_impersonation` | Claims to be a bank officer, government body, police |
| `organization_impersonation` | Claims to be a delivery service, customer support, a company |
| `impersonation_relationship` | Claims to be a relative, boss or colleague |
| `threat_coercion` | Arrest, legal action, account suspension, harm |
| `suspicious_link` | A link the reader is pushed to open, or a structurally suspicious link |
| `isolation_secrecy` | "Don't tell anyone", "don't call the bank", stay on the line |
| `urgency_pressure` | Artificial deadlines |
| `synthetic_voice_indicator`, `scripted_delivery`, `other_suspicious` | Weak hints only (see §7) |

`fake_interface` and `visual_manipulation` exist for the future video phase and are not used.

## 3. Context-aware analysis

Each piece of evidence carries a `context`:

| Value | Meaning | Score factor |
|---|---|---|
| `directed_at_recipient` | The content itself asks or pressures the reader | 1.00 |
| `unclear` | Cannot tell whether it is aimed at the reader | 0.60 |
| `discussed_or_described` | News, advice, a bank's own warning, an explanation of scams | 0.10 |
| `fictional_or_example` | A story, script or worked example | 0.10 |

So "never share your OTP with anyone", a news article about OTP fraud, or a short story with a scam scene is not flagged as
a scam, while the same words addressed to the reader are. The model is asked to judge context from what the text
**does** (instructs, or merely describes); the server applies the factor. A legitimate bank notification that contains an
OTP and a warning not to share it produces no evidence at all.

**Limit:** the judgement itself is made by the model and can be wrong in both directions (a scam dressed up as a "news
article" may be discounted; an unusual legitimate message may be flagged). It could not be tested against the real model
in this project; the tests prove how the server treats each possible answer.

## 4. Links

Extracted from the text: bare `http(s)://` and `www.` links, Markdown `[text](url)` and HTML `<a href>` links (up to 20).
Per link the server notes: IP-address host, credentials (`user@`) in the URL, punycode, a well-known brand name on a domain
that is not that brand's (using a small curated list plus look-alike character/Levenshtein checks), visible link text that
names a different domain than the destination, redirect URLs inside parameters, many sub-domains or hyphens, and
URL shorteners.

* **Scored** (become `suspicious_link` evidence, labelled `suspicious_url_pattern` or `possible_brand_impersonation`):
  structural patterns that are unusual in legitimate messages.
* **Informational only** (`shortened_url`, `unverified_domain`): shown to the reader, never scored. A shortener or an
  unfamiliar domain is not treated as suspicious on its own.
* Links are **never visited, resolved or downloaded**, and no reputation service is consulted. Every finding says so
  (`reputation_verified` is always `false`). Only `scheme://host` is displayed: paths, parameters and credentials are not
  echoed back.
* Link evidence gets context `directed_at_recipient` only when the model found directed evidence; otherwise `unclear`.

**Limits:** the brand list is small, so many real phishing domains match no brand and some legitimate look-alike names are
flagged as "possible". Prose links without a scheme or `www.` are not detected. The registrable-domain rule is a heuristic
without the public-suffix list.

## 5. Audio

Gemini listens to the recording and reports spoken impersonation, fake authority, OTP/credential requests, payment
instructions, threats, urgency, secrecy and remote-access installs. It is told **not** to identify speakers, authenticate
anyone, or claim a voice is cloned or synthetic. The result always says that the analysis "does not identify who is
speaking or prove that a voice is real or synthetic".

* **Silence is not safety.** A WAV with no audible signal is refused as `INSUFFICIENT_EVIDENCE` and Gemini is not called.
  Silence/duration checks work for **WAV only**; for other formats the server cannot check and says nothing about it.
* **Unclear audio:** if the model reports partial or insufficient coverage and finds nothing, the result is
  `unable_to_assess`, never Low Risk.
* **Unsupported/empty/oversized files** are rejected before analysis (Phase 5 validation, unchanged).
* **Provider timeout or failure** gives `unable_to_assess` with a `reason_code`; the temporary file and the remote upload
  are deleted.

### Transcript snippets and timestamps

Each piece of evidence may carry a short `quote` (at most 300 characters) and approximate `timestamp_start`/`timestamp_end`
(seconds). Timestamps are returned **only when the model gave them** and are never invented. Where the file's length is
known (WAV) a timestamp more than one second past the end is removed and the result says so; otherwise the result states
that the timestamps could not be checked. Timestamps are approximations from the model, not forensic times.

The full transcript is **not** requested, stored or returned: only the short excerpts that support a finding, within the
40-signal limit. Uploaded audio is deleted after analysis (no permanent media retention).

## 6. Languages

English, Tamil, Hindi and mixed Tamil-English are supported by the prompt and exercised by mocked tests.

* Quotes keep the **original script exactly** as the model returned it (no transliteration or normalisation is applied to the
  displayed text); explanations are in English.
* `language` and `language_uncertain` are reported. Non-English analysis always carries a limitation saying it may be less
  reliable; an uncertain language adds another.
* Duplicate detection normalises text with Unicode awareness, so a repeated Tamil or Hindi quote is counted once and
  different quotes are not merged.
* **No equal-accuracy claim is made.** Accuracy for Tamil, Hindi, code-mixed text, dialects and noisy audio is expected to
  be lower than for clear English and has not been measured.

## 7. Evidence schema (Gemini → server)

`EvidenceReport`: `coverage`, `language`, `language_uncertain`, `limitations`, `signals[]`. Each signal: `category`, `name`,
`evidence`, `source`, `context`, `quote`, `timestamp_start`, `timestamp_end`, `severity`, `reliability`. The Gemini schema
marks `language`, `language_uncertain`, `limitations`, `context` and `quote` as required so the model must state them; the
Python side is lenient (defaults) so an older-style answer still validates. Unknown keys are dropped. See
[risk-engine.md](risk-engine.md) §2 for the field rules.

`AnalysisResponse` adds `language`, `language_uncertain`, `limitations` (strings), `urls` (`display`, `domain`, `indicators`,
`note`, `reputation_verified: false`) and, per signal, `why_it_matters` and `context`. A failed analysis carries none of
them (`urls` and `limitations` must be empty when `status` is `unable_to_assess`).

## 8. Risk-engine integration

1. Gemini evidence is validated and checked against the mode (text accepts `text_content`; audio accepts audio sources).
2. Link evidence is appended; timestamps are verified; limitations are built.
3. `risk_engine.assess` computes impact (weight × severity × reliability × context), groups duplicates, applies
   diminishing returns and compounds categories. Partial coverage can never clear content; no evidence with full coverage
   is `low_risk`; no evidence with partial or insufficient coverage is `unable_to_assess` (null score).
4. The response is built from the assessment; the model contributes no score, verdict, status, threshold or recommendation.

## 9. False positives and false negatives

**False positives (flagged but legitimate):** unusual real messages that ask for payment or a login, genuine fraud-team calls,
legitimate links to unlisted domains that happen to resemble a brand, and content whose context the model misjudged.
**False negatives (missed scams):** scams written without the listed behaviours, new tactics, heavily abbreviated or
code-mixed language, noisy or very short audio, scams disguised as discussion, and links to unlisted brands. A Low Risk
result means "no listed indicators were found in what was analysed", **not** that the content is safe. ScamShield does not
guarantee detection, does not verify domain reputation, does not authenticate voices and does not detect deepfakes.
