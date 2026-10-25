"""MVI Nextcloud-only shared connection for B4, with Gitea optional for B2."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import http.client
import json
import threading
import uuid

import pytest

from obsidian_automation.admin_dashboard import HTML, JS, make_server
from obsidian_automation.snapshot_setup import (
    SetupStore, NEXTCLOUD_FIELDS, GITEA_FIELDS, validate_nextcloud
)
from obsidian_automation.snapshot_job import SnapshotJob
from obsidian_automation.github_daily_setup import DailySetup
from obsidian_automation.github_daily_job import DailyJob
from obsidian_automation.github_daily_runonce import run_once
from obsidian_automation import github_daily_source_route as source
from test_snapshot_setup import sample
from test_github_daily_source_route import _FakeClient


GITHUB_TOKEN = "GH_SYNTHETIC_ONLY_NO_REAL_USER_TOKEN_123456"


def project_copy(_source, target):
    p = target / "Example" / "Project.md"
    p.parent.mkdir(parents=True, mode=0o700)
    p.write_text("---\ntype: project\nstatus: running\n"
                 "github_repo: upiscium/Test\ngithub_watch: true\n---\n")
    return True


def prepare(tmp_path):
    store = SetupStore(tmp_path, probe=lambda _: True, obscure=lambda _: "encoded")
    store.nextcloud_probe = lambda _: True
    daily = DailySetup(tmp_path / "github-daily", store,
                       probe=lambda _: True, copier=project_copy)
    return store, daily


def test_one_registry_accepts_nextcloud_without_gitea(tmp_path):
    store, daily = prepare(tmp_path)
    value = {k: sample()[k] for k in NEXTCLOUD_FIELDS}
    assert store.save_nextcloud(value)
    assert store.load() is None
    assert store.load_nextcloud() == value
    assert store.connections() is None
    assert store.generation() is None
    assert store.source_connections().rclone_remote == "nextcloud:Vault"
    assert store.status() == {"version": 1, "configured": False,
                              "nextcloud": "configured", "gitea": "missing"}
    assert store.nextcloud_status()["configured"] is True
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.rclone.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "policy.toml").exists()
    assert not (tmp_path / "git-askpass").exists()
    assert not daily.configured()
    assert daily.status()["nextcloud_source"] == "configured"
    assert daily.save({"github_token": GITHUB_TOKEN})
    assert daily.configured()
    with daily.project_source() as (config, token):
        assert token == GITHUB_TOKEN
        assert len(list((config.vault_root/"10-Project").rglob("*.md"))) == 1
        temporary = config.vault_root
    assert not temporary.exists()
    assert len(list(tmp_path.glob("connections.json"))) == 1
    assert GITHUB_TOKEN not in json.dumps(store.status())


def test_strict_provider_input_validation_and_partial_integrity(tmp_path):
    store, _ = prepare(tmp_path)
    value = {k: sample()[k] for k in NEXTCLOUD_FIELDS}
    for broken in [dict(value, vault_folder="../Vault"),
                   dict(value, webdav_url="http://bad.invalid/remote.php/dav/files/user/"),
                   dict(value, nextcloud_user="bad:username"),
                   dict(value, git_token="unexpected")]:
        with pytest.raises(ValueError):
            validate_nextcloud(broken)
    assert store.save_nextcloud(value)
    store.rclone.write_text("forged")
    assert store.load_nextcloud() is None
    assert store.source_connections() is None
    assert store.status()["nextcloud"] == "missing"
    assert store.load() is None


def test_b4_only_full_source_to_preview_fake_github_without_gitea(tmp_path, monkeypatch):
    store, daily = prepare(tmp_path)
    assert store.save_nextcloud({k: sample()[k] for k in NEXTCLOUD_FIELDS})
    assert daily.save({"github_token": GITHUB_TOKEN})
    observed = datetime(2026, 10, 6, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(source, "_utc_now", lambda: observed)
    with daily.project_source() as (config, token):
        assert token == GITHUB_TOKEN
        report = run_once(config, _FakeClient(visible_pr=True, include_commit=True),
                          cas_root=daily.state/"cas", preview_root=daily.state/"preview",
                          now=observed)
    assert report["status"] == "preview_ready"
    assert report["date_jst"] == "2026-10-05"
    assert report["rendered_entity_count"] >= 1
    assert report["model_used"] is False and report["vault_modified"] is False
    assert not store.connections()
    assert not (tmp_path / "git-askpass").exists()
    assert len(list((daily.state/"cas"/"capture").iterdir())) == 1


def test_gitea_upgrade_reuses_nextcloud_account_without_reentering_credentials(tmp_path):
    store, daily = prepare(tmp_path)
    nc = {k: sample()[k] for k in NEXTCLOUD_FIELDS}
    assert store.save_nextcloud(nc)
    assert daily.save({"github_token": GITHUB_TOKEN})
    gitea = {k: sample()[k] for k in GITEA_FIELDS}
    assert store.save({**store.load_nextcloud(), **gitea})
    assert store.load() == sample()
    assert store.connections() is not None
    assert store.status()["gitea"] == "read_verified_at_save"
    assert daily.configured()
    assert store.load_nextcloud() == nc
    assert not daily.status()["vault_write"]


def test_nextcloud_only_http_then_b4_run_then_optional_gitea_upgrade(tmp_path):
    store, daily = prepare(tmp_path)
    calls = []

    def preview_result():
        calls.append("B4")
        return {
            "version":1, "status":"preview_ready", "date_jst":"2026-10-24",
            "capture_sha256":"a"*64, "digest_sha256":"b"*64,
            "original_event_count":4, "rendered_entity_count":2,
            "model_used":False, "publication_authorized":False,
            "vault_modified":False,
        }

    with closing(SnapshotJob(tmp_path, lambda _:("no_changes","verify","c"*40),
                             configured=False)) as snapshot:
        with DailyJob(daily.state, preview_result, configured=daily.configured) as job:
            server = make_server(port=0, snapshot_job=snapshot,
                                 snapshot_setup=store, daily_job=job,
                                 daily_setup=daily, observer=lambda:[],ai_observer=lambda:{})
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            port = server.server_address[1]
            origin = f"http://127.0.0.1:{port}"

            def request(method, path, data=None, *, csrf=None, at_origin=True):
                conn = http.client.HTTPConnection("127.0.0.1",port,timeout=4)
                headers={"Host":f"127.0.0.1:{port}"}
                if at_origin:headers["Origin"]=origin
                if csrf:headers["X-Obsidian-CSRF"]=csrf
                if data is not None: headers["Content-Type"]="application/json"
                conn.request(method,path,json.dumps(data) if data is not None else None,
                             headers=headers)
                response=conn.getresponse()
                status,raw=response.status,response.read()
                conn.close()
                return status,json.loads(raw) if raw.startswith(b"{") else raw

            def token():return request("GET","/api/v1/operation-session")[1]["csrf_token"]
            try:
                assert request("GET","/api/v1/nextcloud/connections")[1]["configured"] is False
                assert request("POST","/api/v1/nextcloud/connections",{})[0] == 403
                nc={k:sample()[k] for k in NEXTCLOUD_FIELDS}
                assert request("POST","/api/v1/nextcloud/connections",nc,
                               csrf=token(),at_origin=False)[0] == 403
                assert request("POST","/api/v1/nextcloud/connections",
                               {**nc,"arbitrary":"evil"},csrf=token())[0] == 400
                assert request("POST","/api/v1/snapshot/connections",
                               {k:sample()[k] for k in GITEA_FIELDS},
                               csrf=token())[0] == 400
                assert request("POST","/api/v1/nextcloud/connections",nc,
                               csrf=token())[0] == 200
                assert request("GET","/api/v1/nextcloud/connections")[1]["configured"] is True
                assert request("GET","/api/v1/snapshot/connections")[1]["configured"] is False
                assert store.status()["gitea"] == "missing"
                assert daily.status()["nextcloud_source"] == "configured"
                assert request("POST","/api/v1/daily/connections",
                               {"github_token":GITHUB_TOKEN},csrf=token())[0] == 200
                assert daily.configured()
                assert job.settings()["enabled"] is False
                # R1 P2: an invalid changed Nextcloud remote cannot
                # silently pause an already scheduled B4 Preview.
                job.update({"enabled":True,"interval_seconds":86400})
                store.nextcloud_probe = lambda _: False
                attempted = dict(nc, vault_folder="UnavailableVault")
                assert request("POST","/api/v1/nextcloud/connections",attempted,
                               csrf=token())[0] == 422
                assert job.settings()["enabled"] is True
                assert store.load_nextcloud() == nc
                assert daily.configured()
                store.nextcloud_probe = lambda _: True
                job.update({"enabled":False,"interval_seconds":86400})
                code,receipt=request("POST","/api/v1/daily/runs",
                                     {"request_id":uuid.uuid4().hex},csrf=token())
                assert code == 202
                job._worker.join(3)
                assert request("GET","/api/v1/daily/runs/"+receipt["run_id"])[1]["status"] == "preview_ready"
                assert calls == ["B4"]
                assert snapshot.settings()["configured"] is False
                assert snapshot.status()["last_run"] is None
                assert request("GET","/api/v1/workflows")[1]["workflows"][3]["integration"]=="connected"
                # Gitea-only POST upgrades the very SAME connection record.
                gitea={k:sample()[k] for k in GITEA_FIELDS}
                assert request("POST","/api/v1/snapshot/connections",gitea,
                               csrf=token())[0] == 200
                assert snapshot.settings()["configured"] is True
                assert store.load() == sample()
                assert store.load_nextcloud() == nc
                assert daily.configured()
                # The same guarantee holds after optional B2 Gitea setup.
                job.update({"enabled":True,"interval_seconds":86400})
                store.probe = lambda _: False
                changed = dict(nc, vault_folder="UnavailableVault")
                assert request("POST","/api/v1/nextcloud/connections",changed,
                               csrf=token())[0] == 422
                assert store.load() == sample()
                assert job.settings()["enabled"] is True
                assert GITHUB_TOKEN not in json.dumps(request("GET","/api/v1/daily/connections")[1])
                assert 'id="nextcloud-form"' in HTML
                assert "nextcloudReady" in JS
                assert "innerHTML" not in JS
            finally:
                server.shutdown();t.join(3);server.server_close()


def test_invalid_nextcloud_credentials_do_not_commit(tmp_path):
    store,_=prepare(tmp_path)
    value={k:sample()[k] for k in NEXTCLOUD_FIELDS}
    store.nextcloud_probe=lambda _:False
    assert store.save_nextcloud(value) is False
    assert store.load_nextcloud() is None
    assert not store.path.exists()
    assert not store.rclone.exists()
