"""#472 D1/B1/A0 isolated Broker observation channel tests, no provider.

Most successful round-trips deliberately substitute a SYNTHETIC peer UID:
they verify transport framing and exact identity, NOT real distinct-UID
privilege isolation. Real kernel SO_PEERCRED denial is tested separately.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import time

import pytest

import obsidian_automation.vault_format_broker_observation as module
from obsidian_automation.vault_format_broker_observation import (
    BrokerObservationDenied, OwnerBrokerObservationClient,
)
from obsidian_automation.vault_format_effect_journal import EffectAttempt

_HEX_A = "a" * 64
_HEX_B = "b" * 64
_HEX_C = "c" * 64
_ACCOUNT = "4" * 64


@pytest.fixture
def approved():
    return EffectAttempt(
        run_id="a" * 32, operation_id="b" * 32,
        plan_sha256="1" * 64, policy_sha256="2" * 64,
        source_generation="3" * 64, account_binding_sha256=_ACCOUNT,
        provider_proof_sha256="5" * 64,
        receipt_root="/tmp/isolated-fake-receipts",
        target_path="98-System/synthetic.md",
        precondition_etag='"synthetic-etag"',
        before_sha256="6" * 64, after_sha256="7" * 64,
        credential_generation="8" * 64,
        claim_boot_epoch=2, claim_credential_epoch=1,
    )


def _json(payload):
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


def _generation_response(req):
    return {
        **req, "ok": True,
        "credential_generation": _HEX_A,
        "credential_epoch": 9,
        "broker_ledger_revision": _HEX_B,
    }


def _finality_response(req, *, disposition="verified_remote_effect"):
    base = {**req, "ok": True, "disposition": disposition}
    if disposition != "pending":
        base.update(broker_journal_seq=7, receipt_sha256=_HEX_C)
    return base


def _roundtrip(monkeypatch, responder, invoke, *, synthetic_peer=True,
               expect_request=True):
    """Actual one-connection AF_UNIX stream, bounded remote frame to EOF."""
    with tempfile.TemporaryDirectory(prefix="oa472-broker-") as root:
        p = Path(root) / "broker.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(p))
            p.chmod(0o660)
            server.listen(1)
            server.settimeout(3)
            seen = []
            failures = []

            def serve_once():
                try:
                    conn, _ = server.accept()
                    with conn:
                        conn.settimeout(2)
                        req = bytearray()
                        while True:
                            part = conn.recv(4096)
                            if not part:
                                break
                            req.extend(part)
                        seen.append(bytes(req))
                        reply = responder(bytes(req))
                        if reply is None:
                            return
                        if isinstance(reply, tuple):
                            for chunk in reply:
                                conn.sendall(chunk)
                        else:
                            conn.sendall(reply)
                except (OSError, TimeoutError) as exc:
                    failures.append(type(exc).__name__)

            thread = threading.Thread(target=serve_once, daemon=True)
            thread.start()
            try:
                with monkeypatch.context() as patch:
                    # Does not establish real Broker UID/site ownership.
                    def parent(_path, _uid, _group):
                        return os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                    def identity(fd, name, _uid, _group):
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        assert stat.S_ISSOCK(info.st_mode)
                        return info.st_dev, info.st_ino
                    patch.setattr(module, "_trusted_parent", parent)
                    patch.setattr(module, "_socket_identity", identity)
                    if synthetic_peer:
                        patch.setattr(
                            OwnerBrokerObservationClient, "_peer_uid",
                            staticmethod(lambda _sock: os.geteuid() + 1),
                        )
                    client = OwnerBrokerObservationClient(
                        p, broker_uid=os.geteuid() + 1,
                        ipc_group_gid=os.getegid(),
                    )
                    result = invoke(client)
            finally:
                thread.join(timeout=4)
            assert not thread.is_alive()
            if expect_request:
                assert len(seen) == 1 and seen[0].endswith(b"\n")
                assert seen[0].count(b"\n") == 1
                assert not failures
            else:
                assert not any(seen)
            return result, seen


def test_broker_credential_generation_observation_exact_account(monkeypatch):
    result, sent = _roundtrip(
        monkeypatch, lambda raw: _json(_generation_response(json.loads(raw))),
        lambda client: client.observe_credential(_ACCOUNT),
    )
    assert result.account_binding_sha256 == _ACCOUNT
    assert result.credential_generation == _HEX_A
    assert result.credential_epoch == 9
    assert result.broker_ledger_revision == _HEX_B
    assert json.loads(sent[0]) == {
        "v": 1, "op": "credential.observe",
        "account_binding_sha256": _ACCOUNT,
    }


@pytest.mark.parametrize("disposition", [
    "verified_remote_effect", "verified_no_effect", "pending",
])
def test_broker_finality_is_exact_attempt_observation(
    monkeypatch, approved, disposition,
):
    result, sent = _roundtrip(
        monkeypatch,
        lambda raw: _json(_finality_response(json.loads(raw),
                                             disposition=disposition)),
        lambda client: client.observe_finality(approved),
    )
    sent_request = json.loads(sent[0])
    assert sent_request["v"] == 1 and sent_request["op"] == "operation.finality"
    assert sent_request["run_id"] == approved.run_id
    assert sent_request["operation_id"] == approved.operation_id
    assert sent_request["account_binding_sha256"] == _ACCOUNT
    assert len(sent_request["attempt_sha256"]) == 64
    assert "target_path" not in sent_request
    assert "receipt_root" not in sent_request
    assert "provider_proof_sha256" not in sent_request
    assert result.disposition == disposition
    assert result.attempt_sha256 == sent_request["attempt_sha256"]
    if disposition == "pending":
        assert result.receipt_sha256 is None
        assert result.broker_journal_seq is None
    else:
        assert result.receipt_sha256 == _HEX_C
        assert result.broker_journal_seq == 7


def test_exact_attempt_digest_changes_on_any_target_or_etag(monkeypatch, approved):
    requests = []
    for identity in (approved, replace(approved, target_path="98-System/other.md"),
                     replace(approved, precondition_etag='"different"')):
        result, sent = _roundtrip(
            monkeypatch,
            lambda raw: _json(_finality_response(json.loads(raw),
                                                 disposition="pending")),
            lambda client: client.observe_finality(identity),
        )
        requests.append(json.loads(sent[0])["attempt_sha256"])
        assert result.disposition == "pending"
    assert len(set(requests)) == 3


def test_actual_kernel_peer_same_uid_is_denied_before_request(monkeypatch):
    # Directory/socket checks are synthetic only; peer UID is REAL kernel data.
    with pytest.raises(BrokerObservationDenied, match="^broker_peer_untrusted$"):
        _roundtrip(
            monkeypatch, lambda _raw: None,
            lambda client: client.observe_credential(_ACCOUNT),
            synthetic_peer=False, expect_request=False,
        )


def test_invalid_policy_and_cross_role_identity_never_connect():
    with pytest.raises(BrokerObservationDenied, match="^broker_policy_invalid$"):
        OwnerBrokerObservationClient(
            Path("/tmp/broker.sock"), broker_uid=os.geteuid(),
            ipc_group_gid=os.getegid(),
        )
    with pytest.raises(BrokerObservationDenied, match="^broker_policy_invalid$"):
        OwnerBrokerObservationClient(
            Path("/tmp/broker.sock"), broker_uid=True,
            ipc_group_gid=os.getegid(),
        )
    with pytest.raises(BrokerObservationDenied, match="^broker_path_invalid$"):
        OwnerBrokerObservationClient(
            Path("relative.sock"), broker_uid=os.geteuid() + 1,
            ipc_group_gid=os.getegid(),
        )


def test_no_private_state_or_body_in_read_only_requests(monkeypatch, approved):
    _, frames = _roundtrip(
        monkeypatch,
        lambda raw: _json(_finality_response(json.loads(raw),
                                             disposition="pending")),
        lambda client: client.observe_finality(approved),
    )
    data = frames[0]
    assert b"synthetic.md" not in data
    assert b"/tmp/isolated-fake-receipts" not in data
    assert b"provider_proof_sha256" not in data
    assert b"credential_generation" not in data
    assert b"PUT" not in data


def test_private_exchange_rejects_dispatch_or_arbitrary_ops_without_socket(monkeypatch):
    client = OwnerBrokerObservationClient(
        Path("/tmp/absent-broker.sock"),
        broker_uid=os.geteuid() + 1, ipc_group_gid=os.getegid(),
    )
    for request in (
        {"v": 1, "op": "operation.dispatch"},
        {"v": 1, "op": "broker.rotate", "credential": "secret"},
        {"v": True, "op": "credential.observe", "account_binding_sha256": _ACCOUNT},
        {"v": 1, "op": "credential.observe", "account_binding_sha256": _ACCOUNT, "dispatch": True},
    ):
        with pytest.raises(BrokerObservationDenied, match="^broker_request_invalid$"):
            client._exchange(request)


@pytest.mark.parametrize("bad_response", [
    lambda req: {**_generation_response(req), "credential_epoch": True},
    lambda req: {**_generation_response(req), "credential_epoch": -1},
    lambda req: {**_generation_response(req), "credential_epoch": 1 << 62},
    lambda req: {**_generation_response(req), "credential_generation": _ACCOUNT[:-1]},
    lambda req: {**_generation_response(req), "broker_ledger_revision": "not-a-digest"},
    lambda req: {**_generation_response(req), "extra": "not allowed"},
    lambda req: {**_generation_response(req), "account_binding_sha256": "f" * 64},
])
def test_invalid_credential_observation_fails_closed(monkeypatch, bad_response):
    with pytest.raises(BrokerObservationDenied, match="^(broker_response_invalid|broker_identity_mismatch)$"):
        _roundtrip(
            monkeypatch, lambda raw: _json(bad_response(json.loads(raw))),
            lambda client: client.observe_credential(_ACCOUNT),
        )


@pytest.mark.parametrize("bad_response", [
    lambda req: {**_finality_response(req), "attempt_sha256": "f" * 64},
    lambda req: {**_finality_response(req), "operation_id": "f" * 32},
    lambda req: {**_finality_response(req), "disposition": "verified"},
    lambda req: {**_finality_response(req), "broker_journal_seq": True},
    lambda req: {**_finality_response(req), "broker_journal_seq": 0},
    lambda req: {**_finality_response(req), "receipt_sha256": "0" * 64},
    lambda req: {**_finality_response(req), "target_path": "98-System/fake.md"},
    lambda req: {**_finality_response(req, disposition="pending"), "receipt_sha256": _HEX_C},
])
def test_invalid_finality_never_returns_a_verified_observation(
    monkeypatch, approved, bad_response,
):
    with pytest.raises(BrokerObservationDenied, match="^(broker_response_invalid|broker_identity_mismatch)$"):
        _roundtrip(
            monkeypatch, lambda raw: _json(bad_response(json.loads(raw))),
            lambda client: client.observe_finality(approved),
        )


@pytest.mark.parametrize("response_bytes", [
    b'{"v":1,"ok":true,"op":"credential.observe","op":"credential.observe"}\n',
    b'{"v":1,"ok":true,"credential_generation":NaN}\n',
    b'{}\n{}\n',
    b'{"v":1,"ok":true}\ntrailing',
    b'{"v":1,"ok":true}\x00\n',
    b'{"v":1,"ok":true}\n' + b'x' * 1100,
    b'{"v":1,"ok":true}\n' + b' ' * 30,
])
def test_duplicate_malformed_or_tail_frame_denied(monkeypatch, response_bytes):
    with pytest.raises(BrokerObservationDenied, match="^broker_response_invalid$"):
        _roundtrip(
            monkeypatch, lambda _raw: response_bytes,
            lambda client: client.observe_credential(_ACCOUNT),
        )


def test_rejected_peer_does_not_send_request_or_reconnect(monkeypatch):
    with pytest.raises(BrokerObservationDenied, match="^broker_peer_untrusted$"):
        _roundtrip(
            monkeypatch, lambda _raw: None,
            lambda client: client.observe_credential(_ACCOUNT),
            synthetic_peer=False, expect_request=False,
        )


def test_broker_negative_status_is_not_a_finality_proof(monkeypatch, approved):
    with pytest.raises(BrokerObservationDenied, match="^broker_observation_unavailable$"):
        _roundtrip(
            monkeypatch,
            lambda _raw: _json({"v": 1, "ok": False, "reason": "broker_denied"}),
            lambda client: client.observe_finality(approved),
        )


def test_invalid_claim_identity_fails_before_socket(monkeypatch, approved):
    client = OwnerBrokerObservationClient(
        Path("/tmp/not-created.sock"),
        broker_uid=os.geteuid() + 1, ipc_group_gid=os.getegid(),
    )
    with pytest.raises(BrokerObservationDenied, match="^broker_request_invalid$"):
        client.observe_finality(replace(approved, target_path="Private/secrets.md"))
    with pytest.raises(BrokerObservationDenied, match="^broker_request_invalid$"):
        client.observe_credential("bad")


def test_owner_pid_change_never_reuses_client():
    client = OwnerBrokerObservationClient(
        Path("/tmp/not-created.sock"),
        broker_uid=os.geteuid() + 1, ipc_group_gid=os.getegid(),
    )
    client._owner_pid = -1
    with pytest.raises(BrokerObservationDenied, match="^broker_owner_mismatch$"):
        client.observe_credential(_ACCOUNT)


def test_absent_broker_service_is_fail_closed():
    client = OwnerBrokerObservationClient(
        Path("/tmp/no-such-broker.sock"),
        broker_uid=os.geteuid() + 1, ipc_group_gid=os.getegid(),
    )
    with pytest.raises(BrokerObservationDenied, match="^broker_socket_untrusted$"):
        client.observe_credential(_ACCOUNT)


def test_unwired_from_owner_worker_snapshot_runtime():
    root = Path(__file__).resolve().parents[1] / "src" / "obsidian_automation"
    for filename in (
        "snapshot_app.py", "vault_format_job.py",
        "vault_format_owner_ipc.py", "vault_format_owner_session.py",
        "vault_format_owner_coordination.py",
        "vault_format_worker_ipc_client.py",
    ):
        assert "vault_format_broker_observation" not in (root / filename).read_text()
