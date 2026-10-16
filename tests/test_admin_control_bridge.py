"""Socket->durable intake->fake effect broker->receipt, NEVER live systemd."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import threading

import pytest

from obsidian_automation.admin_control_bridge import run_accepted_control_once
from obsidian_automation.admin_control_queue_socket import (
    OperationQueueClient, OperationQueueSocket,
)
from obsidian_automation.admin_operation_broker import (
    BrokerRejected, OperationBroker,
)
from obsidian_automation.admin_operation_queue import DurableOperationQueue
from test_admin_operation_broker import FakeUnits, SPEC, NOW, snapshot

RID = "12345678-1234-4234-8234-112233445566"
RID2 = "bbbbbbbb-1234-4234-8234-112233445566"


@pytest.fixture
def system():
    with tempfile.TemporaryDirectory(prefix="obsidian-ctrl-", dir="/tmp") as folder:
        parent = Path(folder)
        parent.chmod(0o700)
        audit = parent / "broker-audit"
        audit.mkdir(mode=0o700)
        queue_root = parent / "pending-requests"
        queue_root.mkdir(mode=0o700)
        yield parent, queue_root, audit, parent / "operator.sock"


def request(rid=RID, action="pause"):
    return {
        "version": 1, "request_id": rid,
        "service_id": SPEC.identifier, "action": action,
    }


def queue(root, *, enabled=True):
    return DurableOperationQueue(
        queue_root=root, require_root=False, enabled=enabled,
        clock=lambda: NOW,
    )


def broker(root, adapter, *, enabled=True):
    @contextmanager
    def fake_maintenance_exclusion():
        # TEST-ONLY. Real deployment MUST NEVER use a no-op maintenance gate.
        yield
    return OperationBroker(
        audit_root=root, adapter=adapter,
        maintenance_gate=fake_maintenance_exclusion,
        clock=lambda: NOW, require_root=False, effects_enabled=enabled,
    )


def remote_submit(path, q, payload):
    server = OperationQueueSocket(
        path, queue=q, allowed_peer_uid=os.geteuid(),
        allowed_peer_gid=os.getegid(), require_root=False,
    )
    result = []
    with server:
        client = threading.Thread(
            target=lambda: result.append(OperationQueueClient(path).submit(payload))
        )
        client.start()
        assert server.serve_once()
        client.join(timeout=3)
        assert not client.is_alive()
    return result[0]


def remote_receipt(path, q, rid):
    server = OperationQueueSocket(
        path, queue=q, allowed_peer_uid=os.geteuid(),
        allowed_peer_gid=os.getegid(), require_root=False,
    )
    result = []
    with server:
        client = threading.Thread(
            target=lambda: result.append(OperationQueueClient(path).receipt(rid))
        )
        client.start()
        assert server.serve_once()
        client.join(timeout=3)
        assert not client.is_alive()
    return result[0]


def test_full_synthetic_pause_from_peer_socket_to_durable_effect_receipt(system):
    _, queue_root, audit, sock = system
    q = queue(queue_root)
    adapter = FakeUnits()
    bk = broker(audit, adapter)
    accepted = remote_submit(sock, q, request())
    assert accepted["state"] == "accepted"
    assert accepted["can_execute"] is False
    assert not adapter.calls
    assert not (audit / "pending.json").exists()
    completed = run_accepted_control_once(
        queue=q, broker=bk, trusted_snapshot=lambda: snapshot(adapter, NOW),
    )
    assert completed["state"] == "completed"
    assert remote_receipt(sock, q, RID)["state"] == "completed"
    assert len(adapter.calls) == 1
    assert adapter.timer_state == "paused"
    assert (audit / f"request-{RID}.json").exists()
    assert (audit / f"result-{RID}.json").exists()
    assert not (audit / "pending.json").exists()
    assert not (queue_root / "active.json").exists()
    # Re-submission after lost response cannot produce a second effect.
    assert remote_submit(sock, q, request())["state"] == "completed"
    assert len(adapter.calls) == 1


def test_full_synthetic_pause_then_resume_uses_new_id_and_new_state(system):
    _, queue_root, audit, sock = system
    q = queue(queue_root)
    adapter = FakeUnits()
    bk = broker(audit, adapter)
    remote_submit(sock, q, request())
    run_accepted_control_once(
        queue=q, broker=bk, trusted_snapshot=lambda: snapshot(adapter, NOW),
    )
    assert adapter.timer_state == "paused"
    remote_submit(sock, q, request(RID2, "resume"))
    run_accepted_control_once(
        queue=q, broker=bk, trusted_snapshot=lambda: snapshot(adapter, NOW),
    )
    assert adapter.timer_state == "scheduled"
    assert len(adapter.calls) == 2
    assert remote_receipt(sock, q, RID)["state"] == "completed"
    assert remote_receipt(sock, q, RID2)["state"] == "completed"


def test_full_bridge_stops_if_broker_effects_are_not_authorized(system):
    _, queue_root, audit, sock = system
    q = queue(queue_root)
    adapter = FakeUnits()
    bk = broker(audit, adapter, enabled=False)
    remote_submit(sock, q, request())
    with pytest.raises(BrokerRejected, match="control_worker_unqualified"):
        run_accepted_control_once(
            queue=q, broker=bk, trusted_snapshot=lambda: snapshot(adapter, NOW),
        )
    assert q.receipt(RID)["state"] == "accepted"
    assert not adapter.calls
    assert not (audit / "pending.json").exists()


def test_full_bridge_fails_closed_if_worker_observation_becomes_stale(system):
    _, queue_root, audit, sock = system
    q = queue(queue_root)
    adapter = FakeUnits()
    bk = broker(audit, adapter)
    remote_submit(sock, q, request())
    adapter.timer_state = "paused"  # actual systemd changed before claim
    with pytest.raises(BrokerRejected, match="indeterminate_effect"):
        run_accepted_control_once(
            queue=q, broker=bk,
            trusted_snapshot=lambda: snapshot(adapter, NOW),
        )
    assert remote_receipt(sock, q, RID)["state"] == "indeterminate"
    assert not adapter.calls
    with pytest.raises(BrokerRejected, match="queue_recovery_required"):
        q.claim_for_worker()


def test_full_bridge_uncertain_systemd_effect_requires_manual_reconciliation(system):
    _, queue_root, audit, sock = system
    q = queue(queue_root)
    adapter = FakeUnits(fail_action=True)
    bk = broker(audit, adapter)
    remote_submit(sock, q, request())
    with pytest.raises(BrokerRejected, match="indeterminate_effect"):
        run_accepted_control_once(
            queue=q, broker=bk,
            trusted_snapshot=lambda: snapshot(adapter, NOW),
        )
    assert remote_receipt(sock, q, RID)["state"] == "indeterminate"
    assert (audit / "pending.json").exists()
    assert (queue_root / "active.json").exists()
    assert len(adapter.calls) == 1
    with pytest.raises(BrokerRejected, match="queue_busy"):
        q.submit(request(RID2))
    with pytest.raises(BrokerRejected, match="queue_recovery_required"):
        q.work_once(lambda _: True)
    assert len(adapter.calls) == 1


def test_run_once_cannot_be_admitted_by_queue_or_bridge(system):
    _, queue_root, audit, sock = system
    q = queue(queue_root)
    with pytest.raises(BrokerRejected, match="run_once_unqualified"):
        q.submit(request(action="run_once"))
    assert not (queue_root / "active.json").exists()
    assert not (audit / "pending.json").exists()


def test_trusted_snapshot_throwing_does_not_echo_sensitive_result(system):
    _, queue_root, audit, sock = system
    q = queue(queue_root)
    bk = broker(audit, FakeUnits())
    remote_submit(sock, q, request())
    def unsafe():
        raise RuntimeError("Bearer ROOT-AUDIT-DO-NOT-EXPOSE")
    with pytest.raises(BrokerRejected, match="^indeterminate_effect$") as exc:
        run_accepted_control_once(queue=q, broker=bk, trusted_snapshot=unsafe)
    assert "DO-NOT-EXPOSE" not in str(exc.value)
    assert q.receipt(RID)["state"] == "indeterminate"
    assert not (audit / "pending.json").exists()
