"""Cheap, local facts about an uploaded audio file: its length and whether it is silent.

Used for two things only:
* **Silence guard.** A recording that is entirely silent is refused as ``INSUFFICIENT_EVIDENCE`` without calling
  the AI model: silence is not evidence of safety, and a model asked to analyse nothing may invent something.
* **Timestamp check.** The AI model reports timestamps; knowing the real length lets the server drop any timestamp
  that falls outside the recording instead of displaying an invented one.

Only uncompressed PCM WAV is inspected (its header states the length exactly and its samples can be read cheaply).
For every other format both facts are unknown (``None``), and the server then neither refuses the file nor
verifies its timestamps: it says so in the response's limitations. Nothing here decodes compressed audio, and the
file is read in 1 MB chunks, never fully loaded into memory.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

SILENCE_THRESHOLD_16BIT = 8       # |sample| of 8/32768 (about -72 dBFS) or below counts as silence
SILENCE_THRESHOLD_8BIT = 2        # unsigned 8-bit: within +-2 of the 128 midpoint
CHUNK = 1024 * 1024


@dataclass(frozen=True)
class AudioFacts:
    duration_seconds: float | None = None
    silent: bool | None = None            # None = could not tell


def _read_wav_header(handle) -> tuple[int, int, int, int, int] | None:
    """(audio_format, channels, sample_rate, bits, data_size) with the file positioned at the sample data."""
    head = handle.read(12)
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    fmt = None
    for _ in range(64):                                  # bounded chunk walk
        header = handle.read(8)
        if len(header) < 8:
            return None
        chunk_id, size = header[:4], struct.unpack("<I", header[4:])[0]
        if chunk_id == b"fmt " and size >= 16:
            body = handle.read(size)
            audio_format, channels, rate, _byte_rate, _align, bits = struct.unpack("<HHIIHH", body[:16])
            if audio_format == 0xFFFE and len(body) >= 26:            # WAVE_FORMAT_EXTENSIBLE: real format in the sub-format
                audio_format = struct.unpack("<H", body[24:26])[0]
            fmt = (audio_format, channels, rate, bits)
            if size % 2:
                handle.read(1)
        elif chunk_id == b"data":
            if fmt is None:
                return None
            return (*fmt, size)
        else:
            handle.seek(size + (size % 2), 1)
    return None


def inspect_audio(path: Path, kind: str | None) -> AudioFacts:
    """Facts about the file at ``path``. Never raises; unknown facts are None."""
    if kind != "wav":
        return AudioFacts()
    try:
        total = path.stat().st_size
        with open(path, "rb") as handle:
            header = _read_wav_header(handle)
            if header is None:
                return AudioFacts()
            audio_format, channels, rate, bits, declared = header
            start = handle.tell()
            data_size = min(declared, total - start) if declared not in (0, 0xFFFFFFFF) else total - start
            bytes_per_second = rate * channels * (bits // 8) if rate and channels and bits else 0
            duration = data_size / bytes_per_second if bytes_per_second else None
            if audio_format != 1 or bits not in (8, 16) or data_size <= 0:
                return AudioFacts(duration_seconds=duration)
            remaining = data_size
            peak = 0
            while remaining > 0:
                chunk = handle.read(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                if bits == 16:
                    usable = len(chunk) - (len(chunk) % 2)
                    samples = memoryview(chunk[:usable]).cast("h")
                    peak = max(peak, max(samples, default=0), -min(samples, default=0))
                    if peak > SILENCE_THRESHOLD_16BIT:
                        return AudioFacts(duration_seconds=duration, silent=False)
                else:
                    peak = max(peak, max(abs(b - 128) for b in chunk))
                    if peak > SILENCE_THRESHOLD_8BIT:
                        return AudioFacts(duration_seconds=duration, silent=False)
            return AudioFacts(duration_seconds=duration, silent=True)
    except (OSError, struct.error, ValueError):
        return AudioFacts()
