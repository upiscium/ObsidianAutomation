import http.client
import json
import threading
import uuid

import pytest

from obsidian_automation.snapshot_job import Busy, Invalid, SnapshotJob


def test_settings_tick_manual_restart_and_owner(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []

    def runner(run_id):
        calls.append(run_id)
        started.set()
        assert release.wait(3)
        return "no_changes", "verify", "a" * 40

    job = SnapshotJob(tmp_path, runner, clock=lambda: 1000)
    try:
        assert job.settings()["enabled"] is False
        assert not job.tick()
        with pytest.raises(Invalid):
            job.update(dict(enabled=True, interval_seconds=True, source_name="nextcloud", destination_name="gitea"))
        with pytest.raises(Busy):
            SnapshotJob(tmp_path, runner)
        job.update(dict(enabled=True, interval_seconds=60, source_name="nextcloud", destination_name="gitea"))
        assert not job.tick()
        request = uuid.uuid4().hex
        kind, receipt = job.submit(request)
        assert kind == "accepted" and started.wait(3)
        assert job.submit(request)[1]["run_id"] == receipt["run_id"]
        assert job.submit(uuid.uuid4().hex)[0] == "busy"
        job.update(dict(enabled=False, interval_seconds=60, source_name="nextcloud", destination_name="gitea"))
        assert not job.tick()
        release.set()
        job._worker.join(3)
        assert job.run(receipt["run_id"])["status"] == "no_changes"
        assert len(calls) == 1
        assert job.submit(uuid.uuid4().hex)[0] == "accepted"  # manual while Off
        job._worker.join(3)
    finally:
        release.set()
        job.close()
    restarted = SnapshotJob(tmp_path, runner)
    try:
        assert restarted.run(receipt["run_id"])["status"] == "no_changes"
        with restarted.db:
            restarted.db.execute("UPDATE runs SET status='running' WHERE run_id=?", (receipt["run_id"],))
    finally:
        restarted.close()
    recovered = SnapshotJob(tmp_path, runner)
    try:
        assert recovered.run(receipt["run_id"])["status"] == "unconfirmed"
    finally:
        recovered.close()




def test_due_tick_and_off_does_not_cancel(tmp_path):
    current = [1000.0]
    started, release = threading.Event(), threading.Event()

    def runner(_):
        started.set()
        assert release.wait(3)
        return "no_changes", "verify", "a" * 40

    job = SnapshotJob(tmp_path, runner, clock=lambda: current[0])
    try:
        job.update(dict(enabled=True, interval_seconds=60, source_name="nextcloud", destination_name="gitea"))
        assert job.settings()["next_due"] == 1060
        current[0] = 1060
        assert job.tick() and started.wait(3)
        assert not job.tick()
        job.update(dict(enabled=False, interval_seconds=60, source_name="nextcloud", destination_name="gitea"))
        assert job.status()["last_run"]["status"] == "running"
        release.set()
        job._worker.join(3)
        assert job.status()["last_run"]["status"] == "no_changes"
    finally:
        release.set()
        job.close()


def test_runner_failure_is_sanitized(tmp_path):
    def runner(_):
        raise RuntimeError("token=PRIVATE vault text /home/private")

    job = SnapshotJob(tmp_path, runner)
    try:
        _, receipt = job.submit(uuid.uuid4().hex)
        job._worker.join(3)
        result = json.dumps(job.run(receipt["run_id"]))
        assert '"status": "unconfirmed"' in result
        assert "PRIVATE" not in result and "/home/private" not in result
    finally:
        job.close()
