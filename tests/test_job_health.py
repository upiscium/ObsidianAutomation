import json
import sqlite3
import threading
import uuid

import pytest

from obsidian_automation.job_health import aggregate, job_health
from obsidian_automation.snapshot_job import SnapshotJob
from obsidian_automation.three_services_server import make_server
from test_three_services_server import request

OK = ("saved", "no_changes")
BAD = ("failed", "unconfirmed")


def table(rows):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE runs (run_id TEXT, status TEXT, created REAL, completed REAL)")
    for index, (status, completed) in enumerate(rows, 1):
        db.execute("INSERT INTO runs VALUES (?,?,?,?)", (str(index), status, float(index), completed))
    return db


def health(rows, **kwargs):
    kwargs.setdefault("threshold", 3)
    return job_health(table(rows), threading.RLock(), service="x", success=OK, failure=BAD, **kwargs)


def test_no_runs_is_healthy():
    assert health([]) == {"service": "x", "consecutive_failures": 0, "failing_since": None,
                          "last_success": None, "threshold": 3, "alert": False}


def test_a_streak_counts_from_the_newest_run_and_alerts_at_the_threshold():
    rows = [("saved", 10.0), ("failed", 11.0), ("failed", 12.0), ("unconfirmed", 13.0)]
    result = health(rows)
    assert (result["consecutive_failures"], result["alert"]) == (3, True)
    assert result["failing_since"] == 2.0 and result["last_success"] == 10.0
    assert health(rows[:3])["alert"] is False  # two failures, threshold three


def test_a_success_ends_the_streak():
    result = health([("failed", 1.0), ("failed", 2.0), ("failed", 3.0), ("no_changes", 4.0)])
    assert (result["consecutive_failures"], result["alert"]) == (0, False)
    assert result["failing_since"] is None and result["last_success"] == 4.0


def test_active_and_neutral_and_unknown_runs_do_not_break_or_extend_a_streak():
    rows = [("failed", 1.0), ("failed", 2.0), ("skipped", 3.0), ("running", None),
            ("queued", None), ("failed", 4.0), ("mystery", 5.0)]
    result = health(rows, neutral=("skipped",))
    assert result["consecutive_failures"] == 3 and result["alert"] is True


def test_a_never_successful_service_still_alerts_and_has_no_last_success():
    result = health([("failed", 1.0)] * 3)
    assert result["alert"] is True and result["last_success"] is None


def test_the_threshold_is_validated():
    for bad in (0, -1, True, "3", None):
        with pytest.raises(ValueError):
            health([], threshold=bad)


def test_aggregate_skips_missing_services_and_isolates_a_broken_one():
    def boom():
        raise RuntimeError("secret path /vault/x")

    result = aggregate([("a", None), ("b", lambda: {"service": "b", "alert": True}), ("c", boom)])
    assert [item["service"] for item in result["services"]] == ["b", "c"]
    assert result["alert"] is True
    assert result["services"][1]["unavailable"] is True
    assert "secret" not in json.dumps(result)


def test_a_snapshot_job_reports_its_own_streak(tmp_path):
    outcomes = iter([("failed", "snapshot", None)] * 3 + [("saved", "verify", "a" * 40)])
    job = SnapshotJob(tmp_path, lambda run_id: next(outcomes))
    try:
        seen = []
        for _ in range(4):
            assert job.submit(uuid.uuid4().hex)[0] == "accepted"
            job._worker.join(5)
            seen.append(job.health())
        assert [item["consecutive_failures"] for item in seen] == [1, 2, 3, 0]
        assert [item["alert"] for item in seen] == [False, False, True, False]
        assert seen[2]["service"] == "snapshot" and seen[2]["threshold"] == 3
    finally:
        job.close()


def test_the_health_route_lists_only_enabled_services_and_is_read_only(tmp_path):
    server = make_server(port=0, bind_address="127.0.0.1")
    failing = iter([("failed", "sync", None)] * 3)
    job = SnapshotJob(tmp_path, lambda run_id: next(failing))
    server.snapshot_job = job
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        code, body = request(server.server_port, "GET", "/api/v1/health")
        data = json.loads(body)
        assert code == 200 and [s["service"] for s in data["services"]] == ["snapshot"]
        assert data["alert"] is False
        for _ in range(3):
            job.submit(uuid.uuid4().hex)
            job._worker.join(5)
        data = json.loads(request(server.server_port, "GET", "/api/v1/health")[1])
        assert data["alert"] is True and data["services"][0]["consecutive_failures"] == 3
        assert set(data["services"][0]) == {"service", "consecutive_failures", "failing_since",
                                            "last_success", "threshold", "alert"}
        assert request(server.server_port, "POST", "/api/v1/health", payload={})[0] == 405
        assert request(server.server_port, "GET", "/static/health.js")[0] == 200
        assert b"/static/health.js" in request(server.server_port, "GET", "/")[1]
    finally:
        server.shutdown()
        server.server_close()
        job.close()


def test_daily_and_core_promotion_expose_their_own_service_name_and_threshold(tmp_path):
    from obsidian_automation.core_promotion_job import CorePromotionJob
    from obsidian_automation.github_daily_job import DailyJob
    daily = DailyJob(tmp_path / "daily", lambda: {}, configured=lambda: False)
    core = CorePromotionJob(tmp_path / "core", lambda run_id: None,
                            configured=lambda: False, source_generation=lambda: None)
    try:
        for job, name, threshold in ((daily, "daily", 2), (core, "core_promotion", 3)):
            result = job.health()
            assert (result["service"], result["threshold"]) == (name, threshold)
            assert (result["consecutive_failures"], result["alert"]) == (0, False)
    finally:
        daily.close()
        core.close()
