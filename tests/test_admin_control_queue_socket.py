"""Actual disposable Linux AF_UNIX queue intake with no privileged effects."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading

import pytest

from obsidian_automation.admin_broker_socket import BrokerSocketClient, LocalBrokerProtocolError
from obsidian_automation.admin_control_queue_socket import (
    OperationQueueClient, OperationQueueSocket,
)
from obsidian_automation.admin_operation_queue import DurableOperationQueue

RID = "12345678-1234-4234-8234-123456789abc"
RID2 = "bbbbbbbb-1234-4234-8234-123456789abc"


@pytest.fixture
def site():
    with tempfile.TemporaryDirectory(prefix="obsidian-cqs-", dir="/tmp") as root:
        p = Path(root)
        p.chmod(0o700)
        rootq = p / "queue"
        rootq.mkdir(mode=0o700)
        yield p / "queue.sock", rootq


def setup(site, *, enabled=True, peer_uid=None, peer_gid=None,
          allow_run_once=False):
    path, journal = site
    store = DurableOperationQueue(
        queue_root=journal, require_root=False, enabled=enabled,
        allow_run_once=allow_run_once,
    )
    server = OperationQueueSocket(
        path, queue=store, require_root=False,
        allowed_peer_uid=os.geteuid() if peer_uid is None else peer_uid,
        allowed_peer_gid=os.getegid() if peer_gid is None else peer_gid,
    )
    return server, store


def call(srv, operation):
    result = []
    errors = []
    def client():
        try:
            result.append(operation())
        except BaseException as exc:
            errors.append(exc)
    with srv:
        t = threading.Thread(target=client)
        t.start()
        assert srv.serve_once()
        t.join(timeout=3)
        assert not t.is_alive()
    if errors:
        raise errors[0]
    return result[0]


def request(rid=RID, *, action="pause"):
    return {
        "version": 1, "request_id": rid,
        "service_id": "github-sync", "action": action,
    }


def test_disabled_default_socket_refuses_durable_acceptance(site):
    server, store = setup(site, enabled=False)
    result = call(
        server, lambda: OperationQueueClient(site[0]).submit(request()),
    )
    assert result == {"version": 1, "error": "queue_disabled"}
    assert not list(site[1].iterdir())
    assert site[0].exists() is False


def test_submit_and_receipt_over_authenticated_unix_and_durable_disk(site):
    server, store = setup(site)
    received = call(
        server, lambda: OperationQueueClient(site[0]).submit(request()),
    )
    assert received["request_id"] == RID
    assert received["action"] == "pause"
    assert received["state"] == "accepted"
    assert received["authority"] == "durable_queue_only"
    assert received["can_execute"] is False
    assert (site[1] / "active.json").is_file()
    assert stat.S_IMODE((site[1] / "active.json").stat().st_mode) == 0o600
    other, _ = setup(site)
    observed = call(
        other, lambda: OperationQueueClient(site[0]).receipt(RID),
    )
    assert observed == received
    assert store.receipt(RID) == received
    assert site[0].exists() is False


def test_duplicate_submit_does_not_queue_second_systemd_effect(site):
    first, store = setup(site)
    accepted = call(
        first, lambda: OperationQueueClient(site[0]).submit(request()),
    )
    second, _ = setup(site)
    repeated = call(
        second, lambda: OperationQueueClient(site[0]).submit(request()),
    )
    assert accepted == repeated
    assert len(list(site[1].glob("request-*.json"))) == 1


def test_queue_server_can_report_processing_and_complete_with_external_fake_worker(site):
    first, store = setup(site)
    call(first, lambda: OperationQueueClient(site[0]).submit(request()))
    store.claim_for_worker()
    pending_server, _ = setup(site)
    processing = call(
        pending_server, lambda: OperationQueueClient(site[0]).receipt(RID),
    )
    assert processing["state"] == "processing"
    assert processing["can_execute"] is False
    from obsidian_automation.admin_operation_queue import QueuedOperation
    store.settle(QueuedOperation(RID, "github-sync", "pause"), completed=True)
    done_server, _ = setup(site)
    result = call(
        done_server, lambda: OperationQueueClient(site[0]).receipt(RID),
    )
    assert result["state"] == "completed"
    assert result["authority"] == "durable_queue_only"
    assert not (site[1] / "active.json").exists()


def test_indeterminate_receipt_can_be_read_but_second_request_blocked(site):
    server, store = setup(site)
    call(server, lambda: OperationQueueClient(site[0]).submit(request()))
    with pytest.raises(Exception, match="indeterminate_effect"):
        store.work_once(lambda _: False)
    second, _ = setup(site)
    outcome = call(second, lambda: OperationQueueClient(site[0]).receipt(RID))
    assert outcome["state"] == "indeterminate"
    third, _ = setup(site)
    via_raw = call(third, lambda: BrokerSocketClient(site[0]).query({
        "version": 1, "op": "submit", **{k: v for k, v in request(RID2).items() if k != "version"},
        "confirmation": "operator_confirmed_v1",
    }))
    assert via_raw == {"version": 1, "error": "queue_busy"}


@pytest.mark.parametrize("override", [
    {"op": "execute"},
    {"op": "run_once"},
    {"action": "run_once"},
    {"action": "start"},
    {"action": "systemctl stop"},
    {"service_id": "sshd.service"},
    {"service_id": "../../etc"},
    {"confirmation": True},
    {"confirmation": "yes"},
    {"command": "sh -c reboot"},
    {"observed_at": "client-state"},
    {"version": True},
])
def test_arbitrary_command_and_mutation_are_never_supported(site, override):
    server, _ = setup(site)
    raw = {
        "version": 1, "op": "submit", "request_id": RID,
        "service_id": "github-sync", "action": "pause",
        "confirmation": "operator_confirmed_v1",
    }
    raw.update(override)
    result = call(
        server, lambda: BrokerSocketClient(site[0]).query(raw),
    )
    assert result in (
        {"version": 1, "error": "queue_request_rejected"},
        {"version": 1, "error": "read_only_request_rejected"},
        {"version": 1, "error": "run_once_unqualified"},
    )
    assert not list(site[1].glob("request-*.json"))


def test_no_socket_request_can_drive_a_worker_callback(site):
    server, store = setup(site)
    invoked = []
    def forbidden(_):
        invoked.append("ran")
        return True
    received = call(
        server, lambda: OperationQueueClient(site[0]).submit(request()),
    )
    assert received["state"] == "accepted"
    # Caller's registered callback isn't part of socket API. It stays unused.
    assert invoked == []
    assert store.receipt(RID)["state"] == "accepted"
    new_server, _ = setup(site)
    denied = call(
        new_server, lambda: BrokerSocketClient(site[0]).query({
            "version": 1, "op": "execute",
            "request_id": RID, "service_id": "github-sync",
            "action": "pause",
        }),
    )
    assert denied == {
        "version": 1, "error": "read_only_request_rejected",
    }
    assert invoked == []


def test_peer_uid_and_gid_mismatch_disconnected_before_queue_files(site):
    for change in (
        {"peer_uid": os.geteuid()+1},
        {"peer_gid": os.getegid()+1},
    ):
        srv, _ = setup(site, **change)
        def attempt():
            try:
                return OperationQueueClient(site[0]).submit(request())
            except LocalBrokerProtocolError:
                return "denied"
        assert call(srv, attempt) == "denied"
    assert not list(site[1].iterdir())


def test_duplicate_header_json_keys_rejected_before_queue_authority(site):
    import struct
    sock, _ = site
    server, _ = setup(site)
    def double():
        body = (
            b'{"version":1,"op":"submit","op":"submit",'
            b'"request_id":"' + RID.encode() + b'",'
            b'"service_id":"github-sync","action":"pause",'
            b'"confirmation":"operator_confirmed_v1"}'
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(2)
            conn.connect(str(sock))
            conn.sendall(struct.pack("!I", len(body))+body)
            raw = conn.recv(1024)
        return raw
    data = call(server, double)
    assert b"read_only_request_rejected" in data
    assert not list(site[1].glob("request-*.json"))


def test_default_socket_identity_is_privileged_even_for_receipts(site, monkeypatch):
    store = DurableOperationQueue(queue_root=site[1], enabled=False)
    sock = OperationQueueSocket(
        site[0], queue=store, allowed_peer_uid=os.geteuid(),
    )
    monkeypatch.setattr(os, "geteuid", lambda: 1002)
    with pytest.raises(LocalBrokerProtocolError, match="privileged_socket_required"):
        sock.open()
    assert not site[0].exists()


def test_typed_socket_client_reports_busy_without_inventing_a_success(site):
    first, q = setup(site)
    call(first, lambda: OperationQueueClient(site[0]).submit(request()))
    second, _ = setup(site)
    changed = call(
        second, lambda: OperationQueueClient(site[0]).submit(request(RID2)),
    )
    assert changed == {"version": 1, "error": "queue_busy"}
    assert not (site[1] / f"request-{RID2}.json").exists()


def test_client_rejects_forged_service_identity_even_with_false_authority(
    monkeypatch, site,
):
    from obsidian_automation.admin_control_queue_socket import OperationQueueClient
    forged = {
        "version": 1, "request_id": RID,
        "service_id": "sshd.service", "action": "pause",
        "state": "completed", "authority": "durable_queue_only",
        "can_execute": False,
    }
    monkeypatch.setattr(BrokerSocketClient, "query", lambda *a, **kw: forged)
    with pytest.raises(LocalBrokerProtocolError, match="unverifiable_queue_receipt"):
        OperationQueueClient(site[0]).receipt(RID)
