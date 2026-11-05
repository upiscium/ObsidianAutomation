"""Synthetic B2a-S / B2a-L compatibility proof only.

This is NOT production orchestration or a genuine independently controlled
Human grant. Private keys and approval files are disposable test fixtures.
No Nextcloud access, HTTP requests, credential reads, or Writer injection.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from test_472_signed_run_drain import (
    setup as signed_setup, RUN, PLAN, POLICY, OPERATION, SOURCE,
    BINDING, PROOF,
)
from obsidian_automation.vault_format_run_ledger import (
    DurableRunLedger, RunIntent,
)
from obsidian_automation.vault_format_run_drain import (
    DurableRunDrain, RunDrainDenied,
)
from obsidian_automation.vault_format_writer import FormatWriteError


def test_signed_run_and_ledger_claim_can_coexist_inside_drain(tmp_path):
    _, receipt, _, _, verifier = signed_setup(tmp_path)
    state = tmp_path / "owned-ledger"
    state.mkdir(mode=0o700)
    ledger = DurableRunLedger(state)
    assert ledger.mark_unconfirmed_after_restart() == 0

    intent = RunIntent(
        run_id=RUN, plan_sha256=PLAN, policy_sha256=POLICY,
        operation_id=OPERATION, source_generation=SOURCE,
        account_binding_sha256=BINDING, provider_proof_sha256=PROOF,
        receipt_root=str(receipt),
    )
    # A verifier trusted ONLY within this synthetic fixture; production must
    # independently own the signer and ledger process/authority.
    assert not ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    with pytest.raises(FormatWriteError, match="run_approval_unverified"):
        ledger.register(
            replace(intent, plan_sha256="0" * 64),
            verify_external_approval=lambda row: verifier(
                row.run_id, row.plan_sha256, row.policy_sha256,
                row.operation_id,
            ),
        )
    ledger.register(
        intent,
        verify_external_approval=lambda row: verifier(
            row.run_id, row.plan_sha256, row.policy_sha256,
            row.operation_id,
        ),
    )
    assert not ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    assert ledger.claim(intent)
    assert ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    assert not ledger.claim(intent)

    gate = DurableRunDrain(verifier, receipt_root=receipt)
    with gate.effect(RUN, PLAN, POLICY, OPERATION):
        # Future B1 callback is permitted only while the coordinated
        # externally approved lease is actually held by trusted orchestration.
        assert ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
        with pytest.raises(RunDrainDenied, match="drain_busy"):
            gate.stop_and_drain(RUN, timeout=0)
    assert gate.stop_and_drain(RUN) == "stopped_after_drain"
    assert not verifier(RUN, PLAN, POLICY, OPERATION)
    # This separate source-only stop changes only the signed grant. The
    # ledger remains claimed until a trusted owner explicitly revokes it.
    # Neither state alone is a production authority; future orchestration
    # must coordinate them and preserve the shared effect lease.
    assert ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    assert ledger.revoke(RUN)
    assert not ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    assert not ledger.claim(intent)
    with pytest.raises(RunDrainDenied, match="signed_run_unverified"):
        with gate.effect(RUN, PLAN, POLICY, OPERATION):
            pass


def test_permissive_ledger_approval_cannot_replace_missing_signed_run(tmp_path):
    _, receipt, _, _, verifier = signed_setup(tmp_path)
    state = tmp_path / "owned-ledger"
    state.mkdir(mode=0o700)
    ledger = DurableRunLedger(state)
    ledger.mark_unconfirmed_after_restart()
    forged = RunIntent(
        run_id="1" * 32, plan_sha256=PLAN, policy_sha256=POLICY,
        operation_id="2" * 32, source_generation=SOURCE,
        account_binding_sha256=BINDING, provider_proof_sha256=PROOF,
        receipt_root=str(receipt),
    )
    # Ledger callback is intentionally permissive: it is NOT a real grant.
    ledger.register(forged, verify_external_approval=lambda _: True)
    assert ledger.claim(forged)
    assert ledger.verify_claimed(
        forged.run_id, forged.plan_sha256,
        forged.policy_sha256, forged.operation_id,
    )
    gate = DurableRunDrain(verifier, receipt_root=receipt)
    with pytest.raises(RunDrainDenied, match="signed_run_unverified"):
        with gate.effect(
            forged.run_id, forged.plan_sha256,
            forged.policy_sha256, forged.operation_id,
        ):
            pass


def test_external_verifier_mutation_cannot_change_registered_intent(tmp_path):
    """Approval must seal the exact immutable snapshot, not an aliased object."""
    state = tmp_path / "owned-ledger"
    state.mkdir(mode=0o700)
    ledger = DurableRunLedger(state)
    ledger.mark_unconfirmed_after_restart()
    intent = RunIntent(
        run_id=RUN, plan_sha256=PLAN, policy_sha256=POLICY,
        operation_id=OPERATION, source_generation=SOURCE,
        account_binding_sha256=BINDING, provider_proof_sha256=PROOF,
        receipt_root=str(tmp_path / "receipts"),
    )
    def verifier(x):
        assert x.plan_sha256 == PLAN
        object.__setattr__(x, "plan_sha256", "1" * 64)
        object.__setattr__(intent, "plan_sha256", "2" * 64)
        return True
    ledger.register(intent, verify_external_approval=verifier)
    assert ledger.claim(replace(intent, plan_sha256=PLAN))
    assert ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)


def test_mutating_caller_run_id_during_claim_cannot_claim_different_row(
    tmp_path, monkeypatch,
):
    """Selecting run A and updating run B would violate single-use binding."""
    import sqlite3
    state = tmp_path / "owned-ledger"
    state.mkdir(mode=0o700)
    ledger = DurableRunLedger(state)
    ledger.mark_unconfirmed_after_restart()
    a = RunIntent(
        run_id=RUN, plan_sha256=PLAN, policy_sha256=POLICY,
        operation_id=OPERATION, source_generation=SOURCE,
        account_binding_sha256=BINDING, provider_proof_sha256=PROOF,
        receipt_root=str(tmp_path / "receipts"),
    )
    b = replace(a, run_id="1" * 32, operation_id="2" * 32)
    for intent in (a, b):
        ledger.register(intent, verify_external_approval=lambda _: True)
    original_row = DurableRunLedger._row
    def intercept(db, run_id):
        row = original_row(db, run_id)
        object.__setattr__(a, "run_id", b.run_id)
        return row
    monkeypatch.setattr(DurableRunLedger, "_row", staticmethod(intercept))
    assert ledger.claim(a)
    with sqlite3.connect(ledger.path) as db:
        statuses = dict(db.execute("SELECT run_id,status FROM run_intents"))
    assert statuses[RUN] == "claimed"
    assert statuses[b.run_id] == "approved"
