"""#472 D1/B1/B2 — UNWIRED accepted-socket Broker read-only responder.

Processes ONE already accepted Linux AF_UNIX stream after checking the
connected Owner peer via SO_PEERCRED, then closes it. This is NOT a socket
listener, daemon, Owner Claim validator, credential issuer, Provider egress
endpoint, Writer capability, or authenticated Provider finality proof.
No socket is created/bound here and nothing imports this in production.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
import socket
import struct
import time

from .vault_format_broker_journal import (
    BrokerJournalDenied, BrokerPrivateJournal, _check_name,
)
from .vault_format_broker_observation import (
    BrokerCredentialObservation, BrokerFinalityObservation, _bad_constant,
    _object_pairs, _valid_int, _valid_sha, _MAX_REQUEST, _MAX_RESPONSE,
)

_PEERCRED = struct.Struct("3i")
_TIMEOUT = 3.0


class BrokerReadOnlyDenied(ValueError):
    """Fixed, non-sensitive local failure codes; not a source of authority."""


def _parse_request(conn: socket.socket, deadline: float) -> dict:
    """Read exactly one bounded ASCII JSON line, with actual EOF proof."""
    data = bytearray()
    try:
        while len(data) <= _MAX_REQUEST:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrokerReadOnlyDenied("broker_request_invalid")
            conn.settimeout(remaining)
            chunk = conn.recv(_MAX_REQUEST + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
    except (TimeoutError, OSError):
        raise BrokerReadOnlyDenied("broker_request_invalid") from None
    if (not data or len(data) > _MAX_REQUEST or data.count(b"\n") != 1
            or not data.endswith(b"\n") or b"\x00" in data):
        raise BrokerReadOnlyDenied("broker_request_invalid")
    try:
        req = json.loads(
            data[:-1].decode("ascii", errors="strict"),
            object_pairs_hook=_object_pairs, parse_constant=_bad_constant,
        )
    except (ValueError, UnicodeError, TypeError):
        raise BrokerReadOnlyDenied("broker_request_invalid") from None
    if (type(req) is not dict or type(req.get("v")) is not int
            or req["v"] != 1 or type(req.get("op")) is not str):
        raise BrokerReadOnlyDenied("broker_request_invalid")
    op = req["op"]
    if op == "credential.observe":
        if (set(req) != {"v", "op", "account_binding_sha256"}
                or not _valid_sha(req.get("account_binding_sha256"))):
            raise BrokerReadOnlyDenied("broker_request_invalid")
    elif op == "operation.finality":
        if (set(req) != {
                "v", "op", "run_id", "operation_id",
                "account_binding_sha256", "attempt_sha256",
        } or not _check_name(req.get("run_id"), 32)
                or not _check_name(req.get("operation_id"), 32)
                or not _valid_sha(req.get("account_binding_sha256"))
                or not _valid_sha(req.get("attempt_sha256"))):
            raise BrokerReadOnlyDenied("broker_request_invalid")
    else:
        # No PUT/dispatch/rotate/reconcile/Owner journal operations.
        raise BrokerReadOnlyDenied("broker_request_invalid")
    return req


def _encode(response: dict) -> bytes:
    if (type(response) is not dict or type(response.get("v")) is not int
            or response["v"] != 1 or type(response.get("ok")) is not bool):
        raise BrokerReadOnlyDenied("broker_response_invalid")
    try:
        frame = json.dumps(
            response, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii") + b"\n"
    except (TypeError, ValueError, UnicodeError):
        raise BrokerReadOnlyDenied("broker_response_invalid") from None
    if len(frame) > _MAX_RESPONSE:
        raise BrokerReadOnlyDenied("broker_response_invalid")
    return frame


class BrokerReadOnlySession:
    """Handle one pre-accepted stream, not a runnable listener.

    The separately installed Broker process/UID and pre-provisioned Broker-
    owned 0710 parent / 0660 socket lifecycle are NOT supplied here.
    Owner UID and GID are pinned at construction; successful tests must
    substitute SO_PEERCRED to model a distinct Owner principal, because
    the local CI test runner deliberately has only one actual OS UID.
    """

    def __init__(
        self, journal: BrokerPrivateJournal, *,
        owner_uid: int, owner_gid: int,
    ) -> None:
        if (not hasattr(socket, "SO_PEERCRED")
                or type(journal) is not BrokerPrivateJournal
                or os.geteuid() == 0
                or type(owner_uid) is not int or owner_uid <= 0
                or type(owner_gid) is not int or owner_gid <= 0
                or owner_uid == os.geteuid()):
            raise BrokerReadOnlyDenied("broker_session_policy_invalid")
        journal._require_process()
        self._journal = journal
        self._broker_uid = os.geteuid()
        self._broker_gid = os.getegid()
        self._broker_pid = os.getpid()
        self._owner_uid = owner_uid
        self._owner_gid = owner_gid

    def _require_broker(self) -> None:
        if (os.geteuid() != self._broker_uid
                or os.getegid() != self._broker_gid
                or os.getpid() != self._broker_pid):
            raise BrokerReadOnlyDenied("broker_process_changed")
        self._journal._require_process()

    @staticmethod
    def _connected_peer(conn: socket.socket) -> tuple[int, int, int]:
        """Kernel connected credentials; not the origin of a forwarded FD."""
        try:
            if (conn.family != socket.AF_UNIX
                    or conn.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
                    != socket.SOCK_STREAM):
                raise BrokerReadOnlyDenied("broker_peer_invalid")
            raw = conn.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED.size,
            )
            if len(raw) != _PEERCRED.size:
                raise BrokerReadOnlyDenied("broker_peer_invalid")
            pid, uid, gid = _PEERCRED.unpack(raw)
            if pid <= 0 or uid < 0 or gid < 0:
                raise BrokerReadOnlyDenied("broker_peer_invalid")
            return pid, uid, gid
        except (OSError, ValueError, AttributeError, struct.error):
            raise BrokerReadOnlyDenied("broker_peer_invalid") from None

    def _handle(self, req: dict) -> dict:
        self._require_broker()
        operation = req["op"]
        if operation == "credential.observe":
            obs = self._journal.observe_credential(
                req["account_binding_sha256"],
            )
            if (type(obs) is not BrokerCredentialObservation
                    or obs.account_binding_sha256 != req["account_binding_sha256"]
                    or not _valid_sha(obs.credential_generation)
                    or not _valid_int(obs.credential_epoch)
                    or not _valid_sha(obs.broker_ledger_revision)):
                raise BrokerReadOnlyDenied("broker_response_invalid")
            return {
                "v": 1, "ok": True, "op": operation,
                **asdict(obs),
            }

        if operation == "operation.finality":
            obs = self._journal.observe_finality_by_key(
                run_id=req["run_id"], operation_id=req["operation_id"],
                account_binding_sha256=req["account_binding_sha256"],
                attempt_sha256=req["attempt_sha256"],
            )
            expected = {
                "run_id": req["run_id"], "operation_id": req["operation_id"],
                "account_binding_sha256": req["account_binding_sha256"],
                "attempt_sha256": req["attempt_sha256"],
            }
            if (type(obs) is not BrokerFinalityObservation
                    or any(getattr(obs, k) != v for k, v in expected.items())):
                raise BrokerReadOnlyDenied("broker_response_invalid")
            if obs.disposition == "pending":
                if obs.broker_journal_seq is not None or obs.receipt_sha256 is not None:
                    raise BrokerReadOnlyDenied("broker_response_invalid")
                return {
                    "v": 1, "ok": True, "op": operation,
                    **expected, "disposition": "pending",
                }
            if (obs.disposition != "verified_no_effect"
                    or not _valid_int(obs.broker_journal_seq, positive=True)
                    or not _valid_sha(obs.receipt_sha256)
                    or obs.receipt_sha256 == "0" * 64):
                # No verified_remote_effect path in D1/B1/B2.
                raise BrokerReadOnlyDenied("broker_response_invalid")
            return {
                "v": 1, "ok": True, "op": operation,
                **expected, "disposition": "verified_no_effect",
                "broker_journal_seq": obs.broker_journal_seq,
                "receipt_sha256": obs.receipt_sha256,
            }
        raise BrokerReadOnlyDenied("broker_request_invalid")

    def _serve_connected(self, conn: socket.socket) -> None:
        """Process one peer without closing it; trusted caller owns cleanup.

        Wrong peer receives NO bytes or Broker state access. Authenticated
        malformed requests receive a fixed generic denial only. A sent
        response is an observation, never durable Owner settlement.
        """
        # The trusted listener owns this descriptor and will close it once.
        try:
            self._require_broker()
            _, uid, gid = self._connected_peer(conn)
            if uid != self._owner_uid or gid != self._owner_gid:
                return
        except (BrokerReadOnlyDenied, BrokerJournalDenied):
            return
        try:
            deadline = time.monotonic() + _TIMEOUT
            request = _parse_request(conn, deadline)
            self._require_broker()
            response = self._handle(request)
        except BrokerReadOnlyDenied:
            response = {"v": 1, "ok": False, "reason": "broker_denied"}
        except BrokerJournalDenied as error:
            # Unknown/mismatch must not reveal account or operation
            # existence; a missing/unsafe DB cannot produce success.
            reason = str(error)
            denied = (
                reason in (
                    "broker_account_unknown", "broker_account_invalid",
                    "broker_attempt_unknown", "broker_attempt_mismatch",
                    "broker_attempt_invalid",
                )
            )
            response = {
                "v": 1, "ok": False,
                "reason": "broker_denied" if denied else "broker_unavailable",
            }
        except Exception:
            response = {"v": 1, "ok": False, "reason": "broker_unavailable"}
        try:
            frame = _encode(response)
        except BrokerReadOnlyDenied:
            frame = b'{"v":1,"ok":false,"reason":"broker_unavailable"}\n'
        try:
            conn.settimeout(max(0.01, deadline - time.monotonic()))
            conn.sendall(frame)
            conn.shutdown(socket.SHUT_WR)
        except (OSError, TimeoutError):
            # Never retry or presume that a response was delivered.
            pass

    def serve_once(self, conn: socket.socket) -> None:
        """Standalone B2 use: handler owns the final connection close."""
        try:
            self._serve_connected(conn)
        finally:
            try:
                conn.close()
            except OSError:
                raise BrokerReadOnlyDenied("broker_cleanup_unverified") from None
