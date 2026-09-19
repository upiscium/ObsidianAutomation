from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


CANONICAL_IO_LOCK_NAME = "canonical-io.lock"
READ_VIEW_LOCK_DIR_NAME = "read-view"
MIRROR_READ_LOCK_NAME = "mirror-read.lock"


class ProductionIOError(RuntimeError):
    """Raised when canonical production I/O cannot be serialized safely."""


def _require_safe_directory(path: Path, *, label: str) -> Path:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ProductionIOError(f"{label} does not exist: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ProductionIOError(f"{label} is not a safe directory: {path}")
    return path


@contextmanager
def canonical_io_lock(ai_root: Path) -> Iterator[Path]:
    """Serialize canonical WebDAV effects and pull-only mirror refreshes.

    This lock is deliberately global for one AI Writer state root. Per-mutation
    lifecycle locking remains separate in production_orchestrator.
    """

    root = _require_safe_directory(ai_root.absolute(), label="AI state root")
    lock_dir = _require_safe_directory(root / "24-Locks", label="production lock directory")
    lock_path = lock_dir / CANONICAL_IO_LOCK_NAME

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    try:
        # 24-Locks is shared operational state. 0660 preserves the parent
        # default ACL entries used by the production Sync identity.
        fd = os.open(lock_path, flags, 0o660)
    except OSError as exc:
        raise ProductionIOError("cannot open canonical production I/O lock") from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ProductionIOError("canonical production I/O lock is not a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield lock_path
    except OSError as exc:
        raise ProductionIOError("cannot acquire canonical production I/O lock") from exc
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


@contextmanager
def mirror_read_lock(ai_root: Path) -> Iterator[Path]:
    """Stabilize one host-local mirror view while Reader derives artifacts.

    This lock does not prove that the local mirror is current with the remote
    canonical Vault. It only prevents the local pull-only mirror refresher from
    mutating the mirror while a Reader operation validates and re-reads bytes.
    """

    root = _require_safe_directory(ai_root.absolute(), label="AI state root")
    lock_dir = _require_safe_directory(
        root / "24-Locks",
        label="production lock directory",
    )
    read_view_dir = _require_safe_directory(
        lock_dir / READ_VIEW_LOCK_DIR_NAME,
        label="mirror read-view lock directory",
    )
    lock_path = read_view_dir / MIRROR_READ_LOCK_NAME

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    try:
        # The read-view subdirectory has a narrow default ACL shared only by
        # Sync and Reader. 0660 preserves those named-user entries regardless
        # of which one creates the inode first.
        fd = os.open(lock_path, flags, 0o660)
    except OSError as exc:
        raise ProductionIOError("cannot open mirror read-view lock") from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ProductionIOError("mirror read-view lock is not a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield lock_path
    except OSError as exc:
        raise ProductionIOError("cannot acquire mirror read-view lock") from exc
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
