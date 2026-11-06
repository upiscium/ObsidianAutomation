"""#472 R2/B2b isolated owner-side coordination acceptance.

All keys and credentials are disposable synthetic fixtures. The same-UID
operator here is NOT an operating-system authorization / production issuer.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from test_472_signed_run_drain import (
    BINDING, OPERATION, PLAN, POLICY, PROOF, RUN, SOURCE,
    _sign, setup as signed_setup,
)
from obsidian_automation.vault_format_owner_coordination import (
    OperatorRunCoordinator, OwnerCoordinationDenied,
)
from obsidian_automation.vault_format_run_ledger import DurableRunLedger, RunIntent
from obsidian_automation.vault_format_run_drain import DurableRunDrain, RunDrainDenied
from obsidian_automation.vault_format_signed_run import SignedRunVerifier


@pytest.fixture
def owner_fixture(tmp_path):
    trusted, receipts, key, grant, signed = signed_setup(tmp_path)
    state = trusted / "run-ledger"
    state.mkdir(mode=0o700)
    ledger = DurableRunLedger(state)
    drain = DurableRunDrain(signed, receipt_root=receipts, maximum_stop_seconds=0.2)
    owner = OperatorRunCoordinator(drain, ledger)
    intent = RunIntent(
        run_id=RUN, plan_sha256=PLAN, policy_sha256=POLICY,
        operation_id=OPERATION, source_generation=SOURCE,
        account_binding_sha256=BINDING, provider_proof_sha256=PROOF,
        receipt_root=str(receipts),
    )
    return owner, intent, state, trusted, key, grant, receipts


def test_signed_registration_one_shot_and_durable_owner_stop(owner_fixture):
    owner, intent, *_ = owner_fixture
    with pytest.raises(OwnerCoordinationDenied, match="owner_recovery_required"):
        owner.register_signed(intent)
    assert owner.recover_after_quiescence() == 0
    owner.register_signed(intent)
    owner.claim_signed(intent)
    assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    with pytest.raises(OwnerCoordinationDenied, match="owner_claim_denied"):
        owner.claim_signed(intent)
    assert owner.stop_and_revoke(RUN) == "stopped_after_drain"
    assert owner.ledger.is_revoked(RUN)
    assert not owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    assert not owner.drain.verifier(RUN, PLAN, POLICY, OPERATION)
    assert owner.stop_and_revoke(RUN) == "already_stopped"


def test_unsigned_or_mismatched_intent_cannot_register(owner_fixture):
    owner, intent, *_ = owner_fixture
    owner.recover_after_quiescence()
    for altered in (
        replace(intent, run_id="1" * 32, operation_id="2" * 32),
        replace(intent, plan_sha256="9" * 64),
        replace(intent, source_generation="8" * 64),
        replace(intent, receipt_root=str(owner.drain.receipt_root) + "-other"),
    ):
        with pytest.raises(OwnerCoordinationDenied, match="owner_"):
            owner.register_signed(altered)
        assert not owner.ledger.claim(altered)


def test_post_stop_ledger_failure_never_acknowledges_and_retry_is_durable(
    owner_fixture, monkeypatch,
):
    owner, intent, *_ = owner_fixture
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    owner.claim_signed(intent)
    old_revoke = owner.ledger.revoke

    def fail_revoke(run_id):
        raise OSError("FAKE SECRET LDAP TOKEN MUST NOT LEAK")

    monkeypatch.setattr(owner.ledger, "revoke", fail_revoke)
    with pytest.raises(
        OwnerCoordinationDenied, match="^stop_postcondition_unverified$",
    ) as err:
        owner.stop_and_revoke(RUN)
    assert "LDAP" not in str(err.value)
    assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    assert not owner.drain.verifier(RUN, PLAN, POLICY, OPERATION)
    monkeypatch.setattr(owner.ledger, "revoke", old_revoke)
    assert owner.stop_and_revoke(RUN) == "already_stopped"
    assert owner.ledger.is_revoked(RUN)


def test_stop_postcondition_must_be_exact_true(owner_fixture):
    owner, intent, *_ = owner_fixture
    owner.recover_after_quiescence()
    for bad in (False, 1, None):
        with pytest.raises(RunDrainDenied, match="stop_postcondition_unverified"):
            owner.drain.stop_and_drain(RUN, on_drained=lambda: bad)
    assert owner.drain.stop_and_drain(RUN, on_drained=lambda: True) == "already_stopped"


def test_recovery_rotation_and_stop_cannot_ack_while_effect_in_flight(owner_fixture):
    owner, intent, *_ = owner_fixture
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    owner.claim_signed(intent)
    rotated = []
    with owner.drain.effect(RUN, PLAN, POLICY, OPERATION):
        with pytest.raises(OwnerCoordinationDenied, match="owner_drain_timeout"):
            owner.recover_after_quiescence(timeout=0.02)
        with pytest.raises(OwnerCoordinationDenied, match="owner_drain_timeout"):
            owner.change_under_quiescence(lambda: rotated.append(True) or True, timeout=0.02)
        with pytest.raises(OwnerCoordinationDenied, match="drain_timeout"):
            owner.stop_and_revoke(RUN, timeout=0.02)
        assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
        assert owner.drain.verifier(RUN, PLAN, POLICY, OPERATION)
    assert rotated == []
    owner.change_under_quiescence(lambda: rotated.append(True) or True)
    assert rotated == [True]
    assert owner.stop_and_revoke(RUN) == "stopped_after_drain"
    assert owner.ledger.is_revoked(RUN)


def test_quiescent_change_requires_confirmed_success_and_sanitizes(owner_fixture):
    owner, *_ = owner_fixture
    called = []
    for response in (False, 1, None):
        with pytest.raises(OwnerCoordinationDenied, match="owner_change_unverified"):
            owner.change_under_quiescence(lambda: called.append(response) or response)
    def failed():
        raise RuntimeError("SYNTHETIC PRIVATE STRING")
    with pytest.raises(OwnerCoordinationDenied, match="^owner_change_unverified$") as e:
        owner.change_under_quiescence(failed)
    assert "PRIVATE" not in str(e.value)
    assert len(called) == 3


def test_invalid_timeout_and_wrong_operator_uid_fail_closed(owner_fixture, monkeypatch):
    owner, *_ = owner_fixture
    for timeout in (float("nan"), float("inf"), -1, True, 99):
        with pytest.raises(OwnerCoordinationDenied, match="owner_timeout_invalid"):
            owner.recover_after_quiescence(timeout=timeout)
    import obsidian_automation.vault_format_owner_coordination as module
    monkeypatch.setattr(module.os, "geteuid", lambda: owner.drain.verifier.trusted_owner_uid + 1)
    with pytest.raises(OwnerCoordinationDenied, match="owner_principal_required"):
        owner.recover_after_quiescence()


def test_exec_fresh_python_rejects_prior_claim_before_after_recovery(owner_fixture):
    """Proves a freshly exec'd interpreter has no inherited owner boot flag."""
    owner, intent, state, *_ = owner_fixture
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    owner.claim_signed(intent)
    assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    script = (
        "import json,sys\n"
        "from pathlib import Path\n"
        "from obsidian_automation.vault_format_run_ledger import DurableRunLedger, RunIntent\n"
        "x=json.loads(sys.argv[2]); ld=DurableRunLedger(Path(sys.argv[1]))\n"
        "print(json.dumps([ld.verify_claimed(x['run_id'], x['plan_sha256'],"
        " x['policy_sha256'], x['operation_id']), ld.claim(RunIntent(**x))]))\n"
    )
    def fresh_check():
        # New executable/interpreter, unlike fork-based synthetic tests.
        proc = subprocess.run(
            [sys.executable, "-c", script, str(state), json.dumps(vars(intent))],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=10, check=True,
        )
        return json.loads(proc.stdout)
    assert fresh_check() == [False, False]
    assert owner.recover_after_quiescence() == 1
    assert fresh_check() == [False, False]
    assert not owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    with pytest.raises(OwnerCoordinationDenied, match="owner_claim_denied"):
        owner.claim_signed(intent)


def test_signed_owner_lifecycle_guards_actual_synthetic_recurring_writer(tmp_path):
    """One fake WebDAV PUT/readback under the signed + ledger + effect gate."""
    from obsidian_automation.core_promotion_transport import HTTPResponse
    from obsidian_automation.vault_formatter import create_plan
    from obsidian_automation.vault_format_canary_binding import _generation
    from obsidian_automation.vault_format_recurring_admission import RecurringPolicy, _policy_digest
    from obsidian_automation.vault_format_recurring_binding import RecurringEffectWriter
    from obsidian_automation.vault_format_writer import _binding

    trusted, receipt, signer, grant, _ = signed_setup(tmp_path)
    vault = tmp_path / "fake-vault"
    file = vault / "98-System" / "synthetic.md"
    file.parent.mkdir(parents=True)
    before = b"# Synthetic\nfirst\n  \nsecond\n"
    file.write_bytes(before)
    plan = create_plan(vault)
    assert plan["counts"]["would_change"] == 1
    base_url = "https://dav.invalid/remote.php/dav/files/user/ObsidianVault"
    details = {
        "webdav_url": "https://dav.invalid/remote.php/dav/files/user/",
        "vault_folder": "ObsidianVault", "nextcloud_user": "user",
        "nextcloud_password": "ONLY-FAKE",
    }
    policy = RecurringPolicy(
        source_generation=_generation(details),
        account_binding_sha256=_binding(base_url, "user"),
        provider_proof_sha256=PROOF, max_targets=2, max_selected_bytes=4096,
    )
    policy_sha = _policy_digest(policy)
    grant.update({
        "plan_sha256": plan["plan_sha256"], "policy_sha256": policy_sha,
        "source_generation": policy.source_generation,
        "account_binding_sha256": policy.account_binding_sha256,
    })
    _sign(trusted, signer, grant)
    import shutil
    signed = SignedRunVerifier(
        trusted, receipt, signer_identity="vault-operator",
        expected_policy=policy, trusted_owner_uid=os.geteuid(),
        ssh_keygen=Path(shutil.which("ssh-keygen")),
        clock=lambda: grant["issued_at"] + 30,
    )
    state = trusted / "run-ledger"
    state.mkdir(mode=0o700)
    ledger = DurableRunLedger(state)
    drain = DurableRunDrain(signed, receipt_root=receipt)
    owner = OperatorRunCoordinator(drain, ledger)
    owner.recover_after_quiescence()
    intent = RunIntent(
        run_id=RUN, plan_sha256=plan["plan_sha256"],
        policy_sha256=policy_sha, operation_id=OPERATION,
        source_generation=policy.source_generation,
        account_binding_sha256=policy.account_binding_sha256,
        provider_proof_sha256=PROOF, receipt_root=str(receipt),
    )
    owner.register_signed(intent)
    owner.claim_signed(intent)
    class Shared:
        def __init__(self):
            self._lock = threading.RLock()
        def load_nextcloud(self):
            return dict(details)

    remote = {"body": before, "etag": '"v1"'}
    sent = []
    readback_bodies = []
    def fake_dav(*, method, target_url, username, password, headers,
                 body, timeout, response_limit):
        assert target_url == base_url + "/98-System/synthetic.md"
        assert (username, password) == ("user", "ONLY-FAKE")
        sent.append(method)
        if method == "GET":
            readback_bodies.append(remote["body"])
            return HTTPResponse(200, remote["body"], remote["etag"])
        assert method == "PUT" and headers["If-Match"] == remote["etag"]
        remote["body"] = body
        remote["etag"] = '"v2"'
        return HTTPResponse(204, b"", remote["etag"])
    writer = RecurringEffectWriter(
        Shared(), source_generation=lambda: _generation(details),
        policy=policy, run_id=RUN, receipt_root=receipt,
        policy_verifier=lambda *a: a == (
            policy_sha, policy.source_generation, policy.account_binding_sha256,
            policy.provider_proof_sha256,
        ),
        run_verifier=ledger.verify_claimed,
        provider_verifier=lambda *a: a == (
            PROOF, policy.source_generation, policy.account_binding_sha256,
        ),
        transport=fake_dav,
    )
    with drain.effect(RUN, plan["plan_sha256"], policy_sha, OPERATION):
        result = writer(plan)
    assert result["status"] == "complete"
    assert sent.count("PUT") == 1
    assert sent.count("GET") >= 2
    assert sent.index("PUT") < len(sent) - 1
    assert sent[-1] == "GET"
    assert readback_bodies[0] == before
    assert readback_bodies[-1] == remote["body"]
    assert remote["body"] != before
    assert file.read_bytes() == before
    assert owner.stop_and_revoke(RUN) == "stopped_after_drain"
    assert not ledger.verify_claimed(RUN, plan["plan_sha256"], policy_sha, OPERATION)
    with pytest.raises(RunDrainDenied, match="signed_run_unverified"):
        with drain.effect(RUN, plan["plan_sha256"], policy_sha, OPERATION):
            writer(plan)
    assert sent.count("PUT") == 1


def test_wrong_ledger_database_cannot_be_bound_to_same_signed_authority(
    owner_fixture, tmp_path,
):
    owner, intent, *_ = owner_fixture
    untrusted_alt = tmp_path / "other-owner-ledger"
    untrusted_alt.mkdir(mode=0o700)
    wrong = DurableRunLedger(untrusted_alt)
    wrong.mark_unconfirmed_after_restart()
    wrong.register(intent, verify_external_approval=lambda _: True)
    assert wrong.claim(intent)
    assert wrong.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    with pytest.raises(
        OwnerCoordinationDenied, match="^owner_ledger_root_mismatch$",
    ):
        OperatorRunCoordinator(owner.drain, wrong)
    # Our canonical trusted owner's durable row remains absent: the signer
    # is not enough to accept an arbitrary supplied database for stop ack.
    assert not owner.ledger.is_revoked(RUN)


@pytest.mark.parametrize("close_call", [1, 2])
def test_os_close_failure_never_strands_owner_serial_lock(
    owner_fixture, monkeypatch, close_call,
):
    import obsidian_automation.vault_format_owner_coordination as owner_module

    owner, *_ = owner_fixture
    original_close = owner_module.os.close
    counter = {"value": 0}

    def fail_one_close(fd):
        counter["value"] += 1
        original_close(fd)
        if counter["value"] == close_call:
            raise OSError("SYNTHETIC SENSITIVE TOKEN")

    # Inject the close failure *after* entry, so the lower-level authority
    # directory traversal does not receive an unrelated fault injection.
    try:
        with pytest.raises(OwnerCoordinationDenied, match="^owner_lease_unavailable$") as err:
            with owner.quiesced():
                monkeypatch.setattr(owner_module.os, "close", fail_one_close)
        assert "SENSITIVE" not in str(err.value)
    finally:
        monkeypatch.setattr(owner_module.os, "close", original_close)

    # The previously held owner guard must be released even on close error.
    with owner.quiesced(timeout=0):
        pass


def test_same_authority_root_but_alternate_db_name_denied(owner_fixture):
    owner, *_ = owner_fixture
    original_path = owner.ledger.path
    owner.ledger.path = original_path.with_name("alternate.sqlite3")
    try:
        with pytest.raises(
            OwnerCoordinationDenied, match="^owner_ledger_root_mismatch$",
        ):
            OperatorRunCoordinator(owner.drain, owner.ledger)
    finally:
        owner.ledger.path = original_path


def test_symlinked_authoritative_ledger_directory_denied(tmp_path):
    from obsidian_automation.vault_format_writer import FormatWriteError
    trusted, _, _, _, _ = signed_setup(tmp_path)
    alternate = tmp_path / "other-private-ledger"
    alternate.mkdir(mode=0o700)
    (trusted / "run-ledger").symlink_to(alternate, target_is_directory=True)
    with pytest.raises(FormatWriteError, match="^run_ledger_state_unsafe$"):
        DurableRunLedger(trusted / "run-ledger")


def test_cross_process_effect_lease_prevents_owner_stop_and_rotation(
    owner_fixture,
):
    """Separate OS process holds signed gate while Owner attempts transitions."""
    from multiprocessing import get_context
    from test_472_signed_run_drain import _separate_process_hold

    owner, intent, _, trusted, _, _, receipts = owner_fixture
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    owner.claim_signed(intent)
    ctx = get_context("fork")
    parent, child = ctx.Pipe()
    worker = ctx.Process(
        target=_separate_process_hold,
        args=(str(trusted), str(receipts), child),
    )
    worker.start()
    rotated = []
    try:
        assert parent.poll(5)
        assert parent.recv() == "entered"
        with pytest.raises(OwnerCoordinationDenied, match="owner_drain_timeout"):
            owner.recover_after_quiescence(timeout=0.05)
        with pytest.raises(OwnerCoordinationDenied, match="owner_drain_timeout"):
            owner.change_under_quiescence(
                lambda: rotated.append(True) or True, timeout=0.05,
            )
        with pytest.raises(OwnerCoordinationDenied, match="drain_timeout"):
            owner.stop_and_revoke(RUN, timeout=0.05)
        assert rotated == []
        assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    finally:
        if worker.is_alive():
            parent.send("release")
        worker.join(5)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        parent.close()
        child.close()
    assert worker.exitcode == 0
    assert owner.stop_and_revoke(RUN) == "stopped_after_drain"
    assert owner.ledger.is_revoked(RUN)


def test_ledger_path_rebound_after_owner_init_cannot_ack_stop(owner_fixture):
    """A changed ledger.path must not silently redirect authoritative revoke."""
    import shutil

    owner, intent, _, trusted, _, _, _ = owner_fixture
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    owner.claim_signed(intent)
    canonical = owner.ledger.path
    alternate = canonical.with_name("alternate.sqlite3")
    shutil.copy2(canonical, alternate)
    assert alternate.is_file()
    owner.ledger.path = alternate
    try:
        with pytest.raises(
            OwnerCoordinationDenied, match="^owner_ledger_root_mismatch$",
        ):
            owner.stop_and_revoke(RUN)
    finally:
        owner.ledger.path = canonical
    assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    assert not (trusted / (RUN + ".revoked")).exists()
    assert owner.stop_and_revoke(RUN) == "stopped_after_drain"
    assert owner.ledger.is_revoked(RUN)
