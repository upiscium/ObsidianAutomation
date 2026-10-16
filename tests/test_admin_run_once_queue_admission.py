"""Manager Job admission != oneshot/on-success/remote success."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from obsidian_automation.admin_operation_broker import BrokerRejected
from obsidian_automation.admin_operation_queue import (
    DurableOperationQueue, QueuedOperation,
)
from obsidian_automation.admin_systemd_admission import SystemdJobAdmission
from test_admin_operation_queue import RID, RID2, FIXED_CLOCK, queue_root

JOB = "/org/freedesktop/systemd1/job/893"
BOOT = "12345678-1234-1234-8234-123456789abc"
SPEC = "github-sync"


def queue(root, *, allow=True, enabled=True):
    return DurableOperationQueue(
        queue_root=root, require_root=False, enabled=enabled,
        allow_run_once=allow, clock=lambda: FIXED_CLOCK,
    )


def operation(rid=RID):
    return {"version": 1, "request_id": rid, "service_id": SPEC, "action": "run_once"}


def ack(rid=RID, *, job=JOB, boot=BOOT):
    return SystemdJobAdmission(rid, SPEC, job, boot)


def test_run_once_intake_requires_separately_enabled_mode(queue_root):
    for enabled, allow in ((False, True), (True, False), (False, False)):
        q = queue(queue_root, allow=allow, enabled=enabled)
        with pytest.raises(BrokerRejected, match="queue_disabled|run_once_unqualified"):
            q.submit(operation())
        assert not list(queue_root.iterdir())


def test_durable_job_admitted_not_business_completed(queue_root):
    q = queue(queue_root)
    first = q.submit(operation())
    assert first["state"] == "accepted"
    assert first["authority"] == "durable_queue_only"
    assert "systemd_job_path" not in first
    assert q.receipt(RID)["state"] == "accepted"
    invoked = []
    def executor(action):
        invoked.append(action)
        assert q.receipt(RID)["state"] == "processing"
        return ack()
    out = q.work_once(executor)
    assert out == {
        "version": 1, "request_id": RID, "service_id": SPEC,
        "action": "run_once", "state": "systemd_job_accepted",
        "authority": "systemd_admission_only",
        "systemd_job_path": JOB, "boot_id": BOOT,
        "can_execute": False,
    }
    assert len(invoked) == 1
    stored = json.loads((queue_root/f"result-{RID}.json").read_text())
    assert stored["state"] == "systemd_job_accepted"
    assert stored["systemd_job_path"] == JOB
    assert stored["boot_id"] == BOOT
    assert not (queue_root / "active.json").exists()
    assert q.receipt(RID) == out
    assert q.submit(operation()) == out
    assert invoked == [QueuedOperation(RID, SPEC, "run_once")]


def test_new_request_may_be_admitted_after_prior_systemd_job_ack(queue_root):
    q = queue(queue_root)
    q.submit(operation())
    q.work_once(lambda _: ack())
    newer = q.submit(operation(RID2))
    assert newer["state"] == "accepted"
    assert q.receipt(RID)["state"] == "systemd_job_accepted"
    assert q.receipt(RID2)["state"] == "accepted"


@pytest.mark.parametrize("outcome", [
    True, False, None, 1, "JOB_OK",
    ack(job="/tmp/forged-job"),
    ack(rid=RID2),
    ack(boot="unverifiable"),
])
def test_boolean_or_forged_job_never_confused_with_dispatch_success(queue_root, outcome):
    q = queue(queue_root)
    q.submit(operation())
    with pytest.raises(BrokerRejected, match="indeterminate_effect"):
        q.work_once(lambda _: outcome)
    assert q.receipt(RID)["state"] == "indeterminate"
    assert (queue_root / "active.json").exists()
    with pytest.raises(BrokerRejected, match="queue_busy"):
        q.submit(operation(RID2))
    with pytest.raises(BrokerRejected, match="queue_recovery_required"):
        q.claim_for_worker()


def test_failed_job_dispatch_callback_redacts_and_never_replays(queue_root):
    q = queue(queue_root)
    q.submit(operation())
    invoked = []
    def broken(_):
        invoked.append(True)
        raise RuntimeError("SECRET_WOULD_LEAK_FROM_SYSTEMD")
    with pytest.raises(BrokerRejected, match="^indeterminate_effect$") as exc:
        q.work_once(broken)
    assert "SECRET_" not in str(exc.value)
    assert invoked == [True]
    with pytest.raises(BrokerRejected, match="queue_recovery_required"):
        q.work_once(broken)
    assert invoked == [True]


def test_dispatch_outcome_cleanup_crash_reports_pending_cleanup(queue_root, monkeypatch):
    q = queue(queue_root)
    q.submit(operation())
    import os
    unlink = os.unlink
    def sabotage(name, **kwargs):
        if name == "active.json":
            raise OSError("PRIVATE-FILEPATH")
        return unlink(name, **kwargs)
    monkeypatch.setattr(os, "unlink", sabotage)
    with pytest.raises(BrokerRejected, match="queue_state_invalid"):
        q.work_once(lambda _: ack())
    stored = q.receipt(RID)
    assert stored["state"] == "systemd_job_accepted_pending_cleanup"
    assert stored["systemd_job_path"] == JOB
    assert stored["authority"] == "systemd_admission_only"
    with pytest.raises(BrokerRejected, match="queue_busy"):
        q.submit(operation(RID2))


def test_unknown_future_job_status_not_inferred_from_ack(queue_root):
    q = queue(queue_root)
    q.submit(operation())
    q.work_once(lambda _: ack())
    body = q.receipt(RID)
    assert "systemd_job_path" in body
    assert "status" not in body
    assert body["state"] != "completed"


def test_corrupt_job_path_or_missing_prior_claim_not_readable(queue_root):
    q = queue(queue_root)
    q.submit(operation())
    q.work_once(lambda _: ack())
    result = queue_root / f"result-{RID}.json"
    blob = json.loads(result.read_text())
    blob["systemd_job_path"] = "/tmp/not-systemd"
    result.write_text(json.dumps(blob))
    with pytest.raises(BrokerRejected):
        q.receipt(RID)
    result.write_text(json.dumps({
        **blob, "systemd_job_path": JOB,
    }))
    (queue_root / f"processing-{RID}.json").unlink()
    with pytest.raises(BrokerRejected):
        q.receipt(RID)
    with pytest.raises(BrokerRejected, match="queue_recovery_required"):
        q.submit(operation(RID2))


def test_result_identity_cannot_be_swapped_with_a_new_request(queue_root):
    q = queue(queue_root)
    q.submit(operation())
    q.work_once(lambda _: ack())
    record = queue_root / f"result-{RID}.json"
    value = json.loads(record.read_text())
    value["request_id"] = RID2
    record.write_text(json.dumps(value))
    with pytest.raises(BrokerRejected, match="queue_state_invalid"):
        q.receipt(RID)
    with pytest.raises(BrokerRejected, match="queue_recovery_required"):
        q.submit(operation(RID2))


def test_old_pause_resume_receipt_still_readable_after_one_shot(queue_root):
    q = queue(queue_root)
    q.submit({"version": 1, "request_id": RID, "service_id": SPEC, "action": "pause"})
    q.work_once(lambda _: True)
    q.submit(operation(RID2))
    q.work_once(lambda _: ack(RID2))
    assert q.receipt(RID)["state"] == "completed"
    assert q.receipt(RID2)["state"] == "systemd_job_accepted"
