"""#472 D1/B1/B3 — UNWIRED preprovisioned Broker read-only listener.

This source-only component may bind and accept only on a pre-provisioned
Broker-owned temporary path. It neither provisions an OS UID/GID nor
installs/runs a service, credentials, Provider network path, Owner journal,
Writer, Vault or Nextcloud. It handles one connected stream per explicit
serve_once call via BrokerReadOnlySession; no autonomous loop/activation.
Broker and Owner must be distinct UIDs; this module does not itself prove
cross-UID trust, executable provenance, or forwarded-descriptor origin.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import socket
import stat
import threading

from .vault_format_broker_readonly_session import BrokerReadOnlySession

_LOCK_NAME = "broker-readonly-listener.lock"
_BACKLOG = 4
_ACCEPT_TIMEOUT = 0.2
_SOCKET_MODE = 0o660
_PARENT_MODE = 0o710


class BrokerListenerDenied(ValueError):
    """Stable public failure reasons with no source path or secrets."""


def _require_socket_path(path: Path) -> None:
    if (not isinstance(path, Path) or not path.is_absolute()
            or ".." in path.parts or path.name in ("", ".", "..")
            or "\x00" in str(path)):
        raise BrokerListenerDenied("listener_path_invalid")
    try:
        encoded = os.fsencode(path)
    except (UnicodeError, ValueError, TypeError):
        raise BrokerListenerDenied("listener_path_invalid") from None
    if len(encoded) >= 100:
        raise BrokerListenerDenied("listener_path_invalid")


def _close_descriptor(fd: int) -> None:
    """Even an unexpected close() failure must not expose path/OS text."""
    try:
        os.close(fd)
    except OSError:
        raise BrokerListenerDenied("listener_cleanup_unverified") from None


def _open_trusted_parent(path: Path, broker_uid: int, group_gid: int) -> int:
    """Return a pinned dirfd without following any directory symlink."""
    _require_socket_path(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = -1
    try:
        fd = os.open("/", flags)
        # Safe path ancestry: only root or the trusted Broker may replace a
        # component; root-owned sticky world-writable /tmp is an exception.
        for name in path.parent.parts[1:]:
            before = os.fstat(fd)
            child = os.open(name, flags, dir_fd=fd)
            previous, fd = fd, child
            # Update the currently owned descriptor before closing the old
            # one, so cleanup can still close child on an injected failure.
            _close_descriptor(previous)
            info = os.fstat(fd)
            mode = stat.S_IMODE(info.st_mode)
            if (not stat.S_ISDIR(info.st_mode)
                    or info.st_uid not in (0, broker_uid)
                    or (mode & 0o022 and not (
                        info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
                    ))):
                raise BrokerListenerDenied("listener_ancestor_unsafe")
        parent = os.fstat(fd)
        if (not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != broker_uid
                or parent.st_gid != group_gid
                or stat.S_IMODE(parent.st_mode) != _PARENT_MODE):
            raise BrokerListenerDenied("listener_parent_unsafe")
        out, fd = fd, -1
        return out
    except BrokerListenerDenied:
        raise
    except (OSError, ValueError, TypeError):
        raise BrokerListenerDenied("listener_parent_unsafe") from None
    finally:
        if fd != -1:
            _close_descriptor(fd)


def _lock_preprovisioned(parent_fd: int, broker_uid: int) -> int:
    """No implicit lease creation or stale lock deletion."""
    fd = -1
    try:
        fd = os.open(
            _LOCK_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        st = os.fstat(fd)
        named = os.stat(_LOCK_NAME, dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != broker_uid
                or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600
                or (st.st_dev, st.st_ino) != (named.st_dev, named.st_ino)):
            raise BrokerListenerDenied("listener_lock_unsafe")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            raise BrokerListenerDenied("listener_busy") from None
        result, fd = fd, -1
        return result
    except BrokerListenerDenied:
        raise
    except OSError:
        raise BrokerListenerDenied("listener_lock_unsafe") from None
    finally:
        if fd != -1:
            _close_descriptor(fd)


def _sock_state(parent_fd: int, filename: str):
    return os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)


class BrokerUnixReadOnlyListener:
    """Explicit one-accept-at-a-time broker-side fixture/listener.

    Must be constructed with a source-only BrokerReadOnlySession. Neither this
    object nor BrokerReadOnlySession offers a general daemon loop or service unit.
    A wrong Broker EUID, inode, socket mode/group or stale path fails closed.
    """

    def __init__(
        self, handler: BrokerReadOnlySession, path: Path, *, ipc_group_gid: int,
    ) -> None:
        if (type(handler) is not BrokerReadOnlySession
                or type(ipc_group_gid) is not int or ipc_group_gid < 0):
            raise BrokerListenerDenied("listener_policy_invalid")
        _require_socket_path(path)
        handler._require_broker()
        self.handler = handler
        self.path = path
        self.ipc_group_gid = ipc_group_gid
        self.broker_uid = os.geteuid()
        self._gate = threading.Lock()
        self._listener: socket.socket | None = None
        self._parent_fd: int | None = None
        self._lock_fd: int | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._parent_identity: tuple[int, int] | None = None
        # Any indeterminate socket, accepted peer or lock cleanup permanently
        # poisons this instance: only a separate trusted operator may recover.
        self._cleanup_unverified = False

    def _require_broker(self) -> None:
        if os.geteuid() != self.broker_uid:
            raise BrokerListenerDenied("listener_broker_required")
        self.handler._require_broker()

    def _validate_pinned(self) -> None:
        if (self._parent_fd is None or self._lock_fd is None
                or self._socket_identity is None
                or self._parent_identity is None):
            raise BrokerListenerDenied("listener_not_open")
        fresh = _open_trusted_parent(self.path, self.broker_uid, self.ipc_group_gid)
        try:
            st = os.fstat(fresh)
            if (st.st_dev, st.st_ino) != self._parent_identity:
                raise BrokerListenerDenied("listener_identity_changed")
            lock = os.fstat(self._lock_fd)
            named_lock = _sock_state(fresh, _LOCK_NAME)
            if ((lock.st_dev, lock.st_ino) != (named_lock.st_dev, named_lock.st_ino)
                    or lock.st_uid != self.broker_uid or lock.st_nlink != 1
                    or not stat.S_ISREG(lock.st_mode)
                    or stat.S_IMODE(lock.st_mode) != 0o600):
                raise BrokerListenerDenied("listener_lock_unsafe")
            st = _sock_state(fresh, self.path.name)
            if (not stat.S_ISSOCK(st.st_mode)
                    or (st.st_dev, st.st_ino) != self._socket_identity
                    or st.st_uid != self.broker_uid
                    or st.st_gid != self.ipc_group_gid
                    or stat.S_IMODE(st.st_mode) != _SOCKET_MODE):
                raise BrokerListenerDenied("listener_identity_changed")
        except BrokerListenerDenied:
            raise
        except OSError:
            raise BrokerListenerDenied("listener_identity_changed") from None
        finally:
            _close_descriptor(fresh)

    def open(self) -> "BrokerUnixReadOnlyListener":
        self._require_broker()
        if not self._gate.acquire(blocking=False):
            raise BrokerListenerDenied("listener_busy")
        parent_fd = None
        lock_fd = None
        listener = None
        bound_identity = None
        try:
            if self._cleanup_unverified:
                raise BrokerListenerDenied("listener_cleanup_unverified")
            if self._listener is not None:
                raise BrokerListenerDenied("listener_already_open")
            parent_fd = _open_trusted_parent(
                self.path, self.broker_uid, self.ipc_group_gid,
            )
            st = os.fstat(parent_fd)
            parent_identity = (st.st_dev, st.st_ino)
            lock_fd = _lock_preprovisioned(parent_fd, self.broker_uid)
            try:
                _sock_state(parent_fd, self.path.name)
            except FileNotFoundError:
                pass
            else:
                # Even a stale socket must be removed by a separately
                # reviewed operator, NEVER automatically at startup.
                raise BrokerListenerDenied("listener_path_occupied")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.settimeout(_ACCEPT_TIMEOUT)
            listener.bind(str(self.path))
            try:
                st = _sock_state(parent_fd, self.path.name)
            except OSError:
                # A bound-but-unverifiable inode is an indeterminate cleanup,
                # even if no cleanup failure is independently detected later.
                self._cleanup_unverified = True
                raise BrokerListenerDenied("listener_cleanup_unverified") from None
            bound_identity = (st.st_dev, st.st_ino)
            if not stat.S_ISSOCK(st.st_mode) or st.st_uid != self.broker_uid:
                raise BrokerListenerDenied("listener_socket_unsafe")
            if st.st_gid != self.ipc_group_gid:
                # Will fail closed unless Broker is allowed to chgrp to the
                # pre-provisioned group. No arbitrary UID ownership changes.
                os.chown(self.path, -1, self.ipc_group_gid,
                         follow_symlinks=False)
            os.chmod(self.path, _SOCKET_MODE, follow_symlinks=False)
            state = _sock_state(parent_fd, self.path.name)
            if (not stat.S_ISSOCK(state.st_mode)
                    or (state.st_dev, state.st_ino) != bound_identity
                    or state.st_uid != self.broker_uid
                    or state.st_gid != self.ipc_group_gid
                    or stat.S_IMODE(state.st_mode) != _SOCKET_MODE):
                raise BrokerListenerDenied("listener_socket_unsafe")
            listener.listen(_BACKLOG)
            self._listener, listener = listener, None
            self._parent_fd, parent_fd = parent_fd, None
            self._lock_fd, lock_fd = lock_fd, None
            self._socket_identity = bound_identity
            self._parent_identity = parent_identity
            return self
        except BrokerListenerDenied:
            raise
        except OSError:
            raise BrokerListenerDenied("listener_unavailable") from None
        finally:
            cleanup_failed = False
            try:
                if listener is not None:
                    try:
                        listener.close()
                    except OSError:
                        cleanup_failed = True
                if (bound_identity is not None and parent_fd is not None
                        and not cleanup_failed):
                    # Only an acknowledged successful close permits unlink.
                    # If close() failed, the listener FD may still be active;
                    # leave the name occupied for trusted operator recovery.
                    # On failed setup, delete only the pinned Broker socket.
                    # Any inability to inspect, unlink or sync its directory
                    # prevents a successful cleanup acknowledgement.
                    try:
                        st = _sock_state(parent_fd, self.path.name)
                        if (stat.S_ISSOCK(st.st_mode)
                                and st.st_uid == self.broker_uid
                                and (st.st_dev, st.st_ino) == bound_identity):
                            os.unlink(self.path.name, dir_fd=parent_fd)
                            os.fsync(parent_fd)
                        else:
                            cleanup_failed = True
                    except FileNotFoundError:
                        pass
                    except OSError:
                        cleanup_failed = True
            finally:
                try:
                    if parent_fd is not None:
                        try:
                            os.close(parent_fd)
                        except OSError:
                            cleanup_failed = True
                    if lock_fd is not None:
                        try:
                            os.close(lock_fd)
                        except OSError:
                            cleanup_failed = True
                finally:
                    # Poison while holding the gate. Releasing first would
                    # let a concurrent open() rebind the absent socket name
                    # after an indeterminate unlink/fsync cleanup.
                    if cleanup_failed:
                        self._cleanup_unverified = True
                    self._gate.release()
            if cleanup_failed:
                raise BrokerListenerDenied("listener_cleanup_unverified")

    def serve_once(self) -> bool:
        """Accept at most one peer, delegating its UID/command to handler."""
        self._require_broker()
        if not self._gate.acquire(blocking=False):
            raise BrokerListenerDenied("listener_busy")
        try:
            if self._cleanup_unverified:
                raise BrokerListenerDenied("listener_cleanup_unverified")
            self._validate_pinned()
            try:
                accepted, _ = self._listener.accept()
            except socket.timeout:
                return False
            except OSError:
                raise BrokerListenerDenied("listener_unavailable") from None
            # Recheck the bound name/lock AFTER accept as well. Another
            # inode might have replaced the pathname between preflight and
            # accept; in that case never expose even read-only observations.
            # This listener has sole ownership of the accepted descriptor
            # and must report any indeterminate final close as a failure.
            try:
                self._validate_pinned()
                # One FD owner only. Unlike standalone B2 serve_once(),
                # this variant NEVER closes the accepted socket itself.
                # The listener records any close() failure below.
                self.handler._serve_connected(accepted)
            except BrokerListenerDenied:
                raise
            except Exception:
                raise BrokerListenerDenied("listener_request_unverified") from None
            finally:
                try:
                    accepted.close()
                except OSError:
                    # An indeterminate accepted FD prevents further accepts
                    # and automatic socket-name cleanup on this instance.
                    self._cleanup_unverified = True
                    raise BrokerListenerDenied("listener_cleanup_unverified") from None
            return True
        finally:
            self._gate.release()

    def close(self) -> None:
        """Stop without unlinking any socket that replaced our own inode."""
        self._require_broker()
        if not self._gate.acquire(blocking=False):
            raise BrokerListenerDenied("listener_busy")
        listener, self._listener = self._listener, None
        parent_fd, self._parent_fd = self._parent_fd, None
        lock_fd, self._lock_fd = self._lock_fd, None
        identity, self._socket_identity = self._socket_identity, None
        parent_identity, self._parent_identity = self._parent_identity, None
        failure = self._cleanup_unverified
        try:
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    # Do not unlink a socket whose accept fd may still be
                    # live. Operator inspection is required after bad close.
                    failure = True
            if parent_fd is not None and not failure:
                try:
                    fresh = _open_trusted_parent(
                        self.path, self.broker_uid, self.ipc_group_gid,
                    )
                    try:
                        st = os.fstat(fresh)
                        sock = _sock_state(fresh, self.path.name)
                        # An old lock descriptor is not sufficient: never
                        # acknowledge cleanup if the named stable lock was
                        # replaced after open(), even when our socket inode
                        # is still visible.
                        if lock_fd is None:
                            failure = True
                        else:
                            held_lock = os.fstat(lock_fd)
                            named_lock = _sock_state(fresh, _LOCK_NAME)
                            lock_safe = (
                                stat.S_ISREG(held_lock.st_mode)
                                and held_lock.st_uid == self.broker_uid
                                and held_lock.st_nlink == 1
                                and stat.S_IMODE(held_lock.st_mode) == 0o600
                                and (held_lock.st_dev, held_lock.st_ino) ==
                                    (named_lock.st_dev, named_lock.st_ino)
                            )
                            if (not lock_safe
                                    or parent_identity != (st.st_dev, st.st_ino)
                                    or identity != (sock.st_dev, sock.st_ino)
                                    or not stat.S_ISSOCK(sock.st_mode)
                                    or sock.st_uid != self.broker_uid
                                    or sock.st_gid != self.ipc_group_gid
                                    or stat.S_IMODE(sock.st_mode) != _SOCKET_MODE):
                                failure = True
                            else:
                                os.unlink(self.path.name, dir_fd=fresh)
                                os.fsync(fresh)
                    finally:
                        os.close(fresh)
                except FileNotFoundError:
                    failure = True
                except (OSError, BrokerListenerDenied):
                    failure = True
        finally:
            try:
                if parent_fd is not None:
                    try:
                        os.close(parent_fd)
                    except OSError:
                        failure = True
                if lock_fd is not None:
                    try:
                        os.close(lock_fd)
                    except OSError:
                        failure = True
            finally:
                # Latch failure while still holding the broker gate; otherwise
                # another thread could reopen between gate release and latch.
                if failure:
                    self._cleanup_unverified = True
                self._gate.release()
        if failure:
            raise BrokerListenerDenied("listener_cleanup_unverified")

    def __enter__(self) -> "BrokerUnixReadOnlyListener":
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
