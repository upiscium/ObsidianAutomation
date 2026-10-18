"""#300 Phase I: disabled-by-default, peer-verified *admission* Unix socket.

This is a distinct AF_UNIX listener from the preview service. It can accept
a fixed, confirmed request into a private durable queue and read a receipt,
but it NEVER invokes an executor, systemctl, OperationBroker.apply or
unprivileged client-supplied commands. No production systemd unit starts it.
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from typing import Callable, Mapping

from .admin_broker_socket import (
    BrokerSocketClient, LocalBrokerProtocolError, ReadOnlyBrokerSocket,
)
from .admin_operation_broker import BrokerRejected
from .admin_operation_queue import DurableOperationQueue, _request, _receipt_id
from .admin_status import SERVICES
from .admin_operation_preview import _check_request, _response_for_request
from .admin_systemd_admission import _JOB_PATH, _BOOT_ID

DEFAULT_OPERATION_QUEUE_SOCKET = Path(
    "/run/obsidian-admin/operation.sock"
)
_ALLOWED_RECEIPT_STATES = frozenset((
    "accepted", "processing", "completed", "indeterminate",
    "completed_pending_cleanup", "systemd_job_accepted",
    "systemd_job_accepted_pending_cleanup",
))
_ALLOWED_ACTIONS = frozenset(("pause", "resume", "run_once"))
_ALLOWED_SERVICE_IDS = frozenset(spec.identifier for spec in SERVICES)
_DISPLAY_AUTHORITY = "durable_queue_only"
_ERROR_CODES = frozenset((
    "queue_disabled", "queue_busy", "admission_lock_busy",
    "queue_recovery_required", "queue_missing_receipt",
    "run_once_unqualified", "operator_worker_unavailable",
))


def _validate_projection(
    value: object, *, request_id: str,
    expected_service: str | None = None, expected_action: str | None = None,
) -> dict[str, object]:
    fields = {
        "version", "request_id", "service_id", "action",
        "state", "authority", "can_execute",
    }
    job_state = (
        type(value) is dict
        and value.get("state") in (
            "systemd_job_accepted", "systemd_job_accepted_pending_cleanup",
        )
    )
    if job_state:
        fields |= {"systemd_job_path", "boot_id"}
    if (
        type(value) is not dict
        or set(value) != fields
        or type(value["version"]) is not int
        or value["version"] != 1
        or type(value["request_id"]) is not str
        or value["request_id"] != request_id
        or type(value["service_id"]) is not str
        or value["service_id"] not in _ALLOWED_SERVICE_IDS
        or (expected_service is not None
            and value["service_id"] != expected_service)
        or type(value["action"]) is not str
        or (expected_action is not None and value["action"] != expected_action)
        or value["action"] not in _ALLOWED_ACTIONS
        or type(value["state"]) is not str
        or value["state"] not in _ALLOWED_RECEIPT_STATES
        or value["authority"] != (
            "systemd_admission_only" if job_state else _DISPLAY_AUTHORITY
        )
        or (job_state and value["action"] != "run_once")
        or (not job_state and value["action"] == "run_once"
            and value["state"] in ("completed", "completed_pending_cleanup"))
        or type(value["can_execute"]) is not bool
        or value["can_execute"] is not False
        or (
            job_state and (
                type(value["systemd_job_path"]) is not str
                or _JOB_PATH.fullmatch(value["systemd_job_path"]) is None
                or type(value["boot_id"]) is not str
                or _BOOT_ID.fullmatch(value["boot_id"]) is None
            )
        )
    ):
        raise LocalBrokerProtocolError("unverifiable_queue_receipt")
    return value


class OperationQueueSocket(ReadOnlyBrokerSocket):
    """No-effect operator intake with OS SO_PEERCRED and fixed queue identity."""

    def __init__(
        self, path: Path, *, queue: DurableOperationQueue,
        allowed_peer_uid: int,
        allowed_peer_gid: int | None = None,
        socket_gid: int | None = None,
        require_root: bool = True,
        observation_provider: Callable[[], Mapping[str, object]] | None = None,
        ready: bool | Callable[[], bool] = False,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        selected_observation: Callable[[str], Mapping[str, object]] | None = None,
        enforce_worker_readiness: bool = False,
    ):
        super().__init__(
            path, allowed_peer_uid=allowed_peer_uid,
            allowed_peer_gid=allowed_peer_gid,
            socket_gid=socket_gid,
            require_root=require_root,
        )
        self.queue = queue
        self.observation_provider = observation_provider
        self.clock = clock
        self.ready = ready
        self.selected_observation = selected_observation
        # The legacy Phase I test-only socket may accept inert queue records
        # without a worker. Production MUST turn this on: new accepted work
        # cannot be stranded after a crashed or recovery-blocked worker.
        self.enforce_worker_readiness = enforce_worker_readiness is True

    def _is_ready(self) -> bool:
        try:
            status = self.ready() if callable(self.ready) else self.ready
            return status is True
        except Exception:
            return False

    def _dispatch(self, raw: dict) -> dict:
        if raw == {"version": 1, "op": "capabilities"}:
            # Only a separately accepted and currently-running local worker
            # exposes operator availability. Merely enqueuing intent never
            # gives the Dashboard systemd permissions.
            if self._is_ready() and self.queue.enabled is True:
                return {
                    "version": 1, "authority": "operator_ready",
                    "operations": {
                        "pause": True, "resume": True,
                        "run_once": self.queue.allow_run_once is True,
                    }, "reason": "worker_ready",
                }
            return super()._dispatch(raw)
        if type(raw) is dict and raw.get("op") == "preview":
            if self.observation_provider is None:
                raise LocalBrokerProtocolError("preview_unavailable")
            checked = _check_request(raw)
            provider = (
                (lambda: self.selected_observation(checked["service_id"]))
                if self.selected_observation is not None
                else self.observation_provider
            )
            return _response_for_request(
                checked, provider=provider, clock=self.clock,
            )
        if type(raw) is not dict or any(type(k) is not str for k in raw):
            raise LocalBrokerProtocolError("queue_bad_request")
        op = raw.get("op")
        try:
            if op == "submit":
                if self.enforce_worker_readiness and not self._is_ready():
                    return {"version": 1, "error": "operator_worker_unavailable"}
                if (
                    set(raw) != {
                        "version", "op", "request_id", "service_id",
                        "action", "confirmation",
                    }
                    or type(raw.get("version")) is not int
                    or raw["version"] != 1
                    or raw.get("confirmation") != "operator_confirmed_v1"
                ):
                    raise BrokerRejected("invalid_operation")
                # The literal confirmation string is an API binding, not a
                # trusted claim about human action. Auth is the OS peer UID
                # plus a future separately reviewed browser control gate.
                request = {
                    "version": 1,
                    "request_id": raw["request_id"],
                    "service_id": raw["service_id"],
                    "action": raw["action"],
                }
                return self.queue.submit(request)
            if op == "receipt":
                if (
                    set(raw) != {"version", "op", "request_id"}
                    or type(raw.get("version")) is not int
                    or raw["version"] != 1
                ):
                    raise BrokerRejected("invalid_operation")
                return self.queue.receipt(_receipt_id(raw["request_id"]))
        except BrokerRejected as exc:
            reason = (
                exc.code if type(exc) is BrokerRejected
                and type(exc.code) is str
                and exc.code in _ERROR_CODES
                else "queue_request_rejected"
            )
            return {"version": 1, "error": reason}
        raise LocalBrokerProtocolError("queue_operation_not_supported")


class OperationQueueClient:
    """Fixed operator intake, capabilities, and receipt queries; no OS effect."""

    def __init__(self, path: Path, *, timeout: float = 1.0):
        self._socket = BrokerSocketClient(path, timeout=timeout)

    def capabilities(self) -> dict[str, object]:
        response = self._socket.query({"version": 1, "op": "capabilities"})
        if (
            type(response) is not dict or set(response) != {
                "version", "authority", "operations", "reason",
            }
            or type(response["version"]) is not int or response["version"] != 1
            or type(response["operations"]) is not dict
            or set(response["operations"]) != {"pause", "resume", "run_once"}
            or any(type(v) is not bool for v in response["operations"].values())
        ):
            raise LocalBrokerProtocolError("unverifiable_operator_capabilities")
        enabled = (
            response["authority"] == "operator_ready"
            and response["reason"] == "worker_ready"
            and response["operations"]["pause"] is True
            and response["operations"]["resume"] is True
        )
        inert = (
            response["authority"] == "observation_only"
            and response["reason"] in (
                "cross_updater_exclusion_unqualified", "broker_unavailable",
            )
            and not any(response["operations"].values())
        )
        if not (enabled or inert):
            raise LocalBrokerProtocolError("unverifiable_operator_capabilities")
        return response

    def submit(self, request: object) -> dict[str, object]:
        action = _request(request)
        result = self._socket.query({
            "version": 1, "op": "submit",
            "request_id": action.request_id,
            "service_id": action.service_id, "action": action.action,
            "confirmation": "operator_confirmed_v1",
        })
        if (
            type(result) is dict
            and set(result) == {"version", "error"}
            and type(result["version"]) is int
            and result["version"] == 1
            and type(result["error"]) is str
            and result["error"] in (_ERROR_CODES | {"queue_request_rejected"})
        ):
            return result
        return _validate_projection(
            result, request_id=action.request_id,
            expected_service=action.service_id, expected_action=action.action,
        )

    def receipt(self, request_id: str) -> dict[str, object]:
        rid = _receipt_id(request_id)
        response = self._socket.query({
            "version": 1, "op": "receipt",
            "request_id": rid,
        })
        if (
            type(response) is dict
            and set(response) == {"version", "error"}
            and type(response.get("version")) is int
            and response["version"] == 1
            and response["error"] in (_ERROR_CODES | {"queue_request_rejected"})
        ):
            return response
        return _validate_projection(response, request_id=rid)
