"""B4 redacted durable failure-stage diagnostics, all synthetic/no Providers."""
from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import sqlite3
import threading
import uuid

import pytest

from obsidian_automation import snapshot_app
from obsidian_automation.admin_dashboard import make_server
from obsidian_automation.github_daily_job import (
    DailyJob, DailyStageFailure, Invalid,
)
from obsidian_automation.github_daily_setup import DailySetupError

SECRET = "SYNTHETIC_PRIVATE_TOKEN_URL_DO_NOT_EXPOSE"
SHA = "a" * 64


def preview():
    return {
        "version": 1, "status": "preview_ready", "date_jst": "2026-11-09",
        "capture_sha256": SHA, "digest_sha256": "b" * 64,
        "original_event_count": 3, "rendered_entity_count": 1,
        "model_used": False, "publication_authorized": False,
        "vault_modified": False,
    }


def terminal(tmp_path, runner, publisher=None):
    with DailyJob(tmp_path / "b4", runner, publisher=publisher,
                  configured=lambda: True) as job:
        kind, received = job.submit(uuid.uuid4().hex)
        assert kind == "accepted"
        job._worker.join(3)
        assert not job._worker.is_alive()
        finished = job.run(received["run_id"])
        assert finished == job.status()["last_run"]
        return finished


@pytest.mark.parametrize(("stage","code"), [
    ("source_admission", "not_configured"),
    ("source_admission", "source_unavailable"),
    ("source_pull", "pull_failed"),
    ("project_selection", "no_watched_projects"),
    ("source_verification", "source_changed"),
    ("collection_preview", "collection_or_preview_failed"),
])
def test_fixed_stage_failure_is_durable_and_redacted(tmp_path, stage, code):
    def runner():
        raise DailyStageFailure(stage, code)
    state = tmp_path / stage
    row = terminal(state, runner)
    assert row["status"] == "failed"
    assert (row["failure_stage"], row["failure_code"]) == (stage, code)
    assert row["capture_sha256"] is None
    assert row["date_jst"] is None
    assert SECRET not in json.dumps(row)
    with DailyJob(state / "b4", lambda: pytest.fail("no replay"),
                  configured=lambda: True) as restarted:
        assert restarted.run(row["run_id"])["failure_code"] == code
        assert restarted.submit(row["request_id"])[0] == "replayed"


def test_runner_exception_does_not_persist_or_return_exception_message(tmp_path):
    row = terminal(tmp_path, lambda: (_ for _ in ()).throw(
        RuntimeError("Nextcloud URL=" + SECRET)))
    assert row["status"] == "failed"
    assert (row["failure_stage"], row["failure_code"]) == (
        "runner", "unexpected_exception",
    )
    assert SECRET not in json.dumps(row)
    with sqlite3.connect(tmp_path / "b4/daily.sqlite3") as db:
        stored = db.execute(
            "SELECT failure_stage, failure_code FROM runs"
        ).fetchone()
        assert stored == ("runner", "unexpected_exception")


def test_mutated_or_invalid_failure_marker_never_persists_unbounded_text(tmp_path):
    with pytest.raises(Invalid):
        DailyStageFailure("provider_url_" + SECRET, SECRET)
    marker = DailyStageFailure("source_pull", "pull_failed")
    marker.stage = ["unhashable"]
    marker.code = SECRET
    row = terminal(tmp_path, lambda: (_ for _ in ()).throw(marker))
    assert (row["failure_stage"], row["failure_code"]) == (
        "runner", "unexpected_exception",
    )
    with sqlite3.connect(tmp_path / "b4/daily.sqlite3") as db:
        assert db.execute(
            "SELECT failure_stage,failure_code FROM runs"
        ).fetchone() == ("runner", "unexpected_exception")
    assert SECRET not in json.dumps(row)


def test_invalid_preview_never_attempts_publisher_and_is_classified(tmp_path):
    calls = []
    def publish(_):
        calls.append("called")
        return {}
    row = terminal(tmp_path, lambda: {**preview(),
                   "vault_modified": True}, publisher=publish)
    assert row["status"] == "failed"
    assert (row["failure_stage"], row["failure_code"]) == (
        "preview_validation", "invalid_preview_receipt",
    )
    assert row["capture_sha256"] is None
    assert not calls


def test_preview_ready_and_published_receipts_have_null_failure_fields(tmp_path):
    row = terminal(tmp_path / "no-publisher", preview)
    assert row["status"] == "preview_ready"
    assert row["failure_stage"] is None and row["failure_code"] is None

    def publisher(value):
        return {
            "version": 1, "status": "published", "publication_verified": True,
            "model_used": False, "vault_modified": True,
            "publication_outcome": "applied",
            "date_jst": value["date_jst"],
            "capture_sha256": value["capture_sha256"],
            "digest_sha256": value["digest_sha256"],
            "before_content_sha256": "1" * 64,
            "after_content_sha256": "2" * 64,
        }
    row = terminal(tmp_path / "publisher", preview, publisher)
    assert row["status"] == "published"
    assert row["publication_outcome"] == "applied"
    assert (row["failure_stage"], row["failure_code"]) == (None, None)


@pytest.mark.parametrize(("publisher","code"), [
    (lambda _: (_ for _ in ()).throw(RuntimeError(SECRET)),
     "outcome_unconfirmed"),
    (lambda _: {"status": "published", "hidden": SECRET},
     "invalid_publication_receipt"),
])
def test_publisher_failures_keep_preview_evidence_without_claiming_no_effect(
        tmp_path, publisher, code):
    row = terminal(tmp_path / code, preview, publisher)
    assert row["status"] == "publish_failed"
    assert (row["failure_stage"], row["failure_code"]) == (
        "publication", code,
    )
    assert row["capture_sha256"] == SHA
    assert row["digest_sha256"] == "b" * 64
    assert row["publication_outcome"] is None
    assert SECRET not in json.dumps(row)
    with DailyJob(tmp_path / code / "b4", preview, publisher=publisher,
                  configured=lambda: True) as reopened:
        assert reopened.submit(row["request_id"])[0] == "replayed"
        assert reopened.run(row["run_id"])["status"] == "publish_failed"


def test_old_sqlite_receipts_migrate_with_null_unknown_diagnostics(tmp_path):
    directory = tmp_path / "legacy"
    directory.mkdir()
    path = directory / "daily.sqlite3"
    failed_id, published_id = uuid.uuid4().hex, uuid.uuid4().hex
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE runs ("
            "run_id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,"
            "trigger TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,"
            "completed REAL,date_jst TEXT,capture_sha256 TEXT,digest_sha256 TEXT,"
            "original_event_count INTEGER,rendered_entity_count INTEGER)"
        )
        for n, status in enumerate(("failed", "preview_ready")):
            rid = (failed_id, published_id)[n]
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (rid, uuid.uuid4().hex, "scheduled", status,
                        1000. + n, 1100. + n, None, None, None, None, None))
    for _ in range(2):
        with DailyJob(directory, preview, configured=lambda: True) as job:
            failed = job.run(failed_id)
            assert failed["status"] == "failed"
            assert failed["failure_stage"] is None
            assert failed["failure_code"] is None
            assert job.run(published_id)["status"] == "preview_ready"
            assert job.run(published_id)["failure_stage"] is None
            cols = [row[1] for row in job.db.execute("PRAGMA table_info(runs)")]
            assert cols.count("publication_outcome") == 1
            assert cols.count("failure_stage") == 1
            assert cols.count("failure_code") == 1


def test_corrupted_sqlite_failure_fields_are_redacted_at_get(tmp_path):
    with DailyJob(tmp_path / "db", preview, configured=lambda: True) as job:
        kind, row = job.submit(uuid.uuid4().hex)
        assert kind == "accepted"
        job._worker.join(3)
        with job.db:
            job.db.execute(
                "UPDATE runs SET status='failed',failure_stage=?,failure_code=?"
                " WHERE run_id=?", (SECRET, SECRET, row["run_id"]),
            )
        safe = job.run(row["run_id"])
        assert (safe["failure_stage"], safe["failure_code"]) == (None, None)
        assert SECRET not in json.dumps(safe)
        with job.db:
            job.db.execute(
                "UPDATE runs SET status='published',failure_stage=?,failure_code=?"
                " WHERE run_id=?", ("source_pull", "pull_failed", row["run_id"]),
            )
        safe = job.run(row["run_id"])
        assert (safe["failure_stage"], safe["failure_code"]) == (None, None)


class _FakeSetup:
    def __init__(self, *, reason=None, bad_untyped=False, postcheck=False):
        self.reason = reason
        self.bad_untyped = bad_untyped
        self.postcheck = postcheck
        self.acquired = False

    @contextmanager
    def project_source(self):
        if self.bad_untyped:
            raise RuntimeError(SECRET)
        if self.reason is not None:
            raise DailySetupError(self.reason)
        self.acquired = True
        cfg = type("SyntheticConfig", (), {"request_timeout_seconds": 10})()
        yield cfg, "FAKE_TOKEN_NOT_USED"
        if self.postcheck:
            raise DailySetupError("source_connections_changed")


@pytest.mark.parametrize(("reason","stage","code"), [
    ("github_daily_not_configured", "source_admission", "not_configured"),
    ("project_mirror_pull_failed", "source_pull", "pull_failed"),
    ("no_watched_github_projects", "project_selection", "no_watched_projects"),
    ("source_connections_changed", "source_verification", "source_changed"),
    (SECRET, "source_admission", "source_unavailable"),
])
def test_source_stage_is_coarse_and_only_whitelisted_errors_can_surface(
        tmp_path,monkeypatch,reason,stage,code):
    monkeypatch.setattr(snapshot_app,"GitHubClient",
                        lambda **_: pytest.fail("no Github client on source failure"))
    monkeypatch.setattr(snapshot_app,"run_once",
                        lambda *_, **__: pytest.fail("no collection"))
    setup = _FakeSetup(reason=reason)
    def run():
        return snapshot_app.collect_b4_preview(setup,tmp_path)
    row = terminal(tmp_path,run)
    assert row["status"] == "failed"
    assert (row["failure_stage"], row["failure_code"]) == (stage, code)
    assert not setup.acquired
    assert SECRET not in json.dumps(row)


def test_unexpected_source_exception_is_finite_and_no_provider_is_called(
        tmp_path,monkeypatch):
    monkeypatch.setattr(snapshot_app,"GitHubClient",
                        lambda **_: pytest.fail("no provider"))
    row = terminal(
        tmp_path,lambda: snapshot_app.collect_b4_preview(
            _FakeSetup(bad_untyped=True),tmp_path,
        ),
    )
    assert (row["failure_stage"], row["failure_code"]) == (
        "source_admission", "source_unavailable",
    )


def test_collection_exception_does_not_expose_fake_token(tmp_path,monkeypatch):
    invoked = []
    monkeypatch.setattr(snapshot_app,"GitHubClient",
                        lambda **_: object())
    def failing_collector(*_args,**_kwargs):
        invoked.append(1)
        raise RuntimeError(SECRET)
    monkeypatch.setattr(snapshot_app,"run_once",failing_collector)
    row=terminal(
        tmp_path,lambda: snapshot_app.collect_b4_preview(
            _FakeSetup(),tmp_path,
        ),
    )
    assert invoked == [1]
    assert row["status"] == "failed"
    assert (row["failure_stage"], row["failure_code"]) == (
        "collection_preview", "collection_or_preview_failed",
    )
    assert SECRET not in json.dumps(row)


def test_postflight_source_rotation_overrides_preview_success(tmp_path,monkeypatch):
    monkeypatch.setattr(snapshot_app,"GitHubClient",
                        lambda **_: object())
    monkeypatch.setattr(snapshot_app,"run_once",
                        lambda *_a,**_kw: preview())
    row=terminal(
        tmp_path,lambda: snapshot_app.collect_b4_preview(
            _FakeSetup(postcheck=True),tmp_path,
        ),
    )
    assert row["status"] == "failed"
    assert (row["failure_stage"], row["failure_code"]) == (
        "source_verification", "source_changed",
    )
    assert row["capture_sha256"] is None


def test_http_get_only_redacted_diagnostics_and_no_provider_side_effect(
        tmp_path):
    calls = []
    with DailyJob(tmp_path / "daily",lambda: calls.append(1) or (
            _ for _ in ()).throw(RuntimeError(SECRET)),
            configured=lambda:True) as job:
        _,row=job.submit(uuid.uuid4().hex)
        job._worker.join(3)
        assert calls == [1]
        with make_server(port=0, daily_job=job, observer=lambda:[],
                         ai_observer=lambda:{}) as server:
            th=threading.Thread(target=server.serve_forever,daemon=True)
            th.start()
            try:
                port=server.server_address[1]
                for path in ("/api/v1/daily",
                             "/api/v1/daily/runs/"+row["run_id"]):
                    con=http.client.HTTPConnection("127.0.0.1",port,timeout=3)
                    con.request("GET",path)
                    resp=con.getresponse()
                    body=resp.read()
                    con.close()
                    assert resp.status==200
                    assert SECRET.encode() not in body
                    response=json.loads(body)
                    receipt=response["last_run"] if path=="/api/v1/daily" else response
                    assert receipt["failure_stage"]=="runner"
                    assert receipt["failure_code"]=="unexpected_exception"
                assert calls==[1]  # GET cannot trigger any second Run
            finally:
                server.shutdown()
                th.join(3)


def test_changing_exception_attributes_never_write_untrusted_sqlite_values(tmp_path):
    class ChangingMarker(DailyStageFailure):
        def __init__(self):
            super().__init__("source_pull", "pull_failed")
            self.stage_reads = 0
            self.code_reads = 0

        def __getattribute__(self, name):
            if name == "stage":
                reads = object.__getattribute__(self, "stage_reads")
                object.__setattr__(self, "stage_reads", reads + 1)
                return "source_pull" if reads == 0 else SECRET
            if name == "code":
                reads = object.__getattribute__(self, "code_reads")
                object.__setattr__(self, "code_reads", reads + 1)
                return "pull_failed" if reads == 0 else SECRET
            return super().__getattribute__(name)

    marker = ChangingMarker()
    row = terminal(tmp_path, lambda: (_ for _ in ()).throw(marker))
    assert row["status"] == "failed"
    assert (row["failure_stage"], row["failure_code"]) == (
        "source_pull", "pull_failed",
    )
    assert marker.stage_reads == marker.code_reads == 1
    with sqlite3.connect(tmp_path / "b4/daily.sqlite3") as db:
        assert db.execute(
            "SELECT failure_stage,failure_code FROM runs"
        ).fetchone() == ("source_pull", "pull_failed")
    assert SECRET not in json.dumps(row)


@pytest.mark.parametrize("missing", ["stage", "code"])
def test_missing_exception_attribute_always_terminalizes_without_leak(tmp_path, missing):
    marker = DailyStageFailure("source_pull", "pull_failed")
    delattr(marker, missing)
    row = terminal(tmp_path, lambda: (_ for _ in ()).throw(marker))
    assert row["status"] == "failed"
    assert (row["failure_stage"], row["failure_code"]) == (
        "runner", "unexpected_exception",
    )
    with sqlite3.connect(tmp_path / "b4/daily.sqlite3") as db:
        assert db.execute(
            "SELECT status, failure_stage, failure_code FROM runs"
        ).fetchone() == ("failed", "runner", "unexpected_exception")


def test_readonly_legacy_view_accepts_both_old_and_new_b4_run_shapes(tmp_path):
    from obsidian_automation.webui_legacy_read import project_legacy_read
    from test_webui_legacy_read import daily, daily_run

    t = 2000000000.0
    old = daily(last=daily_run("failed", created=100, completed=110))
    old_view = project_legacy_read(None, old, t)["b4"]
    assert old_view["integration"] == "pending"
    assert old_view["lastAttempt"]["outcome"] == "failure"

    with DailyJob(tmp_path / "daily", lambda: (
            _ for _ in ()).throw(
                DailyStageFailure("source_pull", "pull_failed")
            ), configured=lambda: True) as job:
        _, received = job.submit(uuid.uuid4().hex)
        job._worker.join(3)
        view = project_legacy_read(None, job.status(), t)["b4"]
        assert view["integration"] == "pending"
        assert view["lastAttempt"]["outcome"] == "failure"
        assert view["lastVerifiedOutcome"] is None
        assert view["availableActions"] == []
        assert view["schedule"]["state"] == "disabled"


def test_readonly_legacy_view_rejects_malformed_untrusted_failure_pair(tmp_path):
    from obsidian_automation.webui_legacy_read import project_legacy_read
    from test_webui_legacy_read import daily, daily_run

    t = 2000000000.0
    value = daily(last=daily_run("failed", created=100, completed=110))
    value["last_run"].update(
        failure_stage=SECRET, failure_code=SECRET,
    )
    view = project_legacy_read(None, value, t)["b4"]
    assert view["integration"] == "unavailable"
    assert view["availableActions"] == []

    value["last_run"].update(
        failure_stage="source_pull", failure_code="pull_failed",
        status="preview_ready",
    )
    assert project_legacy_read(None, value, t)["b4"]["integration"] == "unavailable"

    value["last_run"].update(
        status="failed", failure_stage=None, failure_code="pull_failed",
    )
    assert project_legacy_read(None, value, t)["b4"]["integration"] == "unavailable"


@pytest.mark.parametrize("state", [
    "queued", "running", "failed", "preview_ready",
    "published", "publish_failed", "unconfirmed",
])
def test_real_b4_status_common_workflow_get_no_effect_or_unavailability(
        tmp_path, state):
    """Real DailyJob.status with the new exact B4 schema for all states."""
    calls = []
    def runner():
        calls.append("runner")
        if state == "failed":
            raise DailyStageFailure("source_pull", "pull_failed")
        return preview()

    def publisher(record):
        calls.append("publisher")
        if state == "publish_failed":
            raise RuntimeError(SECRET)
        return {
            "version": 1, "status": "published",
            "publication_verified": True, "model_used": False,
            "vault_modified": True, "publication_outcome": "applied",
            "date_jst": record["date_jst"],
            "capture_sha256": record["capture_sha256"],
            "digest_sha256": record["digest_sha256"],
            "before_content_sha256": "1" * 64,
            "after_content_sha256": "2" * 64,
        }

    with DailyJob(tmp_path / state, runner, configured=lambda: True,
                  publisher=(publisher if state in
                             ("published", "publish_failed") else None)) as job:
        if state in ("queued", "running", "unconfirmed"):
            request_id, run_id = uuid.uuid4().hex, uuid.uuid4().hex
            import time
            now = time.time() - 3
            completed = now + 1 if state == "unconfirmed" else None
            with job.db:
                job.db.execute(
                    "INSERT INTO runs "
                    "(run_id,request_id,trigger,status,created,completed)"
                    " VALUES (?,?,?,?,?,?)",
                    (run_id, request_id, "scheduled", state, now, completed),
                )
        else:
            _, received = job.submit(uuid.uuid4().hex)
            job._worker.join(3)
            assert job.run(received["run_id"])["status"] == state

        with make_server(port=0, daily_job=job, observer=lambda: [],
                         ai_observer=lambda: {}) as server:
            th = threading.Thread(target=server.serve_forever, daemon=True)
            th.start()
            try:
                before = len(calls)
                for uri in ("/api/v1/workflow-control",
                            "/api/v1/workflow-control/b4"):
                    con = http.client.HTTPConnection(
                        "127.0.0.1", server.server_address[1], timeout=3,
                    )
                    con.request("GET", uri)
                    response = con.getresponse()
                    payload = response.read()
                    con.close()
                    assert response.status == 200
                    obj = json.loads(payload)
                    b4 = (obj["workflows"][3] if uri.endswith("workflow-control")
                          else obj)
                    assert b4["integration"] == "pending"
                    assert b4["lastAttempt"]["effect"]["state"] == "unknown"
                    assert b4["lastVerifiedOutcome"] is None
                    assert b4["availableActions"] == []
                    if state in ("queued", "running"):
                        assert b4["execution"]["state"] == state
                    elif state in ("failed", "publish_failed"):
                        assert b4["lastAttempt"]["outcome"] == "failure"
                    else:
                        assert b4["lastAttempt"]["outcome"] == "unknown"
                    if state in ("published", "publish_failed"):
                        assert b4["lastAttempt"]["effect"]["kind"] == "daily_vault"
                    assert SECRET.encode() not in payload
                assert len(calls) == before  # GET never admits work
            finally:
                server.shutdown()
                th.join(3)


@pytest.mark.parametrize("preexisting_columns", [
    ("publication_outcome",),
    ("publication_outcome", "failure_stage"),
    ("publication_outcome", "failure_code"),
    ("failure_stage",),
    ("failure_code",),
])
def test_partially_upgraded_receipt_schema_is_idempotently_migrated(
        tmp_path,preexisting_columns):
    root=tmp_path/"partial"
    root.mkdir()
    path=root/"daily.sqlite3"
    rid=uuid.uuid4().hex
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE runs ("
            "run_id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,"
            "trigger TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,"
            "completed REAL,date_jst TEXT,capture_sha256 TEXT,digest_sha256 TEXT,"
            "original_event_count INTEGER,rendered_entity_count INTEGER)"
        )
        for name in preexisting_columns:
            db.execute("ALTER TABLE runs ADD COLUMN "+name+" TEXT")
        db.execute("INSERT INTO runs "
                   "(run_id,request_id,trigger,status,created,completed)"
                   " VALUES (?,?,?,?,?,?)",
                   (rid,uuid.uuid4().hex,"scheduled","failed",42.,45.))
    for _ in range(2):
        with DailyJob(root,preview,configured=lambda:True) as job:
            record=job.run(rid)
            assert record["status"]=="failed"
            assert record["failure_stage"] is None
            assert record["failure_code"] is None
            assert record["publication_outcome"] is None
            names=[r[1] for r in job.db.execute("PRAGMA table_info(runs)")]
            for name in ("publication_outcome","failure_stage","failure_code"):
                assert names.count(name)==1
            assert job.db.execute(
                "SELECT count(*) FROM runs"
            ).fetchone()==(1,)


@pytest.mark.parametrize("shape", ["legacy", "extended"])
def test_old_and_extended_b4_all_seven_states_workflow_control_get_matrix(
        tmp_path, shape):
    """No TestJob actions: seven Run states, both old/new exact shapes, GET only."""
    from test_webui_legacy_read import daily, daily_run

    class SyntheticLegacyJob:
        def __init__(self):
            self.status_value = None
            self.read_count = 0
        def status(self):
            self.read_count += 1
            return self.status_value

    fake = SyntheticLegacyJob()
    with make_server(port=0, daily_job=fake, observer=lambda: [],
                     ai_observer=lambda: {}) as server:
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()
        try:
            states = (
                "queued", "running", "failed", "preview_ready",
                "published", "publish_failed", "unconfirmed",
            )
            for state in states:
                finish = None if state in ("queued", "running") else 60
                base = daily_run(state, created=50, completed=finish)
                if shape == "extended":
                    if state == "failed":
                        pair = ("source_pull", "pull_failed")
                    elif state == "publish_failed":
                        pair = ("publication", "outcome_unconfirmed")
                    else:
                        pair = (None, None)
                    base.update(failure_stage=pair[0], failure_code=pair[1])
                fake.status_value = daily(last=base)
                for uri in ("/api/v1/workflow-control",
                            "/api/v1/workflow-control/b4"):
                    conn = http.client.HTTPConnection(
                        "127.0.0.1",server.server_address[1],timeout=3,
                    )
                    conn.request("GET",uri)
                    response = conn.getresponse()
                    raw = response.read()
                    conn.close()
                    assert response.status == 200
                    decoded = json.loads(raw)
                    item = (decoded["workflows"][3]
                            if uri.endswith("/workflow-control") else decoded)
                    assert item["integration"] == "pending"
                    assert item["availableActions"] == []
                    assert item["lastVerifiedOutcome"] is None
                    assert item["lastAttempt"]["effect"]["state"] == "unknown"
                    if state in ("failed", "publish_failed"):
                        assert item["lastAttempt"]["outcome"] == "failure"
                    else:
                        assert item["lastAttempt"]["outcome"] == (
                            None if state in ("queued", "running") else "unknown"
                        )
                    if state in ("published", "publish_failed"):
                        assert item["lastAttempt"]["effect"]["kind"] == "daily_vault"
                    assert SECRET.encode() not in raw
            assert fake.read_count == len(states) * 2
        finally:
            server.shutdown()
            th.join(3)
