"""No-production fixture for proposed read-only rejection observer lock (#321)."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

import obsidian_automation.production_io as production_io
from obsidian_automation.production_io import (
    CANONICAL_IO_LOCK_NAME,
    ProductionIOError,
    canonical_io_lock,
    canonical_io_lock_observer,
)


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "state"
    lock_dir = root / "24-Locks"
    lock_dir.mkdir(parents=True)
    return root, lock_dir / CANONICAL_IO_LOCK_NAME


def test_readonly_observer_never_creates_missing_lock(tmp_path: Path) -> None:
    root, lock = _fixture(tmp_path)
    with pytest.raises(ProductionIOError, match="pre-existing"):
        with canonical_io_lock_observer(root):
            pytest.fail("must not acquire or silently create missing lock")
    assert not lock.exists()


def test_observer_opens_existing_lock_readonly_without_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, lock = _fixture(tmp_path)
    # Bootstrap is an already privileged installer/writer operation.
    with canonical_io_lock(root):
        pass
    lock.write_bytes(b"seed")
    lock.chmod(0o400)
    lock_dir = lock.parent
    lock_dir.chmod(0o500)
    original_open = os.open
    seen_flags: list[int] = []

    def traced_open(path, flags, *args, **kwargs):
        if Path(path) == lock:
            seen_flags.append(flags)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(production_io.os, "open", traced_open)
    try:
        before = lock.read_bytes()
        stat_before = lock.stat()
        with canonical_io_lock_observer(root) as path:
            assert path == lock
            assert lock.read_bytes() == before
            # Same inode must be exclusively locked against other opens.
            competing_fd = original_open(lock, os.O_RDONLY | os.O_CLOEXEC)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(competing_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(competing_fd)
        assert lock.stat().st_ino == stat_before.st_ino
        assert lock.read_bytes() == before
    finally:
        lock_dir.chmod(0o700)
        lock.chmod(0o600)
    assert seen_flags
    assert all(flags & os.O_ACCMODE == os.O_RDONLY for flags in seen_flags)
    assert all(not flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND) for flags in seen_flags)


def test_observer_rejects_symlink_and_hardlink(tmp_path: Path) -> None:
    root, lock = _fixture(tmp_path)
    target = tmp_path / "target"
    target.write_bytes(b"safe")
    lock.symlink_to(target)
    with pytest.raises(ProductionIOError):
        with canonical_io_lock_observer(root):
            pytest.fail("symlink is not a real lock")
    lock.unlink()
    os.link(target, lock)
    with pytest.raises(ProductionIOError, match="single regular"):
        with canonical_io_lock_observer(root):
            pytest.fail("hardlink allows pathname ambiguity")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO unavailable")
def test_observer_rejects_fifo_nonblocking(tmp_path: Path) -> None:
    root, lock = _fixture(tmp_path)
    os.mkfifo(lock)
    with pytest.raises(ProductionIOError, match="single regular"):
        with canonical_io_lock_observer(root):
            pytest.fail("FIFO must never acquire observer lock")


def test_observer_rejects_replaced_inode_during_lock_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, lock = _fixture(tmp_path)
    lock.write_bytes(b"old")
    original_flock = fcntl.flock
    old_path = lock.with_suffix(".old")

    def replace_on_acquire(fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_EX:
            lock.rename(old_path)
            lock.write_bytes(b"replacement")
        original_flock(fd, operation)

    monkeypatch.setattr(production_io.fcntl, "flock", replace_on_acquire)
    with pytest.raises(ProductionIOError, match="pathname was replaced"):
        with canonical_io_lock_observer(root):
            pytest.fail("must not acquire a disconnected historical inode")


def test_observer_does_not_wrap_errors_from_review_code(tmp_path: Path) -> None:
    root, lock = _fixture(tmp_path)
    lock.touch()
    with pytest.raises(OSError, match="source-only failure"):
        with canonical_io_lock_observer(root):
            raise OSError("source-only failure")
