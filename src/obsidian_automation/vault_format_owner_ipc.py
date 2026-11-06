"""#472 R2/B2c-A0: unwired, one-request Unix peer-credential IPC boundary.

No socket listener, filesystem socket path, OS users, signer, scheduler,
network, or Writer is created. Future trusted owner service must provision
the socket and its parent safely. These functions are not an API server.

SO_PEERCRED is Linux-only and authenticates the CONNECTED PEER UID, not the
source of a forwarded request. Same-UID callers are the same OS principal.
A read-only status answer is NOT an effect-time lease or Writer permission.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import time
from typing import Any

from .vault_format_owner_coordination import (
    OperatorRunCoordinator, OwnerCoordinationDenied,
)
from .vault_format_run_ledger import RunIntent, _sealed_intent

_MAX_FRAME = 2048
_SOCKET_TIMEOUT = 1.0
_PEERCRED = struct.Struct("3i")
_ID_FIELDS = ("run_id", "plan_sha256", "policy_sha256", "operation_id")
_INTENT_FIELDS = tuple(RunIntent.__dataclass_fields__)
_WORKER_OPS = frozenset(("run.claim", "run.status"))
_OWNER_OPS = frozenset(("run.stop", "owner.recover"))
_CODES = frozenset((
    "ipc_denied", "ipc_unsupported", "ipc_invalid", "ipc_frame_invalid",
    "ipc_peer_invalid", "ipc_unavailable", "ipc_run_unverified",
    "ipc_claim_denied", "ipc_stop_unverified", "ipc_recovery_unverified",
))


class OwnerIPCDenied(ValueError):
    """Public reason codes never include request/credential values."""


def _duplicates_denied(pairs: list[tuple[str, Any]]) -> dict:
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate field")
        out[key] = value
    return out


def _reject_constant(_: str) -> None:
    raise ValueError("invalid numeric constant")


def _request_frame(conn: socket.socket) -> dict:
    """Require one bounded frame and a write-half-close before dispatch."""
    data = bytearray()
    deadline = time.monotonic() + _SOCKET_TIMEOUT
    try:
        while len(data) <= _MAX_FRAME:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OwnerIPCDenied("ipc_frame_invalid")
            conn.settimeout(remaining)
            chunk = conn.recv(_MAX_FRAME + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
            # Unix streams have no frame boundary. A newline returned by
            # recv() cannot prove that further bytes are not forthcoming.
            # Require peer write-half-close/EOF under the total deadline.
    except (TimeoutError, OSError):
        raise OwnerIPCDenied("ipc_frame_invalid") from None
    if (not data or len(data) > _MAX_FRAME or data.count(b"\n") != 1
            or not data.endswith(b"\n") or b"\x00" in data):
        raise OwnerIPCDenied("ipc_frame_invalid")
    try:
        request = json.loads(
            data[:-1].decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicates_denied,
            parse_constant=_reject_constant,
        )
    except (ValueError, UnicodeError, TypeError):
        raise OwnerIPCDenied("ipc_frame_invalid") from None
    if type(request) is not dict:
        raise OwnerIPCDenied("ipc_invalid")
    return request


class OperatorUnixIPC:
    """Protocol enforcement over an ALREADY ACCEPTED AF_UNIX connection.

    Constructor has no test-mode or peer-identity injection. Worker must be
    an OS principal different from this separately trusted owner UID.
    `serve_once` retrieves the UID using Linux SO_PEERCRED before any input.
    The future listener must enforce socket inode ownership, filesystem mode,
    parent traversal and lifecycle/cleanup independently.
    """

    def __init__(self, owner: OperatorRunCoordinator, *, worker_uid: int) -> None:
        if type(owner) is not OperatorRunCoordinator:
            raise OwnerIPCDenied("ipc_invalid")
        if not hasattr(socket, "SO_PEERCRED"):
            raise OwnerIPCDenied("ipc_unsupported")
        owner._require_owner()
        uid = os.geteuid()
        if type(worker_uid) is not int or worker_uid < 0 or worker_uid == uid:
            raise OwnerIPCDenied("ipc_invalid")
        self._owner = owner
        self._owner_uid = uid
        self._worker_uid = worker_uid

    @staticmethod
    def _peer_uid(conn: socket.socket) -> int:
        try:
            if (conn.family != socket.AF_UNIX
                    or conn.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
                    != socket.SOCK_STREAM):
                raise OwnerIPCDenied("ipc_peer_invalid")
            raw = conn.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED.size,
            )
            if len(raw) != _PEERCRED.size:
                raise OwnerIPCDenied("ipc_peer_invalid")
            pid, uid, gid = _PEERCRED.unpack(raw)
            if pid <= 0 or uid < 0 or gid < 0:
                raise OwnerIPCDenied("ipc_peer_invalid")
            return uid
        except (OSError, AttributeError, ValueError, struct.error):
            raise OwnerIPCDenied("ipc_peer_invalid") from None

    def _dispatch(self, peer_uid: int, req: dict) -> dict:
        """Exact-role, exact-schema commands; no Vault body/credentials."""
        self._owner._require_owner()
        if (type(peer_uid) is not int
                or peer_uid not in (self._worker_uid, self._owner_uid)
                or type(req.get("v")) is not int or req["v"] != 1
                or type(req.get("op")) is not str):
            raise OwnerIPCDenied("ipc_denied")
        op = req["op"]
        if op in _WORKER_OPS:
            if peer_uid != self._worker_uid:
                raise OwnerIPCDenied("ipc_denied")
        elif op in _OWNER_OPS:
            if peer_uid != self._owner_uid:
                raise OwnerIPCDenied("ipc_denied")
        else:
            raise OwnerIPCDenied("ipc_unsupported")

        if op == "run.status":
            if set(req) != {"v", "op", *_ID_FIELDS}:
                raise OwnerIPCDenied("ipc_invalid")
            values = [req[k] for k in _ID_FIELDS]
            if any(type(v) is not str for v in values):
                raise OwnerIPCDenied("ipc_invalid")
            # This is *observation*, not permission to transmit a PUT.
            try:
                self._owner._require_ledger_binding()
                claimed = (
                    self._owner.ledger.verify_claimed(*values)
                    and self._owner.drain.verifier(*values)
                )
                self._owner._require_ledger_binding()
            except Exception:
                raise OwnerIPCDenied("ipc_run_unverified") from None
            return {"v": 1, "ok": True, "claimed_now": claimed is True}

        if op == "run.claim":
            if set(req) != {"v", "op", *_INTENT_FIELDS}:
                raise OwnerIPCDenied("ipc_invalid")
            values = [req[k] for k in _INTENT_FIELDS]
            if any(type(v) is not str for v in values):
                raise OwnerIPCDenied("ipc_invalid")
            try:
                self._owner.claim_signed(_sealed_intent(RunIntent(*values)))
            except Exception:
                raise OwnerIPCDenied("ipc_claim_denied") from None
            return {"v": 1, "ok": True, "claimed_once": True}

        if op == "run.stop":
            if set(req) != {"v", "op", "run_id"} or type(req["run_id"]) is not str:
                raise OwnerIPCDenied("ipc_invalid")
            try:
                result = self._owner.stop_and_revoke(req["run_id"])
            except Exception:
                raise OwnerIPCDenied("ipc_stop_unverified") from None
            if result not in ("stopped_after_drain", "already_stopped"):
                raise OwnerIPCDenied("ipc_stop_unverified")
            return {"v": 1, "ok": True, "stopped": True}

        if set(req) != {"v", "op"}:
            raise OwnerIPCDenied("ipc_invalid")
        try:
            count = self._owner.recover_after_quiescence()
        except Exception:
            raise OwnerIPCDenied("ipc_recovery_unverified") from None
        return {"v": 1, "ok": True, "unconfirmed": count}

    def serve_once(self, conn: socket.socket) -> None:
        """Process one connection and close it. No general-purpose RPC loop.

        Authentication MUST precede parsing or any Ledger/Owner side effect.
        Public response is bounded and uses static reason codes only.
        """
        try:
            peer_uid = self._peer_uid(conn)
            # Reject unrelated OS principals BEFORE allowing any request
            # bytes or framing timeout to consume this handler's capacity.
            if peer_uid not in (self._worker_uid, self._owner_uid):
                raise OwnerIPCDenied("ipc_denied")
            conn.settimeout(_SOCKET_TIMEOUT)
            req = _request_frame(conn)
            response = self._dispatch(peer_uid, req)
        except OwnerIPCDenied as error:
            reason = str(error)
            response = {"v": 1, "ok": False,
                        "reason": reason if reason in _CODES else "ipc_unavailable"}
        except Exception:
            response = {"v": 1, "ok": False, "reason": "ipc_unavailable"}
        try:
            conn.sendall(json.dumps(response, separators=(",", ":")).encode("ascii") + b"\n")
        except OSError:
            # Never retry any mutating command merely because reply failed.
            pass
        finally:
            conn.close()
