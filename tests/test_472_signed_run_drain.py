"""Synthetic, owner-private SSHSIG run verification and OS drain acceptance.

All signing keys are temporary fake operator keys; no production key, real
Nextcloud credentials, Writer injection, Scheduler or WebDAV is involved.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest

from obsidian_automation.vault_format_signed_run import (
    SignedRunVerifier, SignedRunDenied, receipt_root_digest,
)
from obsidian_automation.vault_format_run_drain import (
    DurableRunDrain, RunDrainDenied,
)
from obsidian_automation.vault_format_recurring_admission import (
    RecurringPolicy, _policy_digest,
)

SSH = shutil.which("ssh-keygen")
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or SSH is None,
    reason="source-only Linux/OpenSSH signed run tests",
)
RUN = "d" * 32
PLAN = "a" * 64
SOURCE = "c" * 64
BINDING = "e" * 64
PROOF = "f" * 64
POLICY_OBJ = RecurringPolicy(
    source_generation=SOURCE, account_binding_sha256=BINDING,
    provider_proof_sha256=PROOF, max_targets=2, max_selected_bytes=4096,
)
POLICY = _policy_digest(POLICY_OBJ)
OPERATION = hashlib.sha256(("oa472-r2-v1:" + RUN).encode()).hexdigest()[:32]
CLOCK = 1793862000


def _json(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode()


def _sign(grant_root, key, payload, *, signer_namespace="obsidian-vault-formatter-run"):
    path = grant_root / (payload["run_id"] + ".json")
    path.write_bytes(_json(payload))
    path.chmod(0o600)
    subprocess.run(
        [SSH, "-Y", "sign", "-f", str(key),
         "-n", signer_namespace, str(path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=5,
    )
    path.with_suffix(".json.sig").rename(
        grant_root / (payload["run_id"] + ".sshsig")
    )
    (grant_root / (payload["run_id"] + ".sshsig")).chmod(0o600)
    return path


def setup(tmp_path):
    trusted = tmp_path / "externally-approved-grants"
    receipt = tmp_path / "private-writer-receipts"
    trusted.mkdir(mode=0o700)
    receipt.mkdir(mode=0o700)
    (trusted / "effect-drain.lock").write_bytes(b"")
    (trusted / "effect-drain.lock").chmod(0o600)
    key = tmp_path / "operator-key"
    subprocess.run(
        [SSH, "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=5,
    )
    (trusted / "allowed_signers").write_text(
        "vault-operator " + (tmp_path / "operator-key.pub").read_text()
    )
    (trusted / "allowed_signers").chmod(0o600)
    grant = {
        "version": 1,
        "run_id": RUN, "plan_sha256": PLAN, "policy_sha256": POLICY,
        "operation_id": OPERATION,
        "source_generation": SOURCE,
        "account_binding_sha256": BINDING,
        "provider_proof_sha256": PROOF,
        "receipt_root_sha256": receipt_root_digest(receipt),
        "scope": "98-System/**", "policy_version": "system-blank-lines-v3",
        "issued_at": CLOCK - 30, "expires_at": CLOCK + 600,
    }
    _sign(trusted, key, grant)
    verifier = SignedRunVerifier(
        trusted, receipt,
        signer_identity="vault-operator",
        expected_policy=POLICY_OBJ, trusted_owner_uid=os.geteuid(),
        ssh_keygen=Path(SSH), clock=lambda: CLOCK,
    )
    return trusted, receipt, key, grant, verifier


def verify(verifier, *, run_id=RUN, plan=PLAN, policy=POLICY, operation=OPERATION):
    return verifier.verify(run_id, plan, policy, operation)


def test_authentic_detached_signature_and_binding(tmp_path):
    _, _, _, _, verifier = setup(tmp_path)
    assert verify(verifier) is True
    assert verifier(RUN, PLAN, POLICY, OPERATION) is True


@pytest.mark.parametrize("wrong", ["run", "plan", "policy", "operation"])
def test_wrong_exact_run_binding_rejected(tmp_path, wrong):
    _, _, _, _, verifier = setup(tmp_path)
    kwargs = {
        "run_id": RUN, "plan": PLAN, "policy": POLICY, "operation": OPERATION,
    }
    kwargs[wrong if wrong != "run" else "run_id"] = (
        "1" * 32 if wrong in ("run", "operation") else "1" * 64
    )
    with pytest.raises(SignedRunDenied):
        verify(verifier, **kwargs)


def test_signature_or_body_tampering_rejected(tmp_path):
    root, _, _, grant, verifier = setup(tmp_path)
    path = root / (RUN + ".json")
    grant["issued_at"] += 1
    path.write_bytes(_json(grant))
    assert verifier(RUN, PLAN, POLICY, OPERATION) is False
    with pytest.raises(SignedRunDenied, match="signed_run_signature_invalid"):
        verify(verifier)


def test_wrong_signature_namespace_is_denied(tmp_path):
    root, receipt, key, grant, _ = setup(tmp_path)
    _sign(root, key, grant, signer_namespace="wrong-namespace")
    verifier = SignedRunVerifier(
        root, receipt, signer_identity="vault-operator",
        expected_policy=POLICY_OBJ, trusted_owner_uid=os.geteuid(), ssh_keygen=Path(SSH),
        clock=lambda: CLOCK,
    )
    assert verifier(RUN, PLAN, POLICY, OPERATION) is False


def test_expired_grant_and_wrong_receipt_root_are_denied(tmp_path):
    root, receipt, key, grant, verifier = setup(tmp_path)
    verifier.clock = lambda: CLOCK + 601
    with pytest.raises(SignedRunDenied, match="signed_run_outside_window"):
        verify(verifier)
    different = tmp_path / "another-receipt-root"
    different.mkdir(mode=0o700)
    another = SignedRunVerifier(
        root, different, signer_identity="vault-operator",
        expected_policy=POLICY_OBJ, trusted_owner_uid=os.geteuid(), ssh_keygen=Path(SSH),
        clock=lambda: CLOCK,
    )
    with pytest.raises(SignedRunDenied, match="signed_run_binding_mismatch"):
        verify(another)


@pytest.mark.parametrize("attack", ["symlink_grant", "hardlink_sig", "world_writable_signers"])
def test_unsafe_approval_files_rejected(tmp_path, attack):
    root, receipt, _, grant, verifier = setup(tmp_path)
    if attack == "symlink_grant":
        original = root / (RUN + ".json")
        backup = root / "backup"
        original.rename(backup)
        original.symlink_to(backup)
    if attack == "hardlink_sig":
        os.link(root / (RUN + ".sshsig"), root / "another-hardlink")
    if attack == "world_writable_signers":
        (root / "allowed_signers").chmod(0o666)
    with pytest.raises(SignedRunDenied):
        verify(verifier)


def test_missing_signature_does_not_authorize(tmp_path):
    root, _, _, _, verifier = setup(tmp_path)
    (root / (RUN + ".sshsig")).unlink()
    assert verifier(RUN, PLAN, POLICY, OPERATION) is False


def test_effect_claim_is_durable_and_prevents_replay(tmp_path):
    root, receipt, _, _, verifier = setup(tmp_path)
    gate = DurableRunDrain(verifier, receipt_root=receipt)
    with gate.effect(RUN, PLAN, POLICY, OPERATION):
        assert (receipt / f"recurring-run-{RUN}.claimed").read_text().strip() == (
            "formatter-run-claimed-v1"
        )
    with pytest.raises(RunDrainDenied, match="run_already_claimed"):
        with gate.effect(RUN, PLAN, POLICY, OPERATION):
            pytest.fail("replay must not run")


def test_stop_waits_for_effect_then_is_durable(tmp_path):
    root, receipt, _, _, verifier = setup(tmp_path)
    gate = DurableRunDrain(verifier, receipt_root=receipt, maximum_stop_seconds=2)
    active = threading.Event()
    release = threading.Event()
    errors = []

    def effect():
        try:
            with gate.effect(RUN, PLAN, POLICY, OPERATION):
                active.set()
                assert release.wait(3)
        except BaseException as e:
            errors.append(type(e).__name__)

    t = threading.Thread(target=effect)
    t.start()
    try:
        assert active.wait(2)
        with pytest.raises(RunDrainDenied, match="drain_timeout"):
            gate.stop_and_drain(RUN, timeout=0.1)
        assert not (root / (RUN + ".revoked")).exists()
    finally:
        release.set()
        t.join(4)
    assert not errors
    assert gate.stop_and_drain(RUN) == "stopped_after_drain"
    assert (root / (RUN + ".revoked")).read_bytes() == b"formatter-run-stopped-v1\n"
    assert verifier(RUN, PLAN, POLICY, OPERATION) is False
    assert gate.stop_and_drain(RUN) == "already_stopped"


def _separate_process_hold(root, receipt, parent_conn):
    verifier = SignedRunVerifier(
        Path(root), Path(receipt), signer_identity="vault-operator",
        expected_policy=POLICY_OBJ, trusted_owner_uid=os.geteuid(), ssh_keygen=Path(SSH),
        clock=lambda: CLOCK,
    )
    gate = DurableRunDrain(verifier, receipt_root=Path(receipt))
    with gate.effect(RUN, PLAN, POLICY, OPERATION):
        parent_conn.send("entered")
        parent_conn.recv()
    parent_conn.close()


def test_cross_process_lock_denies_ack_while_remote_effect_active(tmp_path):
    root, receipt, _, _, verifier = setup(tmp_path)
    gate = DurableRunDrain(verifier, receipt_root=receipt, maximum_stop_seconds=1)
    ctx = multiprocessing.get_context("fork")
    parent, child = ctx.Pipe()
    process = ctx.Process(
        target=_separate_process_hold, args=(str(root), str(receipt), child),
    )
    process.start()
    try:
        assert parent.poll(3)
        assert parent.recv() == "entered"
        with pytest.raises(RunDrainDenied, match="drain_timeout"):
            gate.stop_and_drain(RUN, timeout=0.1)
    finally:
        if process.is_alive():
            parent.send("release")
        process.join(4)
        if process.is_alive():
            process.terminate()
            process.join(3)
        parent.close()
        child.close()
    assert process.exitcode == 0
    assert gate.stop_and_drain(RUN) == "stopped_after_drain"


def _crash_after_claim(root, receipt):
    verifier = SignedRunVerifier(
        Path(root), Path(receipt), signer_identity="vault-operator",
        expected_policy=POLICY_OBJ, trusted_owner_uid=os.geteuid(), ssh_keygen=Path(SSH),
        clock=lambda: CLOCK,
    )
    gate = DurableRunDrain(verifier, receipt_root=Path(receipt))
    with gate.effect(RUN, PLAN, POLICY, OPERATION):
        os._exit(17)


def test_claim_survives_process_crash_and_replay_blocked(tmp_path):
    root, receipt, _, _, verifier = setup(tmp_path)
    ctx = multiprocessing.get_context("fork")
    p = ctx.Process(target=_crash_after_claim, args=(str(root), str(receipt)))
    p.start()
    p.join(4)
    assert p.exitcode == 17
    gate = DurableRunDrain(verifier, receipt_root=receipt)
    with pytest.raises(RunDrainDenied, match="run_already_claimed"):
        with gate.effect(RUN, PLAN, POLICY, OPERATION):
            pass


def test_unsigned_operator_stop_does_not_create_or_approve_run(tmp_path):
    root, receipt, _, _, verifier = setup(tmp_path)
    gate = DurableRunDrain(verifier, receipt_root=receipt)
    assert gate.stop_and_drain(RUN) == "stopped_after_drain"
    assert verifier(RUN, PLAN, POLICY, OPERATION) is False
    assert not list(receipt.iterdir())


def test_symlinked_lock_is_rejected_before_effect(tmp_path):
    root, receipt, _, _, verifier = setup(tmp_path)
    original = root / "effect-drain.lock"
    original.unlink()
    original.symlink_to(root / "allowed_signers")
    with pytest.raises(RunDrainDenied, match="missing_drain_lock"):
        DurableRunDrain(verifier, receipt_root=receipt)


def test_no_stop_ack_on_timeout_or_fake_timeout_argument(tmp_path):
    root, receipt, _, _, verifier = setup(tmp_path)
    gate = DurableRunDrain(verifier, receipt_root=receipt)
    with pytest.raises(RunDrainDenied, match="invalid_stop_budget"):
        gate.stop_and_drain(RUN, timeout=100)
    with pytest.raises(RunDrainDenied, match="invalid_stop_budget"):
        gate.stop_and_drain(RUN, timeout=True)
    assert not (root / (RUN + ".revoked")).exists()


def test_unsigned_executable_cannot_fake_ssh_verification(tmp_path):
    root, receipt, _, _, _ = setup(tmp_path)
    fake = tmp_path / "fake-ssh-keygen"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    with pytest.raises(SignedRunDenied, match="untrusted_signature_verifier"):
        SignedRunVerifier(
            root, receipt, signer_identity="vault-operator",
            expected_policy=POLICY_OBJ, trusted_owner_uid=os.geteuid(),
            ssh_keygen=fake, clock=lambda: CLOCK,
        )


def test_signed_grant_plus_drain_can_guard_actual_fake_webdav_writer(tmp_path):
    """Synthetic effect-time integration; NO real provider or approval key."""
    from obsidian_automation.core_promotion_transport import HTTPResponse
    from obsidian_automation.vault_formatter import create_plan
    from obsidian_automation.vault_format_canary_binding import _generation
    from obsidian_automation.vault_format_recurring_admission import (
        RecurringPolicy, _policy_digest,
    )
    from obsidian_automation.vault_format_recurring_binding import RecurringEffectWriter
    from obsidian_automation.vault_format_writer import _binding

    trusted, receipt, signer, grant, _ = setup(tmp_path)
    vault = tmp_path / "fake-vault"
    target = vault / "98-System" / "synthetic.md"
    target.parent.mkdir(parents=True)
    before = b"# Synthetic\nfirst\n  \nsecond\n"
    target.write_bytes(before)
    plan = create_plan(vault)
    assert plan["counts"]["would_change"] == 1

    base_url = "https://dav.invalid/remote.php/dav/files/user/ObsidianVault"
    details = {
        "webdav_url": "https://dav.invalid/remote.php/dav/files/user/",
        "vault_folder": "ObsidianVault",
        "nextcloud_user": "user",
        "nextcloud_password": "SYNTHETIC-ONLY",
    }
    policy = RecurringPolicy(
        source_generation=_generation(details),
        account_binding_sha256=_binding(base_url, "user"),
        provider_proof_sha256=PROOF,
        max_targets=2, max_selected_bytes=4096,
    )
    grant.update({
        "plan_sha256": plan["plan_sha256"],
        "policy_sha256": _policy_digest(policy),
        "source_generation": policy.source_generation,
        "account_binding_sha256": policy.account_binding_sha256,
        "provider_proof_sha256": policy.provider_proof_sha256,
    })
    _sign(trusted, signer, grant)
    signed = SignedRunVerifier(
        trusted, receipt, signer_identity="vault-operator",
        expected_policy=policy, trusted_owner_uid=os.geteuid(), ssh_keygen=Path(SSH),
        clock=lambda: CLOCK,
    )
    assert signed(RUN, plan["plan_sha256"], _policy_digest(policy), OPERATION)
    gate = DurableRunDrain(signed, receipt_root=receipt)
    class Shared:
        def __init__(self):
            self._lock = threading.RLock()
        def load_nextcloud(self):
            with self._lock:
                return dict(details)
    remote = {"body": before, "etag": '"v1"'}
    sent = []
    def fake_dav(*, method, target_url, username, password, headers, body,
                 timeout, response_limit):
        assert target_url == base_url + "/98-System/synthetic.md"
        assert username == "user" and password == "SYNTHETIC-ONLY"
        sent.append(method)
        if method == "GET":
            return HTTPResponse(200, remote["body"], remote["etag"])
        assert method == "PUT" and headers["If-Match"] == remote["etag"]
        remote["body"] = body
        remote["etag"] = '"v2"'
        return HTTPResponse(204, b"", '"v2"')
    writer = RecurringEffectWriter(
        Shared(), source_generation=lambda: _generation(details),
        policy=policy, run_id=RUN, receipt_root=receipt,
        policy_verifier=lambda *args: args[0] == _policy_digest(policy),
        run_verifier=signed,
        provider_verifier=lambda *args: args == (
            policy.provider_proof_sha256, policy.source_generation,
            policy.account_binding_sha256,
        ),
        transport=fake_dav,
    )
    with gate.effect(RUN, plan["plan_sha256"], _policy_digest(policy), OPERATION):
        result = writer(plan)
    assert result["status"] == "complete"
    assert sent.count("PUT") == 1
    assert remote["body"] != before
    # No original local Vault edits and no second effect on this run.
    assert target.read_bytes() == before
    with pytest.raises(RunDrainDenied, match="run_already_claimed"):
        with gate.effect(RUN, plan["plan_sha256"], _policy_digest(policy), OPERATION):
            writer(plan)
    assert sent.count("PUT") == 1
    assert gate.stop_and_drain(RUN) == "stopped_after_drain"
    assert signed(RUN, plan["plan_sha256"], _policy_digest(policy), OPERATION) is False


@pytest.mark.parametrize("field", [
    "source_generation", "account_binding_sha256", "provider_proof_sha256",
])
def test_signed_grant_must_match_pinned_policy_fields(tmp_path, field):
    root, _, key, grant, verifier = setup(tmp_path)
    # Signature is genuine, but the signed policy field contradicts the
    # pinned RecurringPolicy digest. Never let an inconsistent signed record
    # silently authorize an unrelated credential or provider proof.
    grant[field] = "0" * 64
    _sign(root, key, grant)
    with pytest.raises(SignedRunDenied, match="signed_run_binding_mismatch"):
        verify(verifier)


def test_signed_policy_pinned_against_caller_mutation(tmp_path):
    from dataclasses import replace
    root, receipt, _, _, _ = setup(tmp_path)
    original = replace(POLICY_OBJ)
    pinned = SignedRunVerifier(
        root, receipt, expected_policy=original,
        signer_identity="vault-operator",
        trusted_owner_uid=os.geteuid(), ssh_keygen=Path(SSH),
        clock=lambda: CLOCK,
    )
    object.__setattr__(original, "source_generation", "1" * 64)
    assert pinned(RUN, PLAN, POLICY, OPERATION) is True


def test_tampered_existing_revocation_not_acknowledged(tmp_path):
    root, receipt, _, _, verifier = setup(tmp_path)
    fake = root / (RUN + ".revoked")
    fake.symlink_to(root / "allowed_signers")
    assert verifier(RUN, PLAN, POLICY, OPERATION) is False
    gate = DurableRunDrain(verifier, receipt_root=receipt)
    with pytest.raises(RunDrainDenied, match="unverified_prior_stop"):
        gate.stop_and_drain(RUN)


def test_signed_receipt_identity_must_equal_effect_claim_root(tmp_path):
    root, receipt, _, _, verifier = setup(tmp_path)
    other = tmp_path / "other-private-writer-root"
    other.mkdir(mode=0o700)
    with pytest.raises(RunDrainDenied, match="receipt_root_identity_mismatch"):
        DurableRunDrain(verifier, receipt_root=other)
    assert not list(other.iterdir())


def test_directory_fsync_failure_cannot_be_turned_into_acknowledged_stop(
        tmp_path, monkeypatch):
    import stat
    from obsidian_automation import vault_format_run_drain as drain
    root, receipt, _, _, verifier = setup(tmp_path)
    gate = DurableRunDrain(verifier, receipt_root=receipt)
    original = drain.os.fsync
    root_inode = root.stat().st_ino
    failed = [False]

    def fail_once(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode) and info.st_ino == root_inode and not failed[0]:
            failed[0] = True
            raise OSError("SYNTHETIC_FSYNC_FAILED")
        return original(fd)

    monkeypatch.setattr(drain.os, "fsync", fail_once)
    with pytest.raises(RunDrainDenied, match="revocation_not_durable"):
        gate.stop_and_drain(RUN)
    assert failed[0] is True
    assert (root / (RUN + ".revoked")).is_file()
    # Existing marker must be re-synced before the next stop can report
    # "already_stopped". The marker can remain present after a failed sync.
    monkeypatch.setattr(drain.os, "fsync", original)
    assert gate.stop_and_drain(RUN) == "already_stopped"
    assert verifier(RUN, PLAN, POLICY, OPERATION) is False


def test_worker_owned_claim_is_crash_durable_not_owner_tamper_proof(tmp_path):
    """Known B2a boundary, not a production anti-replay guarantee.

    If the unprivileged worker can erase its own receipt directory contents,
    it can reset the synthetic local claim. This must be prevented by a
    separately controlled durable claim issuer before unattended deployment.
    """
    root, receipt, _, _, verifier = setup(tmp_path)
    gate = DurableRunDrain(verifier, receipt_root=receipt)
    with gate.effect(RUN, PLAN, POLICY, OPERATION):
        pass
    (receipt / f"recurring-run-{RUN}.claimed").unlink()
    # Demonstrates why a trusted, non-worker-writable durable one-use ledger
    # is a REQUIRED later production gate, not something B2a can promise.
    with gate.effect(RUN, PLAN, POLICY, OPERATION):
        pass


def test_file_fsync_failure_cannot_be_turned_into_acknowledged_stop(
        tmp_path, monkeypatch):
    import stat
    from obsidian_automation import vault_format_run_drain as drain
    root, receipt, _, _, verifier = setup(tmp_path)
    gate = DurableRunDrain(verifier, receipt_root=receipt)
    original = drain.os.fsync
    failed = [False]
    def fail_file_once(fd):
        meta = os.fstat(fd)
        if (not failed[0] and stat.S_ISREG(meta.st_mode)
                and meta.st_size == len(b"formatter-run-stopped-v1\n")):
            failed[0] = True
            raise OSError("SYNTHETIC_MARKER_FILE_FSYNC_FAILURE")
        return original(fd)
    monkeypatch.setattr(drain.os, "fsync", fail_file_once)
    with pytest.raises(RunDrainDenied, match="revocation_not_durable"):
        gate.stop_and_drain(RUN)
    assert failed[0]
    assert (root / (RUN + ".revoked")).is_file()
    monkeypatch.setattr(drain.os, "fsync", original)
    assert gate.stop_and_drain(RUN) == "already_stopped"
    assert verifier(RUN, PLAN, POLICY, OPERATION) is False


def test_short_revocation_write_is_never_acknowledged(tmp_path, monkeypatch):
    from obsidian_automation import vault_format_run_drain as drain
    root, receipt, _, _, verifier = setup(tmp_path)
    gate = DurableRunDrain(verifier, receipt_root=receipt)
    original = drain.os.write
    def short_write(fd, data):
        if data == b"formatter-run-stopped-v1\n":
            return original(fd, data[:4])
        return original(fd, data)
    monkeypatch.setattr(drain.os, "write", short_write)
    with pytest.raises(RunDrainDenied, match="revocation_not_durable"):
        gate.stop_and_drain(RUN)
    monkeypatch.setattr(drain.os, "write", original)
    with pytest.raises(RunDrainDenied, match="unverified_prior_stop"):
        gate.stop_and_drain(RUN)
    assert verifier(RUN, PLAN, POLICY, OPERATION) is False
