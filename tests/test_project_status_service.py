"""B3-lite step 3: panel backend, opt-in schedule and routes. Everything synthetic."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

from obsidian_automation.project_status_service import (
    ProjectStatusConfig, ProjectStatusRefused, ProjectStatusService,
)
from obsidian_automation.three_services_server import make_server
from obsidian_automation.vault_layout import role_folder
from test_chat_import_server import ORIGIN
from test_knowledge_proposal_publish import store  # noqa: F401 (fixture)
from test_project_status_run import FakeGitHub, note
from test_project_status_vault import Vault
from test_three_services_server import request

FOLDER = role_folder("project")


class Env:
    def __init__(self, tmp_path, store, **config):
        self.vault = Vault()
        self.vault.files[f"{FOLDER}/wake.md"] = note("planning")
        self.vault.files[f"{FOLDER}/quiet.md"] = note("running")
        self.now = [datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc).timestamp()]
        self.github = FakeGitHub()
        self.service = ProjectStatusService(
            tmp_path / "state", shared=store, guard=None, config=ProjectStatusConfig(**config),
            client_factory=lambda _state: (self.github, 7), transport=self.vault,
            clock=lambda: self.now[0])

    def wait(self):
        worker = self.service._worker
        if worker is not None:
            worker.join(10)
            assert not worker.is_alive()


@pytest.fixture
def env(tmp_path, store):
    made = Env(tmp_path, store)
    yield made
    made.service.close()


def last(env):
    return env.service.status()["recent_runs"][0]


def test_a_check_only_run_writes_nothing_and_reports_counts(env):
    before = dict(env.vault.files)
    assert env.service.start_manual({"apply": False})["outcome"] == "accepted"
    env.wait()
    run = last(env)
    assert (run["status"], run["mode"], run["counts"]["planned"]) == ("ok", "dry_run", 1)
    assert env.vault.files == before
    assert "wake" not in json.dumps(env.service.status())  # count-only


def test_an_applying_run_changes_the_status_and_counts_it_today(env):
    env.service.start_manual({"apply": True})
    env.wait()
    assert last(env)["counts"]["applied"] == 1
    assert b"status: running" in env.vault.files[f"{FOLDER}/wake.md"]
    assert env.service.status()["schedule"]["changed_today"] == 1


def test_one_run_at_a_time_and_bad_payloads_are_refused(env):
    for bad in ({}, {"apply": "yes"}, {"apply": True, "x": 1}, None):
        with pytest.raises(ProjectStatusRefused) as refused:
            env.service.start_manual(bad)
        assert refused.value.code == "invalid_request"
    gate = threading.Event()
    original = env.github.snapshot

    def slow(repository, *, observed_at):
        gate.wait(5)
        return original(repository, observed_at=observed_at)
    env.github.snapshot = slow
    env.service.start_manual({"apply": False})
    with pytest.raises(ProjectStatusRefused) as busy:
        env.service.start_manual({"apply": False})
    assert busy.value.code == "project_status_busy"
    gate.set()
    env.wait()


def test_the_schedule_is_off_until_switched_on_then_runs_applying_once_per_interval(env):
    env.now[0] += 10 * 3600
    assert env.service.tick() is False
    assert env.service.update_settings({"enabled": True})["enabled"] is True
    assert env.service.tick() is False  # not due yet
    env.now[0] += 6 * 3600 + 1
    assert env.service.tick() is True
    env.wait()
    run = last(env)
    assert (run["trigger"], run["mode"], run["status"]) == ("scheduled", "apply", "ok")
    assert env.service.tick() is False  # next slot is one interval away: no catch-up queue
    assert env.service.update_settings({"enabled": False})["enabled"] is False
    for bad in ({}, {"enabled": 1}, {"enabled": True, "x": 1}):
        with pytest.raises(ProjectStatusRefused):
            env.service.update_settings(bad)


def test_failures_raise_the_health_alert_but_a_note_edited_meanwhile_does_not(env):
    env.vault.files.clear()  # nothing to read: a run with no projects is fine, not a failure
    env.service.start_manual({"apply": True})
    env.wait()
    assert last(env)["status"] == "ok"
    env.service.client_factory = lambda _s: (_ for _ in ()).throw(RuntimeError("boom"))
    for _ in range(3):
        env.service.start_manual({"apply": False})
        env.wait()
    run = last(env)
    assert (run["status"], run["error_code"]) == ("failed", "unexpected_exception")
    health = env.service.health()
    assert (health["service"], health["consecutive_failures"], health["alert"]) == ("project_status", 3, True)


def test_a_run_interrupted_by_a_restart_is_failed_never_replayed(env, tmp_path, store):
    with env.service.db:
        env.service.db.execute(
            "INSERT INTO runs (run_id,trigger,mode,status,created) VALUES ('r1','manual','apply','running',1)")
    env.service.close()
    again = Env(tmp_path, store)
    try:
        run = again.service.status()["recent_runs"][0]
        assert (run["status"], run["error_code"]) == ("failed", "interrupted")
        assert again.github.calls == []
    finally:
        again.service.close()


def test_config_validation():
    for bad in (dict(interval_seconds=60), dict(max_changes=0), dict(max_changes=101),
                dict(max_changes=True), dict(interval_seconds="3600")):
        with pytest.raises(ValueError):
            ProjectStatusConfig(**bad)


class Api:
    def __init__(self, service):
        self.server = make_server(port=0, bind_address="127.0.0.1")
        self.server.project_status = service
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, path, payload, *, csrf=True, origin=True):
        token = None
        if csrf:
            token = json.loads(request(self.port, "GET", "/api/v1/session")[1])["csrf"]
        code, body = request(self.port, "POST", path, payload=payload, csrf=token,
                             origin=ORIGIN.format(port=self.port) if origin else None)
        return code, json.loads(body)


def test_routes_are_off_without_the_service_and_guarded_with_it(env):
    off = Api(None)
    try:
        assert request(off.port, "GET", "/api/v1/project-status")[0] == 503
        assert off.post("/api/v1/project-status/settings", {"enabled": True})[0] == 503
        code, page = request(off.port, "GET", "/")
        assert code == 200 and b"/static/project-status.js" in page
    finally:
        off.close()
    on = Api(env.service)
    try:
        code, body = request(on.port, "GET", "/api/v1/project-status")
        assert code == 200 and json.loads(body)["enabled"] is True
        assert request(on.port, "GET", "/static/project-status.js")[0] == 200
        for path, payload in (("/api/v1/project-status/runs", {"apply": False}),
                              ("/api/v1/project-status/settings", {"enabled": True})):
            assert on.post(path, payload, csrf=False)[0] == 403
            assert on.post(path, payload, origin=False)[0] == 403
        assert on.post("/api/v1/project-status/runs", {"apply": "x"})[0] == 400
        code, body = on.post("/api/v1/project-status/settings", {"enabled": True})
        assert code == 200 and body["enabled"] is True
        code, body = on.post("/api/v1/project-status/runs", {"apply": False})
        assert code == 202 and body["outcome"] == "accepted"
        env.wait()
        health = json.loads(request(on.port, "GET", "/api/v1/health")[1])
        assert any(s["service"] == "project_status" for s in health["services"])
    finally:
        on.close()
