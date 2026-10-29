"""Loopback-only read v1 Catalog route: no admission or runtime effects."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import http.client
import json
import threading

import pytest

from obsidian_automation.admin_dashboard import make_server
from obsidian_automation.webui_control_read import compose_read_catalog
from obsidian_automation.webui_control_get import read_control_catalog

_ID = "a" * 32


def snapshot():
    return {"version": 1, "job": "vault_snapshot",
            "settings": {"version": 1, "configured": True, "enabled": True,
                         "interval_seconds": 300, "next_due": 4000000000,
                         "source_name": "nextcloud", "destination_name": "gitea",
                         "gitea_write": "verified"},
            "last_run": None}


def daily():
    return {"version": 1, "job": "github_daily_preview",
            "publication_enabled": True,
            "settings": {"configured": True, "enabled": True,
                         "interval_seconds": 3600, "next_due": 4000000000},
            "last_run": None}


class ReadOnlyJob:
    def __init__(self, result):
        self.result = result
        self.reads = 0
        self.effects = 0

    def status(self):
        self.reads += 1
        if isinstance(self.result, Exception):
            raise self.result
        return deepcopy(self.result)

    def submit(self, *_args, **_kwargs):
        self.effects += 1
        raise AssertionError("unexpected job submission")

    def run(self, *_args, **_kwargs):
        self.effects += 1
        raise AssertionError("unexpected receipt lookup")

    def tick(self, *_args, **_kwargs):
        self.effects += 1
        raise AssertionError("unexpected scheduler tick")

    def update(self, *_args, **_kwargs):
        self.effects += 1
        raise AssertionError("unexpected settings change")


@contextmanager
def serving(snapshot_job=None, daily_job=None, sources=None):
    server = make_server(
        port=0, observer=lambda: [], ai_observer=lambda: {},
        snapshot_job=snapshot_job, daily_job=daily_job,
        workflow_control_snapshots=sources,
    )
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def request(server, path="/api/v1/workflow-control", method="GET",
            *, host=None, origin=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {"Host": host if host is not None else f"127.0.0.1:{port}"}
    if origin is not None:
        headers["Origin"] = origin
    try:
        conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def valid_b3_mock():
    item = compose_read_catalog({}, 1.0)["workflows"][2]
    item.update(adapterMode="mock", integration="pending",
                label="Project data into Vault (mock)", revision="b" * 32)
    item["configuration"] = {"state": "missing", "reason": "configuration_missing"}
    item["schedule"] = {"state": "unknown", "intervalSeconds": None,
                        "nextDueAt": None, "reason": "scheduler_decision_required"}
    item["actionReasons"] = {"sync_project": "mock_only",
                             "set_schedule": "scheduler_decision_required"}
    return item


def test_missing_attachments_show_neutral_five_without_actions():
    with serving() as server:
        code, headers, raw = request(server)
        assert code == 200
        data = json.loads(raw)
        assert set(data) == {"schemaVersion", "authority", "observedAt", "workflows"}
        assert data["schemaVersion"] == "webui-workflow-control/v1"
        assert data["authority"] == "observation_only"
        assert [v["id"] for v in data["workflows"]] == ["b1", "b2", "b3", "b4", "b5"]
        assert [v["integration"] for v in data["workflows"]] == [
            "pending", "not_attached", "pending", "not_attached", "pending"]
        assert all(not v["availableActions"] and v["adapterMode"] == "read_only"
                   for v in data["workflows"])
        assert data["workflows"][4]["schedule"]["state"] == "not_applicable"
        assert headers["Cache-Control"] == "no-store"
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "Access-Control-Allow-Origin" not in headers


def test_existing_jobs_are_read_only_and_legacy_routes_preserved():
    b2, b4 = ReadOnlyJob(snapshot()), ReadOnlyJob(daily())
    with serving(b2, b4) as server:
        code, _, body = request(server)
        assert code == 200
        result = json.loads(body)["workflows"]
        assert [v["integration"] for v in result] == [
            "pending", "pending", "pending", "pending", "pending"]
        assert result[1]["configuration"]["state"] == "configured"
        assert result[3]["configuration"]["state"] == "configured"
        assert result[1]["schedule"]["state"] == "enabled"
        assert result[3]["schedule"]["state"] == "enabled"
        assert result[1]["lastVerifiedOutcome"] is None
        assert result[3]["lastVerifiedOutcome"] is None
        assert result[1]["availableActions"] == result[3]["availableActions"] == []
        assert request(server, path="/api/v1/workflows")[0] == 200
        assert json.loads(request(server, path="/api/v1/workflows")[2])["version"] == 1
        assert request(server, path="/api/v1/formatter")[0] == 503
        for job in (b2, b4):
            assert job.reads >= 1
            assert job.effects == 0


def test_one_legacy_status_failure_is_unavailable_not_an_empty_success():
    b2 = ReadOnlyJob(RuntimeError("PRIVATE_CANARY_TOKEN"))
    b4 = ReadOnlyJob(daily())
    with serving(b2, b4) as server:
        code, _, body = request(server)
        assert code == 200
        workflows = json.loads(body)["workflows"]
        assert workflows[1]["integration"] == "unavailable"
        assert workflows[3]["integration"] == "pending"
        assert "PRIVATE_CANARY_TOKEN" not in body.decode()
        assert b2.effects == b4.effects == 0


def test_attached_none_status_is_unavailable_not_missing_owner():
    b2, b4 = ReadOnlyJob(None), ReadOnlyJob(daily())
    with serving(b2, b4) as server:
        code, _, body = request(server)
        assert code == 200
        workflows = json.loads(body)["workflows"]
        assert workflows[1]["integration"] == "unavailable"
        assert workflows[3]["integration"] == "pending"
        assert b2.reads == b4.reads == 1
        assert b2.effects == b4.effects == 0


def test_detail_routes_and_indeterminate_same_id_lookup_only():
    with serving(ReadOnlyJob(snapshot()), ReadOnlyJob(daily())) as server:
        for name in ("b1", "b2", "b3", "b4", "b5"):
            code, _, body = request(server, f"/api/v1/workflow-control/{name}")
            assert code == 200
            value = json.loads(body)
            assert value["id"] == name
            assert value["availableActions"] == []
        code, _, body = request(server, f"/api/v1/workflow-control/b2/requests/{_ID}")
        assert code == 503
        assert json.loads(body) == {
            "schemaVersion": "webui-workflow-control/v1",
            "workflowId": "b2", "requestId": _ID, "decision": "indeterminate",
            "receipt": None, "error": {"code": "admission_unknown", "retry": "lookup"},
        }


@pytest.mark.parametrize("path", [
    "/api/v1/workflow-control/", "/api/v1/workflow-control/b6",
    "/api/v1/workflow-control/b2?x=1",
    "/api/v1/workflow-control/b3/../b4",
    "/api/v1/workflow-control/b2/requests/invalid",
    "/api/v1/workflow-control/b2/requests/" + "A" * 32,
    "/api/v1/workflow-control/b2/requests/" + _ID + "/extra",
])
def test_unknown_and_malformed_paths_are_404(path):
    with serving() as server:
        assert request(server, path=path)[0] == 404


def test_post_and_other_mutation_verbs_never_acquire_control():
    b2, b4 = ReadOnlyJob(snapshot()), ReadOnlyJob(daily())
    with serving(b2, b4) as server:
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            code, _, raw = request(server, method=method)
            assert code == 405
            assert raw == b"Method not allowed"
        assert b2.reads == b4.reads == 0
        assert b2.effects == b4.effects == 0


def test_host_origin_rejections_and_head_do_not_grant_access():
    b2 = ReadOnlyJob(snapshot())
    with serving(b2, ReadOnlyJob(daily())) as server:
        port = server.server_address[1]
        assert request(server, host="example.invalid")[0] == 403
        assert request(server, origin="https://example.invalid")[0] == 403
        assert request(server, host=f"127.0.0.1:{port}",
                       origin=f"http://127.0.0.1:{port}")[0] == 200
        code, headers, raw = request(server, method="HEAD")
        assert code == 200 and raw == b""
        assert int(headers["Content-Length"]) > 0
        assert b2.effects == 0


def test_optional_mock_observation_only_and_no_status_calls_for_absence():
    sample = valid_b3_mock()
    assert compose_read_catalog({"b3": sample}, 2.0)["workflows"][2]["integration"] == "pending"
    with serving(sources={"b3": sample}) as server:
        code, _, raw = request(server)
        assert code == 200
        workflows = json.loads(raw)["workflows"]
        assert workflows[2]["integration"] == "pending"
        assert workflows[2]["adapterMode"] == "mock"
        assert workflows[2]["availableActions"] == []
        assert workflows[1]["integration"] == "not_attached"
        assert workflows[3]["integration"] == "not_attached"
    assert sample["availableActions"] == []


def test_optional_source_scope_cannot_override_b2_or_b4():
    with serving(ReadOnlyJob(snapshot()), ReadOnlyJob(daily()),
                 sources={"b2": {"availableActions": ["run_once"]}}) as server:
        code, _, raw = request(server)
        assert code == 200
        workflows = json.loads(raw)["workflows"]
        assert workflows[1]["integration"] == "pending"
        assert workflows[3]["integration"] == "pending"
        assert workflows[0]["integration"] == "unavailable"
        assert workflows[2]["integration"] == "unavailable"
        assert workflows[4]["integration"] == "unavailable"


def test_attached_status_get_does_not_execute_any_other_job_method():
    b2, b4 = ReadOnlyJob(snapshot()), ReadOnlyJob(daily())
    with serving(b2, b4) as server:
        for _ in range(6):
            assert request(server)[0] == 200
        assert b2.reads == b4.reads == 6
        assert b2.effects == b4.effects == 0


def test_existing_real_jobs_are_only_read_and_never_scheduled(tmp_path):
    # These are synthetic jobs owned by the test, not production state.
    # Construct them outside GET, then assert the new route does not change
    # their SQLite connections or begin any worker/scheduler activity.
    from obsidian_automation.snapshot_job import SnapshotJob
    from obsidian_automation.github_daily_job import DailyJob

    def forbidden_runner(*_args):
        raise AssertionError("GET must never execute a runner")

    snap = SnapshotJob(tmp_path / "snapshot", forbidden_runner, clock=lambda: 100.0)
    daily_job = DailyJob(tmp_path / "daily", forbidden_runner,
                         configured=lambda: True, clock=lambda: 100.0)
    try:
        before_snapshot = snap.db.total_changes
        before_daily = daily_job.db.total_changes
        with serving(snap, daily_job) as server:
            for _ in range(3):
                code, _, body = request(server)
                assert code == 200
                views = json.loads(body)["workflows"]
                assert views[1]["integration"] == "pending"
                assert views[3]["integration"] == "pending"
                assert views[1]["availableActions"] == views[3]["availableActions"] == []
        assert snap.db.total_changes == before_snapshot
        assert daily_job.db.total_changes == before_daily
        assert snap.db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert daily_job.db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert snap._worker is None and snap._scheduler is None
        assert daily_job._worker is None
    finally:
        daily_job.close()
        snap.close()


def test_read_catalog_is_not_a_constructor_and_opaque_source_hooks_not_called():
    class Trap:
        def __getattr__(self, _name):
            raise AssertionError("must never look up invalid optional object")
        def __iter__(self):
            raise AssertionError("must never iterate invalid optional object")
    result = read_control_catalog(
        snapshot_job=None, daily_job=None, observed_at=100,
        optional_snapshots={"b3": Trap()})
    assert result["workflows"][2]["integration"] == "unavailable"
    assert result["workflows"][1]["integration"] == "not_attached"
    assert result["workflows"][4]["schedule"]["state"] == "not_applicable"
