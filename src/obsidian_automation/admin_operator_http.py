"""#300 unprivileged operator confirmation: fixed previews -> queue-only IPC.

This module has no direct systemd/file/secret authority. Browser confirmations
are time-limited and HMAC bound to an exact prior compatible preview's UUID,
service and action. Root Worker STILL revalidates unit state and admission.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import secrets
import struct
import time
from pathlib import Path
from typing import Callable, Mapping

from .admin_broker_socket import LocalBrokerProtocolError
from .admin_control_queue_socket import (
    DEFAULT_OPERATION_QUEUE_SOCKET, OperationQueueClient, _validate_projection,
)
from .admin_operation_broker import BrokerRejected
from .admin_operation_preflight import _REQUEST_ID
from .admin_operation_queue import _request as _queue_request
from .admin_status import SERVICES

TOKEN_VALID_SECONDS = 90
_ACCEPTED = frozenset(("pause", "resume", "run_once"))
_SERVICES = frozenset(spec.identifier for spec in SERVICES)
_TOKEN_SIZE = (8+16+32)*2
_CONFIRM_FIELDS = frozenset((
    "version", "request_id", "service_id", "action",
    "confirmation_ticket", "confirm",
))
_ERROR_CODES = frozenset((
    "queue_disabled", "queue_busy", "admission_lock_busy",
    "queue_recovery_required", "queue_missing_receipt",
    "run_once_unqualified", "queue_request_rejected",
    "operator_worker_unavailable",
))


def disabled_capabilities() -> dict[str, object]:
    return {
        "version": 1, "authority": "observation_only",
        "operations": {"pause": False, "resume": False, "run_once": False},
        "reason": "broker_unavailable",
    }


def safe_capabilities(value: object) -> dict[str, object]:
    """Never let an untrusted socket response grant a false positive."""
    fallback = disabled_capabilities()
    if (
        type(value) is not dict
        or set(value) != {"version", "authority", "operations", "reason"}
        or type(value.get("version")) is not int or value["version"] != 1
        or type(value.get("operations")) is not dict
        or set(value["operations"]) != _ACCEPTED
        or any(type(v) is not bool for v in value["operations"].values())
    ):
        return fallback
    inert = (
        value.get("authority") == "observation_only"
        and value.get("reason") in (
            "cross_updater_exclusion_unqualified", "broker_unavailable",
        )
        and not any(value["operations"].values())
    )
    operational = (
        value.get("authority") == "operator_ready"
        and value.get("reason") == "worker_ready"
        and value["operations"]["pause"] is True
        and value["operations"]["resume"] is True
    )
    return value if inert or operational else fallback


def safe_operator_receipt(value: object, request_id: str) -> dict[str, object]:
    """Only fixed, non-secret durable queue fields can reach the Dashboard."""
    unavailable = {
        "version": 1, "request_id": request_id,
        "status": "unavailable", "authority": "observation_only",
    }
    try:
        return _validate_projection(value, request_id=request_id)
    except (LocalBrokerProtocolError, BrokerRejected, KeyError, TypeError, ValueError):
        return unavailable


class ConfirmationTickets:
    """Per-Web-process, short-lived proof of an *observed* Preview selection.

    A replay only re-submits the exact same request ID, which the separate
    durable queue must deduplicate across crashes and lost HTTP replies.
    """

    def __init__(
        self, *, now: Callable[[], float] = time.monotonic,
        secret: bytes | None = None,
    ):
        self.now = now
        self.secret = secret if secret is not None else secrets.token_bytes(32)
        if type(self.secret) is not bytes or len(self.secret) < 32:
            raise ValueError("invalid_confirmation_key")

    @staticmethod
    def _message(req: Mapping[str, object], data: bytes) -> bytes:
        return (
            str(req["request_id"]).encode("ascii") + b"\x00"
            + str(req["service_id"]).encode("ascii") + b"\x00"
            + str(req["action"]).encode("ascii") + b"\x00" + data
        )

    def issue(self, req: Mapping[str, object]) -> str:
        checked = _queue_request(req)
        now = self.now()
        if not isinstance(now, (int,float)) or not 0 <= now < 2**64-120:
            raise ValueError("clock_unavailable")
        value = struct.pack("!Q", int(now)+TOKEN_VALID_SECONDS) + secrets.token_bytes(16)
        msg = self._message(checked.as_public("accepted"), value)
        tag = hmac.new(self.secret, msg, hashlib.sha256).digest()
        return (value+tag).hex()

    def verify(self, req: Mapping[str, object], token: object) -> bool:
        if (
            type(token) is not str or len(token) != _TOKEN_SIZE
            or any(c not in "0123456789abcdef" for c in token)
        ):
            return False
        try:
            checked = _queue_request(req)
            binary = bytes.fromhex(token)
            body, tag = binary[:24], binary[24:]
            msg = self._message(checked.as_public("accepted"), body)
            if not hmac.compare_digest(
                hmac.new(self.secret, msg, hashlib.sha256).digest(), tag,
            ):
                return False
            now = self.now()
            expiry = struct.unpack("!Q", body[:8])[0]
            return (
                isinstance(now,(int,float))
                and now < expiry <= now + TOKEN_VALID_SECONDS
            )
        except (ValueError, KeyError, BrokerRejected, TypeError):
            return False


def parse_confirm_request(raw: bytes) -> tuple[dict[str, object], str]:
    """Reject extra caller-controlled metadata, fake HTTP approval or paths."""
    if type(raw) is not bytes or not 1 <= len(raw) <= 512:
        raise ValueError("invalid_confirm_request")
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("duplicate")
            out[key] = value
        return out
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
        if (
            type(payload) is not dict or set(payload) != _CONFIRM_FIELDS
            or type(payload["confirm"]) is not bool
            or payload["confirm"] is not True
            or type(payload["confirmation_ticket"]) is not str
        ):
            raise ValueError("invalid")
        request = {
            "version": payload["version"],
            "request_id": payload["request_id"],
            "service_id": payload["service_id"],
            "action": payload["action"],
        }
        _queue_request(request)
        return request, payload["confirmation_ticket"]
    except (ValueError, UnicodeError, TypeError, KeyError, BrokerRejected):
        raise ValueError("invalid_confirm_request") from None


class OperatorHTTPClient:
    """The only non-root bridge to the fixed peer-authenticated Unix Socket."""

    def __init__(self, path: Path = DEFAULT_OPERATION_QUEUE_SOCKET):
        self.path = path

    def capabilities(self) -> dict[str, object]:
        try:
            return safe_capabilities(
                OperationQueueClient(self.path, timeout=1.0).capabilities(),
            )
        except (OSError, LocalBrokerProtocolError, BrokerRejected):
            return disabled_capabilities()

    def submit(self, request: Mapping[str, object]) -> dict[str, object]:
        try:
            response = OperationQueueClient(self.path, timeout=1.0).submit(request)
            if (
                type(response) is dict and set(response) == {"version", "error"}
                and type(response["version"]) is int and response["version"] == 1
                and type(response["error"]) is str
                and response["error"] in _ERROR_CODES
            ):
                return response
            return safe_operator_receipt(response, str(request["request_id"]))
        except (OSError, LocalBrokerProtocolError, BrokerRejected, KeyError):
            return {"version": 1, "error": "operator_submission_unconfirmed"}

    def receipt(self, request_id: str) -> dict[str, object]:
        if type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
            return {
                "version": 1, "status": "unavailable",
                "authority": "observation_only",
            }
        try:
            response = OperationQueueClient(self.path, timeout=1.0).receipt(request_id)
            if type(response) is dict and set(response) == {"version", "error"}:
                return {
                    "version": 1, "request_id": request_id,
                    "status": "unavailable", "authority": "observation_only",
                }
            return safe_operator_receipt(response, request_id)
        except (OSError, LocalBrokerProtocolError, BrokerRejected):
            return {
                "version": 1, "request_id": request_id,
                "status": "unavailable", "authority": "observation_only",
            }
