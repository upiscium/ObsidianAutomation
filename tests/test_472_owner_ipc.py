"""#472 R2/B2c-A0: synthetic Unix peercred IPC boundary verification.

Tests with an overridden peer UID exercise policy logic ONLY; they are not
evidence of distinct real OS principal isolation. Other tests query Linux
SO_PEERCRED from actual AF_UNIX socket pairs with NO identity override.
No listening socket, HTTP traffic, Vault, live Nextcloud, or host users.
"""
from __future__ import annotations

import json
import os
import socket
from dataclasses import asdict

import pytest

from test_472_owner_coordination import owner_fixture
from test_472_signed_run_drain import RUN, PLAN, POLICY, OPERATION
from obsidian_automation.vault_format_owner_ipc import (
    OperatorUnixIPC, OwnerIPCDenied, _MAX_FRAME,
)


def _exchange(server, message: bytes) -> dict:
    owner_sock, requester_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        requester_sock.sendall(message)
        requester_sock.shutdown(socket.SHUT_WR)
        server.serve_once(owner_sock)
        response = requester_sock.recv(512)
        assert response.endswith(b"\n")
        assert len(response) <= 150
        return json.loads(response)
    finally:
        owner_sock.close()
        requester_sock.close()


def _wire(request: dict) -> bytes:
    return json.dumps(request, separators=(",", ":")).encode() + b"\n"


@pytest.fixture
def ipc_fixture(owner_fixture):
    owner, intent, *_ = owner_fixture
    owner_uid = os.geteuid()
    worker_uid = owner_uid + 1
    return OperatorUnixIPC(owner, worker_uid=worker_uid), owner, intent, worker_uid


def test_constructor_requires_different_worker_uid(ipc_fixture):
    _, owner, _, _ = ipc_fixture
    with pytest.raises(OwnerIPCDenied, match="^ipc_invalid$"):
        OperatorUnixIPC(owner, worker_uid=os.geteuid())
    for invalid in (-1, True, 1.5, "1000"):
        with pytest.raises(OwnerIPCDenied, match="^ipc_invalid$"):
            OperatorUnixIPC(owner, worker_uid=invalid)


def test_kernel_peercred_same_uid_denies_worker_claim(ipc_fixture):
    server, owner, intent, _ = ipc_fixture
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    first, second = socket.socketpair()
    try:
        assert server._peer_uid(first) == os.geteuid()
    finally:
        first.close()
        second.close()

    result = _exchange(server, _wire({"v": 1, "op": "run.claim", **asdict(intent)}))
    assert result == {"v": 1, "ok": False, "reason": "ipc_denied"}
    assert not owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)


def test_real_kernel_owner_can_stop_and_recover(ipc_fixture):
    server, owner, intent, _ = ipc_fixture
    assert _exchange(server, _wire({"v": 1, "op": "owner.recover"})) == {
        "v": 1, "ok": True, "unconfirmed": 0,
    }
    owner.register_signed(intent)
    owner.claim_signed(intent)
    assert _exchange(server, _wire({"v": 1, "op": "run.stop", "run_id": RUN})) == {
        "v": 1, "ok": True, "stopped": True,
    }
    assert owner.ledger.is_revoked(RUN)
    assert _exchange(server, _wire({"v": 1, "op": "run.stop", "run_id": RUN})) == {
        "v": 1, "ok": True, "stopped": True,
    }


def test_simulated_distinct_worker_claims_once_and_observes_not_a_lease(
    ipc_fixture, monkeypatch,
):
    server, owner, intent, worker_uid = ipc_fixture
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    # Synthetic role exercise: NO kernel identity is spoofed in production.
    monkeypatch.setattr(server, "_peer_uid", lambda _: worker_uid)
    status = {"v": 1, "op": "run.status",
              "run_id": RUN, "plan_sha256": PLAN,
              "policy_sha256": POLICY, "operation_id": OPERATION}
    assert _exchange(server, _wire(status)) == {
        "v": 1, "ok": True, "claimed_now": False,
    }
    req = {"v": 1, "op": "run.claim", **asdict(intent)}
    assert _exchange(server, _wire(req)) == {
        "v": 1, "ok": True, "claimed_once": True,
    }
    assert _exchange(server, _wire(req)) == {
        "v": 1, "ok": False, "reason": "ipc_claim_denied",
    }
    assert _exchange(server, _wire(status)) == {
        "v": 1, "ok": True, "claimed_now": True,
    }
    monkeypatch.undo()
    assert _exchange(server, _wire({"v": 1, "op": "run.stop", "run_id": RUN}))["ok"] is True
    monkeypatch.setattr(server, "_peer_uid", lambda _: worker_uid)
    assert _exchange(server, _wire(status)) == {
        "v": 1, "ok": True, "claimed_now": False,
    }


def test_worker_cannot_stop_or_recover_and_owner_cannot_claim(ipc_fixture, monkeypatch):
    server, owner, intent, worker_uid = ipc_fixture
    for op, extra in (
        ("run.stop", {"run_id": RUN}),
        ("owner.recover", {}),
    ):
        monkeypatch.setattr(server, "_peer_uid", lambda _: worker_uid)
        assert _exchange(server, _wire({"v": 1, "op": op, **extra})) == {
            "v": 1, "ok": False, "reason": "ipc_denied",
        }
    monkeypatch.undo()
    assert _exchange(server, _wire({"v": 1, "op": "run.claim", **asdict(intent)})) == {
        "v": 1, "ok": False, "reason": "ipc_denied",
    }


@pytest.mark.parametrize("wire_input", [
    b'{"v":1,"v":1,"op":"owner.recover"}\n',
    b'{"v":1,"op":"owner.recover","payload":"secret"}\n',
    b'{"v":true,"op":"owner.recover"}\n',
    b'{"v":1,"op":"owner.recover"} trailing\n',
    b'{"v":1,"op":"owner.recover"}\n{"v":1,"op":"owner.recover"}\n',
    b'{"v":1,"op":"owner.recover"}',
    b'{"v":NaN,"op":"owner.recover"}\n',
    b'["owner.recover"]\n',
    b'\xff\n',
    b'A' * (_MAX_FRAME + 1),
])
def test_bad_frame_is_rejected_before_owner_mutation(ipc_fixture, wire_input):
    server, owner, *_ = ipc_fixture
    result = _exchange(server, wire_input)
    assert result["ok"] is False
    assert result["reason"] in ("ipc_frame_invalid", "ipc_invalid", "ipc_denied")
    assert owner.ledger._boot_reconciled is False


def test_unknown_and_extra_fields_deny_before_action(ipc_fixture):
    server, owner, intent, _ = ipc_fixture
    assert _exchange(server, _wire({"v": 1, "op": "run.wipe"})) == {
        "v": 1, "ok": False, "reason": "ipc_unsupported",
    }
    assert _exchange(server, _wire({
        "v": 1, "op": "owner.recover", "path": "/tmp/not-trusted",
    })) == {"v": 1, "ok": False, "reason": "ipc_invalid"}
    assert owner.ledger._boot_reconciled is False


def test_worker_intent_rejects_payload_or_type_or_policy_mutation(ipc_fixture, monkeypatch):
    server, owner, intent, worker_uid = ipc_fixture
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    monkeypatch.setattr(server, "_peer_uid", lambda _: worker_uid)
    request = {"v": 1, "op": "run.claim", **asdict(intent)}
    for extra, reason in (
        ({"raw_body": "SHOULD_NOT_BE_TRANSMITTED"}, "ipc_invalid"),
        ({"run_id": True}, "ipc_invalid"),
        ({"plan_sha256": "0" * 64}, "ipc_claim_denied"),
    ):
        response = _exchange(server, _wire({**request, **extra}))
        assert response == {"v": 1, "ok": False, "reason": reason}
        assert "SHOULD_NOT_BE_TRANSMITTED" not in json.dumps(response)
    assert not owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)


def test_client_disconnect_does_not_reissue_claim(ipc_fixture, monkeypatch):
    server, owner, intent, worker_uid = ipc_fixture
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    monkeypatch.setattr(server, "_peer_uid", lambda _: worker_uid)
    request = {"v": 1, "op": "run.claim", **asdict(intent)}
    # If the response were lost, replay must still fail due to durable ledger.
    first, second = socket.socketpair()
    try:
        second.sendall(_wire(request))
        second.close()
        server.serve_once(first)
    finally:
        first.close()
        second.close()
    assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    assert _exchange(server, _wire(request)) == {
        "v": 1, "ok": False, "reason": "ipc_claim_denied",
    }


def test_ipc_does_not_create_socket_file_or_writer_path(ipc_fixture, tmp_path):
    server, owner, intent, worker_uid = ipc_fixture
    assert not list(tmp_path.glob("*.sock"))
    assert not hasattr(server, "listen")
    assert not hasattr(server, "connect")
    assert not hasattr(server, "apply_plan")


def test_status_fails_closed_if_owner_ledger_binding_changes(ipc_fixture, monkeypatch):
    server, owner, intent, worker_uid = ipc_fixture
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    owner.claim_signed(intent)
    original = owner.ledger.path
    monkeypatch.setattr(server, "_peer_uid", lambda _: worker_uid)
    owner.ledger.path = original.with_name("wrong-owner.sqlite3")
    try:
        status = {"v": 1, "op": "run.status",
                  "run_id": RUN, "plan_sha256": PLAN,
                  "policy_sha256": POLICY, "operation_id": OPERATION}
        assert _exchange(server, _wire(status)) == {
            "v": 1, "ok": False, "reason": "ipc_run_unverified",
        }
    finally:
        owner.ledger.path = original


def test_slow_trickle_cannot_reset_total_frame_deadline(ipc_fixture, monkeypatch):
    import threading
    import time
    import obsidian_automation.vault_format_owner_ipc as ipc_module

    server, owner, *_ = ipc_fixture
    monkeypatch.setattr(ipc_module, "_SOCKET_TIMEOUT", 0.09)
    server_side, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    sent = []

    def drip():
        payload = _wire({"v": 1, "op": "owner.recover"})
        for byte in payload:
            try:
                sender.sendall(bytes([byte]))
            except OSError:
                break
            time.sleep(0.045)
        sent.append(True)

    thread = threading.Thread(target=drip, daemon=True)
    try:
        thread.start()
        start = time.monotonic()
        server.serve_once(server_side)
        elapsed = time.monotonic() - start
        response = json.loads(sender.recv(256))
        assert response == {"v": 1, "ok": False, "reason": "ipc_frame_invalid"}
        assert elapsed < 0.40
        assert owner.ledger._boot_reconciled is False
    finally:
        server_side.close()
        sender.close()
        thread.join(timeout=2)


def test_unknown_os_peer_uid_denied_without_reading_any_request(
    ipc_fixture, monkeypatch,
):
    import time

    server, owner, _, worker_uid = ipc_fixture
    assert worker_uid + 1 not in (server._worker_uid, server._owner_uid)
    # Inject a synthetic UID for the role rule only. The production method
    # still obtains Linux SO_PEERCRED; no constructor bypass is introduced.
    monkeypatch.setattr(server, "_peer_uid", lambda _: worker_uid + 1)
    server_side, requester = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # Deliberately send zero bytes. An unauthorized peer must not reach
        # the framing receive at all (no one-second trickle budget).
        started = time.monotonic()
        server.serve_once(server_side)
        elapsed = time.monotonic() - started
        assert elapsed < 0.25
        assert json.loads(requester.recv(128)) == {
            "v": 1, "ok": False, "reason": "ipc_denied",
        }
        assert owner.ledger._boot_reconciled is False
    finally:
        server_side.close()
        requester.close()


def test_status_requires_live_signature_even_when_ledger_still_claimed(
    ipc_fixture, monkeypatch,
):
    server, owner, intent, worker_uid = ipc_fixture
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    owner.claim_signed(intent)
    assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    monkeypatch.setattr(server, "_peer_uid", lambda _: worker_uid)
    req = {"v": 1, "op": "run.status", "run_id": RUN,
           "plan_sha256": PLAN, "policy_sha256": POLICY,
           "operation_id": OPERATION}
    assert _exchange(server, _wire(req))["claimed_now"] is True
    # Simulate signed-stop without the owner coordinator's Ledger callback.
    # This is intentionally NOT a combined Off acknowledgment.
    assert owner.drain.stop_and_drain(RUN) == "stopped_after_drain"
    assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    assert _exchange(server, _wire(req)) == {
        "v": 1, "ok": True, "claimed_now": False,
    }


def test_exact_maximum_frame_is_accepted_and_one_extra_byte_is_denied(ipc_fixture):
    server, owner, *_ = ipc_fixture
    minimal = json.dumps({"v": 1, "op": "owner.recover"}).encode("utf-8")
    assert len(minimal) < _MAX_FRAME - 1
    maximum_frame = minimal + b" " * (_MAX_FRAME - 1 - len(minimal)) + b"\n"
    oversized_frame = minimal + b" " * (_MAX_FRAME - len(minimal)) + b"\n"
    assert len(maximum_frame) == _MAX_FRAME
    assert len(oversized_frame) == _MAX_FRAME + 1
    assert _exchange(server, oversized_frame) == {
        "v": 1, "ok": False, "reason": "ipc_frame_invalid",
    }
    assert owner.ledger._boot_reconciled is False
    assert _exchange(server, maximum_frame) == {
        "v": 1, "ok": True, "unconfirmed": 0,
    }
    assert owner.ledger._boot_reconciled is True


def test_kernel_peercred_on_accepted_unix_socket_in_fresh_process(
    ipc_fixture, tmp_path,
):
    """A real connected process (not a socketpair/monkeypatch) is same UID.

    The deliberately denied Worker claim proves peer identity is supplied
    by the kernel for a filesystem AF_UNIX stream. A separately provisioned
    different-UID Worker is still an explicitly pending production gate.
    """
    import struct
    import subprocess
    import sys

    server, owner, intent, _ = ipc_fixture
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    path = tmp_path / "synthetic-test-only.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    child = None
    accepted = None
    try:
        listener.bind(str(path))
        listener.listen(1)
        listener.settimeout(5)
        payload = _wire({"v": 1, "op": "run.claim", **asdict(intent)})
        script = (
            "import socket,sys\n"
            "conn=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
            "conn.connect(sys.argv[1])\n"
            "conn.sendall(bytes.fromhex(sys.argv[2]))\n"
            "conn.shutdown(socket.SHUT_WR)\n"
            "print(conn.recv(256).decode().strip())\n"
            "conn.close()\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(path), payload.hex()],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        accepted, _ = listener.accept()
        raw = accepted.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        pid, uid, gid = struct.unpack("3i", raw)
        assert pid == child.pid
        assert uid == os.geteuid()
        assert server._peer_uid(accepted) == os.geteuid()
        server.serve_once(accepted)
        output, errors = child.communicate(timeout=5)
        assert child.returncode == 0, errors
        assert json.loads(output) == {
            "v": 1, "ok": False, "reason": "ipc_denied",
        }
        assert not owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    finally:
        if accepted is not None:
            accepted.close()
        listener.close()
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
        path.unlink(missing_ok=True)


def test_second_frame_in_separate_recv_is_denied_before_dispatch(ipc_fixture):
    """A split stream may not execute the first mutating command."""
    import threading
    import time

    server, owner, *_ = ipc_fixture
    first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    failure = []

    def send_two_distinct_chunks():
        try:
            second.sendall(_wire({"v": 1, "op": "owner.recover"}))
            time.sleep(0.08)  # New recv(), not same-chunk pipelining.
            second.sendall(_wire({"v": 1, "op": "owner.recover"}))
            second.shutdown(socket.SHUT_WR)
        except OSError as error:
            failure.append(str(type(error).__name__))

    worker = threading.Thread(target=send_two_distinct_chunks, daemon=True)
    try:
        worker.start()
        server.serve_once(first)
        response = json.loads(second.recv(256))
        assert response == {
            "v": 1, "ok": False, "reason": "ipc_frame_invalid",
        }
        assert owner.ledger._boot_reconciled is False
    finally:
        first.close()
        second.close()
        worker.join(timeout=2)


def test_complete_frame_without_write_half_close_never_dispatches(
    ipc_fixture, monkeypatch,
):
    """New strict framing contract rejects a complete line without EOF."""
    import obsidian_automation.vault_format_owner_ipc as ipc_module

    server, owner, *_ = ipc_fixture
    monkeypatch.setattr(ipc_module, "_SOCKET_TIMEOUT", 0.06)
    first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        second.sendall(_wire({"v": 1, "op": "owner.recover"}))
        # Intentionally omit second.shutdown(SHUT_WR).
        server.serve_once(first)
        assert json.loads(second.recv(256)) == {
            "v": 1, "ok": False, "reason": "ipc_frame_invalid",
        }
        assert owner.ledger._boot_reconciled is False
    finally:
        first.close()
        second.close()
