"""#300 Phase I: host-owned durable queue -> reviewed broker executor bridge.

This callable is NOT connected to the browser or a production command-line
entrypoint. Only an authorized dedicated worker may call it, with an audited
OperationBroker, installed code/unit provenance, and a qualified same-LXC
maintenance gate. Tests use an explicitly fake systemd adapter only.

Run once is a separately enabled *systemd Job admission* only: it neither
waits for nor asserts downstream OnSuccess/Gitea/Nextcloud business success.
The regular Broker continues to handle Pause/Resume state transitions.

"""
from __future__ import annotations

from typing import Callable, Mapping
import os

from .admin_operation_broker import BrokerRejected, OperationBroker
from .admin_operation_queue import DurableOperationQueue, QueuedOperation
from .admin_systemd_admission import RunOnceJobDispatcher, SystemdJobAdmission


def run_accepted_control_once(
    *, queue: DurableOperationQueue,
    broker: OperationBroker,
    trusted_snapshot: Callable[[], Mapping[str, object]],
    run_once_dispatcher: RunOnceJobDispatcher | None = None,
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
        or (
            queue.allow_run_once is True and (
                type(run_once_dispatcher) is not RunOnceJobDispatcher
                or run_once_dispatcher.enabled is not True
                or (run_once_dispatcher.require_root and os.geteuid() != 0)
            )
        )
    ):
        # The queue might already contain a Run once operation. Reject the
        # entire worker BEFORE claiming it when its Job-admission authority
        # has not been configured, so it does not become indeterminate merely
        # because the operator had not enabled the systemd Manager backend.
        raise BrokerRejected("control_worker_unqualified")

    def execute(action: QueuedOperation) -> bool | SystemdJobAdmission:
        if type(action) is not QueuedOperation:
            raise BrokerRejected("invalid_operation")
        if action.action == "run_once" and (
            type(run_once_dispatcher) is not RunOnceJobDispatcher
            or run_once_dispatcher.enabled is not True
            or queue.allow_run_once is not True
        ):
            raise BrokerRejected("run_once_unqualified")
        snapshot = trusted_snapshot()
        if type(snapshot) is not dict or type(snapshot.get("generated_at")) is not str:
            raise BrokerRejected("trusted_snapshot_unavailable")
        if action.action == "run_once":
            # The only success output is a typed, fixed systemd Job object,
            # not an ambiguous boolean or final business transport receipt.
            return run_once_dispatcher.dispatch(
                request_id=action.request_id,
                service_id=action.service_id,
                trusted_snapshot=snapshot,
            )
        if action.action not in ("pause", "resume"):
            raise BrokerRejected("invalid_operation")
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
