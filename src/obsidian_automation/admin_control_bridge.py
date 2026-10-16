"""#300 Phase I: host-owned durable queue -> reviewed broker executor bridge.

This callable is NOT connected to the browser or a production command-line
entrypoint. Only an authorized dedicated worker may call it, with an audited
OperationBroker, installed code/unit provenance, and a qualified same-LXC
maintenance gate. Tests use an explicitly fake systemd adapter only.

No Run once operation may be accepted: full OnSuccess chain completion proof
has not been qualified. Do not change the queue's fixed allowlist on the
strength of a point-in-time inactive/healthy terminal service.
"""
from __future__ import annotations

from typing import Callable, Mapping
import os

from .admin_operation_broker import BrokerRejected, OperationBroker
from .admin_operation_queue import DurableOperationQueue, QueuedOperation


def run_accepted_control_once(
    *, queue: DurableOperationQueue,
    broker: OperationBroker,
    trusted_snapshot: Callable[[], Mapping[str, object]],
) -> dict[str, object]:
    """Claim once durably, recheck root-owned state, then settle result.

    work_once first persists immutable processing evidence, then releases
    the queue lock before any potentially long operation. The broker owns its
    own private journal and separate OS update/exclusion lock. A crash or
    failed/uncertain broker result leaves both journals fail-closed. No
    duplicate action is ever queued or automatically retried after a crash.
    """
    if (
        type(queue) is not DurableOperationQueue
        or type(broker) is not OperationBroker
        or queue.enabled is not True
        or broker.effects_enabled is not True
        or (broker.require_root and os.geteuid() != 0)
        or (queue.require_root and os.geteuid() != 0)
    ):
        raise BrokerRejected("control_worker_unqualified")

    def execute(action: QueuedOperation) -> bool:
        if type(action) is not QueuedOperation or action.action not in ("pause", "resume"):
            raise BrokerRejected("run_once_unqualified")
        snapshot = trusted_snapshot()
        if type(snapshot) is not dict or type(snapshot.get("generated_at")) is not str:
            raise BrokerRejected("trusted_snapshot_unavailable")
        request = {
            "version": 1,
            "request_id": action.request_id,
            "service_id": action.service_id,
            "action": action.action,
            "observed_at": snapshot["generated_at"],
        }
        result = broker.apply(request, snapshot)
        return (
            result.request_id == action.request_id
            and result.service_id == action.service_id
            and result.action == action.action
            and result.state == "completed"
            and result.authority == "local_effect_observed"
        )

    return queue.work_once(execute)
