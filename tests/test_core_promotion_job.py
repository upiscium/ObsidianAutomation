import sqlite3
import threading

import pytest

from obsidian_automation.core_promotion_job import CorePromotionJob, Invalid

COMMIT = "a" * 40
HEAD = "b" * 40
DIGEST = "c" * 64
RECEIPT = "d" * 64


def sample(status="promoted"):
    return {
        "status": status,
        "base_commit": COMMIT,
        "head_commit": HEAD,
        "plan_sha256": DIGEST if status == "promoted" else None,
        "receipt_sha256": RECEIPT if status == "promoted" else None,
    }


def create(tmp_path, *, runner=None, configured=lambda: True,
           source_generation=lambda: "e" * 64, clock=lambda: 100):
    return CorePromotionJob(
        tmp_path / "promotion-job", runner or (lambda: sample()),
        configured=configured, source_generation=source_generation, clock=clock,
    )


def test_new_install_off_by_default_and_manual_verified(tmp_path):
    with create(tmp_path) as job:
        assert job.settings() == {
            "enabled": False, "interval_seconds": 3600,
            "next_due": None, "configured": True, "source_bound": True
        }
        outcome, receipt = job.submit("1" * 32)
        assert outcome == "accepted"
        job._worker.join()
        run = job.run(receipt["run_id"])
        assert run["status"] == "promoted"
        assert run["receipt_sha256"] == RECEIPT
        assert job.status()["job"] == "core_promotion"


def test_duplicate_request_does_not_run_twice(tmp_path):
    calls = []
    def runner():
        calls.append(1)
        return sample("up_to_date")
    with create(tmp_path, runner=runner) as job:
        first, receipt = job.submit("2" * 32)
        job._worker.join()
        second, repeated = job.submit("2" * 32)
        assert (first, second) == ("accepted", "replayed")
        assert repeated["run_id"] == receipt["run_id"]
        assert calls == [1]


def test_busy_request_does_not_launch_second_writer(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    def runner():
        entered.set()
        assert release.wait(3)
        return sample()
    with create(tmp_path, runner=runner) as job:
        assert job.submit("3" * 32)[0] == "accepted"
        assert entered.wait(3)
        try:
            assert job.submit("4" * 32) == ("busy", None)
        finally:
            release.set()
        job._worker.join()


def test_source_rotation_invalidates_scheduled_authority(tmp_path):
    generation = ["e" * 64]
    calls = []
    with create(tmp_path, source_generation=lambda: generation[0],
                runner=lambda: calls.append(1) or sample()) as job:
        job.update({"enabled": True, "interval_seconds": 60})
        generation[0] = "f" * 64
        assert job.settings()["source_bound"] is False
        assert job.tick() is False
        assert job.submit("5" * 32) == ("unconfigured", None)
        assert calls == []


def test_restart_marks_inflight_indeterminate_without_replaying(tmp_path):
    with create(tmp_path) as job:
        with job.db:
            job.db.execute("INSERT INTO runs "
                           "(run_id,request_id,trigger,status,created) "
                           "VALUES (?,?,?,?,?)",
                           ("6" * 32, "7" * 32, "scheduled", "running", 1))
    calls = []
    with create(tmp_path, runner=lambda: calls.append(1) or sample()) as job:
        assert job.run("6" * 32)["status"] == "indeterminate"
        assert job.submit("a" * 32) == ("needs_reconciliation", None)
        with pytest.raises(Invalid):
            job.update({"enabled": True, "interval_seconds": 3600})
        assert calls == []


def test_exception_after_unknown_effect_not_reported_success(tmp_path):
    def crash():
        raise OSError("do not disclose raw remote details")
    with create(tmp_path, runner=crash) as job:
        outcome, run = job.submit("8" * 32)
        assert outcome == "accepted"
        job._worker.join()
        assert job.run(run["run_id"])["status"] == "indeterminate"


def test_bad_settings_rejected_and_disconnected_job_cannot_enable(tmp_path):
    with create(tmp_path, configured=lambda: False) as job:
        with pytest.raises(Invalid):
            job.update({"enabled": True, "interval_seconds": 3600})
        with pytest.raises(Invalid):
            job.update({"enabled": "yes", "interval_seconds": 60})
        assert job.submit("9" * 32) == ("unconfigured", None)
