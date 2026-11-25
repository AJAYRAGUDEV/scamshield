"""Minimal byte strings that look like each supported container (enough for magic-byte checks) and a few
disguised files. Not playable media; the provider is always mocked."""
from __future__ import annotations

from tests.fakes import load_fixture_bytes

PAD = b"\x00" * 64


def wav() -> bytes:
    return load_fixture_bytes("tiny.wav")


def mp3_id3() -> bytes:
    return b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + PAD


def mp3_frame() -> bytes:
    return b"\xff\xfb\x90\x00" + PAD


def m4a() -> bytes:
    return b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A mp42isom" + PAD


def ogg() -> bytes:
    return b"OggS\x00\x02" + b"\x00" * 20 + b"\x01\x1e\x01vorbis" + PAD


def webm() -> bytes:
    return b"\x1a\x45\xdf\xa3\x9fB\x86\x81\x01B\xf7\x81\x01B\xf2\x81\x04B\xf3\x81\x08B\x82\x84webm" + PAD


def mp4() -> bytes:
    return b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + PAD


def mov() -> bytes:
    return b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  " + PAD


def matroska() -> bytes:
    return b"\x1a\x45\xdf\xa3\x93B\x82\x88matroska" + PAD


AUDIO_SAMPLES = {".wav": wav, ".mp3": mp3_id3, ".m4a": m4a, ".ogg": ogg, ".webm": webm}
AUDIO_MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".webm": "audio/webm"}

# Files that must never be accepted as audio, whatever their name or declared type.
DISGUISED = {
    "windows_executable": b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff" + PAD,
    "linux_executable": b"\x7fELF\x02\x01\x01\x00" + PAD,
    "zip_archive": b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + PAD,
    "shell_script": b"#!/bin/sh\nrm -rf /tmp/x\n" + PAD,
    "html_page": b"<!DOCTYPE html><html><script>alert(1)</script></html>" + PAD,
    "png_image": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + PAD,
    "pdf_document": b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + PAD,
    "heic_image": b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + PAD,
    "plain_text": b"just some text pretending to be a recording" + PAD,
    "truncated_wav": b"RIFF\x24\x00\x00\x00WAVE",
    "wav_without_fmt": b"RIFF\x24\x00\x00\x00WAVEdata" + PAD,
}
