"""No-write browser operation-preview transport for loopback Dashboard #300.

One-use, process-local CSRF tokens prevent off-origin sites from submitting
even an innocuous preview request. A preview never authorizes an operation or
does any effect; privileged systemd action and durable receipts require a
different, separately qualified worker and human approval to activate.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import secrets
import struct
import threading
import time
from typing import Callable, Mapping

from .admin_operation_preflight import ACTIONS, _REQUEST_ID
from .admin_status import SERVICES

MAX_REQUEST_BYTES = 512
TOKEN_LIFETIME_SECONDS = 120
MAX_CONSUMED_TOKENS = 512
_TOKEN_BYTES = 8 + 16 + 32  # expiry, 128-bit nonce, SHA256 MAC
_PERMITTED_IDS = frozenset(spec.identifier for spec in SERVICES)
_ALLOWED_KEYS = frozenset(("version", "request_id", "service_id", "action"))


class PreviewRequestRejected(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class OneTimePreviewCsrf:
    """Stateless issuance + bounded consumed-nonce replay protection.

    Untrusted GETs cannot starve other sessions by filling an issuance pool.
    Only a client that can read and submit a valid HMAC token can consume a
    nonce-cache entry; valid issuance itself allocates no per-token state.
    """

    def __init__(self, *, now: Callable[[], float] = time.monotonic):
        self.now = now
        self._secret = secrets.token_bytes(32)
        self._consumed: dict[bytes, int] = {}
        self._lock = threading.Lock()

    def _purge(self, instant: float) -> None:
        for key, expiry in tuple(self._consumed.items()):
            if expiry <= instant:
                self._consumed.pop(key, None)

    def issue(self) -> dict[str, object]:
        instant = self.now()
        if not isinstance(instant, (int, float)) or not 0 <= instant < 2**64 - 121:
            raise PreviewRequestRejected("preview_clock_unavailable")
        expiry = int(instant) + TOKEN_LIFETIME_SECONDS
        material = struct.pack("!Q", expiry) + secrets.token_bytes(16)
        tag = hmac.new(self._secret, material, hashlib.sha256).digest()
        return {
            "version": 1, "authority": "preview_only",
            "csrf_token": (material + tag).hex(),
            "expires_in_seconds": TOKEN_LIFETIME_SECONDS,
            "can_execute": False,
        }

    def consume(self, token: object) -> bool:
        if (
            type(token) is not str or len(token) != _TOKEN_BYTES * 2
            or any(char not in "0123456789abcdef" for char in token)
        ):
            return False
        try:
            raw = bytes.fromhex(token)
        except ValueError:
            return False
        material, tag = raw[:24], raw[24:]
        expected = hmac.new(self._secret, material, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            return False
        expiry = struct.unpack("!Q", material[:8])[0]
        instant = self.now()
        if not isinstance(instant, (int, float)):
            return False
        if instant >= expiry or expiry > instant + TOKEN_LIFETIME_SECONDS:
            return False
        nonce = material[8:24]
        with self._lock:
            self._purge(instant)
            if nonce in self._consumed or len(self._consumed) >= MAX_CONSUMED_TOKENS:
                return False
            self._consumed[nonce] = expiry
            return True


def parse_operation_preview_request(raw: bytes) -> dict[str, object]:
    """Validate exact browser JSON, with no caller-supplied snapshot/command."""
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_REQUEST_BYTES:
        raise PreviewRequestRejected("invalid_preview_payload")
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate")
                result[key] = value
            return result
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise PreviewRequestRejected("invalid_preview_payload") from None
    if (
        type(obj) is not dict
        or set(obj) != _ALLOWED_KEYS
        or type(obj.get("version")) is not int
        or obj["version"] != 1
        or type(obj["request_id"]) is not str
        or _REQUEST_ID.fullmatch(obj["request_id"]) is None
        or type(obj["service_id"]) is not str
        or obj["service_id"] not in _PERMITTED_IDS
        or type(obj["action"]) is not str
        or obj["action"] not in ACTIONS
    ):
        raise PreviewRequestRejected("invalid_preview_payload")
    return {
        "version": 1, "op": "preview",
        "request_id": obj["request_id"],
        "service_id": obj["service_id"], "action": obj["action"],
    }


def unavailable_preview() -> dict[str, object]:
    return {
        "version": 1, "authority": "preview_only", "can_execute": False,
        "status": "rejected", "reason": "preview_unavailable",
    }


def strict_browser_preview(
    result: object, request: Mapping[str, object],
) -> dict[str, object]:
    """Second fail-closed trust boundary for any returned socket projection."""
    fallback = unavailable_preview()
    if (
        type(result) is not dict
        or set(result) not in (
            {"version", "authority", "can_execute", "status", "reason"},
            {"version", "authority", "can_execute", "status", "reason", "candidate"},
        )
        or type(result.get("version")) is not int or result["version"] != 1
        or result["authority"] != "preview_only"
        or type(result["can_execute"]) is not bool or result["can_execute"] is not False
        or result["status"] not in ("rejected", "compatible")
        or type(result["reason"]) is not str
        or len(result["reason"]) > 80
    ):
        return fallback
    if result["status"] == "compatible":
        candidate = result.get("candidate")
        if (
            type(candidate) is not dict
            or set(candidate) != {
                "request_id", "service_id", "action", "observed_at", "authority",
            }
            or candidate.get("request_id") != request["request_id"]
            or candidate.get("service_id") != request["service_id"]
            or candidate.get("action") != request["action"]
            or candidate.get("authority") != "requires_broker_live_recheck"
            or type(candidate.get("observed_at")) is not str
            or len(candidate["observed_at"]) > 40
            or result["reason"] != "not_operational"
        ):
            return fallback
    else:
        if (
            result["reason"] not in {
                "preview_unavailable", "invalid_request", "invalid_version",
                "invalid_request_id", "invalid_observation_time", "invalid_clock",
                "invalid_observation", "stale_observation", "unverifiable_service",
                "inconsistent_service_state", "operation_state_mismatch",
                "unknown_action", "unknown_service", "deployment_authority_unverified",
            }
            or "candidate" in result
        ):
            return fallback
    # Only the five fixed public fields, never arbitrary keys/systemd output.
    return result
