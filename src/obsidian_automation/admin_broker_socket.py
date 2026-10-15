"""#300: local peer-authenticated read-only broker socket (NOT deployed).

This does not expose OperationBroker.apply. All write operations are denied.
A future privileged worker must pass the reviewed multi-host/updater/UID gates
before *separately* introducing any effectful command.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import socket
import stat
import struct
import time

MAX_FRAME = 2048
_MAX_BACKLOG = 8
_PEER_CREDS = struct.Struct("3i")


class LocalBrokerProtocolError(RuntimeError):
    """Stable, intentionally non-diagnostic error without raw payload."""


def _fail(code: str) -> None:
    raise LocalBrokerProtocolError(code)


def _parse_frame(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or len(raw) > MAX_FRAME:
        _fail("invalid_frame")
    try:
        def strict_pairs(pairs):
            result = {}
            for k, v in pairs:
                if k in result:
                    raise ValueError("duplicate")
                result[k] = v
            return result
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=strict_pairs)
    except (ValueError, UnicodeError):
        _fail("invalid_frame")
    if not isinstance(result, dict):
        _fail("invalid_frame")
    return result


def _encode_response(value: dict) -> bytes:
    payload = json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
    ).encode("ascii")
    if len(payload) > MAX_FRAME:
        _fail("response_over_bound")
    return struct.pack("!I", len(payload)) + payload


def _recv_exact(
    connection: socket.socket, count: int, *, deadline: float,
) -> bytes:
    if not 0 <= count <= MAX_FRAME:
        _fail("invalid_length")
    chunks = []
    while count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _fail("request_deadline_expired")
        try:
            connection.settimeout(remaining)
            part = connection.recv(count)
        except (OSError, TimeoutError):
            _fail("io_unavailable")
        if not part:
            _fail("truncated_frame")
        chunks.append(part)
        count -= len(part)
    return b"".join(chunks)


def _receive(connection: socket.socket, *, deadline: float) -> dict:
    header = _recv_exact(connection, 4, deadline=deadline)
    length = struct.unpack("!I", header)[0]
    if not 1 <= length <= MAX_FRAME:
        _fail("invalid_length")
    return _parse_frame(_recv_exact(connection, length, deadline=deadline))


def _safe_parent(path: Path, *, require_root: bool) -> int:
    if not isinstance(path, Path) or not path.is_absolute() or path.name in ("", ".", ".."):
        _fail("invalid_socket_path")
    if ".." in path.parts or len(os.fsencode(path)) >= 100:
        # Conservative bound for Linux sockaddr_un.sun_path (108 bytes).
        _fail("invalid_socket_path")
    fd = None
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        for name in path.parent.parts[1:]:
            next_fd = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            if require_root and (
                info.st_uid != 0 or (stat.S_IMODE(info.st_mode) & 0o022)
            ):
                _fail("unsafe_socket_parent")
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            _fail("unsafe_socket_parent")
        mode = stat.S_IMODE(info.st_mode)
        if (
            info.st_uid != (0 if require_root else os.geteuid())
            or mode & 0o027  # no group write, no world traverse/read/write
        ):
            _fail("unsafe_socket_parent")
        result, fd = fd, None
        return result
    except (OSError, ValueError):
        _fail("unsafe_socket_parent")
    finally:
        if fd is not None:
            os.close(fd)


def _capabilities() -> dict:
    # Explicit negative authority. Nothing in this module can make it true.
    return {
        "version": 1, "authority": "observation_only",
        "operations": {"pause": False, "resume": False, "run_once": False},
        "reason": "cross_updater_exclusion_unqualified",
    }



def _socket_lease(parent_fd: int, socket_name: str, *, require_root: bool) -> int:
    handle = None
    try:
        handle = os.open(
            socket_name + ".lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=parent_fd,
        )
        info = os.fstat(handle)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != (0 if require_root else os.geteuid())
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            _fail("unsafe_socket_lease")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            _fail("socket_lease_busy")
        path_info = os.stat(
            socket_name + ".lock", dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino):
            _fail("unsafe_socket_lease")
        result, handle = handle, None
        return result
    except OSError:
        _fail("unsafe_socket_lease")
    finally:
        if handle is not None:
            os.close(handle)


class ReadOnlyBrokerSocket:
    """One-request-at-a-time local AF_UNIX listener, no HTTP and no root service.

    The explicit peer UID and owning directory are host configuration,
    never browser-supplied. Require root by default; tests use an isolated
    current-UID socket without touching the production path.
    """

    def __init__(
        self, path: Path, *, allowed_peer_uid: int,
        allowed_peer_gid: int | None = None,
        socket_gid: int | None = None,
        require_root: bool = True,
    ):
        self.path = path
        self.allowed_peer_uid = allowed_peer_uid
        self.allowed_peer_gid = allowed_peer_gid
        self.socket_gid = socket_gid
        self.require_root = require_root
        self._listener: socket.socket | None = None
        self._parent_fd: int | None = None
        self._lease_fd: int | None = None
        self._identity: tuple[int, int] | None = None

    def open(self) -> "ReadOnlyBrokerSocket":
        if self._listener is not None:
            _fail("already_open")
        if self.require_root and os.geteuid() != 0:
            _fail("privileged_socket_required")
        if (
            type(self.allowed_peer_uid) is not int
            or not 0 <= self.allowed_peer_uid <= 2**32 - 2
            or (self.allowed_peer_gid is not None and (
                type(self.allowed_peer_gid) is not int
                or not 0 <= self.allowed_peer_gid <= 2**32 - 2
            ))
            or (self.socket_gid is not None and (
                type(self.socket_gid) is not int
                or not 0 <= self.socket_gid <= 2**32 - 2
            ))
        ):
            _fail("invalid_peer_policy")
        parent_fd = _safe_parent(self.path, require_root=self.require_root)
        sock = None
        lease_fd = None
        was_bound = False
        bound_identity = None
        try:
            lease_fd = _socket_lease(
                parent_fd, self.path.name, require_root=self.require_root,
            )
            # Refuse occupied paths, including symlinks, FIFO, files or stale
            # socket inodes. Never unlink a socket owned by another process.
            try:
                os.stat(self.path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                _fail("socket_path_occupied")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(0.2)
            sock.bind(str(self.path))
            was_bound = True
            created = os.stat(
                self.path.name, dir_fd=parent_fd, follow_symlinks=False,
            )
            if not stat.S_ISSOCK(created.st_mode):
                _fail("unsafe_socket_inodes")
            bound_identity = (created.st_dev, created.st_ino)
            if self.socket_gid is not None:
                os.chown(self.path, -1, self.socket_gid, follow_symlinks=False)
            os.chmod(self.path, 0o660, follow_symlinks=False)
            info = os.stat(self.path.name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISSOCK(info.st_mode)
                or bound_identity != (info.st_dev, info.st_ino)
                or info.st_uid != (0 if self.require_root else os.geteuid())
                or stat.S_IMODE(info.st_mode) != 0o660
                or (
                    self.socket_gid is not None and info.st_gid != self.socket_gid
                )
            ):
                _fail("unsafe_socket_inodes")
            sock.listen(_MAX_BACKLOG)
            self._identity = bound_identity
            self._listener, sock = sock, None
            self._parent_fd, parent_fd = parent_fd, None
            self._lease_fd, lease_fd = lease_fd, None
            return self
        finally:
            try:
                if sock is not None:
                    try:
                        sock.close()
                    finally:
                        # A post-bind setup failure may have left our socket
                        # name. Clean only under our pinned exclusive lease.
                        if was_bound and lease_fd is not None:
                            try:
                                state = os.stat(
                                    self.path.name, dir_fd=parent_fd,
                                    follow_symlinks=False,
                                )
                                parent_state = os.fstat(parent_fd)
                                parent_safe = (
                                    parent_state.st_uid ==
                                        (0 if self.require_root else os.geteuid())
                                    and not (stat.S_IMODE(parent_state.st_mode) & 0o027)
                                )
                                if (
                                    parent_safe and stat.S_ISSOCK(state.st_mode)
                                    and state.st_uid ==
                                        (0 if self.require_root else os.geteuid())
                                    and (
                                        bound_identity is None or
                                        (state.st_dev, state.st_ino) == bound_identity
                                    )
                                ):
                                    os.unlink(self.path.name, dir_fd=parent_fd)
                                    os.fsync(parent_fd)
                            except FileNotFoundError:
                                pass
            finally:
                # Cleanup errors cannot leave the stable lease held.
                try:
                    if parent_fd is not None:
                        os.close(parent_fd)
                finally:
                    if lease_fd is not None:
                        os.close(lease_fd)

    def __enter__(self) -> "ReadOnlyBrokerSocket":
        return self.open()

    def close(self) -> None:
        listener, self._listener = self._listener, None
        fd, self._parent_fd = self._parent_fd, None
        lease_fd, self._lease_fd = self._lease_fd, None
        try:
            try:
                if listener is not None:
                    listener.close()
            finally:
                if fd is not None:
                    try:
                        info = os.stat(
                            self.path.name, dir_fd=fd, follow_symlinks=False,
                        )
                        parent_state = os.fstat(fd)
                        parent_mode = stat.S_IMODE(parent_state.st_mode)
                        if (
                            lease_fd is not None  # exclusive host lease held
                            and self._identity == (info.st_dev, info.st_ino)
                            and stat.S_ISSOCK(info.st_mode)
                            and parent_state.st_uid == (
                                0 if self.require_root else os.geteuid()
                            )
                            and not (parent_mode & 0o027)
                        ):
                            # Untrusted peer UID cannot write the parent.
                            # Owning/root UID is a trusted host actor.
                            os.unlink(self.path.name, dir_fd=fd)
                            os.fsync(fd)
                    except FileNotFoundError:
                        pass
        finally:
            try:
                if fd is not None:
                    os.close(fd)
            finally:
                try:
                    if lease_fd is not None:
                        os.close(lease_fd)
                finally:
                    self._identity = None

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _authorized(self, conn: socket.socket) -> bool:
        try:
            raw = conn.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, _PEER_CREDS.size,
            )
            if len(raw) != _PEER_CREDS.size:
                return False
            _pid, uid, gid = _PEER_CREDS.unpack(raw)
            return (
                uid == self.allowed_peer_uid
                and (self.allowed_peer_gid is None or gid == self.allowed_peer_gid)
            )
        except OSError:
            return False

    def _dispatch(self, request: dict) -> dict:
        if (set(request) == {"version", "op"}
            and type(request["version"]) is int
            and request["version"] == 1
            and request["op"] == "capabilities"):

            return _capabilities()
        _fail("operation_unsupported")

    def serve_once(self) -> bool:
        """Handle at most one local read-only request, no background worker."""
        if self._listener is None:
            _fail("socket_not_open")
        try:
            conn, _ = self._listener.accept()
        except socket.timeout:
            return False
        with conn:
            deadline = time.monotonic() + 1.0
            conn.settimeout(1.0)
            if not self._authorized(conn):
                # Do not disclose even supported operations to unauthorized UIDs.
                return True
            try:
                response = self._dispatch(_receive(conn, deadline=deadline))
            except Exception:
                response = {
                    "version": 1, "error": "read_only_request_rejected",
                }
            try:
                conn.sendall(_encode_response(response))
            except (OSError, LocalBrokerProtocolError):
                pass
        return True


class BrokerSocketClient:
    """Non-privileged, bounded Unix client for read-only diagnostic calls."""

    def __init__(self, path: Path, *, timeout: float = 1.0):
        self.path = path
        self.timeout = timeout

    def query(self, request: dict) -> dict:
        if not 0 < self.timeout <= 10:
            _fail("invalid_client_timeout")
        framed = _encode_response(request)
        deadline = time.monotonic() + self.timeout
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(self.timeout)
                conn.connect(str(self.path))
                conn.sendall(framed)
                return _receive(conn, deadline=deadline)
        except (OSError, TimeoutError):
            _fail("socket_unavailable")


# Fixed production location, currently no privileged process is installed.
DEFAULT_LOCAL_BROKER_SOCKET = Path("/run/obsidian-automation/admin-control.sock")


def _unavailable_capabilities() -> dict:
    return {
        "version": 1, "authority": "observation_only",
        "operations": {"pause": False, "resume": False, "run_once": False},
        "reason": "broker_unavailable",
    }


def capabilities_from_socket(
    path: Path = DEFAULT_LOCAL_BROKER_SOCKET,
) -> dict:
    """Read-only observation; never interprets a response as write authority.

    A malformed response, false positive or unreachable socket must remain
    an explicitly unverified/blocked observation.
    """
    try:
        result = BrokerSocketClient(path).query({
            "version": 1, "op": "capabilities",
        })
    except (LocalBrokerProtocolError, OSError, TypeError, ValueError):
        return _unavailable_capabilities()
    if (
        not isinstance(result, dict)
        or set(result) != {"version", "authority", "operations", "reason"}
        or type(result.get("version")) is not int
        or result["version"] != 1
        or result["authority"] != "observation_only"
        or result["reason"] != "cross_updater_exclusion_unqualified"
        or not isinstance(result["operations"], dict)
        or set(result["operations"]) != {"pause", "resume", "run_once"}
        or any(type(value) is not bool or value is not False
               for value in result["operations"].values())
    ):
        return _unavailable_capabilities()
    return _capabilities()
