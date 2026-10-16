"""#300 Phase H: typed, peer-verified PREVIEW ONLY across local Unix socket.

Never invokes OperationBroker.apply or systemctl mutation. Even a positive
preview is not a promise that the service can be operated. Future root-worker
commands need a separately reviewed durable admission and effect authority.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from .admin_broker_socket import (
    BrokerSocketClient, DEFAULT_LOCAL_BROKER_SOCKET,
    LocalBrokerProtocolError, ReadOnlyBrokerSocket,
)
from .admin_operation_preflight import (
    ACTIONS, PreflightRejected, _REQUEST_ID, preflight_operation,
)
from .admin_status import SERVICES

_ALLOWED_SERVICES = frozenset(spec.identifier for spec in SERVICES)
_REQ_KEYS = frozenset(("version", "op", "request_id", "service_id", "action"))
_FIXED_CODES = frozenset((
    "invalid_request", "invalid_version", "invalid_request_id",
    "invalid_observation_time", "invalid_clock", "invalid_observation",
    "stale_observation", "unverifiable_service", "unverifiable_service",
    "inconsistent_service_state", "operation_state_mismatch",
    "unknown_action", "unknown_service", "deployment_authority_unverified",
))
_AUTHORITY = "preview_only"
_INACTIVE = "not_operational"

ObservationProvider = Callable[[], Mapping[str, object]]
Clock = Callable[[], datetime]


def _check_request(value: object) -> dict[str, object]:
    # JSON decoding can yield a dict, but no arbitrary mapping subclasses are
    # accepted and no unrecognized caller content will be echoed.
    if (
        type(value) is not dict
        or any(type(key) is not str for key in value)
        or set(value) != _REQ_KEYS
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["op"] != "preview"
        or type(value["request_id"]) is not str
        or _REQUEST_ID.fullmatch(value["request_id"]) is None
        or type(value["service_id"]) is not str
        or value["service_id"] not in _ALLOWED_SERVICES
        or type(value["action"]) is not str
        or value["action"] not in ACTIONS
    ):
        raise LocalBrokerProtocolError("preview_request_rejected")
    return value


def _response_for_request(
    request: Mapping[str, object],
    *,
    provider: ObservationProvider,
    clock: Clock,
) -> dict[str, object]:
    try:
        observation = provider()  # only local root-owned server-supplied data
        now = clock()
        if type(observation) is not dict:
            raise ValueError("invalid snapshot")
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("clock unavailable")
        ts = observation.get("generated_at")
        if type(ts) is not str:
            raise ValueError("missing timestamp")
        internal_request = {
            "version": 1,
            "request_id": request["request_id"],
            "service_id": request["service_id"],
            "action": request["action"],
            "observed_at": ts,
        }
        # deployment_quiescent=True is a *preflight input shape* here only.
        # The result expressly refuses effect authority, and no deployment
        # exclusion is acquired or claimed by this function.
        candidate = preflight_operation(
            internal_request, observation,
            now=now, deployment_quiescent=True,
        )
    except PreflightRejected as exc:
        code = (
            exc.reason if type(exc) is PreflightRejected
            and type(exc.reason) is str and exc.reason in _FIXED_CODES
            else "preview_unavailable"
        )
        return {
            "version": 1, "authority": _AUTHORITY, "can_execute": False,
            "status": "rejected", "reason": code,
        }
    except Exception:
        return {
            "version": 1, "authority": _AUTHORITY, "can_execute": False,
            "status": "rejected", "reason": "preview_unavailable",
        }
    return {
        "version": 1,
        "authority": _AUTHORITY,
        "can_execute": False,
        "status": "compatible",
        "reason": _INACTIVE,
        "candidate": candidate.as_public_dict(),
    }


class OperationPreviewSocket(ReadOnlyBrokerSocket):
    """Read-only sibling of the Capabilities socket, with a preview method.

    Existing ReadOnlyBrokerSocket stays unchanged. Only a fixed, authenticated
    peer may query the versioned preview method on this optional, isolated
    listener. This class has no operation/execution callback or root service.
    """

    def __init__(
        self, path: Path, *, allowed_peer_uid: int,
        observation_provider: ObservationProvider,
        clock: Clock = lambda: datetime.now(timezone.utc),
        allowed_peer_gid: int | None = None,
        socket_gid: int | None = None,
        require_root: bool = True,
    ):
        super().__init__(
            path, allowed_peer_uid=allowed_peer_uid,
            allowed_peer_gid=allowed_peer_gid,
            socket_gid=socket_gid, require_root=require_root,
        )
        self.observation_provider = observation_provider
        self.clock = clock

    def _dispatch(self, request: dict) -> dict:
        if request == {"version": 1, "op": "capabilities"}:
            return super()._dispatch(request)
        data = _check_request(request)
        return _response_for_request(
            data, provider=self.observation_provider, clock=self.clock,
        )


def preview_from_socket(
    request: Mapping[str, object],
    *,
    socket_path: Path = DEFAULT_LOCAL_BROKER_SOCKET,
) -> dict[str, object]:
    """Strict client projection. A fake/old socket cannot elevate authority."""
    rejected = {
        "version": 1, "authority": _AUTHORITY, "can_execute": False,
        "status": "rejected", "reason": "preview_unavailable",
    }
    try:
        candidate = _check_request(request)
        response = BrokerSocketClient(socket_path, timeout=10.0).query(candidate)
        if (
            type(response) is not dict
            or set(response) not in (
                {"version", "authority", "can_execute", "status", "reason"},
                {"version", "authority", "can_execute", "status", "reason", "candidate"},
            )
            or type(response.get("version")) is not int
            or response["version"] != 1
            or response["authority"] != _AUTHORITY
            or type(response["can_execute"]) is not bool
            or response["can_execute"] is not False
            or response["status"] not in ("compatible", "rejected")
            or type(response["reason"]) is not str
        ):
            return rejected
        if response["status"] == "compatible":
            expected = {
                "request_id": request["request_id"],
                "service_id": request["service_id"],
                "action": request["action"],
                "authority": "requires_broker_live_recheck",
            }
            body = response.get("candidate")
            if (
                response["reason"] != _INACTIVE
                or type(body) is not dict
                or set(body) != (set(expected) | {"observed_at"})
                or any(body[key] != val for key, val in expected.items())
                or type(body["observed_at"]) is not str
                or len(body["observed_at"]) > 40
                # This is an observation timestamp only; never use this
                # projection to build a root operation or audit.
                or not body["observed_at"].endswith(("+00:00", "Z"))
            ):
                return rejected
        else:
            if (
                response["reason"] not in (_FIXED_CODES | {"preview_unavailable"})
                or "candidate" in response
            ):
                return rejected
        return response
    except (LocalBrokerProtocolError, OSError, ValueError, TypeError):
        return rejected
