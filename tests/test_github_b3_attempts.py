"""Single-LXC B3 producer-owned *attempt* ledger: source-only synthetic tests.

These tests create only private temporary SQLite files. They never execute
a Project Sync, Writer, systemd job, Vault mutation or provider request.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import stat

import pytest

from obsidian_automation.github_b3_attempts import (
    B3AttemptJournal,
    Busy,
    InvalidAttempt,
    VERSION,
)

REQUEST = "1" * 32
BINDING = "2" * 32
REV1 = "3" * 32
REV2 = "4" * 32
NEXT = "5" * 32
THIRD = "6" * 32


class Clock:
    def __init__(self, now=1000.0):
        self.value = now

    def __call__(self):
        return self.value

    def advance(self, seconds=1.0):
        self.value += seconds


def _journal(tmp_path: Path, *, clock=None, initialize=None) -> B3AttemptJournal:
    root = tmp_path / "private-b3"
    if not root.exists() and not root.is_symlink():
        root.mkdir(mode=0o700)
    if initialize is None:
        initialize = not (root / "b3-attempts.initialized").exists()
    return B3AttemptJournal(root, clock=clock or Clock(), initialize=initialize)


def test_admission_start_and_known_cas_refusal_are_durable(tmp_path):
    clock = Clock()
    with _journal(tmp_path, clock=clock) as j:
        decision, initial = j.admit(REQUEST, BINDING, REV1)
        assert decision == "accepted"
        assert initial["state"] == "queued"
        assert initial["created_at"] == 1000.0
        run = initial["run_id"]
        assert len(run) == 32
        assert j.lookup(run) == initial
        assert j.start(run)["state"] == "running"
        clock.advance()
        final = j.finish(run, terminal_code="cas_conflict")
        assert final["terminal_code"] == "cas_conflict"
        assert final["state"] == "completed"
        assert final["completed_at"] == 1001.0
        assert j.finish(run, terminal_code="cas_conflict") == final
        snapshot = j.snapshot()
        assert snapshot["schema_version"] == VERSION
        assert snapshot["integration"] == "unattached"
        assert snapshot["last_attempt"]["terminal_code"] == "cas_conflict"
        assert snapshot["last_deterministic_refusal"]["terminal_code"] == "cas_conflict"
        assert snapshot["last_verified_pipeline_success"] is None
        assert snapshot["active_incident"] == "unknown"
        assert snapshot["overall_health"] == "unknown"
    with _journal(tmp_path, clock=clock) as j:
        assert j.lookup(run) == final
        assert j.snapshot()["last_deterministic_refusal"]["run_id"] == run


def test_explicit_manual_retry_requires_new_cas_revision(tmp_path):
    clock = Clock()
    with _journal(tmp_path, clock=clock) as j:
        _, first = j.admit(REQUEST, BINDING, REV1)
        j.start(first["run_id"])
        j.finish(first["run_id"], terminal_code="cas_conflict")
        with pytest.raises(InvalidAttempt, match="stale_cas_retry"):
            j.admit(NEXT, BINDING, REV1, retry_of=first["run_id"])
        with pytest.raises(InvalidAttempt, match="retry_parent_missing"):
            j.admit(NEXT, BINDING, REV2, retry_of="9" * 32)
        clock.advance()
        decision, second = j.admit(
            NEXT, BINDING, REV2, retry_of=first["run_id"],
        )
        assert decision == "accepted"
        assert second["retry_of"] == first["run_id"]
        assert j.snapshot()["retrying"] == "recorded"
        assert j.start(second["run_id"])["state"] == "running"
        assert j.snapshot()["retrying"] == "recorded"
        with pytest.raises(Busy, match="b3_attempt_in_progress"):
            j.admit(THIRD, BINDING, REV2)
        j.finish(second["run_id"], terminal_code="authority_rejected")
        assert j.snapshot()["retrying"] == "unknown"
        with pytest.raises(InvalidAttempt, match="duplicate_retry_child"):
            j.admit(THIRD, BINDING, "7" * 32, retry_of=first["run_id"])
        decision, third = j.admit(
            THIRD, BINDING, REV2, retry_of=second["run_id"],
        )
        assert decision == "accepted"
        assert third["retry_of"] == second["run_id"]


def test_request_replay_is_idempotent_and_does_not_duplicate_work(tmp_path):
    with _journal(tmp_path) as j:
        first_decision, first = j.admit(REQUEST, BINDING, REV1)
        second_decision, second = j.admit(REQUEST, BINDING, REV1)
        assert (first_decision, second_decision) == ("accepted", "replayed")
        assert first == second
        assert len(j.snapshot()["recent_attempts"]) == 1
        for args in ((REQUEST, BINDING, REV2), (REQUEST, NEXT, REV1)):
            with pytest.raises(InvalidAttempt, match="request_identity_conflict"):
                j.admit(*args)
        with pytest.raises(InvalidAttempt, match="invalid_transition"):
            j.finish(first["run_id"], terminal_code="cas_conflict")


def test_restart_abandons_only_queued_no_effect_without_replaying(tmp_path):
    clock = Clock()
    with _journal(tmp_path, clock=clock) as j:
        _, first = j.admit(REQUEST, BINDING, REV1)
    clock.advance(10)
    with _journal(tmp_path, clock=clock) as j:
        recovered = j.lookup(first["run_id"])
        assert recovered["state"] == "completed"
        assert recovered["terminal_code"] == "abandoned_before_start"
        assert recovered["completed_at"] == 1010.0
        outcome, duplicate = j.admit(REQUEST, BINDING, REV1)
        assert outcome == "replayed" and duplicate == recovered
        assert j.snapshot()["last_unconfirmed"] is None
        decision, retry = j.admit(NEXT, BINDING, REV1, retry_of=first["run_id"])
        assert decision == "accepted"
        assert retry["retry_of"] == first["run_id"]
        assert j.snapshot()["retrying"] == "recorded"


def test_restart_never_reexecutes_a_running_effect(tmp_path):
    with _journal(tmp_path) as j:
        _, first = j.admit(REQUEST, BINDING, REV1)
        j.start(first["run_id"])
    with _journal(tmp_path) as j:
        assert j.lookup(first["run_id"])["terminal_code"] == "interrupted_unknown"
        assert j.snapshot()["last_verified_pipeline_success"] is None


@pytest.mark.parametrize("code", (
    "http_rejected", "source_finished_unverified", "runner_failed", "effect_unknown",
))
def test_uncertain_remote_effect_is_held_against_blind_retry(tmp_path, code):
    with _journal(tmp_path) as j:
        _, first = j.admit(REQUEST, BINDING, REV1)
        j.start(first["run_id"])
        uncertain = j.finish(first["run_id"], terminal_code=code)
        assert uncertain["state"] == "unconfirmed"
        assert j.snapshot()["last_verified_pipeline_success"] is None
        with pytest.raises(Busy, match="unconfirmed_effect"):
            j.admit(NEXT, BINDING, REV2)
        with pytest.raises(Busy, match="unconfirmed_effect"):
            j.admit(NEXT, BINDING, REV2, retry_of=first["run_id"])
        with pytest.raises(InvalidAttempt, match="terminal_conflict"):
            j.finish(first["run_id"], terminal_code="cas_conflict")


@pytest.mark.parametrize("code", (
    "preflight_rejected", "cas_conflict",
    "authority_rejected",
))
def test_deterministic_refusals_are_not_verified_pipeline_success(tmp_path, code):
    with _journal(tmp_path) as j:
        _, entry = j.admit(REQUEST, BINDING, REV1)
        j.start(entry["run_id"])
        result = j.finish(entry["run_id"], terminal_code=code)
        assert result["state"] == "completed"
        assert j.snapshot()["last_deterministic_refusal"] == result
        assert j.snapshot()["last_verified_pipeline_success"] is None
        assert j.snapshot()["overall_health"] == "unknown"


def test_last_refusal_survives_later_unknown_and_is_not_recovery(tmp_path):
    with _journal(tmp_path) as j:
        _, first = j.admit(REQUEST, BINDING, REV1)
        j.start(first["run_id"])
        j.finish(first["run_id"], terminal_code="authority_rejected")
        _, next_run = j.admit(NEXT, BINDING, REV2)
        j.start(next_run["run_id"])
        j.finish(next_run["run_id"], terminal_code="source_finished_unverified")
        snapshot = j.snapshot()
        assert snapshot["last_attempt"]["run_id"] == next_run["run_id"]
        assert snapshot["last_deterministic_refusal"]["run_id"] == first["run_id"]
        assert snapshot["last_unconfirmed"]["run_id"] == next_run["run_id"]
        assert snapshot["last_verified_pipeline_success"] is None


def test_unknown_or_unstarted_transition_fails_closed(tmp_path):
    with _journal(tmp_path) as j:
        with pytest.raises(InvalidAttempt, match="unknown_run"):
            j.start("a" * 32)
        with pytest.raises(InvalidAttempt, match="unknown_run"):
            j.finish("a" * 32, terminal_code="cas_conflict")
        _, first = j.admit(REQUEST, BINDING, REV1)
        with pytest.raises(InvalidAttempt, match="invalid_transition"):
            j.finish(first["run_id"], terminal_code="cas_conflict")
        j.start(first["run_id"])
        j.finish(first["run_id"], terminal_code="cas_conflict")
        with pytest.raises(InvalidAttempt, match="terminal_conflict"):
            j.finish(first["run_id"], terminal_code="authority_rejected")
        with pytest.raises(InvalidAttempt, match="invalid_transition"):
            j.start(first["run_id"])


def test_refuse_arbitrary_terminal_codes_and_request_input(tmp_path):
    with _journal(tmp_path) as j:
        with pytest.raises(InvalidAttempt, match="invalid_request_identity"):
            j.admit("INVALID", BINDING, REV1)
        with pytest.raises(InvalidAttempt, match="invalid_request_identity"):
            j.admit(REQUEST, "bad", REV1)
        with pytest.raises(InvalidAttempt, match="invalid_request_identity"):
            j.admit(REQUEST, BINDING, True)
        with pytest.raises(InvalidAttempt, match="invalid_trigger"):
            j.admit(REQUEST, BINDING, REV1, trigger="arbitrary_command")
        with pytest.raises(InvalidAttempt, match="invalid_retry_reference"):
            j.admit(REQUEST, BINDING, REV1, retry_of="/tmp/secret")
        _, first = j.admit(REQUEST, BINDING, REV1)
        j.start(first["run_id"])
        for code in ("verified_success", "HTTP403", "raw exception", "interrupted_unknown", True):
            with pytest.raises(InvalidAttempt, match="invalid_terminal_code"):
                j.finish(first["run_id"], terminal_code=code)
        assert j.lookup(first["run_id"])["state"] == "running"
        for limit in (0, True, 65, "unbounded"):
            with pytest.raises(InvalidAttempt, match="invalid_limit"):
                j.snapshot(recent=limit)


def test_busy_one_active_run_even_different_scope(tmp_path):
    with _journal(tmp_path) as j:
        _, first = j.admit(REQUEST, BINDING, REV1)
        with pytest.raises(Busy, match="b3_attempt_in_progress"):
            j.admit(NEXT, "a" * 32, REV2)
        j.start(first["run_id"])
        with pytest.raises(Busy, match="b3_attempt_in_progress"):
            j.admit(NEXT, "a" * 32, REV2)


def test_cannot_retry_different_binding(tmp_path):
    with _journal(tmp_path) as j:
        _, first = j.admit(REQUEST, BINDING, REV1)
        j.start(first["run_id"])
        j.finish(first["run_id"], terminal_code="preflight_rejected")
        with pytest.raises(InvalidAttempt, match="unsafe_retry_parent"):
            j.admit(NEXT, "7" * 32, REV1, retry_of=first["run_id"])


def test_private_state_files_and_lock_contention(tmp_path):
    root = tmp_path / "private-b3"
    with _journal(tmp_path) as j:
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        for name in ("b3-attempts.sqlite3", "b3-attempts.lock",
                     "b3-attempts.initialized"):
            assert stat.S_IMODE((root / name).stat().st_mode) == 0o600
        with pytest.raises(Busy, match="b3_owner_busy"):
            _journal(tmp_path)
    with _journal(tmp_path) as j:
        assert j.snapshot()["last_attempt"] is None


def test_reject_public_state_dir_and_symlink(tmp_path):
    root = tmp_path / "private-b3"
    root.mkdir(mode=0o755)
    with pytest.raises(InvalidAttempt, match="invalid_private_state"):
        _journal(tmp_path)
    root.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(InvalidAttempt, match="invalid_private_state"):
        _journal(tmp_path)


def test_reject_public_db_file_and_broken_store_version(tmp_path):
    with _journal(tmp_path):
        pass
    root = tmp_path / "private-b3"
    (root / "b3-attempts.sqlite3").chmod(0o644)
    with pytest.raises(InvalidAttempt, match="invalid_private_state"):
        _journal(tmp_path)


def test_clock_regression_never_advances_state(tmp_path):
    clock = Clock()
    with _journal(tmp_path, clock=clock) as j:
        _, first = j.admit(REQUEST, BINDING, REV1)
        clock.value = 999
        with pytest.raises(InvalidAttempt, match="clock_regression"):
            j.start(first["run_id"])
        assert j.lookup(first["run_id"])["state"] == "queued"
        clock.value = 1000
        j.start(first["run_id"])
        clock.value = 999
        with pytest.raises(InvalidAttempt, match="clock_regression"):
            j.finish(first["run_id"], terminal_code="cas_conflict")
        assert j.lookup(first["run_id"])["state"] == "running"


def test_bad_clock_values_and_closed_owner(tmp_path):
    clock = Clock()
    with pytest.raises(InvalidAttempt, match="invalid_private_state"):
        B3AttemptJournal(Path("relative/state"), clock=clock)
    with _journal(tmp_path, clock=clock) as j:
        clock.value = float("nan")
        with pytest.raises(InvalidAttempt, match="invalid_time"):
            j.admit(REQUEST, BINDING, REV1)
        assert j.snapshot()["last_attempt"] is None
    with pytest.raises(InvalidAttempt, match="journal_closed"):
        j.snapshot()


def test_no_sensitive_binding_or_revision_in_projection(tmp_path):
    with _journal(tmp_path) as j:
        j.admit(REQUEST, BINDING, REV1)
        text = json.dumps(j.snapshot())
        assert BINDING not in text
        assert REV1 not in text
        assert REQUEST not in text
        assert "path" not in text
        assert "token" not in text
        assert "last_verified_pipeline_success" in text
        assert j.snapshot()["integration"] == "unattached"


def test_recent_history_is_bounded_and_keeps_last_rejection(tmp_path):
    with _journal(tmp_path) as j:
        run_ids = []
        for i in range(5):
            req = f"{i+1:032x}"
            _, run = j.admit(req, BINDING, REV1)
            j.start(run["run_id"])
            j.finish(run["run_id"], terminal_code="preflight_rejected")
            run_ids.append(run["run_id"])
        snapshot = j.snapshot(recent=2)
        assert len(snapshot["recent_attempts"]) == 2
        assert snapshot["last_attempt"]["run_id"] == run_ids[-1]
        assert snapshot["last_deterministic_refusal"]["run_id"] == run_ids[-1]


def test_concurrent_duplicate_admissions_receive_same_run(tmp_path):
    with _journal(tmp_path) as j:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            pairs = list(pool.map(
                lambda _: j.admit(REQUEST, BINDING, REV1), range(8),
            ))
        assert sum(decision == "accepted" for decision, _ in pairs) == 1
        assert sum(decision == "replayed" for decision, _ in pairs) == 7
        assert len({entry["run_id"] for _, entry in pairs}) == 1



def test_missing_persisted_db_cannot_silently_reset_idempotency(tmp_path):
    with _journal(tmp_path) as j:
        _, first = j.admit(REQUEST, BINDING, REV1)
    root = tmp_path / "private-b3"
    (root / "b3-attempts.sqlite3").unlink()
    for initialize in (False, True):
        with pytest.raises(InvalidAttempt, match="store_missing_or_incomplete"):
            B3AttemptJournal(root, initialize=initialize)
    assert (root / "b3-attempts.initialized").exists()
    assert not (root / "b3-attempts.sqlite3").exists()


def test_missing_marker_with_existing_db_is_not_reinitialized(tmp_path):
    with _journal(tmp_path) as j:
        _, first = j.admit(REQUEST, BINDING, REV1)
    root = tmp_path / "private-b3"
    (root / "b3-attempts.initialized").unlink()
    for initialize in (False, True):
        with pytest.raises(InvalidAttempt, match="store_missing_or_incomplete"):
            B3AttemptJournal(root, initialize=initialize)


def test_initialization_requires_explicit_one_time_contract(tmp_path):
    root = tmp_path / "empty-b3"
    root.mkdir(mode=0o700)
    with pytest.raises(InvalidAttempt, match="store_not_initialized"):
        B3AttemptJournal(root)
    assert not (root / "b3-attempts.sqlite3").exists()
    with B3AttemptJournal(root, initialize=True) as j:
        assert j.snapshot()["integration"] == "unattached"
    with B3AttemptJournal(root, initialize=False) as j:
        assert j.snapshot()["last_attempt"] is None


def test_ancestor_directory_symlink_is_not_traversed(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    root = actual / "private-b3"
    root.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(InvalidAttempt, match="invalid_private_state"):
        B3AttemptJournal(alias / "private-b3", initialize=True)
    assert not (root / "b3-attempts.sqlite3").exists()


def test_db_open_anchored_to_original_directory_fd_after_rename(tmp_path, monkeypatch):
    from obsidian_automation import github_b3_attempts as module

    root = tmp_path / "private-b3"
    root.mkdir(mode=0o700)
    renamed = tmp_path / "old-private-b3"
    original = module._private_file
    moved = False

    def rename_during_init(fd, name, *, create=False):
        nonlocal moved
        if name == "b3-attempts.lock" and not moved:
            root.rename(renamed)
            root.mkdir(mode=0o700)
            moved = True
        return original(fd, name, create=create)

    monkeypatch.setattr(module, "_private_file", rename_during_init)
    with B3AttemptJournal(root, initialize=True) as j:
        result, item = j.admit(REQUEST, BINDING, REV1)
        assert result == "accepted"
        assert item["state"] == "queued"
    assert moved
    assert (renamed / "b3-attempts.sqlite3").exists()
    assert (renamed / "b3-attempts.initialized").exists()
    assert not (root / "b3-attempts.sqlite3").exists()


def test_request_id_is_private_even_with_same_run_replay(tmp_path):
    with _journal(tmp_path) as j:
        decision, first = j.admit(REQUEST, BINDING, REV1)
        decision2, second = j.admit(REQUEST, BINDING, REV1)
        assert decision == "accepted" and decision2 == "replayed"
        assert first == second
        assert "request_id" not in first
        assert REQUEST not in json.dumps(j.snapshot(), sort_keys=True)
        assert REQUEST not in json.dumps(j.lookup(first["run_id"]), sort_keys=True)


def test_http_rejection_has_ambiguous_effect_and_never_auto_retries(tmp_path):
    with _journal(tmp_path) as j:
        _, first = j.admit(REQUEST, BINDING, REV1)
        j.start(first["run_id"])
        output = j.finish(first["run_id"], terminal_code="http_rejected")
        assert output["state"] == "unconfirmed"
        assert j.snapshot()["last_deterministic_refusal"] is None
        assert j.snapshot()["last_verified_pipeline_success"] is None
        with pytest.raises(Busy, match="unconfirmed_effect"):
            j.admit(NEXT, BINDING, REV2, retry_of=first["run_id"])



def test_existing_db_without_runs_table_cannot_reset_prior_admissions(tmp_path):
    import sqlite3

    with _journal(tmp_path) as j:
        _, first = j.admit(REQUEST, BINDING, REV1)
    db_path = tmp_path / "private-b3" / "b3-attempts.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE runs")
    # A version-1 file plus a valid one-time marker is NOT a fresh store.
    for initialize in (False, True):
        with pytest.raises(InvalidAttempt, match="missing_runs_table"):
            _journal(tmp_path, initialize=initialize)



def test_existing_store_without_unique_constraints_is_not_accepted(tmp_path):
    import sqlite3

    with _journal(tmp_path) as j:
        _, first = j.admit(REQUEST, BINDING, REV1)
    path = tmp_path / "private-b3" / "b3-attempts.sqlite3"
    with sqlite3.connect(path) as db:
        old = db.execute("SELECT * FROM runs").fetchall()
        schema = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='runs'"
        ).fetchone()[0]
        assert schema.count(" UNIQUE") >= 3
        db.execute("DROP TABLE runs")
        db.execute(schema.replace(" UNIQUE", ""))
        for row in old:
            db.execute(
                "INSERT INTO runs VALUES (" + ",".join("?" for _ in row) + ")",
                row,
            )
    with pytest.raises(InvalidAttempt, match="invalid_runs_constraints"):
        _journal(tmp_path, initialize=False)



def test_existing_store_without_required_not_null_is_rejected(tmp_path):
    import sqlite3

    with _journal(tmp_path) as j:
        _, entry = j.admit(REQUEST, BINDING, REV1)
    path = tmp_path / "private-b3" / "b3-attempts.sqlite3"
    with sqlite3.connect(path) as db:
        old = db.execute("SELECT * FROM runs").fetchall()
        schema = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='runs'"
        ).fetchone()[0]
        assert "request_id TEXT UNIQUE NOT NULL" in schema
        db.execute("DROP TABLE runs")
        db.execute(schema.replace(
            "request_id TEXT UNIQUE NOT NULL", "request_id TEXT UNIQUE",
        ))
        for row in old:
            db.execute(
                "INSERT INTO runs VALUES (" + ",".join("?" for _ in row) + ")",
                row,
            )
    with pytest.raises(InvalidAttempt, match="invalid_runs_schema"):
        _journal(tmp_path, initialize=False)



def test_partial_unique_indexes_do_not_protect_request_idempotency(tmp_path):
    import sqlite3

    with _journal(tmp_path) as j:
        _, entry = j.admit(REQUEST, BINDING, REV1)
    path = tmp_path / "private-b3" / "b3-attempts.sqlite3"
    with sqlite3.connect(path) as db:
        saved = db.execute("SELECT * FROM runs").fetchall()
        schema = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='runs'"
        ).fetchone()[0]
        db.execute("DROP TABLE runs")
        db.execute(schema.replace(" UNIQUE", ""))
        for column in ("run_id", "request_id", "retry_of"):
            db.execute(
                f"CREATE UNIQUE INDEX partial_{column} ON runs ({column}) WHERE 0"
            )
        for row in saved:
            db.execute(
                "INSERT INTO runs VALUES (" + ",".join("?" for _ in row) + ")",
                row,
            )
    with pytest.raises(InvalidAttempt, match="invalid_runs_constraints"):
        _journal(tmp_path, initialize=False)



def test_invalid_clock_before_first_bootstrap_leaves_no_partial_db(tmp_path):
    root = tmp_path / "private-b3"
    root.mkdir(mode=0o700)
    with pytest.raises(InvalidAttempt, match="invalid_time"):
        B3AttemptJournal(root, clock=Clock(float("nan")), initialize=True)
    assert not (root / "b3-attempts.sqlite3").exists()
    assert not (root / "b3-attempts.initialized").exists()
    with B3AttemptJournal(root, clock=Clock(), initialize=True) as journal:
        assert journal.snapshot()["last_attempt"] is None
