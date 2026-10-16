"""Source-only systemd Manager Job admission through durable queue Worker."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile

import pytest

from obsidian_automation.admin_control_bridge import run_accepted_control_once
from obsidian_automation.admin_control_queue_socket import (
    OperationQueueClient, OperationQueueSocket,
)
from obsidian_automation.admin_operation_broker import BrokerRejected, OperationBroker
from obsidian_automation.admin_operation_queue import DurableOperationQueue
from obsidian_automation.admin_systemd_admission import RunOnceJobDispatcher
from test_admin_operation_broker import FakeUnits, NOW, SPEC, snapshot
from test_admin_control_bridge import system, remote_receipt, remote_submit, broker

RID = "12345678-1234-4234-8234-112233445566"
RID2 = "bbbbbbbb-1234-4234-8234-112233445566"
JOB = "/org/freedesktop/systemd1/job/373"


class Manager:
    def __init__(self, *, answer=JOB):
        self.calls = []
        self.answer = answer

    def start_unit(self, spec):
        self.calls.append(spec)
        if type(self.answer) is Exception:
            raise self.answer
        return self.answer


def request(rid=RID):
    return {
        "version": 1, "request_id": rid,
        "service_id": SPEC.identifier, "action": "run_once",
    }


def make_queue(root, *, allow=True):
    return DurableOperationQueue(
        queue_root=root, enabled=True, require_root=False,
        allow_run_once=allow, clock=lambda: NOW,
    )


def make_dispatcher(adapter, mgr, *, enabled=True, gate=None):
    if gate is None:
        @contextmanager
        def gate():
            # Only synthetic tests use no-op maintenance. This is NOT
            # production authority and does not exclude installed updaters.
            yield
    return RunOnceJobDispatcher(
        adapter=adapter, manager=mgr,
        maintenance_gate=gate, clock=lambda: NOW,
        require_root=False, enabled=enabled,
    )


def test_socket_queue_to_systemd_job_ack_no_business_completion(system):
    _, qroot, audit, path = system
    q = make_queue(qroot)
    adapter = FakeUnits(timer_state="paused")
    mgr = Manager()
    accepted = remote_submit(path, q, request())
    assert accepted["state"] == "accepted"
    assert not mgr.calls
    complete = run_accepted_control_once(
        queue=q, broker=broker(audit, adapter),
        trusted_snapshot=lambda: snapshot(adapter, NOW),
        run_once_dispatcher=make_dispatcher(adapter, mgr),
    )
    assert complete == {
        "version": 1, "request_id": RID, "service_id": SPEC.identifier,
        "action": "run_once", "state": "systemd_job_accepted",
        "authority": "systemd_admission_only",
        "systemd_job_path": JOB,
        "boot_id": adapter.boot,
        "can_execute": False,
    }
    assert mgr.calls == [SPEC]
    assert adapter.calls == []  # No legacy systemctl start --blocking.
    assert adapter.started == 100  # No service/business completion assertion.
    assert not (qroot/"active.json").exists()
    assert not (audit/"pending.json").exists()
    assert remote_receipt(path, q, RID) == complete
    assert remote_submit(path, q, request()) == complete
    assert mgr.calls == [SPEC]  # Lost reply must not re-queue a job.


def test_dispatched_job_result_is_not_confused_with_service_success(system):
    _, qroot, audit, path = system
    q = make_queue(qroot)
    a = FakeUnits(timer_state="paused", service_state="succeeded")
    mgr = Manager()
    remote_submit(path, q, request())
    result = run_accepted_control_once(
        queue=q, broker=broker(audit, a),
        trusted_snapshot=lambda: snapshot(a, NOW),
        run_once_dispatcher=make_dispatcher(a, mgr),
    )
    assert result["state"] == "systemd_job_accepted"
    assert result["authority"] == "systemd_admission_only"
    assert "service_result" not in result
    assert "workflow_succeeded" not in result
    assert "gitea_synced" not in result


def test_dispatch_only_when_explicitly_configured(system):
    _, qroot, audit, path = system
    q = make_queue(qroot, allow=False)
    a = FakeUnits(timer_state="paused")
    with pytest.raises(BrokerRejected, match="run_once_unqualified"):
        q.submit(request())
    assert not list(qroot.glob("request-*.json"))
    mgr = Manager()
    dispatcher = make_dispatcher(a, mgr, enabled=False)
    q = make_queue(qroot, allow=True)
    remote_submit(path, q, request())
    with pytest.raises(BrokerRejected, match="control_worker_unqualified"):
        run_accepted_control_once(
            queue=q, broker=broker(audit, a),
            trusted_snapshot=lambda: snapshot(a, NOW),
            run_once_dispatcher=dispatcher,
        )
    assert mgr.calls == []
    assert q.receipt(RID)["state"] == "accepted"
    assert not (qroot / f"processing-{RID}.json").exists()


def test_queued_request_rejected_if_status_changed_before_manager_submission(system):
    _, qroot, audit, path = system
    q = make_queue(qroot)
    a = FakeUnits(timer_state="paused")
    mgr = Manager()
    remote_submit(path, q, request())
    recorded = snapshot(a, NOW)
    a.timer_state = "scheduled"  # actual operator/timer race
    with pytest.raises(BrokerRejected, match="indeterminate_effect"):
        run_accepted_control_once(
            queue=q, broker=broker(audit, a),
            trusted_snapshot=lambda: recorded,
            run_once_dispatcher=make_dispatcher(a, mgr),
        )
    assert mgr.calls == []
    assert q.receipt(RID)["state"] == "indeterminate"
    assert (qroot / "active.json").exists()
    with pytest.raises(BrokerRejected, match="queue_busy"):
        q.submit(request(RID2))


def test_manager_failure_or_unconfirmed_job_does_not_permit_retry(system):
    _, qroot, audit, path = system
    q = make_queue(qroot)
    a = FakeUnits(timer_state="paused")
    mgr = Manager(answer=Exception("Bearer PRIVATE_AUTHORITY"))
    remote_submit(path, q, request())
    with pytest.raises(BrokerRejected, match="indeterminate_effect") as exc:
        run_accepted_control_once(
            queue=q, broker=broker(audit, a),
            trusted_snapshot=lambda: snapshot(a, NOW),
            run_once_dispatcher=make_dispatcher(a, mgr),
        )
    assert "PRIVATE_AUTHORITY" not in str(exc.value)
    assert q.receipt(RID)["state"] == "indeterminate"
    with pytest.raises(BrokerRejected, match="queue_recovery_required"):
        q.claim_for_worker()
    assert mgr.calls == [SPEC]


def test_paused_timer_not_required_for_other_action_vs_run_once(system):
    _, qroot, audit, path = system
    q = make_queue(qroot)
    a = FakeUnits(timer_state="scheduled")
    mgr = Manager()
    remote_submit(path, q, request())
    with pytest.raises(BrokerRejected, match="indeterminate_effect"):
        run_accepted_control_once(
            queue=q, broker=broker(audit, a),
            trusted_snapshot=lambda: snapshot(a, NOW),
            run_once_dispatcher=make_dispatcher(a, mgr),
        )
    assert mgr.calls == []
