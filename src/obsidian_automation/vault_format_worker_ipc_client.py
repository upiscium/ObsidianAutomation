"""#472 R2/B2c-D0 — source-only, one-command Worker Unix IPC client.

No runtime app import, service, policy issuer, credential access, Writer,
network/WebDAV/Vault mutation or persistent worker state. This client NEVER
grants effect-time permission. A successful Claim ACK only reserves the
signed Run in the separately trusted Owner Ledger; actual Writer effects
must separately hold the B2b shared drain lock and verify all provider/CAS
and credential-generation bindings immediately before their side effects.

The kernel authenticates the connected Owner UID (SO_PEERCRED) but cannot
authenticate the original requester behind an accepted-FD forwarding proxy.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import socket
import stat
import struct
import threading
import time
from typing import Any

from .vault_format_run_ledger import RunIntent, _sealed_intent

_MAX_REQUEST = 2048
_MAX_RESPONSE = 256
_TOTAL_SECONDS = 3.0
_PEERCRED = struct.Struct("3i")
_ID_KEYS = ("run_id", "plan_sha256", "policy_sha256", "operation_id")
_REASONS = frozenset({
    "ipc_denied", "ipc_unsupported", "ipc_invalid", "ipc_frame_invalid",
    "ipc_peer_invalid", "ipc_unavailable", "ipc_run_unverified",
    "ipc_claim_denied", "ipc_stop_unverified", "ipc_recovery_unverified",
})
_O_PATH = getattr(os, "O_PATH", 0)


class WorkerIPCDenied(ValueError):
    """Stable public failure code; never reveal socket path, request or key."""


@dataclass(frozen=True, slots=True)
class RunStatusObservation:
    """A read-only report, NOT an effect-time Writer lease."""

    claimed_now: bool


def _require_socket_path(path: Path) -> None:
    """Validate path shape/encodability with one stable public error code."""
    if (not isinstance(path, Path) or not path.is_absolute()
            or path.name in ("", ".", "..") or ".." in path.parts
            or "\x00" in str(path)):
        raise WorkerIPCDenied("worker_path_invalid")
    try:
        encoded = os.fsencode(path)
    except (UnicodeError, ValueError, TypeError):
        raise WorkerIPCDenied("worker_path_invalid") from None
    if len(encoded) >= 100:
        raise WorkerIPCDenied("worker_path_invalid")


def _trusted_parent(path: Path, owner_uid: int, ipc_group_gid: int) -> int:
    """Pin the root-to-parent path without following directory symlinks.

    O_PATH permits a Worker group to traverse the 0710 directory without
    granting read/list permission. This mirrors B1's Owner-side trust rules
    but works under the separate, restricted Worker UID.
    """
    _require_socket_path(path)
    if _O_PATH == 0 or not hasattr(os, "O_NOFOLLOW"):
        raise WorkerIPCDenied("worker_unsupported")
    flags = _O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = -1
    try:
        fd = os.open("/", flags)
        for part in path.parent.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            prev, fd = fd, next_fd
            os.close(prev)
            info = os.fstat(fd)
            mode = stat.S_IMODE(info.st_mode)
            if (not stat.S_ISDIR(info.st_mode)
                    or info.st_uid not in (0, owner_uid)
                    or (mode & 0o022 and not (
                        info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
                    ))):
                raise WorkerIPCDenied("worker_path_untrusted")
        info = os.fstat(fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid
                or info.st_gid != ipc_group_gid
                or stat.S_IMODE(info.st_mode) != 0o710):
            raise WorkerIPCDenied("worker_path_untrusted")
        result, fd = fd, -1
        return result
    except WorkerIPCDenied:
        raise
    except (OSError, ValueError, TypeError):
        raise WorkerIPCDenied("worker_path_untrusted") from None
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                raise WorkerIPCDenied("worker_cleanup_unverified") from None


def _socket_identity(parent_fd: int, name: str, owner_uid: int,
                     group_gid: int) -> tuple[int, int]:
    try:
        state = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISSOCK(state.st_mode)
                or state.st_uid != owner_uid or state.st_gid != group_gid
                or stat.S_IMODE(state.st_mode) != 0o660):
            raise WorkerIPCDenied("worker_socket_untrusted")
        return state.st_dev, state.st_ino
    except WorkerIPCDenied:
        raise
    except OSError:
        raise WorkerIPCDenied("worker_socket_untrusted") from None


def _duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate response field")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("invalid response constant")


class WorkerUnixClient:
    """Explicit Worker-side transport for run.claim and run.status ONLY.

    A single instance may attempt one claim. The claim slot is irreversibly
    consumed BEFORE validation or I/O; a lost/partial response may mean Owner
    committed the Claim, so neither the client nor its caller may infer that
    retries are safe. A NEW instance is not an authority to replay it either:
    the Owner's durable one-shot Ledger is the final anti-replay boundary.
    """

    def __init__(self, path: Path, *, owner_uid: int,
                 ipc_group_gid: int) -> None:
        if (not hasattr(socket, "SO_PEERCRED") or _O_PATH == 0
                or type(owner_uid) is not int or owner_uid < 0
                or type(ipc_group_gid) is not int or ipc_group_gid < 0
                or os.geteuid() == 0 or owner_uid == os.geteuid()):
            raise WorkerIPCDenied("worker_policy_invalid")
        # Fully validate path shape before constructing a reusable client.
        _require_socket_path(path)
        self._path = path
        self._owner_uid = owner_uid
        self._ipc_group_gid = ipc_group_gid
        self._worker_uid = os.geteuid()
        self._pid = os.getpid()
        self._claim_consumed = False
        self._gate = threading.Lock()

    def _require_worker(self) -> None:
        if (os.getpid() != self._pid
                or os.geteuid() != self._worker_uid
                or self._owner_uid == self._worker_uid):
            raise WorkerIPCDenied("worker_process_mismatch")

    @staticmethod
    def _deadline(sock: socket.socket, end: float) -> None:
        left = end - time.monotonic()
        if left <= 0:
            raise WorkerIPCDenied("worker_transport_unverified")
        sock.settimeout(left)

    @staticmethod
    def _server_uid(sock: socket.socket) -> int:
        try:
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                  _PEERCRED.size)
            if len(raw) != _PEERCRED.size:
                raise WorkerIPCDenied("worker_peer_untrusted")
            pid, uid, gid = _PEERCRED.unpack(raw)
            if pid <= 0 or uid < 0 or gid < 0:
                raise WorkerIPCDenied("worker_peer_untrusted")
            return uid
        except WorkerIPCDenied:
            raise
        except (OSError, AttributeError, ValueError, struct.error):
            raise WorkerIPCDenied("worker_peer_untrusted") from None

    @staticmethod
    def _receive(sock: socket.socket, end: float) -> dict:
        """Read an entire bounded response through EOF, not first newline.

        Keeping this method separate enables a deterministic fragmented-read
        test without relying on Linux stream packet/coalescing behavior.
        """
        data = bytearray()
        while len(data) <= _MAX_RESPONSE:
            WorkerUnixClient._deadline(sock, end)
            chunk = sock.recv(_MAX_RESPONSE + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        if (not data or len(data) > _MAX_RESPONSE
                or data.count(b"\n") != 1 or not data.endswith(b"\n")
                or b"\x00" in data):
            raise WorkerIPCDenied("worker_response_invalid")
        try:
            response = json.loads(
                data[:-1].decode("ascii"),
                object_pairs_hook=_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except (ValueError, UnicodeError, TypeError):
            raise WorkerIPCDenied("worker_response_invalid") from None
        if (type(response) is not dict
                or type(response.get("v")) is not int
                or response["v"] != 1
                or type(response.get("ok")) is not bool):
            raise WorkerIPCDenied("worker_response_invalid")
        return response

    def _exchange(self, request: dict) -> dict:
        """One connect, one request, write-half-close, read one reply to EOF.

        NEVER auto-reconnect or auto-resend, even if send() fails midway,
        the Owner closes without replying, or a valid response is truncated.
        """
        try:
            payload = json.dumps(
                request, ensure_ascii=True, separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii") + b"\n"
        except (TypeError, ValueError, UnicodeError):
            raise WorkerIPCDenied("worker_request_invalid") from None
        if len(payload) > _MAX_REQUEST:
            raise WorkerIPCDenied("worker_request_invalid")
        end = time.monotonic() + _TOTAL_SECONDS
        parent_fd = -1
        sock = None
        result = None
        failure = None
        try:
            self._require_worker()
            parent_fd = _trusted_parent(
                self._path, self._owner_uid, self._ipc_group_gid,
            )
            identity = _socket_identity(
                parent_fd, self._path.name,
                self._owner_uid, self._ipc_group_gid,
            )
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._deadline(sock, end)
            sock.connect(str(self._path))
            if self._server_uid(sock) != self._owner_uid:
                raise WorkerIPCDenied("worker_peer_untrusted")
            # Bind the actual connected peer to the still-visible socket
            # inode and pre-provisioned path; no request leaves before this.
            if (_socket_identity(parent_fd, self._path.name,
                                 self._owner_uid, self._ipc_group_gid)
                    != identity):
                raise WorkerIPCDenied("worker_socket_untrusted")
            self._deadline(sock, end)
            sock.sendall(payload)
            self._deadline(sock, end)
            sock.shutdown(socket.SHUT_WR)
            result = self._receive(sock, end)
        except WorkerIPCDenied as err:
            failure = str(err)
        except (OSError, TimeoutError):
            failure = "worker_transport_unverified"
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    failure = "worker_cleanup_unverified"
            if parent_fd != -1:
                try:
                    os.close(parent_fd)
                except OSError:
                    failure = "worker_cleanup_unverified"
        if failure is not None:
            raise WorkerIPCDenied(failure)
        return result

    @staticmethod
    def _response(response: dict, successful_field: str) -> bool:
        if response["ok"] is False:
            if (set(response) != {"v", "ok", "reason"}
                    or type(response.get("reason")) is not str
                    or response["reason"] not in _REASONS):
                raise WorkerIPCDenied("worker_response_invalid")
            raise WorkerIPCDenied(
                "worker_claim_denied" if successful_field == "claimed_once"
                else "worker_status_unverified"
            )
        if (set(response) != {"v", "ok", successful_field}
                or type(response[successful_field]) is not bool):
            raise WorkerIPCDenied("worker_response_invalid")
        return response[successful_field]

    def claim_once(self, intent: RunIntent) -> None:
        """A verified acknowledgement is NOT Writer effect authorization."""
        if os.getpid() != self._pid:
            raise WorkerIPCDenied("worker_process_mismatch")
        if not self._gate.acquire(blocking=False):
            raise WorkerIPCDenied("worker_busy")
        try:
            if self._claim_consumed:
                raise WorkerIPCDenied("worker_claim_consumed")
            self._claim_consumed = True
            self._require_worker()
            try:
                sealed = _sealed_intent(intent)
            except Exception:
                raise WorkerIPCDenied("worker_request_invalid") from None
            req = {"v": 1, "op": "run.claim", **asdict(sealed)}
            answer = self._exchange(req)
            if self._response(answer, "claimed_once") is not True:
                raise WorkerIPCDenied("worker_response_invalid")
        finally:
            self._gate.release()

    def status(self, intent: RunIntent) -> RunStatusObservation:
        """Repeated reads are permitted; result is only a nonbinding snapshot."""
        self._require_worker()
        if not self._gate.acquire(blocking=False):
            raise WorkerIPCDenied("worker_busy")
        try:
            try:
                sealed = _sealed_intent(intent)
            except Exception:
                raise WorkerIPCDenied("worker_request_invalid") from None
            req = {"v": 1, "op": "run.status"}
            req.update({name: getattr(sealed, name) for name in _ID_KEYS})
            answer = self._exchange(req)
            claimed = self._response(answer, "claimed_now")
            return RunStatusObservation(claimed_now=claimed)
        finally:
            self._gate.release()
