"""Bounded offline SQLite fixture for the B1 fake-only ledger interface.

All stores and handles are synthetic. No runtime jobs, model modules, sockets,
Vaults, ZIPs, services, routes or credentials are imported or used.
SQLite provisioning here is explicit test setup, never observe/lookup/constructor.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import hashlib
import json
import sqlite3
import threading

import pytest

from obsidian_automation.webui_b1_adapter import (
    Admission, Attempt, B1FakeAdapter, B1ReadAdapter, FakeReviewProvider,
    InvalidRequest, LedgerState, PreparedBinding, Receipt, SyntheticAccess,
    VERSION, parse_generate,
)


CONTEXT, RECIPE, GENERATION, REVISION = (c * 32 for c in "1234")
REQUEST = "a" * 32
IDENTITY = "b1-synthetic-fixture-v1"


class SQLiteFixture:
    """Only a test durability implementation, NOT a production store design.

    Reopen requires the known fixture identity; missing rows/schema/hash or lost
    stores raise. There is no reset, TTL, repair, recovery execution or migration.
    """
    def __init__(self, connection):
        self.connection = connection
        self.lock = threading.RLock()
        self.fail_before_commit = False
        self.fail_after_commit = False
        self.transactions = 0
        self.writes = 0

    @classmethod
    def provision(cls, path, *, provider=True):
        connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("CREATE TABLE fixture (identity TEXT PRIMARY KEY, payload TEXT, checksum TEXT)")
        state = LedgerState(REVISION, PreparedBinding(CONTEXT, RECIPE, GENERATION, provider))
        payload = json.dumps(asdict(state), sort_keys=True, allow_nan=False)
        connection.execute("INSERT INTO fixture VALUES (?, ?, ?)",
                           (IDENTITY, payload, hashlib.sha256(payload.encode()).hexdigest()))
        return cls(connection)

    @classmethod
    def reopen(cls, path):
        # Existing-only; cannot create a lost store on restart.
        connection = sqlite3.connect(f"file:{path}?mode=rw", uri=True, isolation_level=None,
                                     check_same_thread=False, timeout=2)
        connection.execute("PRAGMA synchronous=FULL")
        return cls(connection)

    def read(self):
        with self.lock:
            rows = self.connection.execute("SELECT identity, payload, checksum FROM fixture").fetchall()
            if len(rows) != 1 or rows[0][0] != IDENTITY:
                raise ValueError("unknown_fixture")
            _, payload, checksum = rows[0]
            if hashlib.sha256(payload.encode()).hexdigest() != checksum:
                raise ValueError("corrupt_fixture")
            data = json.loads(payload)
            return LedgerState(data["revision"], PreparedBinding(**data["binding"]),
                               tuple(Attempt(**a) for a in data["attempts"]))

    def transact(self, operation):
        with self.lock:
            self.transactions += 1
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                old = self.read()
                new, result = operation(old)
                if new != old:
                    payload = json.dumps(asdict(new), sort_keys=True, allow_nan=False)
                    self.connection.execute("UPDATE fixture SET payload=?, checksum=? WHERE identity=?",
                        (payload, hashlib.sha256(payload.encode()).hexdigest(), IDENTITY))
                    self.writes += 1
                if self.fail_before_commit:
                    raise OSError("synthetic private storage error")
                self.connection.commit()
                if self.fail_after_commit:
                    raise OSError("synthetic lost commit response")
                return result
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def close(self):
        self.connection.close()


@pytest.fixture
def store(tmp_path):
    fixture = SQLiteFixture.provision(tmp_path / "synthetic.sqlite")
    yield fixture
    fixture.close()


def adapter(store, provider=None, *, allowed=True):
    return B1FakeAdapter(store, provider=provider, access=SyntheticAccess(allowed), clock=lambda: 100.0)


def request(port, rid=REQUEST, **input_changes):
    return {"schemaVersion": VERSION, "requestId": rid,
            "expectedRevision": port.observe()["revision"], "action": "generate",
            "input": {"contextRef": CONTEXT, "recipeRef": RECIPE, **input_changes}}


def assert_admission(value):
    assert set(value) == set(Admission.__annotations__)
    assert value["schemaVersion"] == VERSION and value["workflowId"] == "b1"
    r = value["receipt"]
    if value["decision"] in {"accepted", "replayed"}:
        assert r is not None and value["error"] is None
    if r:
        assert set(r) == set(Receipt.__annotations__)
        assert r["requestId"] == value["requestId"]
        assert r["action"] == "generate"


def test_read_and_constructor_have_zero_mutations(store):
    before = store.connection.total_changes
    p = FakeReviewProvider()
    port = adapter(store, p)
    read_only = B1ReadAdapter(store, clock=lambda: 100)
    for _ in range(3):
        observed = port.observe()
        assert observed["adapterMode"] == "mock"
        assert observed["integration"] == "pending"
        assert observed["availableActions"] == []
        assert observed["actionReasons"] == {"generate": "mock_only", "set_schedule": "scheduler_decision_required"}
        assert observed["execution"]["state"] == "idle"
        assert observed["revision"] == REVISION
        assert read_only.observe()["actionReasons"]["generate"] == "read_only"
        assert read_only.lookup(REQUEST)["error"] == {"code": "request_not_found", "retry": "lookup"}
    assert store.connection.total_changes == before
    assert store.transactions == p.invocations == 0


@pytest.mark.parametrize("configured", [True, False])
def test_missing_provider_never_runs(store, configured):
    store.transact(lambda s: (replace(s, binding=replace(s.binding, provider_present=configured)), None))
    port = adapter(store)
    assert port.observe()["configuration"] == {"state": "missing", "reason": "configuration_missing"}
    result = port.generate(request(port))
    assert result["decision"] == "rejected"
    assert result["error"]["code"] == "configuration_missing"
    assert not store.read().attempts


def test_authority_checked_before_replay_or_transaction(store):
    p = FakeReviewProvider()
    allowed = adapter(store, p)
    req = request(allowed)
    allowed.generate(req)
    denied = adapter(store, p, allowed=False)
    before = store.transactions
    assert denied.generate(req)["error"]["code"] == "authority_rejected"
    assert denied.run_fake(REQUEST)["error"]["code"] == "authority_rejected"
    assert before == store.transactions and p.invocations == 0


def test_manual_queued_running_and_verified_review_only(store):
    p = FakeReviewProvider()
    port = adapter(store, p)
    req = request(port)
    first = port.generate(req)
    assert_admission(first)
    assert first["decision"] == "accepted"
    assert first["receipt"]["state"] == "queued"
    assert first["receipt"]["effect"] == {"kind": "review_artifact", "state": "unknown", "changed": None}
    assert p.invocations == 0
    queued = port.observe()
    assert queued["actionReasons"]["generate"] == "mock_only"
    assert queued["execution"] == {"state": "queued", "requestId": REQUEST, "startedAt": None}
    assert queued["lastVerifiedOutcome"] is None
    assert queued["revision"] != req["expectedRevision"]
    done = port.run_fake(REQUEST)
    assert_admission(done)
    assert done["receipt"]["outcome"] == "success"
    assert done["receipt"]["effect"] == {"kind": "review_artifact", "state": "verified", "changed": None}
    observed = port.observe()
    assert observed["lastVerifiedOutcome"]["effect"]["kind"] == "review_artifact"
    assert observed["execution"]["state"] == "idle"
    assert p.invocations == 1
    for _ in range(3):
        assert port.generate(req)["receipt"] == done["receipt"]
        assert port.run_fake(REQUEST)["receipt"] == done["receipt"]
        assert port.lookup(REQUEST)["receipt"] == done["receipt"]
    assert p.invocations == 1
    # Output DTO mutation cannot alter the durable binding/receipt.
    done["receipt"]["effect"]["kind"] = "daily_vault"
    assert port.lookup(REQUEST)["receipt"]["effect"]["kind"] == "review_artifact"


def test_conflict_stale_invalid_binding_busy_before_invocation(store):
    p = FakeReviewProvider()
    port = adapter(store, p)
    original = request(port)
    first = port.generate(original)
    assert port.generate({**original, "input": {"contextRef": "c" * 32, "recipeRef": RECIPE}})["error"]["code"] == "request_conflict"
    assert port.generate({**original, "requestId": "d" * 32})["error"]["code"] == "stale_revision"
    assert port.generate(request(port, "e" * 32))["error"]["code"] == "busy"
    assert port.generate(original)["receipt"]["runId"] == first["receipt"]["runId"]
    assert p.invocations == 0
    port.run_fake(REQUEST)
    assert port.generate(request(port, "e" * 32, contextRef="c" * 32))["error"]["code"] == "input_binding_invalid"
    assert len(store.read().attempts) == 1


@pytest.mark.parametrize("scenario, outcome", [
    ("awaiting_unverified", "unknown"), ("mismatched_evidence", "unknown"),
    ("error", "failure"), ("timeout", "unknown"), ("retry_exhausted", "failure"),
    ("runtime_mismatch", "failure"), ("input_rejected", "failure"),
])
def test_unverified_review_retry_error_and_timeout_fail_closed(store, scenario, outcome):
    p = FakeReviewProvider(scenario)
    port = adapter(store, p)
    req = request(port)
    port.generate(req)
    done = port.run_fake(REQUEST)
    assert done["receipt"]["outcome"] == outcome
    assert done["receipt"]["effect"]["state"] == "unknown"
    assert done["receipt"]["error"]["code"] in {"effect_unconfirmed", "execution_failed", "input_rejected"}
    observed = port.observe()
    assert observed["lastVerifiedOutcome"] is None
    assert observed["execution"]["state"] == "unknown"
    assert observed["actionReasons"]["generate"] == "mock_only"
    assert port.generate(request(port, "e" * 32))["error"]["code"] == "reconciliation_required"
    assert port.generate(req)["receipt"] == done["receipt"]
    assert port.run_fake(REQUEST)["receipt"] == done["receipt"]
    assert p.invocations == 1
    serialized = json.dumps([observed, done])
    assert "sentinel" not in serialized and "recipe/source" not in serialized


def test_historical_verified_outcome_not_newer_failure_or_rotated_generation(store):
    port = adapter(store, FakeReviewProvider())
    port.generate(request(port))
    port.run_fake(REQUEST)
    old = port.observe()["lastVerifiedOutcome"]
    failed = adapter(store, FakeReviewProvider("error"))
    failed.generate(request(failed, "e" * 32))
    failed.run_fake("e" * 32)
    observed = failed.observe()
    assert observed["lastAttempt"]["outcome"] == "failure"
    assert observed["lastVerifiedOutcome"] == old
    store.transact(lambda s: (replace(s, revision="f" * 32,
        binding=replace(s.binding, generation="d" * 32)), None))
    assert failed.observe()["lastVerifiedOutcome"] is None


def test_configuration_rotation_replays_old_receipt_without_rebinding(store):
    p = FakeReviewProvider()
    port = adapter(store, p)
    req = request(port)
    port.generate(req)
    done = port.run_fake(REQUEST)
    store.transact(lambda s: (replace(s, revision="f" * 32, binding=PreparedBinding(
        "d" * 32, "e" * 32, "c" * 32, False)), None))
    assert port.generate(req)["receipt"] == done["receipt"]
    assert store.read().attempts[0].generation == GENERATION
    assert store.read().attempts[0].accepted_revision == REVISION
    assert p.invocations == 1


def test_no_queued_execution_after_configuration_rotation(store):
    p = FakeReviewProvider()
    port = adapter(store, p)
    port.generate(request(port))
    store.transact(lambda s: (replace(s, revision="f" * 32,
        binding=replace(s.binding, generation="d" * 32)), None))
    assert port.run_fake(REQUEST)["receipt"]["state"] == "queued"
    assert p.invocations == 0


@pytest.mark.parametrize("field,value,code", [
    ("schemaVersion", "webui-workflow-control/v9", "unsupported_version"),
    ("action", "retry", "unsupported_action"), ("action", "apply", "unsupported_action"),
    ("requestId", True, "invalid_request"), ("requestId", "A" * 32, "invalid_request"),
    ("expectedRevision", "../private", "invalid_request"),
    ("input", {"contextRef": CONTEXT, "recipeRef": "/private/recipe"}, "invalid_request"),
    ("input", {"contextRef": CONTEXT, "recipeRef": RECIPE, "source": "sentinel"}, "invalid_request"),
    ("input", [], "invalid_request"),
])
def test_invalid_input_never_touches_ledger(store, field, value, code):
    p = FakeReviewProvider()
    port = adapter(store, p)
    req = {**request(port), field: value}
    with pytest.raises(InvalidRequest, match=f"^{code}$"):
        port.generate(req)
    assert store.transactions == p.invocations == 0


@pytest.mark.parametrize("payload", [
    '{"schemaVersion":"webui-workflow-control/v1","schemaVersion":"webui-workflow-control/v1"}',
    '{"input":{"recipeRef":1,"recipeRef":2}}', '{"input":NaN}', '{"input":Infinity}',
    '{"input":-Infinity}', '{', '[]', 'null', b'\xff', ' ' * 4097,
])
def test_json_duplicate_nonfinite_and_malformed_rejected(payload):
    with pytest.raises(InvalidRequest):
        parse_generate(payload)


@pytest.mark.parametrize("extra", ["argv", "path", "url", "env", "trigger", "secret", "service"])
def test_extra_authority_keys_rejected(store, extra):
    p = FakeReviewProvider()
    port = adapter(store, p)
    with pytest.raises(InvalidRequest):
        port.generate({**request(port), extra: "synthetic"})
    assert store.transactions == p.invocations == 0


def test_json_exact_generate_request_roundtrip(store):
    port = adapter(store, FakeReviewProvider())
    req = request(port)
    assert parse_generate(json.dumps(req)) == req
    assert port.generate(json.dumps(req))["decision"] == "accepted"


def test_schedule_gated_without_storage_or_engine_policy(store):
    port = adapter(store, FakeReviewProvider())
    req = {**request(port), "action": "set_schedule", "input": {"enabled": True, "intervalSeconds": 1}}
    result = port.set_schedule(req)
    assert set(result) == {"schemaVersion", "workflowId", "requestId", "decision", "schedule", "revision", "error"}
    assert result["error"]["code"] == "scheduler_decision_required"
    assert result["schedule"] is result["revision"] is None
    assert port.observe()["schedule"] == {"state": "unknown", "intervalSeconds": None,
        "nextDueAt": None, "reason": "scheduler_decision_required"}
    assert store.transactions == 0
    for invalid in [True, 1.5, "60", None]:
        with pytest.raises(InvalidRequest):
            port.set_schedule({**req, "input": {"enabled": True, "intervalSeconds": invalid}})


def test_no_runtime_provider_or_read_effect_port(store):
    class WouldBeRuntime:
        def generate(self, _):
            pytest.fail("runtime must not be called")
    with pytest.raises(TypeError, match="fake_provider_required"):
        adapter(store, WouldBeRuntime())
    class FakeSubclass(FakeReviewProvider):
        pass
    with pytest.raises(TypeError, match="fake_provider_required"):
        adapter(store, FakeSubclass())
    assert not hasattr(B1ReadAdapter(store, clock=lambda: 100), "generate")


@pytest.mark.parametrize("damage", ["missing", "corrupt", "invalid_enum", "unknown_version", "bad_time", "bad_effect"])
def test_unknown_corrupt_or_lost_ledger_fails_closed(store, damage):
    p = FakeReviewProvider()
    port = adapter(store, p)
    req = request(port)
    port.generate(req)
    if damage == "missing":
        store.connection.execute("DELETE FROM fixture")
    elif damage == "corrupt":
        store.connection.execute("UPDATE fixture SET checksum='wrong'")
    else:
        # Valid JSON/checksum is insufficient if the typed receipt is malformed.
        r = store.read().attempts[0].receipt
        if damage == "invalid_enum":
            r["state"] = "secret-sentinel"
        elif damage == "unknown_version":
            r["schemaVersion"] = "webui-workflow-control/v9"
        elif damage == "bad_time":
            r["createdAt"] = True
        else:
            r["effect"]["state"] = "verified"
        store.transact(lambda s: (replace(s, attempts=(replace(s.attempts[0], receipt=r),)), None))
    observed = port.observe()
    assert observed["integration"] == "unavailable"
    assert observed["execution"]["state"] == "unknown"
    assert observed["availableActions"] == []
    assert observed["lastAttempt"] is observed["lastVerifiedOutcome"] is None
    assert port.lookup(REQUEST)["decision"] == "indeterminate"
    assert port.generate(req)["error"] == {"code": "admission_unknown", "retry": "lookup"}
    assert port.run_fake(REQUEST)["decision"] == "indeterminate"
    assert "secret-sentinel" not in json.dumps(observed)
    assert p.invocations == 0


def test_absent_store_does_not_bootstrap():
    port = adapter(None, FakeReviewProvider())
    observed = port.observe()
    assert observed["integration"] == "not_attached"
    assert port.lookup(REQUEST)["decision"] == "indeterminate"
    assert port.generate(request(port))["error"]["code"] == "not_attached"


@pytest.mark.parametrize("when", ["before", "after"])
def test_commit_ambiguity_lookup_same_id_no_double_admission(store, when):
    p = FakeReviewProvider()
    port = adapter(store, p)
    req = request(port)
    setattr(store, f"fail_{when}_commit", True)
    result = port.generate(req)
    assert result["decision"] == "indeterminate" and result["receipt"] is None
    assert p.invocations == 0
    setattr(store, f"fail_{when}_commit", False)
    lookup = port.lookup(REQUEST)
    if when == "before":
        assert lookup["error"] == {"code": "request_not_found", "retry": "lookup"}
        assert not store.read().attempts
    else:
        assert lookup["receipt"]["state"] == "queued"
        assert port.generate(req)["receipt"] == lookup["receipt"]
        assert len(store.read().attempts) == 1


def test_crash_after_claim_before_fake_leaves_running_no_reexecution(store, tmp_path):
    p = FakeReviewProvider()
    port = adapter(store, p)
    req = request(port)
    port.generate(req)
    store.fail_after_commit = True  # running claim committed, response lost
    assert port.run_fake(REQUEST)["decision"] == "indeterminate"
    assert p.invocations == 0
    reopened = SQLiteFixture.reopen(tmp_path / "synthetic.sqlite")
    try:
        restarted = adapter(reopened, p)
        before = reopened.connection.total_changes
        assert restarted.lookup(REQUEST)["receipt"]["state"] == "running"
        assert restarted.observe()["execution"]["state"] == "running"
        assert reopened.connection.total_changes == before
        assert restarted.generate(req)["receipt"]["state"] == "running"
        assert restarted.run_fake(REQUEST)["receipt"]["state"] == "running"
        assert restarted.generate(request(restarted, "e" * 32))["error"]["code"] == "busy"
        assert p.invocations == 0
    finally:
        reopened.close()


def test_crash_after_fake_before_completion_and_restart_no_double_invocation(store, tmp_path):
    p = FakeReviewProvider()
    port = adapter(store, p)
    req = request(port)
    port.generate(req)
    original_transact = store.transact
    calls = 0
    def crash_completion(operation):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic worker died after fake effect")
        return original_transact(operation)
    store.transact = crash_completion
    assert port.run_fake(REQUEST)["decision"] == "indeterminate"
    assert p.invocations == 1
    reopened = SQLiteFixture.reopen(tmp_path / "synthetic.sqlite")
    try:
        restarted = adapter(reopened, p)
        assert restarted.lookup(REQUEST)["receipt"]["effect"]["state"] == "unknown"
        assert restarted.run_fake(REQUEST)["receipt"]["state"] == "running"
        assert restarted.generate(req)["receipt"]["state"] == "running"
        assert p.invocations == 1
    finally:
        reopened.close()


def test_completed_receipt_survives_closed_connection_restart(store, tmp_path):
    p = FakeReviewProvider()
    port = adapter(store, p)
    req = request(port)
    port.generate(req)
    done = port.run_fake(REQUEST)
    store.close()
    reopened = SQLiteFixture.reopen(tmp_path / "synthetic.sqlite")
    try:
        restarted = adapter(reopened, p)
        assert restarted.lookup(REQUEST)["receipt"] == done["receipt"]
        assert restarted.generate(req)["receipt"] == done["receipt"]
        assert restarted.run_fake(REQUEST)["receipt"] == done["receipt"]
        assert p.invocations == 1
    finally:
        reopened.close()


def test_concurrent_duplicates_and_workers_across_sqlite_connections(store, tmp_path):
    p = FakeReviewProvider()
    port = adapter(store, p)
    req = request(port)
    reopened = SQLiteFixture.reopen(tmp_path / "synthetic.sqlite")
    second = adapter(reopened, p)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda a: a.generate(req), [port, second]))
            assert sorted(r["decision"] for r in results) == ["accepted", "replayed"]
            assert results[0]["receipt"]["runId"] == results[1]["receipt"]["runId"]
            list(pool.map(lambda a: a.run_fake(REQUEST), [port, second]))
        assert p.invocations == 1 and len(store.read().attempts) == 1
        assert port.lookup(REQUEST)["receipt"]["outcome"] == "success"
    finally:
        reopened.close()


def test_snapshot_and_receipt_schema_against_frozen_contract_validator(store):
    # Reuse the independent contract fixture validator, without editing its owner.
    from test_webui_workflow_contract import validate_workflow
    port = adapter(store, FakeReviewProvider())
    for mode in (port, B1ReadAdapter(store, clock=lambda: 100)):
        validate_workflow(mode.observe())
    port.generate(request(port))
    validate_workflow(port.observe())
    port.run_fake(REQUEST)
    validate_workflow(port.observe())
    serialized = json.dumps([port.observe(), port.lookup(REQUEST)])
    for forbidden in ("recipe_ref", "recipeRef", "contextRef", "generation", "source", "path", "synthetic_review_only"):
        assert forbidden not in serialized


def test_closed_store_never_resets_or_reexecutes(store):
    p = FakeReviewProvider()
    port = adapter(store, p)
    req = request(port)
    port.generate(req)
    store.close()
    assert port.observe()["integration"] == "unavailable"
    assert port.generate(req)["decision"] == "indeterminate"
    assert port.lookup(REQUEST)["decision"] == "indeterminate"
    assert port.run_fake(REQUEST)["decision"] == "indeterminate"
    assert p.invocations == 0


def test_claim_commit_ambiguity_does_not_even_invoke_fake(store):
    p = FakeReviewProvider()
    port = adapter(store, p)
    port.generate(request(port))
    store.fail_after_commit = True
    assert port.run_fake(REQUEST)["decision"] == "indeterminate"
    store.fail_after_commit = False
    assert port.lookup(REQUEST)["receipt"]["state"] == "running"
    assert port.run_fake(REQUEST)["receipt"]["state"] == "running"
    assert p.invocations == 0


def test_result_commit_response_lost_preserves_verified_receipt_without_reexecution(store):
    p = FakeReviewProvider()
    port = adapter(store, p)
    port.generate(request(port))
    original = store.transact
    calls = 0
    def lost_result_response(operation):
        nonlocal calls
        calls += 1
        store.fail_after_commit = calls == 2
        return original(operation)
    store.transact = lost_result_response
    assert port.run_fake(REQUEST)["decision"] == "indeterminate"
    store.fail_after_commit = False
    store.transact = original
    receipt = port.lookup(REQUEST)["receipt"]
    assert receipt["state"] == "completed" and receipt["outcome"] == "success"
    assert port.run_fake(REQUEST)["receipt"] == receipt
    assert p.invocations == 1
