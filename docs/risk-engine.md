# ScamShield risk engine

> **ScamShield is a risk-assessment tool.** A *Low Risk* result does **not** prove that content, a
> person or a communication is authentic or safe. A *High Risk* result is an assessment of indicators,
> not proof of fraud. The system does not guarantee that scams are detected and does not prove that
> audio or video is synthetic or manipulated.

## 1. Architecture

```
input ──► text/audio service ──► Gemini provider ──► EVIDENCE (untrusted)
                                                        │ strict validation (schemas/evidence.py)
                                                        │ mode legality check
                                                        ▼
                                                 risk_engine.assess()   ◄── the only place a score,
                                                        │                    verdict or confidence is decided
                                                        ▼
                                              response_builder (server-written text)
                                                        ▼
                                              AnalysisResponse (public)
```

| Module | Responsibility |
|---|---|
| `app/schemas/evidence.py` | The evidence contract. Also the `response_schema` sent to Gemini. |
| `app/schemas/verdict.py` | Canonical verdicts, analysis status and the **single definition of the thresholds**. |
| `app/services/risk_engine.py` | Deterministic scoring, grouping, confidence. Weights live here. |
| `app/services/response_builder.py` | Summary, scam label, explanation, recommended action, red flags (deterministic text). |
| `app/services/analysis_pipeline.py` | Orchestration and the single failure policy. |
| `app/schemas/analysis.py` | The public response; its validators make an inconsistent response impossible. |

**Gemini reports evidence only.** It never supplies a score, verdict, confidence, summary or
recommendation. If it adds such fields they are ignored by the schema and cannot reach the engine
(tests assert this). The prompt also tells the model to treat the supplied content as untrusted data.

## 2. Evidence schema

`EvidenceReport`: `coverage` (`full` / `partial` / `insufficient`), `signals` (at most 40), and, since Phase 7, `language`
(free text, at most 40 characters), `language_uncertain` (bool) and `limitations` (at most 8 short notes). See
[analysis.md](analysis.md) for what each means.

`EvidenceSignal`:

| Field | Meaning | Validation |
|---|---|---|
| `category` | Kind of indicator (table below) | enum |
| `name` | Short identifier, normalised to `snake_case` | 2-64 chars |
| `evidence` | What in the input shows it | non-empty, clipped to 500 chars |
| `source` | `text_content`, `audio_speech`, `audio_acoustic`, `video_visual`, `video_text` (on-screen text), `video_audio` | enum; must be legal for the mode |
| `quote` | Verbatim excerpt (text or transcribed speech), optional | clipped to 300 chars |
| `timestamp_start` / `timestamp_end` | Seconds, optional | strict numbers 0-86 400; end needs start and must not precede it; strings/booleans/NaN rejected |
| `severity` | `low` / `medium` / `high` / `critical`: how serious the behaviour would be if genuine | enum, **not a number** |
| `reliability` | `weak` / `moderate` / `strong`: how clearly the input supports it | enum, **not a number** |
| `context` | `directed_at_recipient` / `discussed_or_described` / `fictional_or_example` / `unclear`: is the behaviour aimed at the reader, or merely talked about? | enum; defaults to `directed_at_recipient` |

Any invalid field, missing field, wrong-mode source/category, or malformed JSON makes the **whole**
response `INVALID_PROVIDER_RESPONSE` (never a partial score). Text analysis accepts `text_content`
only and no timestamps; audio accepts `audio_speech` / `audio_acoustic`; video (Phase 8, see [video.md](video.md)) accepts
`video_visual` / `video_text` / `video_audio` and is the only mode where `fake_interface` and `visual_manipulation` are legal.

## 3. Signal categories and weights

`CATEGORY_WEIGHT` is the most one signal of that kind can contribute on its own (severity *critical*,
reliability *strong*). Weights are expert judgement, not fitted to data.

| Category | Weight | Why |
|---|---|---|
| `credential_request` | 0.70 | Requests for OTPs, passwords, PINs are the strongest single indicator |
| `payment_redirection` | 0.65 | Changed bank details / "pay this other account" |
| `remote_access_request` | 0.60 | Asks the recipient to install remote-access / screen-sharing software or read out its code |
| `payment_request` | 0.60 | Money, gift cards, crypto |
| `authority_impersonation` | 0.55 | Claims to be police, a bank officer, a government body |
| `threat_coercion` | 0.55 | Arrest, closure, harm, legal threats |
| `fake_interface` | 0.55 | (video) fake payment/login/bank/government screen |
| `advance_fee_or_prize` | 0.50 | Unexpected prize, refund or investment that needs a fee or payment first |
| `suspicious_link` | 0.50 | A link the recipient is pushed to open |
| `organization_impersonation` | 0.45 | Claims to be a company, courier or support desk (below official authorities) |
| `impersonation_relationship` | 0.45 | Claims to be a relative, boss or known contact |
| `isolation_secrecy` | 0.40 | "Don't tell anyone" is a classic coercion marker |
| `visual_manipulation` | 0.35 | (video) possible manipulation; observation only, can never reach *high risk* by itself |
| `urgency_pressure` | 0.30 | Common in legitimate messages too, so weak alone |
| `synthetic_voice_indicator` | 0.20 | Cannot be proven from audio; can never reach *suspicious* alone |
| `other_suspicious` | 0.20 | Catch-all |
| `scripted_delivery` | 0.15 | Many legitimate calls are scripted |

Severity factors: `low` 0.35, `medium` 0.60, `high` 0.85, `critical` 1.00.
Reliability factors: `weak` 0.50, `moderate` 0.75, `strong` 1.00.

## 4. Scoring formula

1. **Signal impact** `p = CATEGORY_WEIGHT × SEVERITY × RELIABILITY × CONTEXT_FACTOR` (0..1). Reliability and
   severity both scale the impact, so weak or ambiguous evidence counts for less. The context factor (added in
   Phase 7, see below) discounts evidence that is only being discussed or told as fiction.
2. **Same event counted once** (see §5). An event's impact is its strongest member's impact.
3. **Diminishing returns within a category.** Independent events of one category are sorted by impact;
   the *k*-th is multiplied by `0.5^k`, and the category total is capped at `1.5 × CATEGORY_WEIGHT`.
   Ten independent urgency statements score the same as three: at most `0.45`.
4. **Categories compound** as independent corroboration: `raw = 1 − Π(1 − category_total)`.
5. **Score** `= floor(raw × 100 + 0.5)`, clamped to 0..100.

**Context factors (Phase 7).** `directed_at_recipient` 1.00, `unclear` 0.60, `discussed_or_described` 0.10,
`fictional_or_example` 0.10. A critical, strong OTP request that a news article merely *describes* therefore
scores about 7, not 70, while the identical request addressed to the reader scores 70. This is a deliberate
extension of the Phase 4 formula (one more multiplier); thresholds, weights of the existing categories and the
other steps are unchanged. The factor is applied by the server to the model's `context` label, so the model
still never produces a score.

**Confidence** is derived only from directed (or `unclear`) evidence: when every signal is discussed or
fictional there is no confidence value (`null`) rather than a misleading high one.

The result is deterministic and independent of signal order (tested, including shuffled and randomised
inputs). Example scores: a weak mention of an "official app" → 10; one moderate payment request → 27;
urgency + payment, both moderate → 37; one strong OTP request → 60-70; an authority claim with threats,
secrecy and a payment demand → 96.

## 5. Deduplication

Within one category, two signals are the **same event** if any holds:

- same `name` and same normalised description (exact duplicate);
- their normalised quotes are equal or one contains the other (both ≥ 8 characters, so generic quotes
  such as "now" do not merge);
- their time ranges overlap or are within 2 seconds;
- same `name` and neither has a quote or timestamp.

Same-event signals form one group counted at its strongest member; the others are reported as
`corroborating_signals`. Signals of different categories are never merged, even with the same quote
(asking for money *and* demanding secrecy in one sentence are two behaviours). Distinct events stay
distinct in `signals`, each with its `impact`.

## 6. Thresholds and verdicts

| Score | Verdict |
|---|---|
| 0-24 | `low_risk` |
| 25-59 | `suspicious` |
| 60-100 | `high_risk` |

Defined once, in `app/schemas/verdict.py`. The backend only emits the canonical values; the dashboard
maps them to labels ("Low Risk" …). The public response validator rejects any response whose verdict
does not match its score, so "score 5, High Risk" cannot be constructed.

## 7. Status, failures and missing evidence

`status` is separate from `verdict`: `completed` (verdict ∈ low_risk / suspicious / high_risk) or
`unable_to_assess` (verdict `unable_to_assess`, **`risk_score: null`**, `confidence: null`,
`signal_scores: null`, no signals, and a `reason_code`). A failed analysis never carries a score and is
never Low Risk. HTTP status is 200 for both; input errors keep their 4xx codes with `{"detail": …}`.

| `reason_code` | Cause |
|---|---|
| `CONFIGURATION_ERROR` | No `GEMINI_API_KEY`, bad model id (404), client could not be built |
| `PROVIDER_AUTH_ERROR` | 401/403, or an invalid-key 400 |
| `PROVIDER_RATE_LIMITED` | 429 |
| `PROVIDER_TIMEOUT` | Deadline, client timeout, 408/504 |
| `PROVIDER_UNAVAILABLE` | Connection failure, 5xx |
| `PROVIDER_ERROR` | Other provider error (e.g. a 400 rejecting the request) |
| `INVALID_PROVIDER_RESPONSE` | Malformed JSON, invalid evidence, empty reply, evidence impossible for the mode |
| `INSUFFICIENT_EVIDENCE` | Coverage `insufficient`, or `partial` with a low-risk-band score |
| `MEDIA_PROCESSING_FAILED` | Local file handling failed |
| `INTERNAL_ANALYSIS_ERROR` | Unexpected server error |

**Missing evidence is not safety.** The provider reports `coverage`. `insufficient` → unable_to_assess.
`partial` can still raise a warning (a scam found in the readable part is still a scam) but can never
clear content: a partial analysis whose score is in the low-risk band becomes `INSUFFICIENT_EVIDENCE`,
because the part that was not understood may contain the scam. An empty signal list is `low_risk` only
with `full` coverage, and then no confidence is claimed. Evidence that fails validation is never
silently dropped.

## 8. Confidence

`confidence` (0..1, or `null`) is how strongly the *validated evidence* supports the assessment. It is
**not** a probability that content is a scam. `confidence = mean_reliability × corroboration × coverage`,
capped at 0.95, where the mean is weighted by event impact; corroboration is 0.70 / 0.85 / 1.0 for 1 / 2 /
3+ **different categories** of evidence (repetition of one kind never raises it); coverage is 1.0 (full)
or 0.5 (partial). With no events it is `null`. `confidence_level`: ≥ 0.75 High, ≥ 0.5 Medium, else Low.

## 9. Limitations

- The engine is only as good as the evidence. Gemini may miss indicators, over-report them, or be
  manipulated by the content itself; the prompt instructs it to treat content as untrusted data, but that
  is not a guarantee.
- Weights, factors and the 2-second event window are expert judgement and have not been calibrated
  against labelled data. Expect both false positives and false negatives.
- `coverage` is self-reported by the model. A model that wrongly claims `full` can yield a low score for
  content it did not understand.
- Synthetic-voice and scripted-delivery indicators are weak supporting evidence by design. ScamShield does
  not verify who is speaking and cannot prove audio or video is synthetic.
- Quote matching is textual; a paraphrased repeat of the same request without a shared quote or timestamp
  may count as an independent event (bounded by the per-category cap).
- Text, audio and (backend only) video are implemented. Video evidence is scored by this same engine.
- Nothing here has been run against the real Gemini API in this project's tests (all provider calls are
  mocked); real-model behaviour is unverified.
