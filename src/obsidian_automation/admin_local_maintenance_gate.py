"""#300 single-LXC fixed-path maintenance gate, intentionally NOT wired to effects.

The gate excludes cooperating host-runtime and pre-review updaters using their
actual lock inodes. It cannot exclude uncooperative service launchers or legacy
GitHub/Promotion writers. A positive acquisition is NOT production authority.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
from typing import Iterator

from .admin_operation_broker import BrokerRejected

HOST_RECEIPT_DIR = Path("/var/lib/obsidian-automation/deployments")
REFRESH_DIR = Path("/run/obsidian-automation")
HOST_LOCK = "host-update.lock"
REFRESH_LOCK = "semantic-refresh-inhibited.lock"
HOST_PENDING = "pending-runtime.json"
REFRESH_PENDING = "semantic-refresh-inhibited.json"


def _lock_existing(parent_fd: int, name: str, owner: int) -> int:
    """Open only a pre-provisioned lock inode; no runtime path creation."""
    try:
        fd = os.open(
            name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
    except OSError:
        raise BrokerRejected("local_lock_unavailable") from None
    try:
        info = os.fstat(fd)
        path_info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or (info.st_dev, info.st_ino) !=
               (path_info.st_dev, path_info.st_ino)
        ):
            raise BrokerRejected("unsafe_local_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BrokerRejected("local_update_busy") from None
        # Verify the pathname still refers to the held inode.
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
            raise BrokerRejected("local_lock_replaced")
        result, fd = fd, None
        return result
    except OSError:
        raise BrokerRejected("local_lock_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)



def _open_pinned_dir(path: Path, *, owner: int, allowed_modes: frozenset[int]) -> int:
    if (
        not isinstance(path, Path) or not path.is_absolute()
        or ".." in path.parts or path == Path("/")
    ):
        raise BrokerRejected("invalid_local_lock_directory")
    fd = None
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        for component in path.parts[1:]:
            next_fd = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                | os.O_CLOEXEC, dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in (0, owner)
                or (
                    stat.S_IMODE(info.st_mode) & 0o022
                    and not (
                        owner != 0 and component == "tmp"
                        and info.st_uid == 0
                        and stat.S_IMODE(info.st_mode) == 0o1777
                    )
                )
            ):
                raise BrokerRejected("unsafe_local_lock_directory")
        info = os.fstat(fd)
        if stat.S_IMODE(info.st_mode) not in allowed_modes:
            raise BrokerRejected("unsafe_local_lock_directory")
        result, fd = fd, None
        return result
    except OSError:
        raise BrokerRejected("local_lock_directory_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


def _absent(parent_fd: int, name: str) -> None:
    try:
        # Includes symlink, FIFO and unreadable file: any occupied name blocks.
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise BrokerRejected("pending_state_unavailable") from None
    raise BrokerRejected("local_update_pending")


@contextmanager
def cooperating_updater_gate(
    *, host_dir: Path = HOST_RECEIPT_DIR,
    refresh_dir: Path = REFRESH_DIR,
    require_root: bool = True,
) -> Iterator[None]:
    """Hold both existing updater locks for a local read/check critical section.

    This is a *partial* exclusion primitive, NOT full Broker effect authority.
    It cannot exclude timer launches, GitHub/Promotion legacy paths, or
    uncooperative writers. No caller may infer effects_enabled from success.
    """
    if require_root and os.geteuid() != 0:
        raise BrokerRejected("privileged_broker_required")
    owner = 0 if require_root else os.geteuid()
    host_fd = refresh_fd = host_lock_fd = refresh_lock_fd = None
    try:
        host_fd = _open_pinned_dir(
            host_dir, owner=owner, allowed_modes=frozenset((0o700, 0o750)),
        )
        host_lock_fd = _lock_existing(host_fd, HOST_LOCK, owner)
        _absent(host_fd, HOST_PENDING)
        refresh_fd = _open_pinned_dir(
            refresh_dir, owner=owner, allowed_modes=frozenset((0o700, 0o750, 0o755)),
        )
        refresh_lock_fd = _lock_existing(refresh_fd, REFRESH_LOCK, owner)
        _absent(refresh_fd, REFRESH_PENDING)
        # Recheck the host update journal after acquiring both locks.
        _absent(host_fd, HOST_PENDING)
        yield
    finally:
        for fd in (refresh_lock_fd, host_lock_fd, refresh_fd, host_fd):
            if fd is not None:
                os.close(fd)
