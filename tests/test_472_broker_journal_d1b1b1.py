"""#472 D1/B1/B1 disposable Broker-private state-machine tests.

There is no Broker daemon, credential, Owner-authenticated connection,
Provider operation, Vault write or deployed service. Successful tests run
as one local UID and cannot establish cross-UID/egress isolation.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from obsidian_automation.vault_format_effect_journal import EffectAttempt
from obsidian_automation.vault_format_broker_observation import _attempt_key
from obsidian_automation.vault_format_broker_journal import (
    BrokerPrivateJournal, BrokerJournalDenied,
)

ACCOUNT = "4" * 64
GEN = "8" * 64
OTHER = "a" * 64


@pytest.fixture
def broker_dir(tmp_path):
    root = tmp_path / "broker-private"
    root.mkdir(mode=0o700)
    return root


@pytest.fixture
def broker(broker_dir):
    return BrokerPrivateJournal(broker_dir, bootstrap=True)


def boot_ready(broker):
    assert broker.start_boot() == 1
    assert broker.reconcile_boot() is True
    broker.provision_generation_metadata(ACCOUNT, GEN)


def attempt(*, run="a", op="b", target="98-System/synthetic.md",
            generation=GEN, epoch=0):
    return EffectAttempt(
        run_id=run * 32, operation_id=op * 32,
        plan_sha256="1" * 64, policy_sha256="2" * 64,
        source_generation="3" * 64, account_binding_sha256=ACCOUNT,
        provider_proof_sha256="5" * 64,
        receipt_root="/tmp/synthetic-receipts",
        target_path=target, precondition_etag='"synthetic"',
        before_sha256="6" * 64, after_sha256="7" * 64,
        credential_generation=generation,
        claim_boot_epoch=1, claim_credential_epoch=epoch,
    )


def rows(broker, sql, args=()):
    conn = sqlite3.connect(broker._db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_initial_boot_closed_and_no_implicit_rebootstrap(broker, broker_dir):
    other = BrokerPrivateJournal(broker_dir)
    with pytest.raises(BrokerJournalDenied, match="^broker_boot_stale$"):
        other.provision_generation_metadata(ACCOUNT, GEN)
    with pytest.raises(BrokerJournalDenied, match="^broker_already_provisioned$"):
        BrokerPrivateJournal(broker_dir, bootstrap=True)
    boot_ready(broker)
    # A fresh handle may observe credential metadata, but cannot mutate
    # authority without an explicit trusted Broker boot transition.
    assert BrokerPrivateJournal(broker_dir).observe_credential(ACCOUNT).credential_generation == GEN
    assert broker.start_boot() == 2
    with pytest.raises(BrokerJournalDenied, match="^broker_boot_pending$"):
        broker.start_boot()


def test_generation_metadata_and_one_shot_fence(broker):
    boot_ready(broker)
    observed = broker.observe_credential(ACCOUNT)
    assert observed.credential_generation == GEN and observed.credential_epoch == 0
    assert observed.broker_ledger_revision != "0" * 64
    with pytest.raises(BrokerJournalDenied, match="^broker_account_exists$"):
        broker.provision_generation_metadata(ACCOUNT, GEN)
    a = attempt()
    broker.fence_pre_dispatch(a)
    pending = broker.observe_finality(a)
    assert pending.disposition == "pending"
    assert pending.receipt_sha256 is None
    assert pending.broker_journal_seq is None
    assert pending.attempt_sha256 == _attempt_key(a)[1]
    with pytest.raises(BrokerJournalDenied, match="^broker_attempt_spent$"):
        broker.fence_pre_dispatch(a)


def test_target_conflict_across_different_run_identity(broker):
    boot_ready(broker)
    broker.fence_pre_dispatch(attempt())
    with pytest.raises(BrokerJournalDenied, match="^broker_target_conflict$"):
        broker.fence_pre_dispatch(attempt(run="c", op="d"))
    broker.fence_pre_dispatch(attempt(run="c", op="d",
                                       target="98-System/other.md"))


def test_never_dispatched_atomic_terminal_receipt_and_permanent_spent_id(broker, broker_dir):
    boot_ready(broker)
    a = attempt()
    broker.fence_pre_dispatch(a)
    terminal = broker.cancel_never_dispatched(a)
    assert terminal.disposition == "verified_no_effect"
    assert terminal.receipt_sha256 and terminal.receipt_sha256 != "0" * 64
    assert terminal.broker_journal_seq == 1
    second = BrokerPrivateJournal(broker_dir)
    assert second.observe_finality(a) == terminal
    with pytest.raises(BrokerJournalDenied, match="^broker_no_dispatch_unverified$"):
        broker.cancel_never_dispatched(a)
    with pytest.raises(BrokerJournalDenied, match="^broker_attempt_spent$"):
        broker.fence_pre_dispatch(a)
    assert rows(broker, "SELECT count(*) FROM broker_resource")[0][0] == 0
    broker.fence_pre_dispatch(attempt(run="c", op="d"))
    terminal_two = broker.cancel_never_dispatched(attempt(run="c", op="d"))
    assert terminal_two.broker_journal_seq == 2
    assert terminal_two.receipt_sha256 != terminal.receipt_sha256


def test_dispatch_possibility_quarantines_account_before_any_effect(broker):
    boot_ready(broker)
    a = attempt()
    broker.fence_pre_dispatch(a)
    broker.mark_dispatch_possible(a)
    obs = broker.observe_finality(a)
    assert obs.disposition == "pending" and obs.receipt_sha256 is None
    assert rows(broker, "SELECT count(*) FROM broker_account_quarantine")[0][0] == 1
    assert rows(broker, "SELECT status FROM broker_attempt")[0][0] == "dispatch_possible"
    with pytest.raises(BrokerJournalDenied, match="^broker_no_dispatch_unverified$"):
        broker.cancel_never_dispatched(a)
    with pytest.raises(BrokerJournalDenied, match="^broker_account_unconfirmed$"):
        broker.fence_pre_dispatch(attempt(run="c", op="d",
                                           target="98-System/other.md"))
    assert broker.request_off() == "pending_inflight"
    broker.mark_uncertain(a)
    assert broker.request_off() == "pending_unconfirmed"
    with pytest.raises(BrokerJournalDenied, match="^broker_no_dispatch_unverified$"):
        broker.cancel_never_dispatched(a)


def test_reboot_preserves_account_quarantine_and_spent_fence(broker, broker_dir):
    boot_ready(broker)
    a = attempt()
    broker.fence_pre_dispatch(a)
    broker.mark_dispatch_possible(a)
    fresh = BrokerPrivateJournal(broker_dir)
    assert fresh.start_boot() == 2
    assert fresh.reconcile_boot() is False
    assert fresh.observe_finality(a).disposition == "pending"
    assert rows(fresh, "SELECT status FROM broker_attempt")[0][0] == "unconfirmed"
    assert rows(fresh, "SELECT count(*) FROM broker_resource")[0][0] == 1
    assert rows(fresh, "SELECT count(*) FROM broker_account_quarantine")[0][0] == 1
    with pytest.raises(BrokerJournalDenied, match="^broker_boot_stale$"):
        broker.fence_pre_dispatch(attempt(run="c", op="d"))
    with pytest.raises(BrokerJournalDenied, match="^broker_boot_pending$"):
        fresh.start_boot()
    with pytest.raises(BrokerJournalDenied, match="^broker_no_dispatch_unverified$"):
        fresh.cancel_never_dispatched(a)


def test_reboot_pre_dispatch_can_cancel_but_not_rearm(broker, broker_dir):
    boot_ready(broker)
    a = attempt()
    broker.fence_pre_dispatch(a)
    fresh = BrokerPrivateJournal(broker_dir)
    fresh.start_boot()
    terminal = fresh.cancel_never_dispatched(a)
    assert terminal.disposition == "verified_no_effect"
    assert fresh.reconcile_boot() is True
    assert fresh.observe_finality(a) == terminal
    with pytest.raises(BrokerJournalDenied, match="^broker_attempt_spent$"):
        fresh.fence_pre_dispatch(a)


def test_off_is_durable_and_does_not_permit_new_fence(broker, broker_dir):
    boot_ready(broker)
    a = attempt()
    broker.fence_pre_dispatch(a)
    assert broker.request_off() == "complete"  # pre-dispatch has no effect path
    with pytest.raises(BrokerJournalDenied, match="^broker_admission_closed$"):
        broker.fence_pre_dispatch(attempt(run="c", op="d"))
    broker.cancel_never_dispatched(a)
    fresh = BrokerPrivateJournal(broker_dir)
    fresh.start_boot()
    assert fresh.reconcile_boot() is False
    with pytest.raises(BrokerJournalDenied, match="^broker_admission_closed$"):
        fresh.fence_pre_dispatch(attempt(run="c", op="d"))


def test_rotation_freeze_is_only_pending_metadata_not_credential_change(broker):
    boot_ready(broker)
    a = attempt()
    broker.fence_pre_dispatch(a)
    assert broker.freeze_generation(ACCOUNT) == "ready_for_separate_rotation"
    with pytest.raises(BrokerJournalDenied, match="^broker_account_unavailable$"):
        broker.fence_pre_dispatch(attempt(run="c", op="d",
                                           target="98-System/other.md"))
    broker.cancel_never_dispatched(a)
    assert broker.observe_credential(ACCOUNT).credential_epoch == 0
    assert broker.observe_credential(ACCOUNT).credential_generation == GEN
    assert broker.freeze_generation(ACCOUNT) == "ready_for_separate_rotation"


def test_rotation_freeze_pending_if_provider_effect_ambiguous(broker):
    boot_ready(broker)
    a = attempt()
    broker.fence_pre_dispatch(a)
    broker.mark_dispatch_possible(a)
    assert broker.freeze_generation(ACCOUNT) == "pending_inflight"
    broker.mark_uncertain(a)
    assert broker.freeze_generation(ACCOUNT) == "pending_unconfirmed"
    assert broker.observe_credential(ACCOUNT).credential_epoch == 0


def test_stale_generation_and_forged_identity_denied(broker):
    boot_ready(broker)
    with pytest.raises(BrokerJournalDenied, match="^broker_generation_stale$"):
        broker.fence_pre_dispatch(attempt(generation=OTHER))
    with pytest.raises(BrokerJournalDenied, match="^broker_generation_stale$"):
        broker.fence_pre_dispatch(attempt(epoch=1))
    a = attempt()
    broker.fence_pre_dispatch(a)
    for other in (
        replace(a, plan_sha256="f" * 64),
        replace(a, precondition_etag='"other"'),
        replace(a, target_path="98-System/elsewhere.md"),
    ):
        with pytest.raises(BrokerJournalDenied, match="^broker_attempt_mismatch$"):
            broker.cancel_never_dispatched(other)
    assert broker.observe_finality(a).disposition == "pending"


def test_invalid_paths_and_identifiers_never_write(broker):
    boot_ready(broker)
    with pytest.raises(BrokerJournalDenied, match="^broker_attempt_invalid$"):
        broker.fence_pre_dispatch(attempt(target="Private/secret.md"))
    with pytest.raises(BrokerJournalDenied, match="^broker_generation_invalid$"):
        broker.provision_generation_metadata(ACCOUNT, "not-hash")
    with pytest.raises(BrokerJournalDenied, match="^broker_account_invalid$"):
        broker.observe_credential("not-hash")
    assert rows(broker, "SELECT count(*) FROM broker_attempt")[0][0] == 0


def test_missing_db_does_not_auto_recreate(broker, broker_dir):
    (broker_dir / broker.FILE).unlink()
    with pytest.raises(BrokerJournalDenied, match="^broker_file_missing$"):
        BrokerPrivateJournal(broker_dir)
    with pytest.raises(BrokerJournalDenied, match="^broker_already_provisioned$"):
        BrokerPrivateJournal(broker_dir, bootstrap=True)


def test_same_token_replaced_inode_rejected(broker, broker_dir, tmp_path):
    db = broker_dir / broker.FILE
    alternative = tmp_path / "copy.db"
    alternative.write_bytes(db.read_bytes())
    alternative.chmod(0o600)
    alternative.replace(db)
    with pytest.raises(BrokerJournalDenied, match="^broker_identity_changed$"):
        broker.observe_credential(ACCOUNT)


def test_unknown_operation_has_no_fabricated_finality(broker):
    boot_ready(broker)
    with pytest.raises(BrokerJournalDenied, match="^broker_attempt_unknown$"):
        broker.observe_finality(attempt())


def test_no_remote_effect_finality_issuer_in_b1b1(broker):
    boot_ready(broker)
    assert not hasattr(broker, "verify_remote_effect")
    assert not hasattr(broker, "send_provider_put")
    assert not hasattr(broker, "issue_credentials")
    assert not hasattr(broker, "rotate_credentials")
    assert not hasattr(broker, "clear_account_quarantine")


def test_parallel_same_target_only_one_fence(broker, broker_dir):
    boot_ready(broker)
    start = threading.Barrier(2, timeout=5)

    def run(run_id, op):
        start.wait()
        try:
            broker.fence_pre_dispatch(attempt(run=run_id, op=op))
            return "fenced"
        except BrokerJournalDenied as e:
            return str(e)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(run, "a", "b"), pool.submit(run, "c", "d")
        statuses = {a.result(), b.result()}
    assert statuses == {"fenced", "broker_target_conflict"}
    assert rows(broker, "SELECT count(*) FROM broker_attempt")[0][0] == 1


def test_crash_before_sql_commit_keeps_broker_closed(broker, broker_dir):
    code = """import os,sqlite3,sys
c=sqlite3.connect(sys.argv[1],isolation_level=None)
c.execute("BEGIN IMMEDIATE")
c.execute("UPDATE broker_gate SET state='open',boot_reconciled=1 WHERE id=1")
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", code, str(broker_dir / broker.FILE)],
                   check=True, capture_output=True, timeout=8)
    assert rows(broker, "SELECT state FROM broker_gate")[0][0] == "closed"


def test_unwired_and_no_new_runtime_effects():
    root = Path(__file__).resolve().parents[1] / "src/obsidian_automation"
    for filename in (
        "snapshot_app.py", "vault_format_job.py",
        "vault_format_owner_ipc.py", "vault_format_owner_session.py",
        "vault_format_broker_observation.py",
        "vault_format_worker_ipc_client.py",
    ):
        assert "vault_format_broker_journal" not in (root / filename).read_text()


def test_broker_off_vs_pre_dispatch_serializes_with_sqlite_single_gate(broker):
    boot_ready(broker)
    start = threading.Barrier(2, timeout=5)

    def begin():
        start.wait()
        try:
            broker.fence_pre_dispatch(attempt())
            return "fenced"
        except BrokerJournalDenied as e:
            return str(e)

    def stop():
        start.wait()
        return broker.request_off()

    with ThreadPoolExecutor(max_workers=2) as pool:
        b = pool.submit(begin)
        s = pool.submit(stop)
        begin_result, stop_result = b.result(), s.result()
    assert begin_result in ("fenced", "broker_admission_closed")
    assert stop_result == "complete"  # pre-dispatch alone has no remote I/O.
    assert rows(broker, "SELECT state FROM broker_gate")[0][0] == "closed"
    assert rows(broker, "SELECT count(*) FROM broker_attempt")[0][0] == (
        1 if begin_result == "fenced" else 0
    )


def test_account_quarantine_is_scoped_to_exact_account(broker):
    boot_ready(broker)
    other_account = "e" * 64
    broker.provision_generation_metadata(other_account, OTHER)
    first = attempt()
    broker.fence_pre_dispatch(first)
    broker.mark_dispatch_possible(first)
    unrelated = replace(attempt(run="c", op="d"), account_binding_sha256=other_account,
                        credential_generation=OTHER,
                        target_path="98-System/independent.md")
    broker.fence_pre_dispatch(unrelated)
    assert broker.observe_finality(unrelated).disposition == "pending"
    broker.cancel_never_dispatched(unrelated)


def test_never_dispatched_receipt_rollback_on_forced_process_crash(broker, broker_dir):
    boot_ready(broker)
    a = attempt()
    broker.fence_pre_dispatch(a)
    code = """import os,sqlite3,sys
c=sqlite3.connect(sys.argv[1],isolation_level=None)
c.execute("BEGIN IMMEDIATE")
c.execute("UPDATE broker_attempt SET status='verified_no_effect' WHERE run_id=?",("a"*32,))
c.execute("DELETE FROM broker_resource")
c.execute("UPDATE broker_gate SET receipt_seq=1 WHERE id=1")
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", code, str(broker_dir / broker.FILE)],
                   check=True, capture_output=True, timeout=8)
    assert broker.observe_finality(a).disposition == "pending"
    assert rows(broker, "SELECT count(*) FROM broker_resource")[0][0] == 1
    assert rows(broker, "SELECT receipt_seq FROM broker_gate")[0][0] == 0


def test_no_effect_terminal_can_be_decided_under_closed_off_gate(broker):
    boot_ready(broker)
    a = attempt()
    broker.fence_pre_dispatch(a)
    assert broker.request_off() == "complete"
    receipt = broker.cancel_never_dispatched(a)
    assert receipt.disposition == "verified_no_effect"
    assert rows(broker, "SELECT count(*) FROM broker_resource")[0][0] == 0
    with pytest.raises(BrokerJournalDenied, match="^broker_dispatch_denied$"):
        broker.mark_dispatch_possible(a)


def test_same_token_different_db_open_inode_is_rejected(broker, broker_dir, tmp_path, monkeypatch):
    """Canonical path is restored before post-connect checks, but opened copy differs."""
    dbfile = broker_dir / broker.FILE
    original_inode = dbfile.stat().st_ino
    copied = tmp_path / "matching-token-different-inode.db"
    copied.write_bytes(dbfile.read_bytes())
    copied.chmod(0o600)
    saved = tmp_path / "saved-pinned-db"
    detached = tmp_path / "opened-alternate-db"
    real_connect = sqlite3.connect
    swapped = False

    def swap_open_restore(database, *args, **kwargs):
        nonlocal swapped
        if (not swapped and isinstance(database, str)
                and database.startswith("file:")
                and broker.FILE in database):
            swapped = True
            os.replace(dbfile, saved)
            os.replace(copied, dbfile)
            try:
                opened = real_connect(database, *args, **kwargs)
            finally:
                os.replace(dbfile, detached)
                os.replace(saved, dbfile)
            return opened
        return real_connect(database, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", swap_open_restore)
        with pytest.raises(BrokerJournalDenied, match="^broker_open_inode_mismatch$"):
            broker.observe_credential(ACCOUNT)
    assert swapped and dbfile.stat().st_ino == original_inode


def test_root_principal_not_allowed_to_construct_broker_state(monkeypatch, broker_dir):
    with monkeypatch.context() as scope:
        scope.setattr(os, "geteuid", lambda: 0)
        with pytest.raises(BrokerJournalDenied, match="^broker_root_disallowed$"):
            BrokerPrivateJournal(broker_dir)


def test_old_instance_cannot_mutate_fresh_broker_boot(broker, broker_dir):
    boot_ready(broker)
    later = BrokerPrivateJournal(broker_dir)
    later.start_boot()
    assert later.reconcile_boot() is True
    with pytest.raises(BrokerJournalDenied, match="^broker_boot_stale$"):
        broker.provision_generation_metadata("f" * 64, OTHER)
    with pytest.raises(BrokerJournalDenied, match="^broker_boot_stale$"):
        broker.fence_pre_dispatch(attempt())


def test_r1_pre_fenced_cross_target_dispatch_blocked_by_existing_account_quarantine(
    broker,
):
    """A pre-fenced second target cannot bypass an account quarantine."""
    boot_ready(broker)
    first = attempt(run="a", op="b", target="98-System/first.md")
    second = attempt(run="c", op="d", target="98-System/second.md")
    broker.fence_pre_dispatch(first)
    broker.fence_pre_dispatch(second)
    assert rows(broker, "SELECT count(*) FROM broker_account_quarantine")[0][0] == 0

    broker.mark_dispatch_possible(first)
    with pytest.raises(BrokerJournalDenied, match="^broker_account_unconfirmed$"):
        broker.mark_dispatch_possible(second)

    assert rows(
        broker, "SELECT target,status FROM broker_attempt ORDER BY target",
    ) == [
        ("98-System/first.md", "dispatch_possible"),
        ("98-System/second.md", "pre_dispatch"),
    ]
    assert rows(
        broker, "SELECT account,reason FROM broker_account_quarantine",
    ) == [(ACCOUNT, "dispatch_may_have_occurred")]
    assert rows(broker, "SELECT count(*) FROM broker_resource")[0][0] == 2

    # Existing pre-dispatch fences must remain cancellable without claiming
    # anything about the unresolved first operation's remote outcome.
    receipt = broker.cancel_never_dispatched(second)
    assert receipt.disposition == "verified_no_effect"
    assert broker.observe_finality(first).disposition == "pending"
    assert rows(broker, "SELECT count(*) FROM broker_resource")[0][0] == 1
    assert rows(broker, "SELECT count(*) FROM broker_account_quarantine")[0][0] == 1


def test_r1_concurrent_pre_fenced_cross_target_dispatch_has_exactly_one_winner(
    broker,
):
    """BEGIN IMMEDIATE must serialize the quarantine check and transition."""
    boot_ready(broker)
    first = attempt(run="a", op="b", target="98-System/race-first.md")
    second = attempt(run="c", op="d", target="98-System/race-second.md")
    broker.fence_pre_dispatch(first)
    broker.fence_pre_dispatch(second)
    barrier = threading.Barrier(2, timeout=5)

    def make_possible(item):
        barrier.wait()
        try:
            broker.mark_dispatch_possible(item)
            return "accepted"
        except BrokerJournalDenied as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(make_possible, first)
        b = pool.submit(make_possible, second)
        outcomes = [a.result(timeout=8), b.result(timeout=8)]
    assert sorted(outcomes) == ["accepted", "broker_account_unconfirmed"]

    attempts = rows(
        broker, "SELECT run_id,status FROM broker_attempt ORDER BY run_id",
    )
    assert sorted(status for _, status in attempts) == [
        "dispatch_possible", "pre_dispatch",
    ]
    assert rows(broker, "SELECT count(*) FROM broker_account_quarantine")[0][0] == 1
    assert rows(broker, "SELECT count(*) FROM broker_resource")[0][0] == 2
    remaining = first if attempts[0][1] == "pre_dispatch" else second
    assert broker.cancel_never_dispatched(remaining).disposition == "verified_no_effect"
    assert rows(broker, "SELECT count(*) FROM broker_account_quarantine")[0][0] == 1


def test_r1_cross_account_pre_fenced_dispatch_remains_independent(broker):
    """An account-wide quarantine must not globally freeze other accounts."""
    boot_ready(broker)
    other_account = "e" * 64
    broker.provision_generation_metadata(other_account, OTHER)
    first = attempt(run="a", op="b", target="98-System/account-one.md")
    second = replace(
        attempt(run="c", op="d", target="98-System/account-two.md"),
        account_binding_sha256=other_account,
        credential_generation=OTHER,
    )
    broker.fence_pre_dispatch(first)
    broker.fence_pre_dispatch(second)
    broker.mark_dispatch_possible(first)
    broker.mark_dispatch_possible(second)
    assert rows(
        broker, "SELECT account,status FROM broker_attempt ORDER BY account",
    ) == [(ACCOUNT, "dispatch_possible"), (other_account, "dispatch_possible")]
    assert rows(
        broker, "SELECT account FROM broker_account_quarantine ORDER BY account",
    ) == [(ACCOUNT,), (other_account,)]
