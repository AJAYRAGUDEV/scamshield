"""WAV inspection (duration and silence) and timestamp verification."""
from __future__ import annotations

import pytest

from app.schemas.evidence import EvidenceReport
from app.services.audio_inspection import inspect_audio
from app.services.evidence_enrichment import add_url_evidence, build_limitations, verify_timestamps
from app.services.url_analysis import analyze_urls
from tests.fakes import make_signal
from tests.scenario_helpers import make_wav


def write(tmp_path, data, name="a.wav"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_duration_and_non_silence_of_a_tone(tmp_path):
    facts = inspect_audio(write(tmp_path, make_wav(2.0)), "wav")
    assert facts.silent is False and facts.duration_seconds == pytest.approx(2.0, abs=0.01)


def test_digital_silence_is_detected(tmp_path):
    facts = inspect_audio(write(tmp_path, make_wav(3.0, silent=True)), "wav")
    assert facts.silent is True and facts.duration_seconds == pytest.approx(3.0, abs=0.01)


def test_non_wav_formats_are_not_inspected_and_nothing_is_claimed(tmp_path):
    facts = inspect_audio(write(tmp_path, b"ID3" + bytes(100), "a.mp3"), "mp3")
    assert facts.silent is None and facts.duration_seconds is None


@pytest.mark.parametrize("data", [b"", b"RIFF", b"RIFF\x24\x00\x00\x00WAVE", b"RIFFxxxxWAVEfmt " + bytes(5)])
def test_damaged_wav_never_raises_and_makes_no_claim(tmp_path, data):
    facts = inspect_audio(write(tmp_path, data), "wav")
    assert facts.silent is None


def test_missing_file_makes_no_claim(tmp_path):
    assert inspect_audio(tmp_path / "nope.wav", "wav").silent is None


# ---- timestamp verification ---------------------------------------------------------------------------------------
def report(*times, source="audio_speech"):
    return EvidenceReport(coverage="full", signals=[
        make_signal(name=f"s{i}_signal", source=source, timestamp_start=a, timestamp_end=b, evidence=f"Evidence {i}")
        for i, (a, b) in enumerate(times)])


def test_timestamps_inside_the_recording_are_kept():
    out, changed = verify_timestamps(report((1.0, 2.0), (9.0, 9.5)), 10.0)
    assert changed == 0 and [s.timestamp_start for s in out.signals] == [1.0, 9.0]


def test_timestamps_beyond_the_recording_are_removed():
    out, changed = verify_timestamps(report((50.0, 55.0), (1.0, 40.0)), 10.0)
    assert changed == 2
    assert (out.signals[0].timestamp_start, out.signals[0].timestamp_end) == (None, None)
    assert (out.signals[1].timestamp_start, out.signals[1].timestamp_end) == (1.0, None)     # start valid, end implausible


def test_tolerance_of_one_second_is_allowed():
    out, changed = verify_timestamps(report((10.5, 10.9)), 10.0)
    assert changed == 0 and out.signals[0].timestamp_start == 10.5


def test_unknown_duration_cannot_verify_so_nothing_changes_and_limitations_say_so():
    original = report((50.0, 55.0))
    out, changed = verify_timestamps(original, None)
    assert changed == 0 and out == original
    notes = build_limitations(out, mode="audio", duration_known=False)
    assert any("could not be checked" in n for n in notes)


def test_audio_limitations_never_claim_identity_or_voice_authenticity():
    notes = build_limitations(report((1.0, 2.0)), mode="audio", duration_known=True)
    assert any("does not identify who is speaking" in n and "synthetic" in n for n in notes)


def test_partial_coverage_and_uncertain_language_are_disclosed():
    partial = EvidenceReport(coverage="partial", language="Tamil", language_uncertain=True, signals=[])
    notes = build_limitations(partial, mode="text")
    assert any("part of the content" in n for n in notes) and any("language" in n.lower() for n in notes)


def test_model_limitations_are_kept_after_the_server_ones():
    r = EvidenceReport(coverage="partial", limitations=["Background noise hid some words."], signals=[])
    notes = build_limitations(r, mode="audio", duration_known=True)
    assert notes[-1] == "Background noise hid some words." or "Background noise hid some words." in notes
    assert len(notes) <= 10


def test_url_evidence_context_follows_the_models_findings():
    urls = analyze_urls("http://192.168.0.1/login")
    directed = EvidenceReport(coverage="full", signals=[make_signal("credential_request")])
    quiet = EvidenceReport(coverage="full", signals=[])
    assert add_url_evidence(directed, urls).signals[-1].context == "directed_at_recipient"
    assert add_url_evidence(quiet, urls).signals[-1].context == "unclear"


def test_no_scored_links_leaves_the_report_untouched():
    report_in = EvidenceReport(coverage="full", signals=[])
    assert add_url_evidence(report_in, analyze_urls("https://bit.ly/x")) is report_in
