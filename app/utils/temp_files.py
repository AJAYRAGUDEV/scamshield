"""Safe handling of uploaded media on local disk.

* A DEDICATED directory (``TEMP_UPLOAD_DIR``, default ``<system temp>/scamshield-uploads``) holds every
  upload, in its own unpredictable sub-directory (``mkdtemp``: random name, mode 0700 on POSIX).
* The file inside has a RANDOM server-chosen name (``secrets.token_hex``) plus a validated extension;
  the client's filename never reaches a path. It is created exclusively (``O_EXCL``) with mode 0600.
  (Windows ignores POSIX modes; there the per-user temp directory's ACLs apply.)
* The directory is removed on success, on validation failure, on provider failure, on timeout and on
  cancellation, including partially written files. Nothing is stored permanently, and nothing here is
  ever exposed through a static route (the frontend is served from a different directory).
* ``sweep_stale_uploads`` removes leftovers from a crashed process at start-up.
* Writes happen in a worker thread (``asyncio.to_thread``) so large uploads do not stall the loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable

from fastapi import UploadFile

from app.core.errors import AnalysisError, InputError, InvalidInput, PayloadTooLarge, ReasonCode
from app.core.logging import log_event

logger = logging.getLogger("scamshield.files")

CHUNK_BYTES = 1024 * 1024
HEAD_BYTES = 4096
UPLOAD_DIR_PREFIX = "up-"
_SAFE_SUFFIX = re.compile(r"\.[a-z0-9]{1,8}")


@dataclass(frozen=True)
class SavedUpload:
    path: Path
    size: int
    kind: str | None = None        # container type reported by the content check, if one was supplied
    work_dir: Path | None = None   # the private directory holding ``path`` (removed by its owner)


def _safe_suffix(suffix: str) -> str:
    suffix = suffix.lower()
    return suffix if _SAFE_SUFFIX.fullmatch(suffix) else ""


def ensure_upload_dir(base: Path) -> Path:
    """Create the dedicated upload directory (owner-only where the OS supports it)."""
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass                                                   # not supported on this platform/filesystem
    return base


def sweep_stale_uploads(base: Path, max_age_seconds: float = 3600.0) -> int:
    """Remove ``up-*`` directories older than ``max_age_seconds`` (crash leftovers). Never raises."""
    removed = 0
    try:
        entries = list(base.iterdir()) if base.is_dir() else []
    except OSError:
        return 0
    cutoff = time.time() - max_age_seconds
    for entry in entries:
        try:
            if entry.name.startswith(UPLOAD_DIR_PREFIX) and entry.is_dir() and not entry.is_symlink() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 0 if entry.exists() else 1
        except OSError:
            continue
    if removed:
        log_event(logger, logging.INFO, "temp_sweep", removed=removed)
    return removed


def _remove_tree(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except Exception as exc:
        log_event(logger, logging.WARNING, "temp_cleanup_error", error=type(exc).__name__)
        shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        log_event(logger, logging.WARNING, "temp_cleanup_incomplete")


def _open_exclusive(path: Path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    return os.fdopen(os.open(path, flags, 0o600), "wb")


def _make_work_dir(upload_dir: Path | None) -> Path:
    try:
        base = ensure_upload_dir(upload_dir or Path(tempfile.gettempdir()) / "scamshield-uploads")
        return Path(tempfile.mkdtemp(prefix=UPLOAD_DIR_PREFIX, dir=base))
    except OSError as exc:
        raise AnalysisError(ReasonCode.MEDIA_PROCESSING_FAILED, f"tempdir: {type(exc).__name__}") from exc


async def _stream_to_file(
    upload: UploadFile, work_dir: Path, *, suffix: str, max_bytes: int, too_large_message: str, empty_message: str,
    inspect_head: Callable[[bytes], str | None] | None,
) -> SavedUpload:
    """Copy ``upload`` into ``work_dir`` under a random name, enforcing the size cap and the content check as it streams."""
    path = work_dir / (secrets.token_hex(16) + _safe_suffix(suffix))
    size = 0
    kind: str | None = None
    try:
        with _open_exclusive(path) as out:
            first = True
            while chunk := await upload.read(CHUNK_BYTES):
                size += len(chunk)
                if size > max_bytes:
                    raise PayloadTooLarge(too_large_message)
                if first:
                    first = False
                    if inspect_head is not None:
                        kind = inspect_head(chunk[:HEAD_BYTES])
                await asyncio.to_thread(out.write, chunk)
    except InputError:
        raise
    except OSError as exc:
        raise AnalysisError(ReasonCode.MEDIA_PROCESSING_FAILED, f"write: {type(exc).__name__}") from exc
    if size == 0:
        raise InvalidInput(empty_message)
    return SavedUpload(path=path, size=size, kind=kind, work_dir=work_dir)


def remove_work_dir(work_dir: Path) -> None:
    """Delete an upload's private directory. Never raises; safe to call twice."""
    _remove_tree(work_dir)


async def store_upload(
    upload: UploadFile,
    *,
    suffix: str,
    max_bytes: int,
    too_large_message: str,
    empty_message: str = "Uploaded file is empty.",
    upload_dir: Path | None = None,
    inspect_head: Callable[[bytes], str | None] | None = None,
) -> SavedUpload:
    """Like ``saved_upload`` but the CALLER owns the result: the private directory survives until the caller calls
    ``remove_work_dir`` (used by background jobs that outlive the request). On ANY failure, including cancellation,
    the directory is removed here before the exception propagates."""
    work_dir = _make_work_dir(upload_dir)
    try:
        return await _stream_to_file(upload, work_dir, suffix=suffix, max_bytes=max_bytes, too_large_message=too_large_message,
                                     empty_message=empty_message, inspect_head=inspect_head)
    except BaseException:
        _remove_tree(work_dir)
        raise


@asynccontextmanager
async def saved_upload(
    upload: UploadFile,
    *,
    suffix: str,
    max_bytes: int,
    too_large_message: str,
    empty_message: str = "Uploaded file is empty.",
    upload_dir: Path | None = None,
    inspect_head: Callable[[bytes], str | None] | None = None,
) -> AsyncIterator[SavedUpload]:
    """Stream ``upload`` to a private file, enforce the size cap, validate its content, always clean up.

    ``inspect_head`` receives the first bytes and must raise (e.g. ``UnsupportedMedia``) to refuse the
    upload; the file is then deleted before anything else is read. Raises ``PayloadTooLarge`` /
    ``InvalidInput`` for caller errors and ``AnalysisError(MEDIA_PROCESSING_FAILED)`` for local I/O failures.
    """
    work_dir = _make_work_dir(upload_dir)
    try:
        yield await _stream_to_file(upload, work_dir, suffix=suffix, max_bytes=max_bytes, too_large_message=too_large_message,
                                    empty_message=empty_message, inspect_head=inspect_head)
    finally:
        _remove_tree(work_dir)
