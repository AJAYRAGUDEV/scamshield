from __future__ import annotations

import asyncio
import io
import os
import sys
import time
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile

from app.core.errors import AnalysisError, InvalidInput, PayloadTooLarge, ReasonCode, UnsupportedMedia
from app.utils import temp_files
from app.utils.temp_files import saved_upload, sweep_stale_uploads


def upload(data: bytes, filename: str = "clip.wav") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=filename)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def base(tmp_path) -> Path:
    return tmp_path / "uploads"


def kwargs(base, **over):
    return {"suffix": ".wav", "max_bytes": 100, "too_large_message": "too big", "upload_dir": base, **over}


def left_behind(base: Path) -> list[Path]:
    return list(base.rglob("*")) if base.exists() else []


# ------------------------------------------------------------------ layout and naming
def test_saves_into_the_dedicated_directory_and_removes_everything_afterwards(base):
    seen = {}

    async def go():
        async with saved_upload(upload(b"hello"), **kwargs(base)) as saved:
            seen["bytes"] = saved.path.read_bytes()
            seen["path"] = saved.path
            seen["size"] = saved.size

    run(go())
    assert seen["bytes"] == b"hello" and seen["size"] == 5
    assert base in seen["path"].parents and seen["path"].parent.parent == base
    assert left_behind(base) == []


def test_file_and_directory_names_are_random_and_unrelated_to_the_client_filename(base):
    names = []

    async def go():
        for _ in range(5):
            async with saved_upload(upload(b"x", filename="../../evil name.wav"), **kwargs(base)) as saved:
                names.append((saved.path.parent.name, saved.path.name))

    run(go())
    dirs, files = zip(*names)
    assert len(set(dirs)) == 5 and len(set(files)) == 5                      # unpredictable and unique
    assert all(d.startswith("up-") for d in dirs)
    assert all(f.endswith(".wav") and len(f) == 36 and "evil" not in f for f in files)   # 32 hex + ".wav"


def test_unsafe_suffix_is_dropped_and_the_path_stays_inside_the_private_directory(base):
    seen = {}

    async def go():
        async with saved_upload(upload(b"x"), **kwargs(base, suffix="/../x")) as saved:
            seen["path"] = saved.path

    run(go())
    assert "." not in seen["path"].name and base in seen["path"].parents


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits are not enforced on Windows")
def test_permissions_are_owner_only(base):
    modes = {}

    async def go():
        async with saved_upload(upload(b"x"), **kwargs(base)) as saved:
            modes["file"] = saved.path.stat().st_mode & 0o777
            modes["dir"] = saved.path.parent.stat().st_mode & 0o777
            modes["base"] = base.stat().st_mode & 0o777

    run(go())
    assert modes == {"file": 0o600, "dir": 0o700, "base": 0o700}


def test_the_file_is_created_exclusively_never_overwriting_an_existing_file(base, monkeypatch):
    monkeypatch.setattr(temp_files.secrets, "token_hex", lambda n: "a" * 32)
    seen = []

    async def go():
        async with saved_upload(upload(b"one"), **kwargs(base)) as first:
            seen.append(first.path)

    run(go())
    existing = seen[0]
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"victim")
    monkeypatch.setattr(temp_files.tempfile, "mkdtemp", lambda **kw: str(existing.parent))   # force a name collision

    async def collide():
        async with saved_upload(upload(b"two"), **kwargs(base)):
            pass

    with pytest.raises(AnalysisError) as exc:
        run(collide())
    assert exc.value.reason_code == ReasonCode.MEDIA_PROCESSING_FAILED


# ------------------------------------------------------------------ cleanup in every situation
def test_cleanup_when_caller_raises(base):
    async def go():
        async with saved_upload(upload(b"x"), **kwargs(base)):
            raise RuntimeError("provider blew up")

    with pytest.raises(RuntimeError):
        run(go())
    assert left_behind(base) == []


def test_cleanup_when_task_is_cancelled(base):
    async def go():
        started = asyncio.Event()

        async def worker():
            async with saved_upload(upload(b"x"), **kwargs(base)):
                started.set()
                await asyncio.sleep(10)

        task = asyncio.create_task(worker())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    assert left_behind(base) == []


def test_oversize_is_rejected_and_the_partial_file_is_removed(base):
    async def go():
        async with saved_upload(upload(b"x" * 250), **kwargs(base, max_bytes=100)):
            pass

    chunk = temp_files.CHUNK_BYTES
    temp_files.CHUNK_BYTES = 64               # force a multi-chunk stream so a PARTIAL file exists when the cap trips
    try:
        with pytest.raises(PayloadTooLarge) as exc:
            run(go())
    finally:
        temp_files.CHUNK_BYTES = chunk
    assert exc.value.status_code == 413 and exc.value.detail == "too big" and left_behind(base) == []


def test_streamed_limit_stops_reading_once_exceeded(base):
    reads = []

    class Counting(UploadFile):
        async def read(self, size=-1):
            reads.append(size)
            return b"x" * 60

    async def go():
        async with saved_upload(Counting(file=io.BytesIO(), filename="a.wav"), **kwargs(base, max_bytes=100)):
            pass

    with pytest.raises(PayloadTooLarge):
        run(go())
    assert len(reads) == 2 and left_behind(base) == []        # 60 ok, 120 > 100 -> stop; never read the rest


def test_exact_limit_is_accepted(base):
    async def go():
        async with saved_upload(upload(b"x" * 100), **kwargs(base)) as saved:
            assert saved.size == 100

    run(go())


def test_empty_upload_is_rejected_and_cleaned(base):
    async def go():
        async with saved_upload(upload(b""), **kwargs(base, empty_message="empty!")):
            pass

    with pytest.raises(InvalidInput, match="empty!"):
        run(go())
    assert left_behind(base) == []


# ------------------------------------------------------------------ content inspection hook
def test_rejected_content_is_removed_before_more_is_read(base):
    reads = []

    class Counting(UploadFile):
        async def read(self, size=-1):
            reads.append(size)
            return b"MZ-not-audio" * 10

    def refuse(head: bytes):
        raise UnsupportedMedia("nope")

    async def go():
        async with saved_upload(Counting(file=io.BytesIO(), filename="a.wav"), **kwargs(base, inspect_head=refuse, max_bytes=1000)):
            pass

    with pytest.raises(UnsupportedMedia):
        run(go())
    assert len(reads) == 1 and left_behind(base) == []


def test_the_inspector_sees_only_the_leading_bytes_and_its_result_is_reported(base):
    heads = []

    def inspector(head: bytes):
        heads.append(head)
        return "wav"

    seen = {}

    async def go():
        async with saved_upload(upload(b"R" * 10_000), **kwargs(base, max_bytes=20_000, inspect_head=inspector)) as saved:
            seen["kind"] = saved.kind

    run(go())
    assert len(heads[0]) == temp_files.HEAD_BYTES and seen["kind"] == "wav"


# ------------------------------------------------------------------ failures
def test_disk_write_failure_is_a_media_processing_error_without_paths_and_cleaned(base, monkeypatch):
    def broken(*a, **k):
        raise PermissionError("disk says no: C:\\secret\\path")

    monkeypatch.setattr(temp_files, "_open_exclusive", broken)

    async def go():
        async with saved_upload(upload(b"x"), **kwargs(base)):
            pass

    with pytest.raises(AnalysisError) as exc:
        run(go())
    assert exc.value.reason_code == ReasonCode.MEDIA_PROCESSING_FAILED and "secret" not in exc.value.internal_detail
    assert left_behind(base) == []


def test_directory_creation_failure_is_a_media_processing_error(base, monkeypatch):
    def boom(*a, **k):
        raise OSError("no space")

    monkeypatch.setattr(temp_files.tempfile, "mkdtemp", boom)

    async def go():
        async with saved_upload(upload(b"x"), **kwargs(base)):
            pass

    with pytest.raises(AnalysisError) as exc:
        run(go())
    assert exc.value.reason_code == ReasonCode.MEDIA_PROCESSING_FAILED


def test_cleanup_errors_are_contained_and_retried_without_raising(base, monkeypatch, caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="scamshield")
    real = temp_files.shutil.rmtree
    calls = []

    def flaky(path, ignore_errors=False, **kw):
        calls.append(ignore_errors)
        if not ignore_errors:
            raise PermissionError("locked: C:\\secret\\dir")
        return real(path, ignore_errors=True)

    monkeypatch.setattr(temp_files.shutil, "rmtree", flaky)

    async def go():
        async with saved_upload(upload(b"x"), **kwargs(base)):
            pass

    run(go())                                              # must not raise
    assert calls == [False, True] and left_behind(base) == []
    assert "temp_cleanup_error" in caplog.text and "secret" not in caplog.text


# ------------------------------------------------------------------ start-up sweep
def make_stale(base: Path, name: str, age_seconds: float) -> Path:
    d = base / name
    d.mkdir(parents=True)
    (d / "leftover.bin").write_bytes(b"x")
    old = time.time() - age_seconds
    os.utime(d, (old, old))
    return d


def test_sweep_removes_only_old_upload_directories(base):
    old = make_stale(base, "up-old", 7200)
    fresh = make_stale(base, "up-fresh", 10)
    foreign = make_stale(base, "not-ours", 7200)
    assert sweep_stale_uploads(base, max_age_seconds=3600) == 1
    assert not old.exists() and fresh.exists() and foreign.exists()


def test_sweep_never_raises(tmp_path):
    assert sweep_stale_uploads(tmp_path / "missing") == 0
    assert sweep_stale_uploads(tmp_path / "a-file-not-a-dir") == 0
    (tmp_path / "a-file-not-a-dir").write_text("x")
    assert sweep_stale_uploads(tmp_path / "a-file-not-a-dir") == 0
