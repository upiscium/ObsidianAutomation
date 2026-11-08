"""#472 D1/B1/B2 source-only accepted-socket Broker observation tests.

Positive sessions mock SO_PEERCRED: a single-UID test runner cannot prove
different real Owner/Broker OS identities. Negative real-kernel SO_PEERCRED
same-UID denial is separately exercised. No listener, network, credentials,
actual Provider/Vault effect or service process is created.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import threading

import pytest

from obsidian_automation.vault_format_broker_journal import BrokerPrivateJournal
from obsidian_automation.vault_format_broker_observation import (
    OwnerBrokerObservationClient, _attempt_key,
)
from obsidian_automation.vault_format_broker_readonly_session import (
    BrokerReadOnlyDenied, BrokerReadOnlySession,
)
from obsidian_automation.vault_format_effect_journal import EffectAttempt

ACCOUNT = "4" * 64
GEN = "8" * 64
OTHER_ACCOUNT = "e" * 64
OWNER_UID = os.geteuid() + 1001
OWNER_GID = os.getegid() + 1002


def attempt(*, run="a", op="b", target="98-System/a.md",
            account=ACCOUNT, generation=GEN):
    return EffectAttempt(
        run_id=run * 32, operation_id=op * 32,
        plan_sha256="1" * 64, policy_sha256="2" * 64,
        source_generation="3" * 64, account_binding_sha256=account,
        provider_proof_sha256="5" * 64,
        receipt_root="/tmp/source-only-broker-session",
        target_path=target, precondition_etag='"synthetic"',
        before_sha256="6" * 64, after_sha256="7" * 64,
        credential_generation=generation,
        claim_boot_epoch=1, claim_credential_epoch=0,
    )


@pytest.fixture
def local_journal(tmp_path):
    state = tmp_path / "broker-journal"
    state.mkdir(mode=0o700)
    broker = BrokerPrivateJournal(state, bootstrap=True)
    assert broker.start_boot() == 1
    assert broker.reconcile_boot() is True
    broker.provision_generation_metadata(ACCOUNT, GEN)
    return broker


@pytest.fixture
def session(local_journal):
    return BrokerReadOnlySession(
        local_journal, owner_uid=OWNER_UID, owner_gid=OWNER_GID,
    )


def credential_request(account=ACCOUNT):
    return {
        "v": 1, "op": "credential.observe",
        "account_binding_sha256": account,
    }


def finality_request(a):
    _, sha = _attempt_key(a)
    return {
        "v": 1, "op": "operation.finality",
        "run_id": a.run_id,
        "operation_id": a.operation_id,
        "account_binding_sha256": a.account_binding_sha256,
        "attempt_sha256": sha,
    }


def request_frame(req):
    return json.dumps(req, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def exchange(session, monkeypatch, payload, *, spoof_owner=True):
    """One disposable AF_UNIX SOCK_STREAM; test-only peer-credential shim."""
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    errors = []
    with monkeypatch.context() as patch:
        if spoof_owner:
            patch.setattr(BrokerReadOnlySession, "_connected_peer",
                          staticmethod(lambda conn: (os.getpid(), OWNER_UID, OWNER_GID)))
        worker = threading.Thread(
            target=lambda: _run_once(session, server, errors), daemon=True,
        )
        worker.start()
        try:
            if payload:
                client.sendall(payload)
            client.shutdown(socket.SHUT_WR)
            pieces = []
            client.settimeout(3)
            while True:
                try:
                    part = client.recv(2048)
                except ConnectionResetError:
                    # Over-limit frames can be rejected before the server
                    # drains the peer's bytes; a kernel reset is fail-closed.
                    break
                if not part:
                    break
                pieces.append(part)
        finally:
            client.close()
        worker.join(timeout=3)
    assert not worker.is_alive() and not errors
    return b"".join(pieces)


def _run_once(session, conn, errors):
    try:
        session.serve_once(conn)
    except BaseException as error:
        errors.append(error)


def decoded(session, monkeypatch, req):
    raw = exchange(session, monkeypatch, request_frame(req))
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1 and len(raw) <= 1024
    return json.loads(raw)


def test_b2_readonly_credential_response_matches_existing_a0_client(
    session, monkeypatch,
):
    req = credential_request()
    response = decoded(session, monkeypatch, req)
    OwnerBrokerObservationClient._require_response(
        response, req, set(req) | {
            "ok", "credential_generation", "credential_epoch",
            "broker_ledger_revision",
        },
    )
    assert response["credential_generation"] == GEN
    assert response["credential_epoch"] == 0
    assert len(response["broker_ledger_revision"]) == 64
    for forbidden in ("credential", "token", "target_path", "provider_secret"):
        assert forbidden not in response


def test_b2_unknown_credential_denied_without_account_enumeration(
    session, monkeypatch,
):
    response = decoded(session, monkeypatch, credential_request(OTHER_ACCOUNT))
    assert response == {"v": 1, "ok": False, "reason": "broker_denied"}


def test_b2_exact_pending_operation_observation_without_provider_effect(
    local_journal, session, monkeypatch,
):
    a = attempt()
    local_journal.fence_pre_dispatch(a)
    req = finality_request(a)
    obs = decoded(session, monkeypatch, req)
    assert obs == {
        "v": 1, "ok": True, **req, "disposition": "pending",
    }
    OwnerBrokerObservationClient._require_response(
        obs, req, set(req) | {"ok", "disposition"},
    )
    assert "receipt_sha256" not in obs
    assert "broker_journal_seq" not in obs
    assert "target_path" not in obs and "before_sha256" not in obs


def test_b2_exact_never_dispatched_receipt_is_repeatable_but_not_authority(
    local_journal, session, monkeypatch,
):
    a = attempt()
    local_journal.fence_pre_dispatch(a)
    before = decoded(session, monkeypatch, finality_request(a))
    assert before["disposition"] == "pending"
    terminal = local_journal.cancel_never_dispatched(a)
    req = finality_request(a)
    result = decoded(session, monkeypatch, req)
    expected = {
        "v": 1, "ok": True, **req,
        "disposition": "verified_no_effect",
        "broker_journal_seq": terminal.broker_journal_seq,
        "receipt_sha256": terminal.receipt_sha256,
    }
    assert result == expected
    OwnerBrokerObservationClient._require_response(
        result, req, set(req) | {
            "ok", "disposition", "broker_journal_seq", "receipt_sha256",
        },
    )
    assert decoded(session, monkeypatch, req) == expected
    assert local_journal.observe_finality(a) == terminal


def test_b2_dispatch_possible_and_reboot_remain_pending(
    local_journal, session, monkeypatch,
):
    a = attempt()
    local_journal.fence_pre_dispatch(a)
    local_journal.mark_dispatch_possible(a)
    req = finality_request(a)
    assert decoded(session, monkeypatch, req)["disposition"] == "pending"
    new_broker = BrokerPrivateJournal(local_journal._state_dir)
    assert new_broker.start_boot() == 2
    assert new_broker.reconcile_boot() is False
    # A fresh observation handler uses the same durable Broker-private DB;
    # it never infers remote success or no-effect after an uncertain reboot.
    new_session = BrokerReadOnlySession(
        new_broker, owner_uid=OWNER_UID, owner_gid=OWNER_GID,
    )
    assert decoded(new_session, monkeypatch, req)["disposition"] == "pending"
    assert "receipt_sha256" not in decoded(new_session, monkeypatch, req)


def test_b2_mismatched_full_attempt_digest_never_reuses_receipt(
    local_journal, session, monkeypatch,
):
    a = attempt()
    local_journal.fence_pre_dispatch(a)
    local_journal.cancel_never_dispatched(a)
    for forged in (
        replace(a, target_path="98-System/other.md"),
        replace(a, plan_sha256="f" * 64),
        replace(a, precondition_etag='"different"'),
        replace(a, receipt_root="/tmp/elsewhere"),
        replace(a, operation_id="c" * 32),
    ):
        result = decoded(session, monkeypatch, finality_request(forged))
        assert result == {"v": 1, "ok": False, "reason": "broker_denied"}


def test_b2_unknown_attempt_never_reports_success(session, monkeypatch):
    response = decoded(session, monkeypatch, finality_request(attempt()))
    assert response == {"v": 1, "ok": False, "reason": "broker_denied"}


@pytest.mark.parametrize(
    "bad",
    [
        {"v": 1, "op": "provider.put", "target": "98-System/a.md"},
        {"v": 1, "op": "credential.rotate", "account_binding_sha256": ACCOUNT},
        {"v": 1, "op": "operation.dispatch", "run_id": "a" * 32},
        {"v": 1, "op": "operation.settle", "attempt_sha256": "a" * 64},
        {"v": 1, "op": "journal.reset"},
        {"v": 1, "op": "operation.finality", "run_id": "a" * 32},
        {"v": 1, "op": "credential.observe", "account_binding_sha256": ACCOUNT,
         "extra": "1"},
        {"v": True, "op": "credential.observe", "account_binding_sha256": ACCOUNT},
        {"v": 2, "op": "credential.observe", "account_binding_sha256": ACCOUNT},
        {"v": 1, "op": "credential.observe", "account_binding_sha256": "A" * 64},
        {"v": 1, "op": "credential.observe", "account_binding_sha256": 1},
        {"v": 1, "op": "credential.observe", "account_binding_sha256": ACCOUNT,
         "raw_vault_body": "no"},
    ],
)
def test_b2_forbidden_operations_and_extra_fields_rejected(
    session, monkeypatch, bad,
):
    assert decoded(session, monkeypatch, bad) == {
        "v": 1, "ok": False, "reason": "broker_denied",
    }


@pytest.mark.parametrize(
    "payload",
    [
        b'{"v":1,"v":1,"op":"credential.observe","account_binding_sha256":"' +
            ACCOUNT.encode() + b'"}\n',
        b'{"v":1,"op":"credential.observe","account_binding_sha256":"' +
            ACCOUNT.encode() + b'"}\nEXTRA',
        b'{"v":1,"op":"credential.observe","account_binding_sha256":"' +
            ACCOUNT.encode() + b'"}\n\n',
        b'{"v":1,"op":"credential.observe","account_binding_sha256":"' +
            ACCOUNT.encode() + b'"}',
        b"\xff\n",
        b"\x00\n",
        b'{"v":NaN,"op":"credential.observe","account_binding_sha256":"' +
            ACCOUNT.encode() + b'"}\n',
        b"x" * 1025 + b"\n",
        b"[]\n",
    ],
)
def test_b2_invalid_wire_frame_denied_without_raw_echo(
    session, monkeypatch, payload,
):
    raw = exchange(session, monkeypatch, payload)
    if len(payload) > 1024:
        # The bounded receiver may close while oversized unread bytes
        # remain, resulting in a reset instead of a structured denial.
        assert raw in (b"", b'{"v":1,"ok":false,"reason":"broker_denied"}\n')
    else:
        assert raw == b'{"v":1,"ok":false,"reason":"broker_denied"}\n'
    assert ACCOUNT.encode() not in raw


def test_b2_kernel_real_same_uid_denied_before_request_read(
    session, monkeypatch,
):
    # The fixture uses one real OS UID. No mocked credentials here:
    # actual SO_PEERCRED must refuse the same-UID peer *without a reply*.
    raw = exchange(session, monkeypatch, b"", spoof_owner=False)
    assert raw == b""


def test_b2_genuine_connected_peer_credential_format():
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        pid, uid, gid = BrokerReadOnlySession._connected_peer(left)
        assert pid == os.getpid() and uid == os.geteuid() and gid == os.getegid()
    finally:
        left.close()
        right.close()


def test_b2_wrong_peer_gid_denied_before_parsing(
    session, monkeypatch,
):
    with monkeypatch.context() as patch:
        patch.setattr(
            BrokerReadOnlySession, "_connected_peer",
            staticmethod(lambda conn: (os.getpid(), OWNER_UID, OWNER_GID + 1)),
        )
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            t = threading.Thread(target=session.serve_once, args=(left,), daemon=True)
            t.start()
            right.settimeout(3)
            assert right.recv(32) == b""
            t.join(timeout=3)
            assert not t.is_alive()
        finally:
            right.close()


def test_b2_reject_root_or_same_uid_constructor(local_journal, monkeypatch):
    with pytest.raises(BrokerReadOnlyDenied, match="^broker_session_policy_invalid$"):
        BrokerReadOnlySession(local_journal, owner_uid=os.geteuid(),
                              owner_gid=OWNER_GID)
    with pytest.raises(BrokerReadOnlyDenied, match="^broker_session_policy_invalid$"):
        BrokerReadOnlySession(local_journal, owner_uid=OWNER_UID, owner_gid=0)
    with monkeypatch.context() as patch:
        patch.setattr(os, "geteuid", lambda: 0)
        with pytest.raises(BrokerReadOnlyDenied, match="^broker_session_policy_invalid$"):
            BrokerReadOnlySession(local_journal, owner_uid=OWNER_UID,
                                  owner_gid=OWNER_GID)


def test_b2_socket_without_eof_times_out_without_querying_journal(
    session, monkeypatch,
):
    import obsidian_automation.vault_format_broker_readonly_session as module
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    errors = []
    with monkeypatch.context() as patch:
        patch.setattr(module, "_TIMEOUT", 0.08)
        patch.setattr(BrokerReadOnlySession, "_connected_peer",
                      staticmethod(lambda conn: (os.getpid(), OWNER_UID, OWNER_GID)))
        thread = threading.Thread(target=lambda: _run_once(session, left, errors),
                                  daemon=True)
        thread.start()
        try:
            right.sendall(request_frame(credential_request()))
            # No shutdown(SHUT_WR), so the request is not authenticated as
            # a complete frame, even though the newline arrived.
            right.settimeout(3)
            buf = bytearray()
            while True:
                block = right.recv(2048)
                if not block:
                    break
                buf.extend(block)
            assert bytes(buf) == b'{"v":1,"ok":false,"reason":"broker_denied"}\n'
        finally:
            right.close()
        thread.join(timeout=3)
    assert not errors and not thread.is_alive()


def test_b2_forged_nonterminal_receipt_is_denied(
    local_journal, session, monkeypatch,
):
    a = attempt()
    local_journal.fence_pre_dispatch(a)
    # Synthetic SQL corruption cannot create a valid pending receipt.
    with local_journal._transaction() as db:
        db.execute("""UPDATE broker_attempt SET receipt_seq=123,
            receipt_digest=? WHERE run_id=?""", ("1" * 64, a.run_id))
    assert decoded(session, monkeypatch, finality_request(a)) == {
        "v": 1, "ok": False, "reason": "broker_unavailable",
    }


def test_b2_corrupt_verified_no_effect_receipt_is_denied(
    local_journal, session, monkeypatch,
):
    a = attempt()
    local_journal.fence_pre_dispatch(a)
    local_journal.cancel_never_dispatched(a)
    with local_journal._transaction() as db:
        db.execute("""UPDATE broker_attempt SET receipt_seq=0
            WHERE run_id=?""", (a.run_id,))
    assert decoded(session, monkeypatch, finality_request(a)) == {
        "v": 1, "ok": False, "reason": "broker_unavailable",
    }


def test_b2_unwired_only_observation_no_runtime_links():
    source = Path(__file__).resolve().parents[1] / "src/obsidian_automation"
    for existing in (
        "vault_format_job.py", "vault_format_writer.py",
        "vault_format_owner_ipc.py", "vault_format_owner_listener.py",
        "vault_format_owner_session.py",
        "vault_format_worker_ipc_client.py",
        "vault_format_broker_observation.py",
    ):
        assert "vault_format_broker_readonly_session" not in (
            source / existing
        ).read_text()
    text = (source / "vault_format_broker_readonly_session.py").read_text()
    for forbidden in ("requests.put(", "httpx.put(", "urllib.request.urlopen("):
        assert forbidden not in text


def test_b2_no_secret_or_unapproved_provider_path_on_success(
    local_journal, session, monkeypatch,
):
    a = attempt(target="98-System/synthetic-only.md")
    local_journal.fence_pre_dispatch(a)
    body = exchange(session, monkeypatch, request_frame(finality_request(a)))
    for secret_or_path in (
        b"98-System", b"/tmp/source-only-broker-session",
        b"synthetic-only.md", b"precondition_etag",
        b"after_sha256", b"before_sha256", b"credential_secret",
    ):
        assert secret_or_path not in body


def test_b2_shape_valid_forged_terminal_receipt_must_fail_closed(
    local_journal, session, monkeypatch,
):
    """A plausible 64-hex receipt is NOT a durable no-dispatch proof."""
    a = attempt()
    local_journal.fence_pre_dispatch(a)
    with local_journal._transaction() as db:
        db.execute("""UPDATE broker_attempt
            SET status='verified_no_effect', receipt_seq=1, receipt_digest=?
            WHERE run_id=? AND operation_id=?""",
            ("1" * 64, a.run_id, a.operation_id))
    assert decoded(session, monkeypatch, finality_request(a)) == {
        "v": 1, "ok": False, "reason": "broker_unavailable",
    }


def test_b2_shape_valid_forgery_with_gate_sequence_also_denied(
    local_journal, session, monkeypatch,
):
    """Checking a monotonic gate sequence alone cannot authenticate receipt."""
    a = attempt()
    local_journal.fence_pre_dispatch(a)
    with local_journal._transaction() as db:
        db.execute("UPDATE broker_gate SET receipt_seq=1 WHERE id=1")
        db.execute("""UPDATE broker_attempt SET status='verified_no_effect',
            receipt_seq=1, receipt_digest=? WHERE run_id=?""",
            ("1" * 64, a.run_id))
    assert decoded(session, monkeypatch, finality_request(a)) == {
        "v": 1, "ok": False, "reason": "broker_unavailable",
    }


def test_b2_forged_correct_receipt_with_unreleased_resource_denied(
    local_journal, session, monkeypatch,
):
    """Reject a partially mutated SQL terminal that still holds its fence."""
    from obsidian_automation.vault_format_broker_journal import _digest
    a = attempt()
    local_journal.fence_pre_dispatch(a)
    _, sha = _attempt_key(a)
    fake_receipt = _digest([
        "broker-never-dispatched-v1", local_journal._binding,
        a.run_id, a.operation_id, sha, 1,
    ])
    with local_journal._transaction() as db:
        db.execute("UPDATE broker_gate SET receipt_seq=1 WHERE id=1")
        db.execute("""UPDATE broker_attempt SET status='verified_no_effect',
            receipt_seq=1, receipt_digest=? WHERE run_id=?""",
            (fake_receipt, a.run_id))
        # The source-only terminal transition normally removes this exact
        # resource key in the same durable transaction.
    assert decoded(session, monkeypatch, finality_request(a)) == {
        "v": 1, "ok": False, "reason": "broker_unavailable",
    }


def test_b2_modified_receipt_after_legitimate_terminal_cannot_be_observed(
    local_journal, session, monkeypatch,
):
    """Digest validation must work even when gate and release are consistent."""
    a = attempt()
    local_journal.fence_pre_dispatch(a)
    local_journal.cancel_never_dispatched(a)
    with local_journal._transaction() as db:
        db.execute("""UPDATE broker_attempt SET receipt_digest=?
            WHERE run_id=? AND operation_id=?""",
            ("2" * 64, a.run_id, a.operation_id))
    assert decoded(session, monkeypatch, finality_request(a)) == {
        "v": 1, "ok": False, "reason": "broker_unavailable",
    }


def test_b2_stale_global_receipt_watermark_after_terminal_is_denied(
    local_journal, session, monkeypatch,
):
    """Correct transcript still requires a durable gate watermark."""
    a = attempt()
    local_journal.fence_pre_dispatch(a)
    local_journal.cancel_never_dispatched(a)
    with local_journal._transaction() as db:
        db.execute("UPDATE broker_gate SET receipt_seq=0 WHERE id=1")
    assert decoded(session, monkeypatch, finality_request(a)) == {
        "v": 1, "ok": False, "reason": "broker_unavailable",
    }
