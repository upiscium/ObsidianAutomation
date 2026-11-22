"""B4 Daily Preview job acceptance: durable, no effect, one in-app scheduler."""
from __future__ import annotations

import http.client
import json
import threading
import time
import uuid

import pytest

from obsidian_automation.github_daily_job import DailyJob, Busy, Invalid
from obsidian_automation.snapshot_job import SnapshotJob

SHA = "a" * 64
DATE = "2026-10-24"


def successful_preview():
    return dict(version=1, status="preview_ready", date_jst=DATE,
                capture_sha256=SHA, digest_sha256="b" * 64,
                original_event_count=18, rendered_entity_count=4,
                model_used=False, publication_authorized=False, vault_modified=False)


def test_daily_job_default_off_configuration_and_invalid_settings(tmp_path):
    state = tmp_path / "daily"
    with DailyJob(state, successful_preview) as job:
        assert job.status()["settings"] == {
            "enabled": False, "interval_seconds": 86400,
            "next_due": None, "configured": False}
        assert job.status()["last_run"] is None
        assert job.submit(uuid.uuid4().hex)[0] == "unconfigured"
        assert job.tick() is False
        with pytest.raises(Invalid):
            job.update({"enabled": True, "interval_seconds": 86400})
        for payload in ({"enabled": 1, "interval_seconds": 86400},
                        {"enabled": False, "interval_seconds": True},
                        {"enabled": False, "interval_seconds": 59},
                        {"enabled": False, "interval_seconds": 86401},
                        {"enabled": False, "interval_seconds": 3600, "other": "bad"}):
            with pytest.raises(Invalid):
                job.update(payload)
        assert job.update({"enabled": False,"interval_seconds": 3600})["enabled"] is False


def test_manual_busy_replay_single_owner_persistent_receipt(tmp_path):
    current = [1000.0]
    started, release = threading.Event(), threading.Event()
    calls = []
    def runner():
        started.set()
        calls.append("invoked")
        assert release.wait(3)
        return successful_preview()
    state = tmp_path / "daily"
    job = DailyJob(state,runner,configured=lambda: True,clock=lambda: current[0])
    try:
        with pytest.raises(Busy):
            DailyJob(state,runner,configured=lambda: True)
        first = uuid.uuid4().hex
        kind, receipt = job.submit(first)
        assert kind == "accepted" and started.wait(3)
        assert job.submit(first)[0] == "replayed"
        assert job.submit(first)[1]["run_id"] == receipt["run_id"]
        assert job.submit(uuid.uuid4().hex)[0] == "busy"
        assert job.settings()["enabled"] is False
        release.set()
        job._worker.join(3)
        assert job.run(receipt["run_id"])["status"] == "preview_ready"
        assert job.run(receipt["run_id"])["rendered_entity_count"] == 4
        assert job.run(receipt["run_id"])["digest_sha256"] == "b" * 64
        assert len(calls) == 1
        assert job.run("x" * 32) is None
        assert job.run("../../private") is None
    finally:
        release.set()
        job.close()
    with DailyJob(state,successful_preview,configured=lambda: True) as restored:
        assert restored.run(receipt["run_id"])["status"] == "preview_ready"
        with restored.db:
            restored.db.execute("UPDATE runs SET status='running' WHERE run_id=?",
                                (receipt["run_id"],))
    with DailyJob(state, successful_preview) as crash_recovery:
        assert crash_recovery.run(receipt["run_id"])["status"] == "unconfirmed"
        assert crash_recovery.settings()["enabled"] is False


def test_shared_scheduler_uses_one_tick_loop_and_off_manual(tmp_path):
    current = [1000.0]
    state = tmp_path / "daily"
    with DailyJob(state,successful_preview,configured=lambda: True,clock=lambda: current[0]) as daily:
        snapshot = SnapshotJob(tmp_path / "snapshot",
                               lambda _: ("no_changes", "verify", "c" * 40),
                               configured=False,clock=lambda: current[0])
        try:
            assert daily.update({"enabled": True,"interval_seconds": 3600})["next_due"] == 4600
            assert not daily.tick()
            current[0] = 4600
            assert daily.tick()
            daily._worker.join(3)
            assert daily.status()["last_run"]["status"] == "preview_ready"
            assert daily.status()["last_run"]["trigger"] == "scheduled"
            assert daily.status()["settings"]["next_due"] == 8200
            assert daily.update({"enabled": False,"interval_seconds": 3600})["next_due"] is None
            assert not daily.tick()
            assert daily.submit(uuid.uuid4().hex)[0] == "accepted"
            daily._worker.join(3)
            assert daily.status()["last_run"]["trigger"] == "manual"
            # Parent B2 loop ticks the B4 hook; B4 has no own scheduler.
            hook_called = threading.Event()
            snapshot.start_scheduler(tick_hooks=(lambda: hook_called.set(),))
            assert hook_called.wait(2.5)
            assert snapshot._scheduler is not None
            assert not hasattr(daily,"_scheduler")
        finally:
            snapshot.close()


@pytest.mark.parametrize("invalid_result", [
    {"version":1,"status":"saved"},
    {**successful_preview(),"status":"saved"},
    {**successful_preview(),"publication_authorized":True},
    {**successful_preview(),"vault_modified":True},
    {**successful_preview(),"capture_sha256":"wrong"},
    {**successful_preview(),"rendered_entity_count":True},
])
def test_untrusted_runner_claims_are_not_published(tmp_path,invalid_result):
    with DailyJob(tmp_path / "daily",lambda: invalid_result,configured=lambda: True) as job:
        kind, receipt = job.submit(uuid.uuid4().hex)
        assert kind == "accepted"
        job._worker.join(3)
        row = job.run(receipt["run_id"])
        assert row["status"] == "failed"
        assert row["capture_sha256"] is None
        assert row["digest_sha256"] is None


def test_runner_exception_does_not_leak_secret(tmp_path):
    def runner():
        raise RuntimeError("PRIVATE_GITHUB_TOKEN_EXAMPLE secret URL credential")
    with DailyJob(tmp_path / "daily", runner, configured=lambda: True) as job:
        kind, row = job.submit(uuid.uuid4().hex)
        assert kind == "accepted"
        job._worker.join(3)
        response = json.dumps(job.run(row["run_id"]))
        assert "PRIVATE_GITHUB" not in response
        assert "failed" in response
        assert job.submit(uuid.uuid4().hex)[0] == "accepted"







def test_fresh_snapshot_app_attaches_b4_unconfigured_without_starting_work(tmp_path, monkeypatch):
    """One app/scheduler owns both jobs; startup alone makes no provider call."""
    import sys
    from obsidian_automation import snapshot_app

    state = tmp_path / "fresh-one-user"
    observed = []

    class FakeServer:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def serve_forever(self, **_):
            assert self.snapshot_job.settings()["enabled"] is False
            assert self.daily_job.settings()["enabled"] is False
            assert self.daily_job.settings()["configured"] is False
            assert self.daily_job.status()["last_run"] is None
            assert self.snapshot_job._scheduler is not None
            assert not hasattr(self.daily_job,"_scheduler")
            observed.append("one_app")

    monkeypatch.setattr(snapshot_app,"make_server",lambda **kwargs: FakeServer())
    monkeypatch.setattr(sys,"argv",["obsidian-snapshot-app","--state",str(state),"--port","0"])
    assert snapshot_app.main() == 0
    assert observed == ["one_app"]
    assert (state / "snapshot.sqlite3").exists()
    assert (state / "github-daily" / "daily.sqlite3").exists()



def test_scheduled_crash_between_durable_admission_and_worker_start_never_replays(tmp_path):
    """Reproduce P2: deadline and queued record must commit together before launch."""
    current = [1000.0]
    state = tmp_path / "private-daily"
    with DailyJob(state, successful_preview, configured=lambda: True,
                  clock=lambda: current[0]) as job:
        job.update({"enabled": True, "interval_seconds": 3600})
        current[0] = 4600.0

        def die_before_worker(_run_id):
            settings = job.db.execute(
                "SELECT next_due FROM settings WHERE id=1").fetchone()
            pending = job.db.execute(
                "SELECT run_id,status FROM runs WHERE trigger='scheduled'").fetchall()
            assert settings[0] == 8200.0
            assert len(pending) == 1 and pending[0][1] == "queued"
            raise RuntimeError("simulated abrupt process loss after durable admission")

        job._launch_worker = die_before_worker
        with pytest.raises(RuntimeError, match="simulated abrupt"):
            job.tick()
        previous_id = job.status()["last_run"]["run_id"]
        assert job.status()["settings"]["next_due"] == 8200.0

    with DailyJob(state, successful_preview, configured=lambda: True,
                  clock=lambda: current[0]) as restarted:
        assert restarted.run(previous_id)["status"] == "unconfirmed"
        assert restarted.settings()["next_due"] == 8200.0
        assert restarted.tick() is False
        count = restarted.db.execute("SELECT count(*) FROM runs").fetchone()[0]
        assert count == 1
        # A human-initiated fresh request is still independent of the
        # indeterminate previously scheduled run.
        assert restarted.submit(uuid.uuid4().hex)[0] == "accepted"
        restarted._worker.join(3)


def test_scheduled_thread_launch_failure_has_durable_no_replay_result(tmp_path, monkeypatch):
    current = [1000.0]
    with DailyJob(tmp_path / "daily", successful_preview,configured=lambda: True,
                  clock=lambda: current[0]) as job:
        job.update({"enabled": True, "interval_seconds": 3600})
        current[0] = 4600.0

        def fail(_thread):
            raise RuntimeError("unable_to_start_worker")

        monkeypatch.setattr(threading.Thread, "start", fail)
        with pytest.raises(RuntimeError, match="unable_to_start_worker"):
            job.tick()
        assert job.status()["last_run"]["status"] == "unconfirmed"
        assert job.status()["settings"]["next_due"] == 8200.0
        assert job._worker is None


def test_daily_receipts_bind_current_nextcloud_source_not_just_configured(tmp_path):
    """A success on Vault A cannot become success on Vault B after GitHub token re-save."""
    generation = ["a" * 64]
    with DailyJob(
        tmp_path / "daily-source-bound",
        successful_preview,
        configured=lambda: True,
        source_generation=lambda: generation[0],
    ) as job:
        # No remote writer: source metadata only, inserted as synthetic
        # historical post-write evidence in the local isolated fixture.
        with job.db:
            job.db.execute(
                "INSERT INTO runs (run_id,request_id,trigger,status,created,"
                "source_generation,publication_outcome) VALUES (?,?,?,?,?,?,?)",
                ("1" * 32, "2" * 32, "scheduled", "published", 123,
                 generation[0], "applied"),
            )
        old = job.run("1" * 32)
        assert old["source_matches_current"] is True
        assert "source_generation" not in old
        generation[0] = "b" * 64
        # GitHub may be reconfigured and the whole service may report ready,
        # but no publication on the replacement Vault has occurred.
        assert job.settings()["configured"] is True
        assert job.run("1" * 32)["status"] == "published"
        assert job.run("1" * 32)["source_matches_current"] is False

        with job.db:
            job.db.execute(
                "INSERT INTO runs (run_id,request_id,trigger,status,created,"
                "source_generation) VALUES (?,?,?,?,?,?)",
                ("3" * 32, "4" * 32, "manual", "published", 124, None),
            )
        assert job.status()["last_run"]["source_matches_current"] is False


def test_daily_run_admission_stores_source_generation_without_exposing_it(tmp_path):
    generation = ["c" * 64]
    with DailyJob(
        tmp_path / "daily-new-binding", successful_preview,
        configured=lambda: True, source_generation=lambda: generation[0],
    ) as job:
        outcome, receipt = job.submit("5" * 32)
        assert outcome == "accepted"
        job._worker.join(timeout=3)
        run = job.run(receipt["run_id"])
        assert run["source_matches_current"] is True
        assert "source_generation" not in run
        generation[0] = "d" * 64
        assert job.run(receipt["run_id"])["source_matches_current"] is False


def test_daily_legacy_sqlite_run_migrates_as_historical_only(tmp_path):
    import sqlite3

    directory = tmp_path / "legacy-daily"
    directory.mkdir()
    db = sqlite3.connect(directory / "daily.sqlite3")
    try:
        db.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, "
            "request_id TEXT UNIQUE NOT NULL, trigger TEXT NOT NULL, "
            "status TEXT NOT NULL, created REAL NOT NULL, completed REAL, "
            "date_jst TEXT, capture_sha256 TEXT, digest_sha256 TEXT, "
            "original_event_count INTEGER, rendered_entity_count INTEGER)"
        )
        db.execute(
            "INSERT INTO runs (run_id,request_id,trigger,status,created) "
            "VALUES (?,?,?,?,?)",
            ("8" * 32, "9" * 32, "scheduled", "published", 123),
        )
        db.commit()
    finally:
        db.close()
    with DailyJob(directory, successful_preview, configured=lambda: True,
                  source_generation=lambda: "b"*64) as migrated:
        assert migrated.run("8" * 32)["status"] == "published"
        assert migrated.run("8" * 32)["source_matches_current"] is False
        assert "source_generation" in {
            row[1] for row in migrated.db.execute("PRAGMA table_info(runs)")
        }
