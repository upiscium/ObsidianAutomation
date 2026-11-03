"""Source-only real SQLite B3 read-port -> strict #480 read-only DTO tests.

All state is disposable and private. No actual WebDAV, Vault, Provider,
systemd, scheduler or WebUI route is accessed; no Writer is launched here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.github_b3_attempts import B3AttemptJournal, Busy
from obsidian_automation.webui_b3_journal_read import B3JournalReadAdapter
from obsidian_automation.webui_control_get import read_control_catalog
from obsidian_automation.webui_optional_read import validate_optional_read_ports

REQUEST = "1" * 32
BINDING = "2" * 32
REVISION = "3" * 32
NEXT = "4" * 32
HASHES = {
    "status_proposal_sha256": "a" * 64,
    "status_after_sha256": "b" * 64,
    "overview_proposal_sha256": "c" * 64,
    "overview_after_sha256": "d" * 64,
}


class Clock:
    def __init__(self, now=1000.0):
        self.at = now

    def __call__(self):
        return self.at

    def advance(self, offset=1.0):
        self.at += offset


@pytest.fixture
def case(tmp_path: Path):
    state = tmp_path / "private"
    state.mkdir(mode=0o700)
    clock = Clock()
    with B3AttemptJournal(state, clock=clock, initialize=True) as journal:
        yield state, clock, journal


def catalog(port, clock):
    return read_control_catalog(
        snapshot_job=None, daily_job=None,
        optional_snapshots={"b3": port.observe()},
        observed_at=clock.at + 5,
    )["workflows"][2]


def test_absent_reader_is_inert_and_not_attached():
    port = B3JournalReadAdapter(None, clock=lambda: 1000.0)
    dto = port.observe()
    assert dto["integration"] == "not_attached"
    assert dto["adapterMode"] == "read_only"
    assert dto["revision"] == "0" * 32
    assert dto["availableActions"] == []
    assert dto["lastAttempt"] is None
    assert dto["lastVerifiedOutcome"] is None
    assert dto["actionReasons"] == {
        "sync_project": "not_attached",
        "set_schedule": "not_attached",
    }
    assert port.detail()["pipelineSuccess"] is None
    assert "not_attached" in str(port.detail())


def test_open_empty_journal_is_pending_and_idle_but_never_runnable(case):
    _, clock, journal = case
    port = B3JournalReadAdapter(journal, clock=clock)
    dto = port.observe()
    assert dto["integration"] == "pending"
    assert dto["execution"]["state"] == "idle"
    assert dto["configuration"] == {"state": "not_observed", "reason": "pending_integration"}
    assert dto["schedule"]["state"] == "unknown"
    assert dto["actionReasons"] == {
        "sync_project": "read_only",
        "set_schedule": "scheduler_decision_required",
    }
    assert dto["availableActions"] == []
    assert port.detail()["writer"] == "not_observed"
    assert port.detail()["unconfirmed"] is False
    composed = catalog(port, clock)
    assert composed["id"] == "b3"
    assert composed["integration"] == "pending"
    assert composed["availableActions"] == []
    assert composed["lastVerifiedOutcome"] is None


def test_durable_queued_running_and_unconfirmed_project_into_unknown_effect(case):
    _, clock, journal = case
    port = B3JournalReadAdapter(journal, clock=clock)
    _, original = journal.admit(REQUEST, BINDING, REVISION)
    queued = port.observe()
    assert queued["execution"] == {"state": "queued", "requestId": REQUEST, "startedAt": None}
    assert queued["lastAttempt"]["requestId"] == REQUEST
    assert queued["lastAttempt"]["outcome"] is None
    assert queued["lastAttempt"]["effect"] == {
        "kind": "project_vault", "state": "unknown", "changed": None
    }
    assert catalog(port, clock)["execution"]["state"] == "queued"
    clock.advance()
    journal.start(original["run_id"])
    running = port.observe()
    assert running["execution"]["state"] == "running"
    assert running["execution"]["startedAt"] == clock.at
    assert catalog(port, clock)["execution"]["state"] == "running"
    clock.advance()
    journal.finish(original["run_id"], terminal_code="effect_unknown")
    unknown = port.observe()
    assert unknown["execution"] == {"state": "unknown", "requestId": None, "startedAt": None}
    assert unknown["lastAttempt"]["state"] == "completed"
    assert unknown["lastAttempt"]["outcome"] == "unknown"
    assert unknown["lastAttempt"]["completedAt"] == clock.at
    assert unknown["lastVerifiedOutcome"] is None
    assert port.detail()["unconfirmed"] is True
    assert catalog(port, clock)["lastAttempt"]["outcome"] == "unknown"
    with pytest.raises(Busy, match="unconfirmed_effect"):
        journal.admit(NEXT, BINDING, REVISION)


def test_verified_writer_pair_still_not_full_pipeline_or_current_vault(case):
    _, clock, journal = case
    port = B3JournalReadAdapter(journal, clock=clock)
    _, run = journal.admit(REQUEST, BINDING, REVISION)
    journal.start(run["run_id"])
    clock.advance()
    journal.record_writer_readback(
        run["run_id"], **HASHES, observed_at=clock.at,
    )
    dto = port.observe()
    detail = port.detail()
    assert dto["lastAttempt"]["state"] == "completed"
    assert dto["lastAttempt"]["outcome"] == "unknown"
    assert dto["lastAttempt"]["effect"]["state"] == "unknown"
    assert dto["lastVerifiedOutcome"] is None
    assert dto["availableActions"] == []
    assert dto["execution"]["state"] == "unknown"
    assert detail["writer"] == "local_pair_observed"
    assert detail["lastWriterPairAt"] == clock.at
    assert detail["pipelineSuccess"] is None
    assert catalog(port, clock)["lastVerifiedOutcome"] is None
    serialized = json.dumps({"workflow": dto, "detail": detail}, sort_keys=True)
    for private in (run["run_id"], BINDING, REVISION, "a" * 64, "b" * 64,
                    "c" * 64, "d" * 64, str(journal._dir_fd)):
        if private == str(journal._dir_fd):
            continue
        assert private not in serialized
    assert REQUEST in serialized


def test_later_failure_preserves_historical_writer_pair_but_not_success(case):
    _, clock, journal = case
    port = B3JournalReadAdapter(journal, clock=clock)
    _, run = journal.admit(REQUEST, BINDING, REVISION)
    journal.start(run["run_id"])
    journal.record_writer_readback(
        run["run_id"], **HASHES, observed_at=clock.at,
    )
    clock.advance()
    _, next_run = journal.admit(NEXT, BINDING, REVISION)
    journal.start(next_run["run_id"])
    journal.finish(next_run["run_id"], terminal_code="cas_conflict")
    latest = port.observe()
    assert latest["lastAttempt"]["requestId"] == NEXT
    assert latest["lastAttempt"]["outcome"] == "unknown"
    assert latest["lastVerifiedOutcome"] is None
    assert port.detail()["writer"] == "local_pair_observed"
    assert port.detail()["lastWriterPairAt"] == 1000.0
    assert catalog(port, clock)["lastVerifiedOutcome"] is None


def test_restart_is_durable_but_read_never_replays_an_effect(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    clock = Clock()
    with B3AttemptJournal(root, clock=clock, initialize=True) as owner:
        _, run = owner.admit(REQUEST, BINDING, REVISION)
        owner.start(run["run_id"])
        clock.advance()
        owner.record_writer_readback(
            run["run_id"], **HASHES, observed_at=clock.at,
        )
    clock.advance()
    with B3AttemptJournal(root, clock=clock, initialize=False) as reopened:
        port = B3JournalReadAdapter(reopened, clock=clock)
        sql = []
        reopened.db.set_trace_callback(sql.append)
        a = port.observe()
        b = port.detail()
        c = port.observe()
        assert a == c
        assert b["lastWriterPairAt"] == 1001.0
        assert all(stmt.lstrip().upper().startswith("SELECT ") for stmt in sql)
        assert b["writer"] == "local_pair_observed"
        assert a["availableActions"] == []


def test_interrupted_running_attempt_remains_unknown_after_restart(tmp_path):
    state = tmp_path / "private"
    state.mkdir(mode=0o700)
    clock = Clock()
    with B3AttemptJournal(state, clock=clock, initialize=True) as journal:
        _, run = journal.admit(REQUEST, BINDING, REVISION)
        journal.start(run["run_id"])
    clock.advance()
    with B3AttemptJournal(state, clock=clock, initialize=False) as journal:
        dto = B3JournalReadAdapter(journal, clock=clock).observe()
        assert dto["lastAttempt"]["requestId"] == REQUEST
        assert dto["lastAttempt"]["outcome"] == "unknown"
        assert dto["execution"]["state"] == "unknown"
        assert dto["lastVerifiedOutcome"] is None
        assert journal.snapshot()["last_unconfirmed"]["terminal_code"] == "interrupted_unknown"


def test_view_failures_reject_secret_errors_and_fail_closed(case, monkeypatch):
    _, clock, journal = case
    port = B3JournalReadAdapter(journal, clock=clock)
    def bad_observe():
        raise RuntimeError("PRIVATE_URL password=secret 10-Project/private")
    monkeypatch.setattr(journal, "workflow_read_observation", bad_observe)
    dto = port.observe()
    assert dto["integration"] == "unavailable"
    assert dto["lastAttempt"] is None
    assert dto["lastVerifiedOutcome"] is None
    assert dto["actionReasons"] == {
        "sync_project": "observation_unavailable",
        "set_schedule": "observation_unavailable",
    }
    assert port.detail()["unconfirmed"] is None
    assert "PRIVATE_URL" not in json.dumps(dto) + json.dumps(port.detail())
    assert catalog(port, clock)["integration"] == "unavailable"


@pytest.mark.parametrize("bad_clock", [
    lambda: float("nan"),
    lambda: float("inf"),
    lambda: -1,
    lambda: True,
    lambda: 10 ** 1000,
    lambda: (_ for _ in ()).throw(RuntimeError("private path")),
])
def test_invalid_clock_fails_closed(bad_clock, case):
    _, _, journal = case
    p = B3JournalReadAdapter(journal, clock=bad_clock)
    a = p.observe()
    assert a["integration"] == "unavailable"
    assert a["availableActions"] == []
    assert a["lastVerifiedOutcome"] is None
    assert "private" not in json.dumps(a)


def test_forged_or_missing_source_never_gets_action_rights(case):
    _, clock, journal = case
    with pytest.raises(TypeError):
        B3JournalReadAdapter(object())
    with pytest.raises(TypeError):
        B3JournalReadAdapter(journal, clock=None)
    p = B3JournalReadAdapter(journal, clock=clock)
    # The existing optional port registry explicitly rejects this real port
    # until a sole route integrator reviews and binds it.
    with pytest.raises(TypeError, match="invalid_workflow_control_read_ports"):
        validate_optional_read_ports({"b3": p})
    assert p.observe()["availableActions"] == []


def test_malformed_journal_projection_never_spoofs_composer(case, monkeypatch):
    _, clock, journal = case
    p = B3JournalReadAdapter(journal, clock=clock)
    for invalid in [
        {"schema_version": "legacy", "last_attempt": None,
         "last_writer_pair_at": None, "has_unconfirmed": False},
        {"schema_version": "github-b3-attempts/v2", "last_attempt": None,
         "last_writer_pair_at": None, "has_unconfirmed": "yes"},
        {"schema_version": "github-b3-attempts/v2", "last_attempt": {
            "request_id": object(), "state": "queued", "terminal_code": None,
            "created_at": 0, "started_at": None, "completed_at": None,
        }, "last_writer_pair_at": None, "has_unconfirmed": False},
    ]:
        monkeypatch.setattr(journal, "workflow_read_observation", lambda invalid=invalid: invalid)
        result = p.observe()
        assert result["integration"] == "unavailable"
        assert result["lastAttempt"] is None
        assert result["availableActions"] == []



def test_observation_time_sampled_after_durable_read_not_before(case, monkeypatch):
    _, clock, journal = case
    _, run = journal.admit(REQUEST, BINDING, REVISION)
    journal.start(run["run_id"])
    original = journal.workflow_read_observation

    def read_with_concurrent_clock_progress():
        clock.advance(1)
        return original()

    monkeypatch.setattr(journal, "workflow_read_observation", read_with_concurrent_clock_progress)
    dto = B3JournalReadAdapter(journal, clock=clock).observe()
    assert dto["integration"] == "pending"
    assert dto["observedAt"] == 1001.0
    assert dto["lastAttempt"]["requestId"] == REQUEST


def test_inconsistent_missing_run_with_unconfirmed_flag_is_unavailable(case, monkeypatch):
    _, clock, journal = case
    monkeypatch.setattr(
        journal, "workflow_read_observation",
        lambda: {
            "schema_version": "github-b3-attempts/v2",
            "last_attempt": None,
            "last_writer_pair_at": None,
            "has_unconfirmed": True,
        },
    )
    result = B3JournalReadAdapter(journal, clock=clock).observe()
    assert result["integration"] == "unavailable"
    assert result["lastVerifiedOutcome"] is None
    assert result["availableActions"] == []



def test_b3_history_lookups_use_one_index_even_with_many_irrelevant_runs(
    tmp_path: Path,
):
    """The query plan must never need a full runs-table scan for history."""
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    with B3AttemptJournal(root, clock=lambda: 10000.0, initialize=True) as journal:
        rows = [
            (
                f"{i + 1:032x}",
                f"{i + 6000:032x}",
                BINDING,
                REVISION,
                "manual",
                float(i),
                float(i),
                float(i),
            )
            for i in range(5000)
        ]
        with journal.db:
            journal.db.executemany(
                "INSERT INTO runs (run_id,request_id,binding_ref,"
                "expected_revision,trigger,created,started,completed,"
                "state,terminal_code) VALUES (?,?,?,?,?,?,?,?,'completed',"
                "'preflight_rejected')",
                rows,
            )
        observed = journal.workflow_read_observation()
        assert observed["last_attempt"]["request_id"] == f"{4999 + 6000:032x}"
        assert observed["last_writer_pair_at"] is None
        assert observed["has_unconfirmed"] is False
        queries = (
            "SELECT r.completed,p.observed FROM runs r "
            "JOIN writer_proofs p ON p.run_id=r.run_id "
            "WHERE r.state='completed' "
            "AND r.terminal_code='writer_readback_observed' "
            "ORDER BY r.seq DESC LIMIT 1",
            "SELECT 1 FROM runs WHERE state='unconfirmed' LIMIT 1",
        )
        for sql in queries:
            details = [
                row[3] for row in journal.db.execute(
                    "EXPLAIN QUERY PLAN " + sql
                )
            ]
            assert any(
                "runs_b3_state_terminal_seq" in item for item in details
            ), details


def test_existing_v2_store_gets_optional_history_index_on_reopen(tmp_path: Path):
    """No user_version bump, run ID reset, duplicate journal or new store."""
    import sqlite3

    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    clock = Clock()
    with B3AttemptJournal(root, clock=clock, initialize=True) as journal:
        _, run = journal.admit(REQUEST, BINDING, REVISION)
        journal.start(run["run_id"])
        journal.finish(run["run_id"], terminal_code="cas_conflict")
    database = root / "b3-attempts.sqlite3"
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        db.execute("DROP INDEX runs_b3_state_terminal_seq")
        assert not list(db.execute(
            "PRAGMA index_info(runs_b3_state_terminal_seq)"
        ))
    clock.advance()
    with B3AttemptJournal(root, clock=clock, initialize=False) as journal:
        assert journal.lookup(run["run_id"])["terminal_code"] == "cas_conflict"
        assert journal.workflow_read_observation()["last_attempt"]["request_id"] == REQUEST
        assert journal.db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert tuple(row[2] for row in journal.db.execute(
            "PRAGMA index_info(runs_b3_state_terminal_seq)"
        )) == ("state", "terminal_code", "seq")


def test_invalid_preexisting_history_index_does_not_silently_pass(tmp_path: Path):
    import sqlite3

    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    with B3AttemptJournal(root, clock=lambda: 1000.0, initialize=True):
        pass
    database = root / "b3-attempts.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("DROP INDEX runs_b3_state_terminal_seq")
        db.execute("CREATE INDEX runs_b3_state_terminal_seq ON runs(trigger)")
    with pytest.raises(Exception, match="invalid_runs_index"):
        B3AttemptJournal(root, clock=lambda: 1000.0, initialize=False)
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert tuple(row[2] for row in db.execute(
            "PRAGMA index_info(runs_b3_state_terminal_seq)"
        )) == ("trigger",)



def test_partial_index_with_right_columns_fails_closed(tmp_path: Path):
    """A partial lookalike cannot serve state='unconfirmed' lookups."""
    import sqlite3

    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    with B3AttemptJournal(root, clock=lambda: 1000.0, initialize=True):
        pass
    database = root / "b3-attempts.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("DROP INDEX runs_b3_state_terminal_seq")
        db.execute(
            "CREATE INDEX runs_b3_state_terminal_seq "
            "ON runs(state,terminal_code,seq DESC) WHERE state='completed'"
        )
    with pytest.raises(Exception, match="invalid_runs_index"):
        B3AttemptJournal(root, clock=lambda: 1000.0, initialize=False)
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        index = [row for row in db.execute("PRAGMA index_list(runs)")
                 if row[1] == "runs_b3_state_terminal_seq"]
        assert len(index) == 1 and index[0][4] == 1
