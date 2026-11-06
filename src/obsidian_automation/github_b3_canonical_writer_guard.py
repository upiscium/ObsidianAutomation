"""Unattached new-LXC cooperative B3 canonical Writer lock (source only).

An independently privileged installer must pre-provision ONE stable root-owned
lock inode. All canonical Writer and Promotion participants must be reviewed
and use this SAME inode before activation. This does not exclude an old LXC,
grant B3 cutover authorization, register a WebUI route, or create host files.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import grp
import os
from pathlib import Path
import stat
import threading
from typing import Iterator

TRUST_ANCHOR = Path("/")
LOCK_ROOT = Path("/var/lib/obsidian-b3-canonical-guard")
LOCK_NAME = "canonical-writer.lock"
SHARED_GROUP = "obsidian-canonical-writers"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_LOCAL_SERIAL = threading.Lock()


class B3CanonicalWriterGuardUnavailable(RuntimeError):
    """Fixed nonsecret refusal; no host names or private file contents."""

    def __init__(self) -> None:
        super().__init__("canonical_writer_guard_unavailable")


def _owner_uid() -> int:
    return 0


def _shared_gid() -> int:
    group = grp.getgrnam(SHARED_GROUP)
    if group.gr_gid <= 0:
        raise B3CanonicalWriterGuardUnavailable()
    return group.gr_gid


def _require_reader_group(group: int) -> None:
    # A future operator must add *each* effect writer to the one dedicated
    # group. A privileged/root or unregistered process cannot quietly join.
    if (
        os.geteuid() == 0
        or group not in os.getgroups()
    ):
        raise B3CanonicalWriterGuardUnavailable()


def _identity(st: os.stat_result) -> tuple[int, ...]:
    return (
        st.st_dev, st.st_ino, st.st_mode, st.st_nlink,
        st.st_uid, st.st_gid, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
    )


def _open_root(*, owner: int, group: int) -> int:
    if (
        type(LOCK_ROOT) is not type(Path("/"))
        or type(TRUST_ANCHOR) is not type(Path("/"))
        or not LOCK_ROOT.is_absolute() or not TRUST_ANCHOR.is_absolute()
        or LOCK_ROOT == TRUST_ANCHOR
        or not LOCK_ROOT.is_relative_to(TRUST_ANCHOR)
        or ".." in LOCK_ROOT.parts or ".." in TRUST_ANCHOR.parts
    ):
        raise B3CanonicalWriterGuardUnavailable()
    fd = os.open(TRUST_ANCHOR, _DIR_FLAGS)
    try:
        anchor = os.fstat(fd)
        if (
            not stat.S_ISDIR(anchor.st_mode)
            or anchor.st_uid != owner
            or stat.S_IMODE(anchor.st_mode) & 0o022
        ):
            raise B3CanonicalWriterGuardUnavailable()
        for part in LOCK_ROOT.relative_to(TRUST_ANCHOR).parts:
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            try:
                info = os.fstat(next_fd)
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != owner
                    or stat.S_IMODE(info.st_mode) & 0o022
                ):
                    raise B3CanonicalWriterGuardUnavailable()
            except BaseException:
                os.close(next_fd)
                raise
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if (info.st_gid != group
                or stat.S_IMODE(info.st_mode) != 0o750):
            raise B3CanonicalWriterGuardUnavailable()
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)


def _verify_file(fd: int, directory_fd: int, *, owner: int, group: int) -> tuple[int, ...]:
    before = os.fstat(fd)
    current = os.stat(LOCK_NAME, dir_fd=directory_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != owner
        or before.st_gid != group
        or stat.S_IMODE(before.st_mode) != 0o640
        or before.st_size != 0
        or _identity(before) != _identity(current)
    ):
        raise B3CanonicalWriterGuardUnavailable()
    return _identity(before)


def _verify_root_unchanged(root_fd: int, *, owner: int, group: int) -> None:
    """Re-traverse the fixed trusted path to reject lock-root replacement.

    No user-controlled path can replace it; only the privileged operator
    holds the root ancestor write permissions. The operator must also
    guarantee no path rotation *between* these checks in production.
    """
    new_fd = _open_root(owner=owner, group=group)
    try:
        previous = os.fstat(root_fd)
        current = os.fstat(new_fd)
        if (
            (previous.st_dev, previous.st_ino, previous.st_mode,
             previous.st_uid, previous.st_gid)
            != (current.st_dev, current.st_ino, current.st_mode,
                current.st_uid, current.st_gid)
        ):
            raise B3CanonicalWriterGuardUnavailable()
    finally:
        os.close(new_fd)


class B3CanonicalWriterGuard:
    """Callable context-manager adapter for the existing B3 WriterGuard port.

    Only existing files can be opened. Fail closed, including on contention,
    lock-path replacement, unsafe role, interrupted acquisition or missing
    trust anchor. A successful hold is OS-local cooperation, NEVER evidence
    of old-LXC retirement or exclusive remote Vault authority.
    """

    @contextmanager
    def __call__(self) -> Iterator[None]:
        if not _LOCAL_SERIAL.acquire(blocking=False):
            raise B3CanonicalWriterGuardUnavailable()
        root_fd = None
        file_fd = None
        acquired = False
        try:
            try:
                owner, group = _owner_uid(), _shared_gid()
                if type(owner) is not int or owner < 0:
                    raise B3CanonicalWriterGuardUnavailable()
                _require_reader_group(group)
                root_fd = _open_root(owner=owner, group=group)
                file_fd = os.open(LOCK_NAME, _FILE_FLAGS, dir_fd=root_fd)
                initial = _verify_file(file_fd, root_fd, owner=owner, group=group)
                fcntl.flock(file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                if _verify_file(file_fd, root_fd, owner=owner, group=group) != initial:
                    raise B3CanonicalWriterGuardUnavailable()
                _verify_root_unchanged(root_fd, owner=owner, group=group)
            except (OSError, ValueError, TypeError):
                raise B3CanonicalWriterGuardUnavailable() from None

            try:
                # Exceptions from the caller's Writer are not lock errors.
                # They must propagate unchanged so B3 can mark effects unknown.
                yield
            finally:
                # Detect any privileged lock rotation while remote effects
                # were underway. A replacement does not confer a clean exit.
                try:
                    if _verify_file(file_fd, root_fd, owner=owner, group=group) != initial:
                        raise B3CanonicalWriterGuardUnavailable()
                    _verify_root_unchanged(root_fd, owner=owner, group=group)
                except (OSError, ValueError, TypeError):
                    raise B3CanonicalWriterGuardUnavailable() from None
        finally:
            try:
                if file_fd is not None:
                    try:
                        if acquired:
                            fcntl.flock(file_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(file_fd)
                if root_fd is not None:
                    os.close(root_fd)
            finally:
                _LOCAL_SERIAL.release()
