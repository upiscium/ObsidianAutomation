"""Formatter Job / shared scheduler / Dashboard Phase1 acceptance (synthetic only)."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager

import pytest

from obsidian_automation.admin_dashboard import HTML, JS, make_server
from obsidian_automation.snapshot_job import SnapshotJob
from obsidian_automation.vault_format_job import FormatterBusy, FormatterInvalid, FormatterJob
from obsidian_automation.vault_format_source import FormatterSource, pull_system_markdown
from obsidian_automation.vault_formatter import create_plan

RAW = b"# Overview\nparagraph\n  \ncontent\n"
TARGET = "98-System/sub/startup.md"


def prepared_plan(tmp_path):
    vault = tmp_path / "read-only-vault"
    target = vault / TARGET
    target.parent.mkdir(parents=True)
    target.write_bytes(RAW)
    return create_plan(vault)


def new_job(tmp_path, runner, *, generation=None, writer=None, clock=None):
    if generation is None:
        generation = ["a" * 64]
    return FormatterJob(
        tmp_path / "formatter-state", runner,
        configured=lambda: generation[0] is not None,
        source_generation=lambda: generation[0],
        writer=writer, clock=(lambda: clock[0]) if clock else time.time,
    )


def test_default_off_and_manual_run_dry_only(tmp_path):
    plan = prepared_plan(tmp_path)
    calls = []
    with new_job(tmp_path, lambda: calls.append("scan") or plan) as job:
        assert job.status()["settings"]["mode"] == "off"
        assert job.status()["settings"]["write_capable"] is False
        assert not job.tick()
        assert calls == []
        name = uuid.uuid4().hex
        kind, receipt = job.submit(name)
        assert kind == "accepted"
        job._worker.join(5)
        final = job.run(receipt["run_id"])
        assert final["status"] == "would_change"
        assert final["run_mode"] == "dry-run"
        assert final["would_change"] == 1
        assert final["scope"] == "98-System/**"
        assert final["plan_sha256"] == plan["plan_sha256"]
        assert final["skipped"] == 0
        assert job.submit(name)[0] == "replayed"
        assert len(calls) == 1
        assert "paragraph" not in json.dumps(job.status())
        with pytest.raises(FormatterInvalid):
            job.submit("bad")
    assert (tmp_path / "read-only-vault" / TARGET).read_bytes() == RAW


def test_settings_validation_auto_apply_unavailable_and_periodic_dry_run(tmp_path):
    plan = prepared_plan(tmp_path)
    clock = [1000.]
    generation = ["b" * 64]
    with new_job(tmp_path, lambda: plan, generation=generation, clock=clock) as job:
        for invalid in (
            {"mode": "auto-apply", "interval_seconds": 3600},
            {"mode": "dry-run", "interval_seconds": True},
            {"mode": "dry-run", "interval_seconds": 20},
            {"mode": "dry-run", "interval_seconds": 3600, "extra": "bad"},
            {"mode": True, "interval_seconds": 3600},
        ):
            with pytest.raises(FormatterInvalid):
                job.update(invalid)
        assert job.status()["last_run"] is None
        result = job.update({"mode": "dry-run", "interval_seconds": 300})
        assert result["next_due"] == 1300
        clock[0] = 1299
        assert not job.tick()
        clock[0] = 1300
        assert job.tick()
        job._worker.join(5)
        assert job.status()["last_run"]["status"] == "would_change"
        assert job.status()["last_run"]["trigger"] == "scheduled"
        assert job.settings()["next_due"] == 1600
        job.update({"mode": "off", "interval_seconds": 300})
        clock[0] = 1700
        assert not job.tick()
        assert job.status()["settings"]["next_due"] is None


def test_generation_change_is_fail_closed_and_does_not_reset_snapshot_job(tmp_path):
    plan = prepared_plan(tmp_path)
    generation = ["c" * 64]
    with new_job(tmp_path, lambda: plan, generation=generation) as job:
        b2 = SnapshotJob(tmp_path / "snapshot", lambda _: ("no_changes","verify",None))
        try:
            b2_settings = b2.settings()
            job.update({"mode": "dry-run", "interval_seconds": 3600})
            generation[0] = "d" * 64
            assert job.status()["settings"]["mode"] == "off"
            assert not job.tick()
            assert b2.settings() == b2_settings
            job.update({"mode": "dry-run", "interval_seconds": 3600})
            job.connections_saved()
            assert job.settings()["mode"] == "off"
        finally:
            b2.close()


def test_single_owner_busy_admission_and_no_duplicate_thread(tmp_path):
    plan = prepared_plan(tmp_path)
    start, release = threading.Event(), threading.Event()
    def scan():
        start.set()
        assert release.wait(4)
        return plan
    job = new_job(tmp_path, scan)
    try:
        with pytest.raises(FormatterBusy):
            new_job(tmp_path, scan)
        request = uuid.uuid4().hex
        accepted, row = job.submit(request)
        assert accepted == "accepted" and start.wait(3)
        assert job.submit(uuid.uuid4().hex)[0] == "busy"
        assert job.submit(request)[0] == "replayed"
        job.update({"mode": "off", "interval_seconds": 3600})
    finally:
        release.set()
        job.close()


def test_restart_marks_inflight_unconfirmed_without_replaying(tmp_path):
    plan = prepared_plan(tmp_path)
    with new_job(tmp_path, lambda: plan) as job:
        kind, receipt = job.submit(uuid.uuid4().hex)
        assert kind == "accepted"
        job._worker.join(5)
        with job.db:
            job.db.execute(
                "UPDATE runs SET status='running' WHERE run_id=?", (receipt["run_id"],)
            )
    with new_job(tmp_path, lambda: pytest.fail("unexpected replay")) as restarted:
        assert restarted.run(receipt["run_id"])["status"] == "unconfirmed"
        assert restarted.status()["settings"]["mode"] == "off"
        assert restarted.status()["last_run"]["status"] == "unconfirmed"
        assert not restarted.tick()


def test_failing_runner_is_redacted_and_never_claims_success(tmp_path):
    def runner():
        raise RuntimeError("SYNTHETIC_PRIVATE_SECRET_CANARY")
    with new_job(tmp_path, runner) as job:
        kind, receipt = job.submit(uuid.uuid4().hex)
        assert kind == "accepted"
        job._worker.join(5)
        row = job.run(receipt["run_id"])
        assert row["status"] == "failed"
        assert "PRIVATE" not in json.dumps(row)


def test_auto_apply_requires_injected_writer_and_runs_schedule_only(tmp_path):
    plan = prepared_plan(tmp_path)
    written = []
    clock = [1200.]
    def mock_writer(p):
        written.append(p["plan_sha256"])
        return {
            "status": "complete", "plan_sha256": p["plan_sha256"],
            "outcomes": [{"result": "applied"}],
        }
    with new_job(tmp_path, lambda: plan, writer=mock_writer, clock=clock) as job:
        assert job.settings()["write_capable"] is True
        job.update({"mode": "auto-apply", "interval_seconds": 300})
        # Manual Run Once is always non-writing even when the schedule is auto.
        kind, row = job.submit(uuid.uuid4().hex)
        assert kind == "accepted"
        job._worker.join(5)
        assert job.run(row["run_id"])["run_mode"] == "dry-run"
        assert not written
        clock[0] = 1500
        assert job.tick()
        job._worker.join(5)
        assert job.status()["last_run"]["status"] == "applied"
        assert len(written) == 1
        job.update({"mode": "off", "interval_seconds": 300})
        assert job.tick() is False


def test_formatter_source_synthetic_pull_is_temporary_read_only(tmp_path):
    class SourceConnection:
        rclone_remote = "nextcloud:Vault"
        rclone_config = tmp_path / "rclone.conf"
        def validate(self):
            return None
    SourceConnection.rclone_config.write_text("[nextcloud]\ntype = local\n")
    class Shared:
        source_settings = {
            "webdav_url": "https://cloud.invalid/remote.php/dav/files/u/",
            "vault_folder": "Vault", "nextcloud_user": "u",
            "nextcloud_password": "SYNTHETIC-NC-PASSWORD",
        }
        def source_connections(self):
            return SourceConnection()
        def load_nextcloud(self):
            return dict(self.source_settings)
    shared = Shared()
    state = tmp_path / "source-state"
    state.mkdir(mode=0o700)
    calls = []
    def copier(source, target):
        calls.append(source.rclone_remote)
        path = target / "sub" / "startup.md"
        path.parent.mkdir(parents=True)
        path.write_bytes(RAW)
        return True
    reader = FormatterSource(state, shared, copier=copier)
    assert reader.configured()
    before = reader.generation()
    plan = reader()
    assert plan["counts"]["would_change"] == 1
    assert plan["mode"] == "dry-run"
    assert calls == ["nextcloud:Vault"]
    assert list(state.iterdir()) == []
    shared.source_settings["nextcloud_password"] = "CHANGED"
    assert reader.generation() != before
    assert list(state.iterdir()) == []


def test_formatter_source_no_remote_writer_commands(tmp_path, monkeypatch):
    class Source:
        rclone_remote = "nextcloud:Vault"
        rclone_config = tmp_path / "rclone.conf"
        def validate(self): pass
    Source.rclone_config.write_text("synthetic\n")
    destination = tmp_path / "98-System"
    destination.mkdir()
    calls = []
    def fake(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(subprocess, "run", fake)
    assert pull_system_markdown(Source(), destination) is True
    assert [row[3] for row in calls] == ["copy", "check"]
    assert not any(any(k in part for k in ("sync", "PUT", "delete", "upload"))
                   for row in calls for part in row)


@contextmanager
def served(job):
    server = make_server(port=0, formatter_job=job, observer=lambda: [],
                         ai_observer=lambda: {})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(4)
        server.server_close()


def request(server, method, path, payload=None, *, origin=None, csrf=None):
    port = server.server_address[1]
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    headers = {"Host":f"127.0.0.1:{port}"}
    if origin: headers["Origin"] = origin
    if csrf: headers["X-Obsidian-CSRF"] = csrf
    if payload is not None: headers["Content-Type"] = "application/json"
    try:
        client.request(method, path, body=json.dumps(payload) if payload is not None else None,
                       headers=headers)
        reply = client.getresponse()
        return reply.status, reply.read()
    finally:
        client.close()


def token(server):
    raw = request(server, "GET", "/api/v1/operation-session")[1]
    return json.loads(raw)["csrf_token"]


def test_formatter_http_status_csrf_mode_denial_and_manual_run(tmp_path):
    plan = prepared_plan(tmp_path)
    with new_job(tmp_path, lambda: plan) as job:
        with served(job) as server:
            port = server.server_address[1]
            origin = f"http://127.0.0.1:{port}"
            assert request(server, "GET", "/api/v1/status")[0] == 200
            assert request(server, "GET", "/api/v1/snapshot")[0] == 503
            assert request(server, "GET", "/api/v1/daily")[0] == 503
            code, raw = request(server, "GET", "/api/v1/formatter")
            data = json.loads(raw)
            assert code == 200
            assert data["settings"]["mode"] == "off"
            assert data["settings"]["write_capable"] is False
            assert "Vault" not in raw.decode("utf-8")
            settings = {"mode":"dry-run","interval_seconds":3600}
            assert request(server,"POST","/api/v1/formatter/settings",settings)[0] == 403
            assert request(server,"POST","/api/v1/formatter/settings",settings,
                           origin="http://attacker.invalid",csrf=token(server))[0] == 403
            assert request(server,"POST","/api/v1/formatter/settings",
                           {"mode":"auto-apply","interval_seconds":3600},
                           origin=origin,csrf=token(server))[0] == 400
            assert request(server,"POST","/api/v1/formatter/settings",settings,
                           origin=origin,csrf=token(server))[0] == 200
            assert job.status()["last_run"] is None
            assert request(server,"POST","/api/v1/formatter/runs",
                           {"request_id":"not-a-uuid"},origin=origin,csrf=token(server))[0] == 400
            id_ = uuid.uuid4().hex
            code, raw = request(server,"POST","/api/v1/formatter/runs",
                                {"request_id":id_},origin=origin,csrf=token(server))
            assert code == 202
            run_id = json.loads(raw)["run_id"]
            job._worker.join(4)
            code, raw = request(server,"GET","/api/v1/formatter/runs/"+run_id)
            assert code == 200 and json.loads(raw)["status"] == "would_change"
            assert request(server,"GET","/api/v1/formatter/runs/../../etc/passwd")[0] == 404
            assert request(server, "POST","/api/v1/formatter/runs",{"request_id":id_},
                           origin=origin,csrf=token(server))[0] == 202
            assert request(server,"POST","/api/v1/formatter/settings",
                           {"mode":"off","interval_seconds":3600},
                           origin=origin,csrf=token(server))[0] == 200
            assert job.status()["settings"]["mode"] == "off"
            assert "formatterRefresh" in JS
            assert "formatter-mode" in HTML
            assert 'option value="auto-apply" disabled' in HTML
            assert "innerHTML" not in JS


def test_formatter_http_unattached_is_disabled():
    with served(None) as server:
        assert request(server,"GET","/api/v1/formatter")[0] == 503
        assert request(server,"POST","/api/v1/formatter/settings",{})[0] in (403,503)


def test_untrusted_writer_complete_without_outcomes_is_indeterminate(tmp_path):
    plan = prepared_plan(tmp_path)
    writer = lambda p: {"status": "complete", "plan_sha256": p["plan_sha256"],
                        "outcomes": []}
    clock = [1000.]
    with new_job(tmp_path, lambda: plan, writer=writer, clock=clock) as job:
        job.update({"mode":"auto-apply","interval_seconds":300})
        clock[0] = 1300
        assert job.tick()
        job._worker.join(4)
        row = job.status()["last_run"]
        assert row["status"] == "indeterminate"
        assert row["reason_code"] == "writer_result_invalid"
        assert row["writer_outcome"] is None


def test_writer_partial_conflict_not_mislabeled_success(tmp_path):
    plan = prepared_plan(tmp_path)
    clock = [1000.]
    writer = lambda p: {
        "status": "partial", "plan_sha256": p["plan_sha256"],
        "outcomes": [{"result": "applied"}, {"result": "cas_conflict"}],
    }
    with new_job(tmp_path, lambda: plan, writer=writer, clock=clock) as job:
        job.update({"mode":"auto-apply","interval_seconds":300})
        clock[0] = 1300
        assert job.tick()
        job._worker.join(4)
        row = job.status()["last_run"]
        assert row["status"] == "conflict"
        assert row["reason_code"] == "writer_partial_conflict"
        assert row["writer_outcome"] == "partial"


def test_scheduled_crash_is_not_replayed_after_restart(tmp_path):
    plan = prepared_plan(tmp_path)
    clock = [1000.]
    with new_job(tmp_path, lambda: plan, clock=clock) as job:
        job.update({"mode":"dry-run","interval_seconds":300})
        clock[0] = 1300
        def crash_before_thread(run_id):
            assert job.settings()["next_due"] == 1600
            assert job.run(run_id)["status"] == "queued"
            raise RuntimeError("synthetic worker admission interruption")
        job._launch_worker = crash_before_thread
        with pytest.raises(RuntimeError, match="interruption"):
            job.tick()
        pending_id = job.status()["last_run"]["run_id"]
    with new_job(tmp_path, lambda: pytest.fail("should not automatically retry"),
                 clock=clock) as job:
        assert job.run(pending_id)["status"] == "unconfirmed"
        assert job.settings()["next_due"] == 1600
        assert job.tick() is False
        assert job.db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_fresh_single_app_attaches_formatter_but_does_not_schedule_work(
        tmp_path, monkeypatch):
    import sys
    from obsidian_automation import snapshot_app

    state = tmp_path / "fresh-runtime"
    observed = []
    class FakeServer:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def serve_forever(self, **_):
            assert self.snapshot_job.settings()["enabled"] is False
            assert self.daily_job.settings()["enabled"] is False
            formatter = self.formatter_job
            assert formatter.status()["settings"]["mode"] == "off"
            assert formatter.status()["settings"]["configured"] is False
            assert formatter.status()["settings"]["write_capable"] is False
            assert formatter.status()["last_run"] is None
            assert self.snapshot_job._scheduler is not None
            assert self.snapshot_job._tick_hooks == (self.daily_job.tick, formatter.tick)
            observed.append("single-scheduler-off")
    monkeypatch.setattr(snapshot_app, "make_server", lambda **kwargs: FakeServer())
    monkeypatch.setattr(
        sys, "argv", ["obsidian-snapshot-app", "--state", str(state), "--port","0"]
    )
    assert snapshot_app.main() == 0
    assert observed == ["single-scheduler-off"]
    assert (state / "vault-formatter/formatter.sqlite3").is_file()
    assert (state / "github-daily/daily.sqlite3").is_file()
    assert (state / "snapshot.sqlite3").is_file()
