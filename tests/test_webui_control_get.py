"""Loopback-only read v1 Catalog route: no admission or runtime effects."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import http.client
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from obsidian_automation.admin_dashboard import make_server
from obsidian_automation.webui_control_read import compose_read_catalog
from obsidian_automation.webui_control_get import read_control_catalog
from obsidian_automation.webui_b1_adapter import B1ReadAdapter, LedgerState, PreparedBinding
from obsidian_automation.webui_b3_adapter import B3FakeAdapter, Binding, SyntheticLedger, SyntheticWriter
from obsidian_automation import webui_b5_ports as b5

_ID = "a" * 32


def test_w4a_fixed_assets_headers_and_head_have_no_job_reads_or_effects():
    from pathlib import Path
    assets = Path(__file__).resolve().parents[1] / "src/obsidian_automation/webui_design_v1"
    routes = {
        "/workflow-control": ("live.html", "text/html"),
        "/static/workflow-control/model.js": ("model.js", "text/javascript"),
        "/static/workflow-control/live.js": ("live.js", "text/javascript"),
        "/static/workflow-control/styles.css": ("styles.css", "text/css"),
    }
    b2, b4 = ReadOnlyJob(snapshot()), ReadOnlyJob(daily())
    with serving(b2, b4) as server:
        for route, (name, mime) in routes.items():
            for method in ("GET", "HEAD"):
                code, headers, body = request(server, route, method)
                assert code == 200
                assert body == ((assets / name).read_bytes() if method == "GET" else b"")
                assert int(headers["Content-Length"]) == (assets / name).stat().st_size
                assert headers["Content-Type"] == mime + "; charset=utf-8"
                assert headers["Cache-Control"] == "no-store"
                assert headers["X-Content-Type-Options"] == "nosniff"
                assert headers["X-Frame-Options"] == "DENY"
                assert headers["Referrer-Policy"] == "no-referrer"
                assert "connect-src 'self'" in headers["Content-Security-Policy"]
                assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
                assert "form-action 'none'" in headers["Content-Security-Policy"]
                assert "*" not in headers["Content-Security-Policy"]
                assert "Access-Control-Allow-Origin" not in headers
            assert request(server, route, host="example.invalid")[0] == 403
            assert request(server, route, origin="https://example.invalid")[0] == 403
            assert request(server, route, "HEAD", host="example.invalid")[0] == 403
            port = server.server_address[1]
            assert request(server, route, origin=f"http://127.0.0.1:{port}")[0] == 200
        assert b2.reads == b4.reads == b2.effects == b4.effects == 0
        assert b'href="/workflow-control"' in request(server, "/")[2]


def test_w4a_static_requests_never_access_runtime_or_mutation_ports():
    class ForbiddenPort:
        def __getattr__(self, name):
            raise AssertionError("runtime port accessed")
    with serving() as server:
        for name in ("snapshot_job", "daily_job", "formatter_job", "snapshot_setup",
                     "daily_setup", "operator", "preview_csrf", "status_singleflight",
                     "capabilities_singleflight"):
            setattr(server, name, ForbiddenPort())
        for path in ("/workflow-control", "/static/workflow-control/live.js",
                     "/static/workflow-control/model.js", "/static/workflow-control/styles.css"):
            for method in ("GET", "HEAD"):
                assert request(server, path, method)[0] == 200
            for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
                code, _, body = request(server, path, method)
                assert code == 405 and body == b"Method not allowed"


@pytest.mark.parametrize("path", [
    "//workflow-control", "///workflow-control", "//static/workflow-control/model.js",
    "/workflow-control/", "/workflow-control?x=1", "/workflow-control/live.html",
    "/static/workflow-control/", "/static/workflow-control/index.html",
    "/static/workflow-control/app.js", "/static/workflow-control/live.html",
    "/static/workflow-control/missing.js", "/static/workflow-control/model.js?x=1",
    "/static/workflow-control/../model.js", "/static/workflow-control/%2e%2e/model.js",
    "/static/workflow-control/%2fetc%2fpasswd", "/static/workflow-control//model.js",
    "/static/workflow-control/model.js/extra", "/static/workflow-control/MODEL.js",
    "/static/workflow-control/..%5cmodel.js", "/static/workflow-control/model.js%00",
])
def test_w4a_no_generic_static_or_demo_exposure(path):
    with serving() as server:
        for method in ("GET", "HEAD"):
            code, headers, body = request(server, path, method)
            assert code == 404
            assert body == (b"Not found" if method == "GET" else b"")
            assert headers["Cache-Control"] == "no-store"


def test_w4a_missing_packaged_resource_is_fixed_404(monkeypatch):
    def missing(*_args):
        raise FileNotFoundError("PRIVATE filesystem location")
    monkeypatch.setattr("obsidian_automation.admin_dashboard.files", missing)
    with serving() as server:
        assert request(server, "/workflow-control")[::2] == (404, b"Not found")


def test_w4a_existing_default_routes_still_disabled_without_attachment():
    with serving() as server:
        for route in ("/", "/static/style.css", "/static/app.js", "/api/v1/workflows",
                      "/api/v1/operator-ready", "/api/v1/operation-capabilities"):
            assert request(server, route)[0] == 200
        for route in ("/api/v1/snapshot", "/api/v1/daily", "/api/v1/formatter",
                      "/api/v1/nextcloud/connections", "/api/v1/snapshot/connections",
                      "/api/v1/daily/connections"):
            assert request(server, route)[0] == 503


def test_w4a_actual_http_catalog_through_live_async_dom():
    from pathlib import Path
    import shutil
    import subprocess
    runtime = shutil.which("node") or shutil.which("bun")
    if runtime is None:
        pytest.skip("Node/Bun unavailable; no dependency downloads")
    script = Path(__file__).with_name("webui_live_v1.test.cjs")
    with serving() as server:
        code, _, body = request(server)
        assert code == 200
    result = subprocess.run([runtime, str(script), "--catalog-stdin"],
                            input=body.decode(), text=True, capture_output=True,
                            timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "webui_live_v1: 13 checks passed" in result.stdout


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
def serving(snapshot_job=None, daily_job=None, sources=None, *, ports=None, formatter_job=None):
    server = make_server(
        port=0, observer=lambda: [], ai_observer=lambda: {},
        snapshot_job=snapshot_job, daily_job=daily_job,
        workflow_control_snapshots=sources,
        workflow_control_read_ports=ports, formatter_job=formatter_job,
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


def optional_ports():
    binding = Binding("b" * 32, "c" * 32, "PRIVATE_PROJECT", "PRIVATE_VAULT_PATH")
    writer = SyntheticWriter(binding)
    ledger = SyntheticLedger.fresh_fixture(binding, writer)
    return {"b1": B1ReadAdapter(None, clock=lambda: 10),
            "b3": B3FakeAdapter(ledger, read_only=True, observed_at=10),
            "b5": b5.ReadPort(None, observed_at=10)}, ledger, writer


def test_explicit_ports_are_lazy_copied_and_refreshed_for_list_and_all_details(monkeypatch):
    ports, ledger, writer = optional_ports()
    attached = dict(ports)
    calls = []
    forbidden = Mock(side_effect=AssertionError("effect or lookup forbidden"))
    for key, port in attached.items():
        original = port.observe

        def observe(key=key, original=original):
            calls.append(key)
            return original()

        monkeypatch.setattr(port, "observe", observe)
        for name in ("lookup", "generate", "sync_project", "stage_archive", "import_inventory",
                     "set_schedule", "submit", "tick", "transact", "lookup_staging", "detail"):
            monkeypatch.setattr(port, name, forbidden, raising=False)
    monkeypatch.setattr(writer, "_apply", forbidden)
    with serving(ports=ports) as server:
        assert calls == []  # Constructor never materializes.
        ports.clear()  # Caller mutation cannot detach the validated copy.
        first = json.loads(request(server)[2])["workflows"]
        assert calls == ["b1", "b3", "b5"]
        assert first[2]["adapterMode"] == "mock" and "mock" in first[2]["label"]
        assert first[2]["integration"] == "pending"
        ledger.lose_history()  # Explicit fixture fault, never GET recovery.
        for identifier in ("b1", "b2", "b3", "b4", "b5"):
            code, _, raw = request(server, f"/api/v1/workflow-control/{identifier}")
            assert code == 200
            value = json.loads(raw)
            assert value["id"] == identifier and value["availableActions"] == []
            assert value["lastVerifiedOutcome"] is None
            if identifier == "b3":
                assert value["integration"] == "unavailable"
            if identifier == "b5":
                assert value["schedule"]["reason"] == "manual_only"
            assert "PRIVATE_" not in raw.decode()
        latest = json.loads(request(server)[2])["workflows"]
        # Refresh observes the fixture fault, not a cached pending/mock snapshot.
        assert latest[2]["integration"] == "unavailable"
        assert calls == ["b1", "b3", "b5"] * 7
        assert request(server, "/api/v1/workflow-control/b3/requests/" + _ID)[0] == 503
        assert calls == ["b1", "b3", "b5"] * 7
    assert writer.calls == 0 and ledger._records == {}
    forbidden.assert_not_called()


def test_outer_time_is_sampled_after_optional_and_legacy_reads_on_every_get(monkeypatch):
    events = []
    now = [10]

    class Ledger:
        def read(self):
            events.append("b1")
            now[0] += 1
            return LedgerState("4" * 32, PreparedBinding("1" * 32, "2" * 32, "3" * 32))

        def transact(self, *_):
            pytest.fail("GET transaction")

    def outer_clock():
        events.append("outer")
        return now[0]

    class Legacy(ReadOnlyJob):
        def status(self):
            events.append("legacy")
            now[0] += 1
            return super().status()

    # B1 samples its own clock before its read. This remains source provenance,
    # not the outer request timestamp, and is preserved by the composer.
    port = B1ReadAdapter(Ledger(), clock=lambda: now[0])
    # Replace this module's clock dependency, not the process-wide time module
    # (HTTP Date headers also use time.time).
    monkeypatch.setattr("obsidian_automation.webui_control_get.time", SimpleNamespace(time=outer_clock))
    with serving(Legacy(snapshot()), ports={"b1": port}) as server:
        first = json.loads(request(server)[2])
        second = json.loads(request(server)[2])
    assert events == ["b1", "legacy", "outer"] * 2
    assert first["observedAt"] == 12 and second["observedAt"] == 14
    assert first["workflows"][0]["observedAt"] == 10
    assert second["workflows"][0]["observedAt"] == 12
    assert second["workflows"][0]["integration"] == "pending"


@pytest.mark.parametrize("malformed", [False, True])
def test_optional_http_failure_or_malformed_dto_isolated_and_secret_redacted(monkeypatch, malformed):
    ports, _, _ = optional_ports()

    def observe():
        if malformed:
            return {"secret": "PRIVATE_TOKEN", "path": "/private/vault"}
        raise RuntimeError("PRIVATE_TOKEN credentials /private/vault")

    monkeypatch.setattr(ports["b1"], "observe", observe)
    with serving(ReadOnlyJob(snapshot()), ReadOnlyJob(daily()), ports=ports) as server:
        code, _, raw = request(server)
        assert code == 200
        values = json.loads(raw)["workflows"]
        assert [w["integration"] for w in values] == [
            "unavailable", "pending", "pending", "pending", "pending"]
        assert values[2]["adapterMode"] == "mock" and "mock" in values[2]["label"]
        assert values[0]["execution"]["state"] == "unknown"
        assert all(w["availableActions"] == [] for w in values)
        assert "PRIVATE_TOKEN" not in raw.decode() and "/private/vault" not in raw.decode()


def test_invalid_injection_and_conflicting_sources_fail_before_server_bind(monkeypatch):
    constructor = Mock(side_effect=AssertionError("server must not be constructed"))
    monkeypatch.setattr("obsidian_automation.admin_dashboard.BoundedDashboardServer", constructor)

    class Trap:
        def __getattr__(self, _name):
            pytest.fail("invalid object method lookup")

    for ports in ({"b1": Trap()}, {"b2": Trap()}, [], {"b5": Trap()}):
        with pytest.raises(TypeError, match="^invalid_workflow_control_read_ports$"):
            make_server(port=0, workflow_control_read_ports=ports)
    for snapshots, ports in (({}, {}), ({"b3": valid_b3_mock()}, {}), ({}, optional_ports()[0])):
        with pytest.raises(ValueError, match="^conflicting_workflow_control_sources$"):
            make_server(port=0, workflow_control_snapshots=snapshots, workflow_control_read_ports=ports)
    constructor.assert_not_called()


def test_optional_ports_not_observed_for_static_head_other_get_post_or_rejected_requests(monkeypatch):
    ports, _, writer = optional_ports()
    observe = Mock(side_effect=AssertionError("optional observation forbidden on this route"))
    for port in ports.values():
        monkeypatch.setattr(port, "observe", observe)
    snap, daily_job = ReadOnlyJob(snapshot()), ReadOnlyJob(daily())
    formatter = ReadOnlyJob({"synthetic": "formatter unchanged"})
    with serving(snap, daily_job, ports=ports, formatter_job=formatter) as server:
        for route in ("/workflow-control", "/static/workflow-control/live.js",
                      "/static/workflow-control/model.js", "/static/workflow-control/styles.css",
                      "/", "/static/style.css", "/static/app.js"):
            for method in ("GET", "HEAD"):
                assert request(server, route, method)[0] == 200
        for route, expected in (("/api/v1/snapshot", snapshot()), ("/api/v1/daily", daily()),
                                ("/api/v1/formatter", formatter.result)):
            code, _, raw = request(server, route)
            assert code == 200 and json.loads(raw) == expected
        assert json.loads(request(server, "/api/v1/workflows")[2])["version"] == 1
        for route in ("/api/v1/workflow-control", "/api/v1/workflow-control/b3"):
            assert request(server, route, "HEAD")[0] == 200
            assert request(server, route, host="example.invalid")[0] == 403
            assert request(server, route, origin="https://example.invalid")[0] == 403
            for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
                assert request(server, route, method)[::2] == (405, b"Method not allowed")
        for route in ("/api/v1/workflow-control/b3/syncs", "/api/v1/workflow-control/b5/imports",
                      "/api/v1/workflow-control/b1/generations", "/api/v1/workflow-control/b5/uploads"):
            assert request(server, route, "POST")[0] == 405
            assert request(server, route)[0] == 404
        # Existing mutation guards still reject before any legacy job effect.
        for route in ("/api/v1/snapshot/runs", "/api/v1/daily/runs", "/api/v1/formatter/runs"):
            assert request(server, route, "POST")[0] == 403
            origin = f"http://127.0.0.1:{server.server_address[1]}"
            assert request(server, route, "POST", origin=origin)[0] == 403
        assert request(server, "/api/v1/workflow-control/b6")[0] == 404
        assert request(server, "/api/v1/workflow-control/b3/requests/" + _ID)[0] == 503
    observe.assert_not_called()
    assert snap.effects == daily_job.effects == formatter.effects == writer.calls == 0
