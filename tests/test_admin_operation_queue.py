"""Durable singleton control intake / worker handoff, with no real systemd."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import threading

import pytest

from obsidian_automation.admin_operation_broker import BrokerRejected
from obsidian_automation.admin_operation_queue import (
    DurableOperationQueue, QueuedOperation, _request,
)

RID = "12345678-1234-4234-8234-123456789abc"
RID2 = "bbbbbbbb-1234-4234-8234-123456789abc"
FIXED_CLOCK = datetime(2026, 10, 16, 20, 38, 7, tzinfo=timezone.utc)


@pytest.fixture
def queue_root():
    with tempfile.TemporaryDirectory(prefix="obsidian-q-", dir="/tmp") as folder:
        p = Path(folder)
        p.chmod(0o700)
        yield p


def queue(root, *, enabled=True):
    return DurableOperationQueue(
        queue_root=root,
        require_root=False, enabled=enabled,
        clock=lambda: FIXED_CLOCK,
    )


def request(rid=RID, *, action="pause", service="github-sync"):
    return {
        "version": 1, "request_id": rid,
        "service_id": service, "action": action,
    }


def _files(root):
    return {p.name for p in root.iterdir()}


def test_inert_by_default_acceptance_never_acts(queue_root):
    q = queue(queue_root, enabled=False)
    with pytest.raises(BrokerRejected, match="queue_disabled"):
        q.submit(request())
    with pytest.raises(BrokerRejected, match="queue_disabled"):
        q.work_once(lambda _: (_ for _ in ()).throw(AssertionError("must not run")))
    assert _files(queue_root) == set()


def test_root_required_by_default(queue_root, monkeypatch):
    q = DurableOperationQueue(queue_root=queue_root, enabled=True)
    monkeypatch.setattr(os, "geteuid", lambda: 1234)
    with pytest.raises(BrokerRejected, match="privileged_worker_required"):
        q.submit(request())
    assert not list(queue_root.iterdir())


def test_fixed_request_validation_denies_arbitrary_actions_units_and_content(queue_root):
    for payload in [
        None, [], {}, True,
        {**request(), "action": "stop"},
        {**request(), "service_id": "sshd.service"},
        {**request(), "action": "pause; rm -rf /"},
        {**request(), "service_id": "../secret"},
        {**request(), "version": 1.0},
        {**request(), "version": True},
        {**request(), "request_id": "1"},
        {**request(), "command": "systemctl"},
        {**request(), "observed_at": "browser-supplied"},
        {**request(), "password": "sensitive"},
    ]:
        with pytest.raises(BrokerRejected, match="invalid_operation"):
            queue(queue_root).submit(payload)
    assert not list(queue_root.iterdir())


def test_accept_is_durable_before_any_worker_effect(queue_root):
    q = queue(queue_root)
    result = q.submit(request())
    assert result == {
        "version": 1, "request_id": RID, "service_id": "github-sync",
        "action": "pause", "state": "accepted",
        "authority": "durable_queue_only", "can_execute": False,
    }
    assert q.receipt(RID)["state"] == "accepted"
    assert _files(queue_root) == {
        "admission.lock", "active.json", f"request-{RID}.json",
    }
    for entry in queue_root.iterdir():
        assert stat.S_IMODE(entry.stat().st_mode) == 0o600
    record = json.loads((queue_root / "active.json").read_text())
    assert record["request_id"] == RID
    assert record["state"] == "accepted"
    assert record["queued_at"] == FIXED_CLOCK.isoformat(timespec="seconds")
    assert "password" not in str(record)


def test_duplicate_request_id_uses_same_durable_receipt_and_no_new_file(queue_root):
    q = queue(queue_root)
    first = q.submit(request())
    inode = (queue_root / f"request-{RID}.json").stat().st_ino
    for _ in range(4):
        assert q.submit(request()) == first
    assert (queue_root / f"request-{RID}.json").stat().st_ino == inode
    assert _files(queue_root) == {
        "admission.lock", "active.json", f"request-{RID}.json",
    }


def test_duplicate_request_id_with_changed_action_must_fail(queue_root):
    q = queue(queue_root)
    q.submit(request())
    for changed in [request(action="resume"), request(service="core-promotion")]:
        with pytest.raises(BrokerRejected, match="queue_state_invalid"):
            q.submit(changed)


def test_another_request_id_is_rejected_until_success_or_manual_reconcile(queue_root):
    q = queue(queue_root)
    q.submit(request())
    with pytest.raises(BrokerRejected, match="queue_busy"):
        q.submit(request(RID2))
    assert not (queue_root / f"request-{RID2}.json").exists()


def test_queue_processing_marker_is_durable_before_any_executor(queue_root):
    q = queue(queue_root)
    q.submit(request())
    action = q.claim_for_worker()
    assert action == QueuedOperation(RID, "github-sync", "pause")
    assert q.receipt(RID)["state"] == "processing"
    assert (queue_root / f"processing-{RID}.json").is_file()
    with pytest.raises(BrokerRejected, match="queue_recovery_required"):
        q.claim_for_worker()
    with pytest.raises(BrokerRejected, match="queue_busy"):
        q.submit(request(RID2))
    assert q.submit(request())["state"] == "processing"


def test_claim_must_not_be_possible_without_prior_durable_acceptance(queue_root):
    q = queue(queue_root)
    with pytest.raises(BrokerRejected, match="queue_missing_receipt"):
        q.claim_for_worker()
    assert _files(queue_root) == {"admission.lock"}


def test_completed_effect_persists_outcome_then_releases_active_marker(queue_root):
    q = queue(queue_root)
    q.submit(request())
    invoked = []
    def executor(action):
        invoked.append(action)
        assert q.receipt(RID)["state"] == "processing"
        return True
    result = q.work_once(executor)
    assert result["state"] == "completed"
    assert invoked == [QueuedOperation(RID, "github-sync", "pause")]
    assert not (queue_root / "active.json").exists()
    assert q.receipt(RID)["state"] == "completed"
    assert q.submit(request())["state"] == "completed"
    assert invoked == [QueuedOperation(RID, "github-sync", "pause")]
    # New fresh request ID is allowed only after the previous one completed.
    assert q.submit(request(RID2, service="core-promotion"))["state"] == "accepted"


def test_worker_may_not_claim_twice_or_repeat_completed_effect(queue_root):
    q = queue(queue_root)
    q.submit(request())
    events = []
    q.work_once(lambda _: (events.append("effect"), True)[1])
    with pytest.raises(BrokerRejected, match="queue_missing_receipt"):
        q.claim_for_worker()
    with pytest.raises(BrokerRejected, match="queue_missing_receipt"):
        q.work_once(lambda _: events.append("replayed"))
    assert events == ["effect"]


@pytest.mark.parametrize("outcome", [False, None, 0, "true"])
def test_non_true_executor_result_indeterminate_and_blocks_replay(queue_root, outcome):
    q = queue(queue_root)
    q.submit(request())
    with pytest.raises(BrokerRejected, match="indeterminate_effect"):
        q.work_once(lambda _: outcome)
    assert q.receipt(RID)["state"] == "indeterminate"
    assert (queue_root / "active.json").exists()
    with pytest.raises(BrokerRejected, match="queue_recovery_required"):
        q.claim_for_worker()
    with pytest.raises(BrokerRejected, match="queue_busy"):
        q.submit(request(RID2))
    assert q.submit(request())["state"] == "indeterminate"


def test_executor_exception_redacted_and_pending_retained(queue_root):
    q = queue(queue_root)
    q.submit(request())
    def fail(_):
        raise RuntimeError("Bearer-SENSITIVE-DONT-ECHO")
    with pytest.raises(BrokerRejected, match="^indeterminate_effect$") as e:
        q.work_once(fail)
    assert "SENSITIVE" not in str(e.value)
    assert q.receipt(RID)["state"] == "indeterminate"
    assert (queue_root / "active.json").exists()
    assert "SENSITIVE" not in "".join(p.read_text() for p in queue_root.glob("*.json"))


def test_processing_marker_crash_recovery_never_repeats_executor(queue_root):
    q = queue(queue_root)
    q.submit(request())
    q.claim_for_worker()
    fresh = queue(queue_root)
    assert fresh.receipt(RID)["state"] == "processing"
    called = []
    with pytest.raises(BrokerRejected, match="queue_recovery_required"):
        fresh.work_once(lambda _: called.append("unsafe replay"))
    assert called == []
    assert (queue_root / "active.json").is_file()


def test_crash_between_active_and_request_journal_blocks_all_queueing(queue_root):
    q = queue(queue_root)
    (queue_root / "active.json").write_text('{"valid": false}\n')
    (queue_root / "active.json").chmod(0o600)
    with pytest.raises(BrokerRejected):
        q.submit(request())
    assert not (queue_root / f"request-{RID}.json").exists()


def test_completed_result_with_interrupted_active_removal_is_not_claimed_settled(
    queue_root, monkeypatch,
):
    q = queue(queue_root)
    q.submit(request())
    original_unlink = os.unlink
    def fail_once(name, **kwargs):
        if name == "active.json":
            raise OSError("PRIVATE-PATH-SHOULD-NOT-ECHO")
        return original_unlink(name, **kwargs)
    monkeypatch.setattr(os, "unlink", fail_once)
    with pytest.raises(BrokerRejected, match="queue_state_invalid"):
        q.work_once(lambda _: True)
    assert q.receipt(RID)["state"] == "completed_pending_cleanup"
    assert q.submit(request())["state"] == "completed_pending_cleanup"
    with pytest.raises(BrokerRejected, match="queue_busy"):
        q.submit(request(RID2))


def test_result_receipt_inconsistent_id_is_never_accepted(queue_root):
    q = queue(queue_root)
    q.submit(request())
    q.claim_for_worker()
    (queue_root / f"result-{RID}.json").write_text(
        json.dumps({
            "record_version": 1, "request_id": RID2,
            "service_id": "github-sync", "action": "pause",
            "state": "completed",
        }),
    )
    (queue_root / f"result-{RID}.json").chmod(0o600)
    with pytest.raises(BrokerRejected, match="queue_state_invalid"):
        q.receipt(RID)


def test_no_client_supplied_paths_or_invalid_receipt_id(queue_root):
    q = queue(queue_root)
    for payload in ["../../secrets", "SSHD", None, True, RID.upper()]:
        with pytest.raises(BrokerRejected, match="invalid_operation"):
            q.receipt(payload)
    assert _files(queue_root) == set()


def test_symlink_root_is_rejected_even_for_read_only_receipts(queue_root):
    symlink = queue_root.parent / (queue_root.name + "-alias")
    symlink.symlink_to(queue_root, target_is_directory=True)
    with pytest.raises(BrokerRejected, match="unsafe_audit_root"):
        queue(symlink).submit(request())
    assert not _files(queue_root)
    symlink.unlink()


@pytest.mark.parametrize("target", ["admission.lock", "active.json"])
def test_symlink_lock_or_active_marker_fails_closed(queue_root, target):
    (queue_root / target).symlink_to("/etc/passwd")
    with pytest.raises(BrokerRejected):
        queue(queue_root).submit(request())
    assert (queue_root / target).is_symlink()


def test_hardlinked_lock_not_accepted(queue_root):
    (queue_root / "admission.lock").touch(mode=0o600)
    os.link(queue_root / "admission.lock", queue_root / "lock-alias")
    with pytest.raises(BrokerRejected, match="unsafe_admission_lock"):
        queue(queue_root).submit(request())


def test_unsafe_private_directory_permissions_rejected(queue_root):
    queue_root.chmod(0o750)
    with pytest.raises(BrokerRejected, match="unsafe_audit_root"):
        queue(queue_root).submit(request())


def test_admission_lock_contention_is_nonblocking(queue_root):
    lock = queue_root / "admission.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(BrokerRejected, match="admission_lock_busy"):
            queue(queue_root).submit(request())
        assert not (queue_root / "active.json").exists()
    finally:
        os.close(fd)


def test_threaded_concurrent_submissions_at_most_one_admitted(queue_root):
    first = queue(queue_root)
    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()
    def submit(rid):
        barrier.wait(timeout=2)
        try:
            value = first.submit(request(rid))
            answer = value["state"]
        except BrokerRejected as e:
            answer = e.code
        with lock:
            outcomes.append(answer)
    a = threading.Thread(target=submit, args=(RID,))
    b = threading.Thread(target=submit, args=(RID2,))
    a.start()
    b.start()
    a.join(timeout=3)
    b.join(timeout=3)
    assert sorted(outcomes) in (
        ["accepted", "admission_lock_busy"],
        ["accepted", "queue_busy"],
    )
    assert len(list(queue_root.glob("request-*.json"))) == 1


def test_missing_active_marker_never_forgets_accepted_request(queue_root):
    q = queue(queue_root)
    q.submit(request())
    (queue_root / "active.json").unlink()  # simulate lost/corrupt marker
    with pytest.raises(BrokerRejected, match="queue_recovery_required"):
        q.submit(request(RID2))
    assert not (queue_root / f"request-{RID2}.json").exists()


def test_missing_active_marker_does_not_forget_indeterminate_result(queue_root):
    q = queue(queue_root)
    q.submit(request())
    with pytest.raises(BrokerRejected, match="indeterminate_effect"):
        q.work_once(lambda _: False)
    (queue_root / "active.json").unlink()
    with pytest.raises(BrokerRejected, match="queue_recovery_required"):
        q.submit(request(RID2))
    assert not (queue_root / f"request-{RID2}.json").exists()


def test_completed_receipt_without_durable_processing_claim_is_not_safe(queue_root):
    q = queue(queue_root)
    q.submit(request())
    q.work_once(lambda _: True)
    (queue_root / f"processing-{RID}.json").unlink()
    with pytest.raises(BrokerRejected, match="queue_recovery_required"):
        q.submit(request(RID2))


def test_root_queue_history_over_bound_fails_closed(queue_root, monkeypatch):
    from obsidian_automation import admin_operation_queue
    monkeypatch.setattr(admin_operation_queue, "MAX_AUDIT_ENTRIES", 3)
    for idx in range(3):
        (queue_root / f"unrelated-{idx}.txt").touch()
    with pytest.raises(BrokerRejected, match="queue_recovery_required"):
        queue(queue_root).submit(request())
    assert not (queue_root / "active.json").exists()


def test_completed_history_remains_readable_during_a_new_active_request(queue_root):
    q = queue(queue_root)
    q.submit(request())
    q.work_once(lambda _: True)
    q.submit(request(RID2, service="core-promotion"))
    assert q.receipt(RID)["state"] == "completed"
    assert q.submit(request())["state"] == "completed"
    assert q.receipt(RID2)["state"] == "accepted"
    q.claim_for_worker()
    assert q.receipt(RID)["state"] == "completed"
    assert q.receipt(RID2)["state"] == "processing"


def test_loss_of_completed_processing_claim_invalidates_all_receipt_reads(queue_root):
    q = queue(queue_root)
    q.submit(request())
    q.work_once(lambda _: True)
    (queue_root / f"processing-{RID}.json").unlink()
    with pytest.raises(BrokerRejected, match="queue_state_invalid"):
        q.receipt(RID)
    with pytest.raises(BrokerRejected, match="queue_state_invalid"):
        q.submit(request())
    with pytest.raises(BrokerRejected, match="queue_recovery_required"):
        q.submit(request(RID2))


def test_uncertain_receipt_cannot_be_trusted_after_active_marker_loss(queue_root):
    q = queue(queue_root)
    q.submit(request())
    with pytest.raises(BrokerRejected, match="indeterminate_effect"):
        q.work_once(lambda _: False)
    (queue_root / "active.json").unlink()
    with pytest.raises(BrokerRejected, match="queue_state_invalid"):
        q.receipt(RID)
    with pytest.raises(BrokerRejected, match="queue_recovery_required"):
        q.submit(request(RID2))


def test_run_once_is_disabled_in_queue_even_when_pause_resume_are_enabled(queue_root):
    q = queue(queue_root)
    with pytest.raises(BrokerRejected, match="run_once_unqualified"):
        q.submit(request(action="run_once"))
    assert not list(queue_root.iterdir())
