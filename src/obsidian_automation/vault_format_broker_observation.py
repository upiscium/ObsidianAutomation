"""#472 D1/B1/A0: UNWIRED Owner->Broker read-only observation client.

Does not implement a Broker, issue credentials, dispatch effects, validate
provider finality, acquire the Owner effect lock, or authorize Writer/Vault I/O.
Only a separately provisioned Broker UID/socket can become a trusted peer.
Linux SO_PEERCRED proves the CONNECTED process UID, not forwarded origin.
A Broker response is an OBSERVATION, not a durable settlement or effect permit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import struct
import time
from typing import Any

from .vault_format_effect_journal import EffectAttempt, _seal
from .vault_format_worker_ipc_client import (
    WorkerIPCDenied, _require_socket_path, _socket_identity, _trusted_parent,
)

_SHA = re.compile(r"[a-f0-9]{64}\Z")
_MAX_REQUEST = 1024
_MAX_RESPONSE = 1024
_TOTAL_SECONDS = 3.0
_PEERCRED = struct.Struct("3i")
_FAILURES = frozenset(("broker_denied", "broker_missing", "broker_unavailable"))
_PENDING = "pending"
_TERMINAL = frozenset(("verified_remote_effect", "verified_no_effect"))


class BrokerObservationDenied(ValueError):
    """Fixed non-sensitive error codes only; no raw payloads or paths."""


@dataclass(frozen=True, slots=True)
class BrokerCredentialObservation:
    """Connected Broker's read-only generation report; NOT claim authority."""

    account_binding_sha256: str
    credential_generation: str
    credential_epoch: int
    broker_ledger_revision: str


@dataclass(frozen=True, slots=True)
class BrokerFinalityObservation:
    """Exact-operation Broker report, never itself a settlement permission."""

    run_id: str
    operation_id: str
    account_binding_sha256: str
    attempt_sha256: str
    disposition: str
    broker_journal_seq: int | None
    receipt_sha256: str | None


def _valid_sha(value: object) -> bool:
    return type(value) is str and _SHA.fullmatch(value) is not None


def _valid_int(value: object, *, positive: bool = False) -> bool:
    return (type(value) is int and value < (1 << 62)
            and (value > 0 if positive else value >= 0))


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict:
    out = {}
    for name, value in pairs:
        if name in out:
            raise ValueError("duplicate JSON member")
        out[name] = value
    return out


def _bad_constant(_: str) -> None:
    raise ValueError("nonstandard JSON constant")


def _decode_frame(sock: socket.socket, deadline: float) -> dict:
    """One line only, terminated by actual EOF; reject split/tail framing."""
    data = bytearray()
    try:
        while len(data) <= _MAX_RESPONSE:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrokerObservationDenied("broker_response_invalid")
            sock.settimeout(remaining)
            part = sock.recv(_MAX_RESPONSE + 1 - len(data))
            if not part:
                break
            data.extend(part)
    except (OSError, TimeoutError):
        raise BrokerObservationDenied("broker_response_invalid") from None
    if (not data or len(data) > _MAX_RESPONSE or b"\x00" in data
            or data.count(b"\n") != 1 or not data.endswith(b"\n")):
        raise BrokerObservationDenied("broker_response_invalid")
    try:
        obj = json.loads(
            data[:-1].decode("ascii", errors="strict"),
            object_pairs_hook=_object_pairs,
            parse_constant=_bad_constant,
        )
    except (ValueError, UnicodeError, TypeError):
        raise BrokerObservationDenied("broker_response_invalid") from None
    if (type(obj) is not dict or type(obj.get("v")) is not int
            or obj["v"] != 1 or type(obj.get("ok")) is not bool):
        raise BrokerObservationDenied("broker_response_invalid")
    return obj


def _attempt_key(attempt: EffectAttempt) -> tuple[EffectAttempt, str]:
    try:
        sealed = _seal(attempt)
        raw = json.dumps(
            asdict(sealed), sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        ).encode("ascii")
        return sealed, hashlib.sha256(raw).hexdigest()
    except Exception:
        raise BrokerObservationDenied("broker_request_invalid") from None


class OwnerBrokerObservationClient:
    """Bounded, peer-UID-authenticated, read-only Owner-to-Broker client.

    A real Owner process and preprovisioned broker-owned 0710 directory /
    0660 socket require distinct OS UIDs and shared restricted IPC GID.
    The Broker daemon, credential store, authenticated journal and issuer
    are not implemented or installed by this class.
    """

    def __init__(
        self, path: Path, *, broker_uid: int, ipc_group_gid: int,
    ) -> None:
        if (not hasattr(socket, "SO_PEERCRED")
                or type(broker_uid) is not int or broker_uid <= 0
                or type(ipc_group_gid) is not int or ipc_group_gid < 0
                or os.geteuid() == 0 or os.geteuid() == broker_uid):
            raise BrokerObservationDenied("broker_policy_invalid")
        try:
            _require_socket_path(path)
        except WorkerIPCDenied:
            raise BrokerObservationDenied("broker_path_invalid") from None
        self._path = path
        self._broker_uid = broker_uid
        self._ipc_group_gid = ipc_group_gid
        self._owner_uid = os.geteuid()
        self._owner_pid = os.getpid()

    def _require_owner_process(self) -> None:
        if (os.getpid() != self._owner_pid
                or os.geteuid() != self._owner_uid
                or self._owner_uid == self._broker_uid):
            raise BrokerObservationDenied("broker_owner_mismatch")

    @staticmethod
    def _peer_uid(sock: socket.socket) -> int:
        try:
            if (sock.family != socket.AF_UNIX
                    or sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
                    != socket.SOCK_STREAM):
                raise BrokerObservationDenied("broker_peer_invalid")
            raw = sock.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED.size,
            )
            if len(raw) != _PEERCRED.size:
                raise BrokerObservationDenied("broker_peer_invalid")
            pid, uid, gid = _PEERCRED.unpack(raw)
            if pid <= 0 or uid < 0 or gid < 0:
                raise BrokerObservationDenied("broker_peer_invalid")
            return uid
        except (OSError, AttributeError, ValueError, struct.error):
            raise BrokerObservationDenied("broker_peer_invalid") from None

    @staticmethod
    def _deadline(sock: socket.socket, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrokerObservationDenied("broker_transport_unverified")
        sock.settimeout(remaining)

    def _exchange(self, request: dict) -> dict:
        """One authenticated socket and one full response, no retry/dispatch."""
        self._require_owner_process()
        # This class is read-only even when a caller reaches this helper.
        if (type(request) is not dict
                or type(request.get("v")) is not int or request["v"] != 1):
            raise BrokerObservationDenied("broker_request_invalid")
        operation = request.get("op")
        if operation == "credential.observe":
            valid = (set(request) == {"v", "op", "account_binding_sha256"}
                     and _valid_sha(request.get("account_binding_sha256")))
        elif operation == "operation.finality":
            valid = (set(request) == {"v", "op", "run_id",
                                      "operation_id", "account_binding_sha256",
                                      "attempt_sha256"}
                     and all(_valid_sha(request.get(k))
                             for k in ("account_binding_sha256", "attempt_sha256"))
                     and all(type(request.get(k)) is str
                             and len(request[k]) == 32
                             and all(ch in "0123456789abcdef" for ch in request[k])
                             for k in ("run_id", "operation_id")))
        else:
            valid = False
        if not valid:
            raise BrokerObservationDenied("broker_request_invalid")
        try:
            frame = json.dumps(
                request, separators=(",", ":"), ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii") + b"\n"
        except (ValueError, TypeError, UnicodeError):
            raise BrokerObservationDenied("broker_request_invalid") from None
        if len(frame) > _MAX_REQUEST:
            raise BrokerObservationDenied("broker_request_invalid")
        deadline = time.monotonic() + _TOTAL_SECONDS
        directory_fd = -1
        conn: socket.socket | None = None
        response: dict | None = None
        failure: str | None = None
        try:
            try:
                directory_fd = _trusted_parent(
                    self._path, self._broker_uid, self._ipc_group_gid,
                )
                sock_identity = _socket_identity(
                    directory_fd, self._path.name,
                    self._broker_uid, self._ipc_group_gid,
                )
            except WorkerIPCDenied:
                raise BrokerObservationDenied("broker_socket_untrusted") from None
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._deadline(conn, deadline)
            conn.connect(str(self._path))
            if self._peer_uid(conn) != self._broker_uid:
                raise BrokerObservationDenied("broker_peer_untrusted")
            # The preprovisioned socket pathname must not have changed since
            # before connect; the kernel-verified peer must be that Broker.
            try:
                after = _socket_identity(
                    directory_fd, self._path.name,
                    self._broker_uid, self._ipc_group_gid,
                )
            except WorkerIPCDenied:
                raise BrokerObservationDenied("broker_socket_untrusted") from None
            if after != sock_identity:
                raise BrokerObservationDenied("broker_socket_untrusted")
            self._require_owner_process()
            self._deadline(conn, deadline)
            conn.sendall(frame)
            self._deadline(conn, deadline)
            conn.shutdown(socket.SHUT_WR)
            response = _decode_frame(conn, deadline)
        except BrokerObservationDenied as error:
            failure = str(error)
        except (TimeoutError, OSError):
            failure = "broker_transport_unverified"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    failure = "broker_cleanup_unverified"
            if directory_fd != -1:
                try:
                    os.close(directory_fd)
                except OSError:
                    failure = "broker_cleanup_unverified"
        if failure is not None:
            raise BrokerObservationDenied(failure)
        self._require_owner_process()
        if response is None:
            raise BrokerObservationDenied("broker_transport_unverified")
        return response

    @staticmethod
    def _require_response(response: dict, expected: dict, keys: set[str]) -> None:
        if response["ok"] is False:
            if (set(response) != {"v", "ok", "reason"}
                    or type(response.get("reason")) is not str
                    or response["reason"] not in _FAILURES):
                raise BrokerObservationDenied("broker_response_invalid")
            raise BrokerObservationDenied("broker_observation_unavailable")
        if set(response) != keys or response["ok"] is not True:
            raise BrokerObservationDenied("broker_response_invalid")
        for key, value in expected.items():
            if type(response.get(key)) is not type(value) or response[key] != value:
                raise BrokerObservationDenied("broker_identity_mismatch")

    def observe_credential(
        self, account_binding_sha256: str,
    ) -> BrokerCredentialObservation:
        """Read-only Broker report; NOT an authority to rotate or admit."""
        if not _valid_sha(account_binding_sha256):
            raise BrokerObservationDenied("broker_request_invalid")
        expected = {
            "v": 1, "op": "credential.observe",
            "account_binding_sha256": account_binding_sha256,
        }
        response = self._exchange(expected)
        self._require_response(
            response, expected, set(expected) | {
                "ok", "credential_generation", "credential_epoch",
                "broker_ledger_revision",
            },
        )
        if (not _valid_sha(response.get("credential_generation"))
                or not _valid_int(response.get("credential_epoch"))
                or not _valid_sha(response.get("broker_ledger_revision"))):
            raise BrokerObservationDenied("broker_response_invalid")
        return BrokerCredentialObservation(
            account_binding_sha256=account_binding_sha256,
            credential_generation=response["credential_generation"],
            credential_epoch=response["credential_epoch"],
            broker_ledger_revision=response["broker_ledger_revision"],
        )

    def observe_finality(
        self, approved: EffectAttempt,
    ) -> BrokerFinalityObservation:
        """Query one immutable attempt digest; never write, dispatch or settle.

        A future Broker must prove durable operation-specific finality in its
        own trusted journal; its claimed receipt is NOT accepted by Owner
        journal or Writer in A0.
        """
        record, attempt_sha256 = _attempt_key(approved)
        expected = {
            "v": 1, "op": "operation.finality",
            "run_id": record.run_id,
            "operation_id": record.operation_id,
            "account_binding_sha256": record.account_binding_sha256,
            "attempt_sha256": attempt_sha256,
        }
        response = self._exchange(expected)
        if response.get("ok") is False:
            self._require_response(response, expected, set())
        disposition = response.get("disposition")
        if disposition == _PENDING:
            self._require_response(
                response, expected, set(expected) | {"ok", "disposition"},
            )
            return BrokerFinalityObservation(
                run_id=record.run_id,
                operation_id=record.operation_id,
                account_binding_sha256=record.account_binding_sha256,
                attempt_sha256=attempt_sha256, disposition=_PENDING,
                broker_journal_seq=None, receipt_sha256=None,
            )
        if type(disposition) is not str or disposition not in _TERMINAL:
            raise BrokerObservationDenied("broker_response_invalid")
        self._require_response(
            response, expected, set(expected) | {
                "ok", "disposition", "broker_journal_seq", "receipt_sha256",
            },
        )
        if (not _valid_int(response.get("broker_journal_seq"), positive=True)
                or not _valid_sha(response.get("receipt_sha256"))
                or response["receipt_sha256"] == "0" * 64):
            raise BrokerObservationDenied("broker_response_invalid")
        return BrokerFinalityObservation(
            run_id=record.run_id,
            operation_id=record.operation_id,
            account_binding_sha256=record.account_binding_sha256,
            attempt_sha256=attempt_sha256, disposition=disposition,
            broker_journal_seq=response["broker_journal_seq"],
            receipt_sha256=response["receipt_sha256"],
        )
