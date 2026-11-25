"""Reusable validation of uploaded media. Nothing the client says is trusted.

Layers, in order:
1. File NAME: no path separators, control characters or NUL bytes (path-traversal shapes are rejected,
   although the name is never used to build a path anyway).
2. EXTENSION: must be on the allowlist for the kind of upload.
3. DECLARED MIME type: must be one that is plausible for that extension. ``application/octet-stream`` /
   empty ("type unknown") is tolerated ONLY because step 4 decides.
4. CONTENT: the first bytes (magic numbers) must identify a supported container AND that container must
   be the one the extension claims. A renamed executable, archive, script or image fails here.
Size and emptiness are enforced while the upload is streamed to disk (``app/utils/temp_files.py``).

Limitations (see docs/security.md): a signature proves the container type, not that the file decodes
or that an ``.m4a`` holds audio rather than video. Deep checks (duration, streams) need ffprobe, which
arrives with the video phase. The video allowlist is defined and tested here but no route uses it yet.
"""
from __future__ import annotations

from app.core.errors import InvalidInput, UnsupportedMedia

HEAD_BYTES = 4096
MAX_FILENAME_CHARS = 255
MIN_FILE_BYTES = 12

AUDIO_MESSAGE = "Unsupported audio format. Use MP3, WAV, M4A, OGG, or WEBM."
VIDEO_MESSAGE = "Unsupported video format. Use MP4, MOV, or WEBM."
CONTENT_MESSAGE = "The file content does not match the type it claims to be, or it is truncated or corrupt."

# extension -> declared MIME types that are plausible for it
AUDIO_MIME_TYPES: dict[str, frozenset[str]] = {
    ".mp3": frozenset({"audio/mpeg", "audio/mp3"}),
    ".wav": frozenset({"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"}),
    ".m4a": frozenset({"audio/mp4", "audio/m4a", "audio/x-m4a"}),
    ".ogg": frozenset({"audio/ogg", "application/ogg"}),
    ".webm": frozenset({"audio/webm", "video/webm"}),
}
VIDEO_MIME_TYPES: dict[str, frozenset[str]] = {
    ".mp4": frozenset({"video/mp4"}),
    ".mov": frozenset({"video/quicktime"}),
    ".webm": frozenset({"video/webm"}),
}
GENERIC_MIME_TYPES = frozenset({"application/octet-stream", ""})

# extension -> container kinds (detected from content) that are acceptable for it
AUDIO_KINDS: dict[str, frozenset[str]] = {
    ".mp3": frozenset({"mp3"}), ".wav": frozenset({"wav"}), ".m4a": frozenset({"mp4"}),
    ".ogg": frozenset({"ogg"}), ".webm": frozenset({"webm"}),
}
VIDEO_KINDS: dict[str, frozenset[str]] = {
    ".mp4": frozenset({"mp4"}), ".mov": frozenset({"mov"}), ".webm": frozenset({"webm"}),
}

# ISO-BMFF "major brands" that real audio/video files use. Others (e.g. heic images) are refused.
_MP4_BRANDS = frozenset({b"M4A ", b"M4B ", b"M4P ", b"M4V ", b"mp41", b"mp42", b"isom", b"iso2", b"iso4", b"iso5",
                         b"iso6", b"avc1", b"dash", b"mmp4", b"MSNV", b"f4v ", b"f4a "})
_QUICKTIME_BRAND = b"qt  "


def safe_filename(filename: str | None) -> str:
    """Return the filename if it has a harmless shape; raise ``InvalidInput`` for traversal-looking names."""
    name = filename or ""
    if len(name) > MAX_FILENAME_CHARS:
        raise InvalidInput("Invalid file name.")
    if any(ch in name for ch in ("/", "\\")) or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise InvalidInput("Invalid file name.")
    if name in (".", ".."):
        raise InvalidInput("Invalid file name.")
    return name


def _suffix(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def _check_metadata(filename: str | None, content_type: str | None, mime_table: dict[str, frozenset[str]], message: str) -> str:
    name = safe_filename(filename)
    suffix = _suffix(name)
    if suffix not in mime_table:
        raise UnsupportedMedia(message)
    declared = (content_type or "").split(";", 1)[0].strip().lower()
    if declared not in mime_table[suffix] and declared not in GENERIC_MIME_TYPES:
        raise UnsupportedMedia("The declared file type does not match the file extension.")
    return suffix


def validate_audio_metadata(filename: str | None, content_type: str | None) -> str:
    """Return the validated (lower-case) extension of an audio upload."""
    return _check_metadata(filename, content_type, AUDIO_MIME_TYPES, AUDIO_MESSAGE)


def validate_video_metadata(filename: str | None, content_type: str | None) -> str:
    """Return the validated extension of a video upload (route arrives in the video phase)."""
    return _check_metadata(filename, content_type, VIDEO_MIME_TYPES, VIDEO_MESSAGE)


def detect_kind(head: bytes) -> str | None:
    """Identify a supported container from its first bytes, or None. Never raises."""
    if len(head) < MIN_FILE_BYTES:
        return None
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav" if b"fmt " in head[12:HEAD_BYTES] else None
    if head[:4] == b"OggS":
        return "ogg" if head[4] == 0 and len(head) >= 27 else None
    if head[:4] == b"\x1a\x45\xdf\xa3":                       # EBML: WebM or Matroska
        return "webm" if b"webm" in head[:64] else None        # Matroska (.mkv) is not supported
    if head[4:8] == b"ftyp":
        size = int.from_bytes(head[:4], "big")
        if size != 1 and size != 0 and size < 12:
            return None
        brand = head[8:12]
        if brand == _QUICKTIME_BRAND:
            return "mov"
        return "mp4" if brand in _MP4_BRANDS else None
    if head[:3] == b"ID3":                                     # ID3v2 tag in front of MPEG audio
        sizes = head[6:10]
        return "mp3" if head[3] in (2, 3, 4) and head[4] != 0xFF and all(b < 0x80 for b in sizes) else None
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:           # MPEG audio frame sync
        version, layer = (head[1] >> 3) & 3, (head[1] >> 1) & 3
        bitrate, rate = (head[2] >> 4) & 0xF, (head[2] >> 2) & 3
        return "mp3" if version != 1 and layer != 0 and bitrate != 15 and rate != 3 else None
    return None


def _check_content(head: bytes, suffix: str, kinds: dict[str, frozenset[str]]) -> str:
    kind = detect_kind(head)
    if kind is None or kind not in kinds.get(suffix, frozenset()):
        raise UnsupportedMedia(CONTENT_MESSAGE)
    return kind


def check_audio_content(head: bytes, suffix: str) -> str:
    """Validate an audio upload's leading bytes against its extension. Returns the detected kind."""
    return _check_content(head, suffix, AUDIO_KINDS)


def check_video_content(head: bytes, suffix: str) -> str:
    return _check_content(head, suffix, VIDEO_KINDS)
