"""Synthetic durable run-intent tests. No real approval or WebDAV."""
from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import stat

import pytest

from obsidian_automation.vault_format_writer import FormatWriteError
from obsidian_automation.vault_format_run_ledger import DurableRunLedger, RunIntent


@pytest.fixture
def setup(tmp_path):
    state = tmp_path / "owner-private"
    state.mkdir(mode=0o700)
    root = tmp_path / "receipts"
    root.mkdir(mode=0o700)
    ledger = DurableRunLedger(state)
    assert ledger.mark_unconfirmed_after_restart() == 0
    intent = RunIntent(
        run_id="a" * 32, plan_sha256="b" * 64, policy_sha256="c" * 64,
        operation_id="d" * 32, source_generation="e" * 64,
        account_binding_sha256="f" * 64, provider_proof_sha256="1" * 64,
        receipt_root=str(root),
    )
    return ledger, intent, state


def test_missing_or_rejected_external_approval_never_registers(setup):
    ledger, intent, state = setup
    with pytest.raises(FormatWriteError, match="run_approval_missing"):
        ledger.register(intent, verify_external_approval=None)
    with pytest.raises(FormatWriteError, match="run_approval_unverified"):
        ledger.register(intent, verify_external_approval=lambda *_: False)
    assert not ledger.claim(intent)
    assert not ledger.verify_claimed(intent.run_id, intent.plan_sha256,
                                     intent.policy_sha256, intent.operation_id)


def test_claim_is_durable_single_use_after_reopen(setup):
    ledger, intent, state = setup
    ledger.register(intent, verify_external_approval=lambda x: x == intent)
    assert ledger.claim(intent)
    reopened = DurableRunLedger(state)
    assert not reopened.verify_claimed(intent.run_id, intent.plan_sha256,
                                       intent.policy_sha256, intent.operation_id)
    assert not reopened.claim(intent)
    assert not reopened.claim(replace(intent, plan_sha256="9" * 64))
    assert reopened.mark_unconfirmed_after_restart() == 1
    assert not reopened.verify_claimed(intent.run_id, intent.plan_sha256,
                                      intent.policy_sha256, intent.operation_id)
    assert not reopened.claim(intent)
    assert reopened.mark_unconfirmed_after_restart() == 0


def test_revoke_is_durable_and_no_rearm(setup):
    ledger, intent, state = setup
    ledger.register(intent, verify_external_approval=lambda *_: True)
    assert ledger.revoke(intent.run_id)
    assert not ledger.claim(intent)
    assert not ledger.revoke(intent.run_id)
    assert not ledger.verify_claimed(intent.run_id, intent.plan_sha256,
                                     intent.policy_sha256, intent.operation_id)
    with pytest.raises(FormatWriteError, match="run_already_registered"):
        ledger.register(intent, verify_external_approval=lambda *_: True)


def test_claimed_run_can_be_revoked_and_readers_observe_it(setup):
    ledger, intent, state = setup
    ledger.register(intent, verify_external_approval=lambda *_: True)
    assert ledger.claim(intent)
    assert ledger.revoke(intent.run_id)
    assert not ledger.verify_claimed(intent.run_id, intent.plan_sha256,
                                     intent.policy_sha256, intent.operation_id)


def test_double_claim_across_independent_connections_only_one_wins(setup):
    ledger, intent, state = setup
    ledger.register(intent, verify_external_approval=lambda *_: True)
    clients = [DurableRunLedger(state) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda x: x.claim(intent), clients))
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 7


def test_registration_cannot_replace_existing_run_or_operation(setup):
    ledger, intent, state = setup
    ledger.register(intent, verify_external_approval=lambda *_: True)
    with pytest.raises(FormatWriteError, match="run_already_registered"):
        ledger.register(replace(intent, plan_sha256="9"*64),
                        verify_external_approval=lambda *_: True)
    with pytest.raises(FormatWriteError, match="run_already_registered"):
        ledger.register(replace(intent, run_id="9"*32),
                        verify_external_approval=lambda *_: True)


@pytest.mark.parametrize("field,replacement", [
    ("run_id", "xyz"), ("operation_id", "z" * 32),
    ("plan_sha256", "b" * 63), ("policy_sha256", True),
    ("receipt_root", "relative"), ("receipt_root", "/tmp/../bad"),
])
def test_malformed_intent_fails_closed(setup, field, replacement):
    ledger, intent, _ = setup
    with pytest.raises(FormatWriteError, match="run_intent_invalid"):
        ledger.register(replace(intent, **{field:replacement}),
                        verify_external_approval=lambda *_: True)


def test_state_permissions_and_symlink_rejected(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir(mode=0o700)
    bad.chmod(0o755)
    with pytest.raises(FormatWriteError, match="run_ledger_state_unsafe"):
        DurableRunLedger(bad)
    good = tmp_path / "good"
    good.mkdir(mode=0o700)
    (good / "formatter-run-intents.sqlite3").symlink_to(tmp_path / "target")
    with pytest.raises(FormatWriteError, match="run_ledger_state_unsafe"):
        DurableRunLedger(good)


def test_private_file_and_no_secret_values_in_schema(setup):
    ledger, intent, _ = setup
    assert stat.S_IMODE(ledger.path.stat().st_mode) == 0o600
    ledger.register(intent, verify_external_approval=lambda *_: True)
    with sqlite3.connect(ledger.path) as db:
        columns={row[1] for row in db.execute("PRAGMA table_info(run_intents)")}
    assert "password" not in columns and "username" not in columns



def test_new_ledger_instance_denied_before_owner_recovery(setup):
    ledger, intent, state = setup
    ledger.register(intent, verify_external_approval=lambda *_: True)
    assert ledger.claim(intent)
    assert ledger.verify_claimed(intent.run_id, intent.plan_sha256,
                                 intent.policy_sha256, intent.operation_id)
    second = DurableRunLedger(state)
    assert not second.verify_claimed(intent.run_id, intent.plan_sha256,
                                     intent.policy_sha256, intent.operation_id)
    assert second.mark_unconfirmed_after_restart() == 1
    assert not ledger.verify_claimed(intent.run_id, intent.plan_sha256,
                                     intent.policy_sha256, intent.operation_id)


def test_corrupt_database_is_fail_closed_and_error_is_static(setup):
    ledger, intent, state = setup
    ledger.path.write_bytes(b"SYNTHETIC_CORRUPT_DB_MARKER")
    with pytest.raises(FormatWriteError, match="^run_ledger_unavailable$") as error:
        ledger.verify_claimed(intent.run_id, intent.plan_sha256,
                              intent.policy_sha256, intent.operation_id)
    assert "SYNTHETIC" not in str(error.value)
    with pytest.raises(FormatWriteError, match="^run_ledger_unavailable$"):
        DurableRunLedger(state)


def test_untrusted_parent_rejected_before_sqlite_open(tmp_path):
    intermediate = tmp_path / "untrusted"
    intermediate.mkdir(mode=0o700)
    state = intermediate / "private"
    state.mkdir(mode=0o700)
    intermediate.chmod(0o777)
    try:
        with pytest.raises(FormatWriteError, match="^run_ledger_state_unsafe$"):
            DurableRunLedger(state)
    finally:
        intermediate.chmod(0o700)


def test_parent_symlink_rejected(tmp_path):
    owned = tmp_path / "owned"
    owned.mkdir(mode=0o700)
    (owned / "private").mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(owned, target_is_directory=True)
    with pytest.raises(FormatWriteError, match="^run_ledger_state_unsafe$"):
        DurableRunLedger(alias / "private")


def test_replaced_state_directory_rejected_after_initialization(tmp_path):
    state = tmp_path / "private"
    state.mkdir(mode=0o700)
    ledger = DurableRunLedger(state)
    ledger.mark_unconfirmed_after_restart()
    state.rename(tmp_path / "moved")
    state.mkdir(mode=0o700)
    with pytest.raises(FormatWriteError, match="^run_ledger_state_unsafe$"):
        ledger.mark_unconfirmed_after_restart()



def _independent_process_claim(args):
    """Spawn a new SQLite connection in a distinct OS process."""
    from pathlib import Path
    from obsidian_automation.vault_format_run_ledger import DurableRunLedger
    location, intent = args
    return DurableRunLedger(Path(location)).claim(intent)


def test_single_claim_across_distinct_processes(setup):
    from multiprocessing import get_context

    ledger, intent, state = setup
    ledger.register(intent, verify_external_approval=lambda *_: True)
    with get_context("fork").Pool(processes=3) as pool:
        results = pool.map(_independent_process_claim,
                           [(str(state), intent)] * 6)
    assert results.count(True) == 1
    assert results.count(False) == 5
    # New process instances may claim exactly once, but their read-only B1
    # verifier must stay OFF until a separately coordinated owner recovery.
    check = DurableRunLedger(state)
    assert not check.verify_claimed(
        intent.run_id, intent.plan_sha256,
        intent.policy_sha256, intent.operation_id,
    )


def _verify_from_independent_process(args):
    """Fresh OS process and independent SQLite connection, no shared boot flag."""
    from pathlib import Path
    from obsidian_automation.vault_format_run_ledger import DurableRunLedger

    location, intent = args
    verifier = DurableRunLedger(Path(location))
    values = (intent.run_id, intent.plan_sha256,
              intent.policy_sha256, intent.operation_id)
    before_recovery = verifier.verify_claimed(*values)
    affected = verifier.mark_unconfirmed_after_restart()
    after_recovery = verifier.verify_claimed(*values)
    replay_allowed = verifier.claim(intent)
    return before_recovery, affected, after_recovery, replay_allowed


def test_fresh_os_process_denies_previous_claim_before_and_after_recovery(setup):
    """An actual second OS process must not inherit prior approval authority."""
    from multiprocessing import get_context

    ledger, intent, state = setup
    ledger.register(intent, verify_external_approval=lambda _: True)
    assert ledger.claim(intent)
    values = (intent.run_id, intent.plan_sha256,
              intent.policy_sha256, intent.operation_id)
    assert ledger.verify_claimed(*values)
    # This synthetic owner-restart barrier is only safe when all active
    # effect workers have been drained. No runtime owner coordinator exists.
    with get_context("fork").Pool(processes=1) as pool:
        result = pool.apply(
            _verify_from_independent_process,
            args=((str(state), intent),),
        )
    assert result == (False, 1, False, False)
    # Recovery in the second process has also invalidated the former
    # owner's read-only verification through durable SQLite state.
    assert not ledger.verify_claimed(*values)
