"""#472 D1/B0: disposable Owner-private journal SQL mechanics tests.

No service, Writer, credentials, provider requests, real authorization or
deployment. The mechanics require a separately trusted Owner and Broker.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
import threading

import pytest

from obsidian_automation.vault_format_effect_journal import (
    EffectAttempt, JournalDenied, OwnerEffectJournal,
)


@pytest.fixture
def state_dir(tmp_path):
    private = tmp_path / "owner-private"
    private.mkdir(mode=0o700)
    return private


@pytest.fixture
def journal(state_dir):
    # Explicit disposable provisioning, never implicit on Owner restart.
    return OwnerEffectJournal(state_dir, bootstrap=True)


def attempt(journal, *, run="a", op="b", target="98-System/notes.md"):
    st = journal.status()
    return EffectAttempt(
        run_id=run*32, operation_id=op*32, plan_sha256="1"*64,
        policy_sha256="2"*64, source_generation="3"*64,
        account_binding_sha256="4"*64, provider_proof_sha256="5"*64,
        receipt_root="/tmp/synthetic-receipt-root", target_path=target,
        precondition_etag='"synthetic-etag"',
        before_sha256="6"*64, after_sha256="7"*64,
        credential_generation=st["credential_generation"],
        claim_boot_epoch=st["boot_epoch"],
        claim_credential_epoch=st["credential_epoch"],
    )


def ready(journal):
    journal.owner_start_boot()
    assert journal.complete_boot_recovery() is True


def test_initial_gate_closed_and_handle_open_not_restart(journal, state_dir):
    assert journal.status()["state"] == "closed"
    assert journal.status()["boot_epoch"] == 0
    second = OwnerEffectJournal(state_dir)
    assert second.status()["boot_epoch"] == 0
    with pytest.raises(JournalDenied, match="^admission_closed$"):
        second.record_begin(attempt(second))
    ready(journal)
    assert second.status()["state"] == "open"
    assert OwnerEffectJournal(state_dir).status()["boot_epoch"] == 1


def test_fenced_attempt_nonreplay_and_cross_run_target_conflict(journal, state_dir):
    ready(journal)
    one = attempt(journal)
    journal.record_begin(one)
    with pytest.raises(JournalDenied, match="^attempt_already_fenced$"):
        OwnerEffectJournal(state_dir).record_begin(one)
    with pytest.raises(JournalDenied, match="^resource_unconfirmed$"):
        journal.record_begin(attempt(journal, run="c", op="d"))
    journal.record_begin(attempt(journal, run="e", op="f",
                                 target="98-System/other.md"))
    assert journal.status()["active"] == 2


def test_off_transaction_closes_before_an_effect_can_begin(journal, state_dir):
    ready(journal)
    one = attempt(journal)
    journal.record_begin(one)
    assert journal.request_off() == "pending_inflight"
    assert OwnerEffectJournal(state_dir).status()["reason"] == "off"
    with pytest.raises(JournalDenied, match="^admission_closed$"):
        journal.record_begin(attempt(journal, run="c", op="d"))
    journal.mark_uncertain(one)
    assert journal.request_off() == "pending_unconfirmed"
    journal.record_verified_outcome(one, disposition="verified_remote_effect")
    assert journal.request_off() == "complete"
    assert journal.status()["state"] == "closed"
    with pytest.raises(JournalDenied, match="^off_precedes_rotation$"):
        journal.request_rotation("a"*64)


def test_rotation_pending_and_no_early_generation_ack(journal):
    ready(journal)
    one = attempt(journal)
    journal.record_begin(one)
    assert journal.request_rotation("a"*64) == "pending_inflight"
    assert journal.status()["credential_epoch"] == 0
    journal.record_verified_outcome(one, disposition="verified_remote_effect")
    assert journal.request_rotation("a"*64) == "complete"
    s = journal.status()
    assert s["state"] == "closed"
    assert s["credential_generation"] == "a"*64
    assert s["credential_epoch"] == 1
    assert s["last_rotation_epoch"] == 0
    assert journal.complete_boot_recovery() is False


def test_boot_restart_fences_active_and_reconcile_without_rearming(journal, state_dir):
    ready(journal)
    old = attempt(journal)
    journal.record_begin(old)
    fresh_handle = OwnerEffectJournal(state_dir)
    next_epoch = fresh_handle.owner_start_boot()
    assert next_epoch == 2
    assert fresh_handle.status()["active"] == 0
    assert fresh_handle.complete_boot_recovery() is False
    fresh_handle.record_verified_outcome(old, disposition="verified_no_effect")
    assert fresh_handle.complete_boot_recovery() is True
    with pytest.raises(JournalDenied, match="^claim_stale_boot$"):
        fresh_handle.record_begin(replace(old, run_id="c"*32, operation_id="d"*32))
    with pytest.raises(JournalDenied, match="^claim_stale_boot$"):
        fresh_handle.record_begin(old)
    # An attacker cannot revive the spent ID by forging the current epoch.
    with pytest.raises(JournalDenied, match="^attempt_already_fenced$"):
        fresh_handle.record_begin(replace(old, claim_boot_epoch=next_epoch))
    fresh_handle.record_begin(attempt(fresh_handle, run="e", op="f"))


def test_terminal_release_and_attempt_disposition_atomic(journal, state_dir):
    ready(journal)
    one = attempt(journal)
    journal.record_begin(one)
    with pytest.raises(JournalDenied, match="^attempt_not_pending$"):
        journal.record_verified_outcome(replace(one, plan_sha256="a"*64),
                                        disposition="verified_remote_effect")
    with pytest.raises(JournalDenied, match="^disposition_invalid$"):
        journal.record_verified_outcome(one, disposition="unknown")
    assert journal.status()["outstanding"] > 0
    journal.record_verified_outcome(one, disposition="verified_remote_effect")
    assert OwnerEffectJournal(state_dir).status()["outstanding"] == 0
    with pytest.raises(JournalDenied, match="^attempt_not_pending$"):
        journal.mark_uncertain(one)
    with pytest.raises(JournalDenied, match="^attempt_not_pending$"):
        journal.record_verified_outcome(one, disposition="verified_no_effect")
    journal.record_begin(attempt(journal, run="c", op="d"))
    with pytest.raises(JournalDenied, match="^attempt_already_fenced$"):
        journal.record_begin(one)


def test_account_quarantine_preserved_independently_of_target(journal):
    ready(journal)
    one = attempt(journal)
    journal.record_begin(one)
    journal.quarantine_account(one.account_binding_sha256)
    with pytest.raises(JournalDenied, match="^account_unresolved$"):
        journal.release_account_after_verified_finality(one.account_binding_sha256)
    journal.record_verified_outcome(one, disposition="verified_remote_effect")
    assert journal.status()["outstanding"] == 1
    with pytest.raises(JournalDenied, match="^account_unconfirmed$"):
        journal.record_begin(attempt(journal, run="c", op="d",
                                     target="98-System/different.md"))
    journal.release_account_after_verified_finality(one.account_binding_sha256)
    assert journal.status()["outstanding"] == 0


@pytest.mark.parametrize("path", [
    "Private/notes.md", "98-System/../Other/a", "98-System//a",
    "98-System/./a", "98-System/a\\b", "98-System/a\nb", "98-System/",
])
def test_invalid_paths_never_reach_journal(journal, path):
    ready(journal)
    with pytest.raises(JournalDenied, match="^attempt_invalid$"):
        journal.record_begin(attempt(journal, target=path))
    assert journal.status()["active"] == 0


def test_stale_credential_epoch_and_boot_rejected(journal):
    ready(journal)
    one = attempt(journal)
    for field, value, reason in (
        ("claim_boot_epoch", 2, "claim_stale_boot"),
        ("claim_credential_epoch", 1, "claim_stale_generation"),
        ("credential_generation", "f"*64, "claim_stale_generation"),
    ):
        with pytest.raises(JournalDenied, match="^" + reason + "$"):
            journal.record_begin(replace(one, **{field: value}))
    assert journal.status()["active"] == 0


def test_symlink_directory_and_file_mode_fail_closed(state_dir, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(state_dir, target_is_directory=True)
    with pytest.raises(JournalDenied, match="^journal_directory_unsafe$"):
        OwnerEffectJournal(alias)
    journal = OwnerEffectJournal(state_dir, bootstrap=True)
    (state_dir / journal.FILE).chmod(0o666)
    with pytest.raises(JournalDenied, match="^journal_file_unsafe$"):
        journal.status()


def test_concurrent_begin_vs_off_has_only_two_serialized_outcomes(journal, state_dir):
    ready(journal)
    # A failed constructor must not strand the other thread indefinitely.
    barrier = threading.Barrier(2, timeout=5)
    one = attempt(journal)

    def begin():
        instance = OwnerEffectJournal(state_dir)
        barrier.wait()
        try:
            instance.record_begin(one)
            return "fenced"
        except JournalDenied as error:
            return str(error)

    def off():
        instance = OwnerEffectJournal(state_dir)
        barrier.wait()
        return instance.request_off()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_begin, f_off = pool.submit(begin), pool.submit(off)
        got_begin, got_off = f_begin.result(), f_off.result()
    assert got_begin in ("fenced", "admission_closed")
    assert got_off in ("complete", "pending_inflight")
    assert journal.status()["state"] == "closed"
    assert journal.status()["active"] == (1 if got_begin == "fenced" else 0)


def test_uncommitted_sql_tx_rolls_back_on_process_death(journal, state_dir):
    code = """import os, sqlite3, sys
db=sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute("BEGIN IMMEDIATE")
db.execute("UPDATE admission_gate SET state='open',boot_reconciled=1 WHERE id=1")
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", code, str(state_dir / journal.FILE)],
                   capture_output=True, timeout=10, check=True)
    assert OwnerEffectJournal(state_dir).status()["state"] == "closed"


def test_sql_busy_failure_is_fail_closed(journal, state_dir):
    ready(journal)
    conn = sqlite3.connect(state_dir / journal.FILE, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # SQLite may close its underlying fd when the other connection holds
        # a write lock; both errors are non-success/fail-closed.
        with pytest.raises(JournalDenied, match="^(journal_unavailable|journal_open_inode_unverifiable)$"):
            journal.record_begin(attempt(journal))
    finally:
        conn.execute("ROLLBACK")
        conn.close()
    assert journal.status()["active"] == 0


def test_unwired_from_snapshot_app():
    source = (Path(__file__).resolve().parents[1] /
              "src/obsidian_automation/snapshot_app.py").read_text()
    assert "vault_format_effect_journal" not in source


def test_missing_or_replaced_db_cannot_silently_rebootstrap(journal, state_dir, tmp_path):
    ready(journal)
    one = attempt(journal)
    journal.record_begin(one)
    assert journal.request_off() == "pending_inflight"
    dbfile = state_dir / journal.FILE
    dbfile.unlink()
    with pytest.raises(JournalDenied, match="^journal_file_missing$"):
        OwnerEffectJournal(state_dir)
    with pytest.raises(JournalDenied, match="^journal_already_provisioned$"):
        OwnerEffectJournal(state_dir, bootstrap=True)
    # An unrelated valid 0600 DB with a different Owner-private binding
    # cannot replace the lost file, even in a newly constructed handle.
    other_dir = tmp_path / "another-owner"
    other_dir.mkdir(mode=0o700)
    other = OwnerEffectJournal(other_dir, bootstrap=True)
    (other_dir / other.FILE).replace(dbfile)
    with pytest.raises(JournalDenied, match="^journal_identity_changed$"):
        OwnerEffectJournal(state_dir)


def test_existing_handle_detects_db_inode_replacement(journal, state_dir, tmp_path):
    ready(journal)
    original = state_dir / journal.FILE
    backup = tmp_path / "copy.db"
    backup.write_bytes(original.read_bytes())
    backup.chmod(0o600)
    backup.replace(original)  # Same bytes but a different inode.
    with pytest.raises(JournalDenied, match="^journal_identity_changed$"):
        journal.status()


def test_no_implicit_boot_or_reset_after_bootstrap(journal, state_dir):
    assert journal.complete_boot_recovery() is False
    with pytest.raises(JournalDenied, match="^journal_already_provisioned$"):
        OwnerEffectJournal(state_dir, bootstrap=True)
    assert journal.status()["boot_epoch"] == 0
    ready(journal)
    assert journal.status()["boot_epoch"] == 1


def test_duplicate_unreconciled_owner_boot_denied(journal):
    ready(journal)
    assert journal.owner_start_boot() == 2
    with pytest.raises(JournalDenied, match="^boot_already_pending$"):
        journal.owner_start_boot()
    assert journal.status()["boot_epoch"] == 2
    assert journal.complete_boot_recovery() is True


def test_pending_rotation_reconciles_boot_without_reopening_gate(journal):
    ready(journal)
    one = attempt(journal)
    journal.record_begin(one)
    assert journal.request_rotation("f"*64) == "pending_inflight"
    assert journal.owner_start_boot() == 2
    assert journal.status()["reason"] == "rotation"
    journal.record_verified_outcome(one, disposition="verified_remote_effect")
    assert journal.request_rotation("f"*64) == "pending_boot"
    assert journal.complete_boot_recovery() is False
    assert journal.status()["boot_reconciled"] is True
    assert journal.status()["state"] == "closed"
    assert journal.request_rotation("f"*64) == "complete"
    assert journal.status()["credential_epoch"] == 1
    assert journal.status()["state"] == "closed"


def test_restarted_off_remains_closed_after_boot_scan(journal):
    ready(journal)
    one = attempt(journal)
    journal.record_begin(one)
    assert journal.request_off() == "pending_inflight"
    journal.owner_start_boot()
    journal.record_verified_outcome(one, disposition="verified_no_effect")
    assert journal.complete_boot_recovery() is False
    assert journal.status()["boot_reconciled"] is True
    assert journal.status()["state"] == "closed"
    assert journal.status()["reason"] == "off"
    assert journal.request_off() == "complete"


def test_pathname_symlink_swapped_during_sql_connect_fail_closed(journal, state_dir, tmp_path, monkeypatch):
    ready(journal)
    original_connect = sqlite3.connect
    otherdir = tmp_path / "alt-private"
    otherdir.mkdir(mode=0o700)
    dbfile = state_dir / journal.FILE
    # Copy the ORIGINAL valid DB, including its journal identity: this is
    # deliberately NOT a different-token DB. The binding check can pass.
    copied_db = otherdir / "same-token-different-inode.sqlite3"
    copied_db.write_bytes(dbfile.read_bytes())
    copied_db.chmod(0o600)
    safe_backup = tmp_path / "kept-original.sqlite3"
    import os

    def swap_before_open(*args, **kwargs):
        # Simulate an adversarial same-UID rename between lstat and SQLite.
        os.replace(dbfile, safe_backup)
        dbfile.symlink_to(copied_db)
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", swap_before_open)
    # The same-token DB bypasses the SQL binding check, so the actual SQLite
    # FD inode comparison rejects the swapped-in symlink's different inode.
    with pytest.raises(JournalDenied, match="^journal_open_inode_mismatch$"):
        journal.status()
    monkeypatch.undo()
    dbfile.unlink()
    os.replace(safe_backup, dbfile)
    assert journal.status()["state"] == "open"


def test_r1_swap_back_same_token_wrong_sqlite_open_inode_never_ack(journal, state_dir, tmp_path, monkeypatch):
    """Canonical inode looks unchanged before/after, but SQLite opens a copy."""
    ready(journal)
    approved = attempt(journal)
    dbfile = state_dir / journal.FILE
    pinned_inode = dbfile.stat().st_ino
    matching_copy = tmp_path / "same-token-copy.sqlite3"
    matching_copy.write_bytes(dbfile.read_bytes())
    matching_copy.chmod(0o600)
    saved = tmp_path / "saved-canonical.sqlite3"
    detached = tmp_path / "sqlite-actually-opened.sqlite3"
    original_connect = sqlite3.connect
    swapped = False

    def swapped_open_restore(database, *args, **kwargs):
        nonlocal swapped
        if (not swapped and isinstance(database, str)
                and database.startswith("file:")
                and journal.FILE in database):
            swapped = True
            # A same-UID hostile file rename at the SQLite-open boundary.
            os.replace(dbfile, saved)
            os.replace(matching_copy, dbfile)
            try:
                opened = original_connect(database, *args, **kwargs)
            finally:
                os.replace(dbfile, detached)
                os.replace(saved, dbfile)
            assert dbfile.stat().st_ino == pinned_inode
            return opened
        return original_connect(database, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", swapped_open_restore)
        with pytest.raises(JournalDenied, match="^journal_open_inode_mismatch$"):
            journal.record_begin(approved)
    assert swapped
    assert dbfile.stat().st_ino == pinned_inode
    with original_connect(dbfile) as db:
        assert db.execute("SELECT count(*) FROM effect_attempt").fetchone()[0] == 0
    with original_connect(detached) as db:
        assert db.execute("SELECT count(*) FROM effect_attempt").fetchone()[0] == 0
    assert journal.status()["state"] == "open"


def test_r1_rename_after_verified_sqlite_connection_blocks_begin(journal, state_dir, tmp_path, monkeypatch):
    """A pinned sqlite fd is not sufficient if its canonical path vanishes."""
    ready(journal)
    approved = attempt(journal)
    dbfile = state_dir / journal.FILE
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(dbfile.read_bytes())
    replacement.chmod(0o600)
    saved = tmp_path / "opened-and-detached.sqlite3"
    actual_connect = OwnerEffectJournal._connect
    renamed = False

    def after_connected(self):
        nonlocal renamed
        db = actual_connect(self)
        if not renamed:
            renamed = True
            os.replace(dbfile, saved)
            os.replace(replacement, dbfile)
        return db

    try:
        with monkeypatch.context() as patch:
            patch.setattr(OwnerEffectJournal, "_connect", after_connected)
            with pytest.raises(JournalDenied, match="^journal_identity_changed$"):
                journal.record_begin(approved)
        assert renamed
        with sqlite3.connect(dbfile) as db:
            assert db.execute("SELECT count(*) FROM effect_attempt").fetchone()[0] == 0
        with sqlite3.connect(saved) as db:
            assert db.execute("SELECT count(*) FROM effect_attempt").fetchone()[0] == 0
    finally:
        dbfile.unlink()
        os.replace(saved, dbfile)
    assert journal.status()["state"] == "open"


def test_r1_procfd_unavailable_fails_closed(journal, monkeypatch):
    ready(journal)
    approved = attempt(journal)
    real_listdir = os.listdir

    def denied_procfs(path):
        if path == "/proc/self/fd":
            raise FileNotFoundError(path)
        return real_listdir(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "listdir", denied_procfs)
        with pytest.raises(JournalDenied, match="^journal_open_inode_unverifiable$"):
            journal.record_begin(approved)
    assert journal.status()["active"] == 0


def test_r1_known_sqlite_rollback_fd_does_not_hide_actual_main_fd(journal, state_dir, monkeypatch):
    """A competing rollback journal descriptor is not the main DB file."""
    ready(journal)
    real_connect = sqlite3.connect
    held_fds = []
    journal_path = state_dir / (journal.FILE + "-journal")
    injected = False

    def with_competing_rollback_fd(database, *args, **kwargs):
        nonlocal injected
        con = real_connect(database, *args, **kwargs)
        if (not injected and isinstance(database, str)
                and database.startswith("file:") and journal.FILE in database):
            injected = True
            held_fds.append(os.open(
                journal_path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600,
            ))
        return con

    try:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", with_competing_rollback_fd)
            assert journal.status()["state"] == "open"
        assert injected
    finally:
        for fd in held_fds:
            os.close(fd)
        journal_path.unlink(missing_ok=True)


def test_r1_unrelated_new_regular_fd_fails_closed_without_guessing(journal, state_dir, tmp_path, monkeypatch):
    """Never silently bind a random extra file descriptor to the SQLite DB."""
    ready(journal)
    before = journal.status()
    approved = attempt(journal)
    real_connect = sqlite3.connect
    other = tmp_path / "unrelated-opened-file"
    other.write_bytes(b"not a journal")
    held_fds = []
    injected = False

    def with_unrelated_regular_fd(database, *args, **kwargs):
        nonlocal injected
        con = real_connect(database, *args, **kwargs)
        if (not injected and isinstance(database, str)
                and database.startswith("file:") and journal.FILE in database):
            injected = True
            held_fds.append(os.open(other, os.O_RDONLY | os.O_NOFOLLOW))
        return con

    try:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", with_unrelated_regular_fd)
            with pytest.raises(JournalDenied, match="^journal_open_inode_unverifiable$"):
                journal.record_begin(approved)
    finally:
        for fd in held_fds:
            os.close(fd)
    assert injected
    assert journal.status()["active"] == before["active"] == 0
