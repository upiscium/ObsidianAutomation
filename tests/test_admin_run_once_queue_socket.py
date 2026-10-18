"""AF_UNIX accepted job receipts are strictly systemd admission evidence only."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from obsidian_automation.admin_broker_socket import BrokerSocketClient, LocalBrokerProtocolError
from obsidian_automation.admin_control_queue_socket import OperationQueueClient
from obsidian_automation.admin_systemd_admission import SystemdJobAdmission
from test_admin_control_queue_socket import site, setup, call, RID, RID2

JOB = "/org/freedesktop/systemd1/job/713"
BOOT = "12345678-1234-1234-8234-123456789abc"


def operation(rid=RID):
    return {
        "version": 1, "request_id": rid,
        "service_id": "github-sync", "action": "run_once",
    }


def admitted(rid=RID):
    return SystemdJobAdmission(rid, "github-sync", JOB, BOOT)


def test_socket_run_once_default_denied_before_any_queue_journal(site):
    server, store = setup(site)
    denied = call(
        server, lambda: OperationQueueClient(site[0]).submit(operation()),
    )
    assert denied == {"version": 1, "error": "run_once_unqualified"}
    assert not list(site[1].iterdir())


def test_durable_systemd_job_receipt_roundtrips_nonprivileged_peer(site):
    srv, store = setup(site, allow_run_once=True)
    accepted = call(srv, lambda: OperationQueueClient(site[0]).submit(operation()))
    assert accepted["state"] == "accepted"
    assert accepted["can_execute"] is False
    assert "systemd_job_path" not in accepted
    assert store.work_once(lambda _: admitted())["state"] == "systemd_job_accepted"
    srv2, _ = setup(site, allow_run_once=True)
    receipt = call(srv2, lambda: OperationQueueClient(site[0]).receipt(RID))
    assert receipt == {
        "version": 1, "request_id": RID,
        "service_id": "github-sync", "action": "run_once",
        "state": "systemd_job_accepted",
        "authority": "systemd_admission_only",
        "can_execute": False,
        "systemd_job_path": JOB, "boot_id": BOOT,
    }
    srv3, _ = setup(site, allow_run_once=True)
    assert call(srv3, lambda: OperationQueueClient(site[0]).submit(operation())) == receipt


@pytest.mark.parametrize("forged", [
    {"version": 1, "request_id": RID, "service_id": "github-sync",
     "action": "run_once", "state": "completed", "authority": "durable_queue_only",
     "can_execute": False},
    {"version": 1, "request_id": RID, "service_id": "github-sync",
     "action": "run_once", "state": "systemd_job_accepted",
     "authority": "systemd_admission_only", "can_execute": True,
     "systemd_job_path": JOB, "boot_id": BOOT},
    {"version": 1, "request_id": RID, "service_id": "github-sync",
     "action": "run_once", "state": "systemd_job_accepted",
     "authority": "systemd_admission_only", "can_execute": False,
     "systemd_job_path": "/org/freedesktop/systemd1/job/0", "boot_id": BOOT},
    {"version": 1, "request_id": RID, "service_id": "sshd",
     "action": "run_once", "state": "systemd_job_accepted",
     "authority": "systemd_admission_only", "can_execute": False,
     "systemd_job_path": JOB, "boot_id": BOOT},
    {"version": 1, "request_id": RID, "service_id": "github-sync",
     "action": "run_once", "state": "systemd_job_accepted",
     "authority": "systemd_admission_only", "can_execute": False,
     "systemd_job_path": JOB, "boot_id": BOOT, "password": "secret"},
])
def test_untrusted_socket_result_cannot_forge_systemd_ack(monkeypatch, site, forged):
    monkeypatch.setattr(BrokerSocketClient, "query", lambda *a, **kw: forged)
    with pytest.raises(LocalBrokerProtocolError, match="unverifiable_queue_receipt"):
        OperationQueueClient(site[0]).receipt(RID)


def test_socket_never_invokes_dispatcher_or_systemctl_after_acceptance(site):
    server, store = setup(site, allow_run_once=True)
    submit = call(server, lambda: OperationQueueClient(site[0]).submit(operation()))
    assert submit["state"] == "accepted"
    assert list(site[1].glob("processing-*.json")) == []
    assert list(site[1].glob("result-*.json")) == []
    # The only IPC operation is a durable queue submit; there is no execute.
    other, _ = setup(site, allow_run_once=True)
    error = call(other, lambda: BrokerSocketClient(site[0]).query({
        "version": 1, "op": "execute",
        "request_id": RID, "service_id": "github-sync", "action": "run_once",
    }))
    assert error == {"version": 1, "error": "read_only_request_rejected"}
    assert store.receipt(RID)["state"] == "accepted"
