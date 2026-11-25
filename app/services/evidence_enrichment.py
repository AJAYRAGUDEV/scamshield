"""Server-side steps applied to the AI model's evidence BEFORE the risk engine sees it.

* ``add_url_evidence``  - appends the structural link findings computed by ``url_analysis`` as evidence.
* ``verify_timestamps`` - drops audio timestamps that fall outside the recording's real length (the model must not
                          be allowed to display an invented time).
* ``build_limitations`` - the honest "what this analysis could NOT do" list shown to the user.

None of this changes the scoring rules: it only decides which validated evidence reaches ``risk_engine.assess``.
"""
from __future__ import annotations

from app.schemas.evidence import DIRECTED, MAX_LIMITATIONS, MAX_SIGNALS, EvidenceReport, EvidenceSignal
from app.services.url_analysis import STRUCTURE_ONLY, UrlAnalysis

TIMESTAMP_TOLERANCE_SECONDS = 1.0
MAX_TOTAL_LIMITATIONS = 10


def add_url_evidence(report: EvidenceReport, urls: UrlAnalysis) -> EvidenceReport:
    """Return the report with the scored link patterns appended as ``suspicious_link`` evidence.

    The signals' context is 'directed' when the model found at least one directed signal, otherwise 'unclear': a link
    pattern inside content the model judged to be mere discussion must not be scored at full strength.
    """
    if not urls.signals:
        return report
    context = DIRECTED if any(s.context == DIRECTED for s in report.signals) else "unclear"
    room = MAX_SIGNALS - len(report.signals)
    added = [EvidenceSignal(**data, context=context) for data in urls.signals[: max(room, 0)]]
    return report.model_copy(update={"signals": [*report.signals, *added]})


def verify_timestamps(report: EvidenceReport, duration_seconds: float | None) -> tuple[EvidenceReport, int]:
    """Remove timestamps that exceed the recording's length. Returns (report, number of signals changed).

    With an unknown duration nothing can be verified and the report is returned unchanged.
    """
    if duration_seconds is None:
        return report, 0
    limit = duration_seconds + TIMESTAMP_TOLERANCE_SECONDS
    changed = 0
    signals: list[EvidenceSignal] = []
    for signal in report.signals:
        if signal.timestamp_start is not None and signal.timestamp_start > limit:
            signal = signal.model_copy(update={"timestamp_start": None, "timestamp_end": None})
            changed += 1
        elif signal.timestamp_end is not None and signal.timestamp_end > limit:
            signal = signal.model_copy(update={"timestamp_end": None})
            changed += 1
        signals.append(signal)
    return report.model_copy(update={"signals": signals}), changed


def build_limitations(
    report: EvidenceReport,
    *,
    mode: str,
    urls: UrlAnalysis | None = None,
    timestamps_removed: int = 0,
    duration_known: bool = False,
) -> list[str]:
    """Plain-English limitations: the server's own first, then the model's (already length-bounded)."""
    notes: list[str] = []
    if report.coverage == "partial":
        notes.append("Only part of the content could be understood, so this assessment is less certain.")
    if mode == "text" and urls and urls.findings:
        notes.append("Links were examined by structure only. They were not visited and domain reputation has not been verified.")
    if mode in ("audio", "video"):
        noun = "recording" if mode == "audio" else "video"
        if timestamps_removed:
            notes.append(f"Some timestamps were removed because they fell outside the length of the {noun}.")
        if any(s.timestamp_start is not None for s in report.signals):
            notes.append(
                f"Timestamps are approximate and come from the AI model; they were only checked against the {noun}'s length."
                if duration_known else
                f"Timestamps are approximate and come from the AI model; they could not be checked against the {noun}."
            )
    if mode == "audio":
        notes.append("The analysis considers what is said and how it sounds. It does not identify who is speaking or prove that a voice is real or synthetic.")
    if mode == "video":
        notes.append("The video was examined for what is shown, written on screen and said. It does not identify people, authenticate voices, documents or sources, or prove that a video is genuine or manipulated.")
        if any(s.category == "visual_manipulation" for s in report.signals):
            notes.append("Possible editing or manipulation indicators are observations only: they are not proof that the video is fake or fraudulent.")
    if report.language_uncertain:
        notes.append("The language could not be interpreted with confidence, so findings may be less reliable.")
    elif report.language and not report.language.lower().startswith("english"):
        notes.append(f"Detection quality can differ by language: {report.language} content may be analysed less accurately than English.")

    ordered: list[str] = []
    for note in [*notes, *report.limitations]:
        if note and note not in ordered:
            ordered.append(note)
    return ordered[:MAX_TOTAL_LIMITATIONS]


__all__ = ["add_url_evidence", "verify_timestamps", "build_limitations", "STRUCTURE_ONLY", "MAX_LIMITATIONS"]
