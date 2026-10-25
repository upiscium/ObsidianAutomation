"""B4 GitHub setup reuses B2's one Nextcloud account: no Vault mutation."""
from __future__ import annotations

from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import threading
import uuid

import pytest

from obsidian_automation.admin_dashboard import HTML, JS, make_server
from obsidian_automation.github_daily_job import DailyJob
from obsidian_automation.github_daily_setup import (
    DailySetup, DailySetupError, pull_project_notes,
)
from obsidian_automation.github_daily_runonce import run_once
from obsidian_automation import github_daily_source_route as source
from test_github_daily_source_route import _FakeClient

TOKEN = "GH_SYNTHETIC_TOKEN_ONLY_NO_REAL_SECRET_12345678"
DAY = datetime(2026, 10, 6, 1, tzinfo=timezone.utc)


class SharedConnections:
    def __init__(self, tmp_path):
        self.available = True
        self.source_settings = {
            "webdav_url":"https://cloud.invalid/remote.php/dav/files/user/",
            "vault_folder":"Vault","nextcloud_user":"user",
            "nextcloud_password":"NC-SYNTHETIC",
        }
        self.config = tmp_path/"rclone.conf"
        self.config.write_text("[nextcloud]\ntype = local\n")
        self.con = type("TestConnection", (), {
            "rclone_remote": "nextcloud:Vault",
            "rclone_config": self.config,
            "validate": lambda self: None,
        })()

    def connections(self):
        return self.con if self.available else None

    def load(self):
        return dict(self.source_settings) if self.available else None


def project_copy(connections, destination: Path):
    assert connections.rclone_remote == "nextcloud:Vault"
    assert destination.name == "10-Project"
    project = destination/"Nested"/"Example.md"
    project.parent.mkdir(mode=0o700, parents=True)
    project.write_text(
        "---\ntype: project\nstatus: running\n"
        "github_repo: upiscium/Test\ngithub_watch: true\n---\n", encoding="utf-8")
    return True


def make_setup(tmp_path, **kwargs):
    shared = SharedConnections(tmp_path)
    state = tmp_path/"github-daily"
    return shared, DailySetup(state,shared,probe=lambda _: True,
                              copier=project_copy,**kwargs)


def test_minimal_single_nextcloud_account_setup_and_private_token(tmp_path):
    shared, setup = make_setup(tmp_path)
    assert setup.status()["configured"] is False
    assert setup.status()["nextcloud_source"] == "configured"
    assert setup.save({"github_token":TOKEN})
    assert setup.status() == {
        "version":1,"configured":True,"github":"configured",
        "nextcloud_source":"configured","project_mirror":"pulled_per_run",
        "vault_write":False,
    }
    assert (setup.state/"github.token").read_text() == TOKEN+"\n"
    for name in ("github.token","watcher.toml","source-generation"):
        file=setup.state/name
        assert file.stat().st_mode & 0o777 == 0o600
    for name in ("mirror","cas","cas/evidence","cas/receipt","cas/capture","preview"):
        assert (setup.state/name).stat().st_mode & 0o777 == 0o700
    assert TOKEN not in json.dumps(setup.status())
    assert not (setup.state/"mirror"/"10-Project").exists()


def test_no_second_nextcloud_dependency_and_generation_change_blocks_old_config(tmp_path):
    shared, setup = make_setup(tmp_path)
    assert setup.save({"github_token":TOKEN})
    shared.source_settings["vault_folder"] = "DifferentVault"
    assert not setup.configured()
    assert setup.status()["github"] == "missing"
    with pytest.raises(DailySetupError):
        with setup.project_source():
            pytest.fail("changed source should refuse any GitHub capture")
    assert setup.save({"github_token":TOKEN})
    assert setup.configured()
    shared.available = False
    assert setup.status()["nextcloud_source"] == "missing"
    assert not setup.configured()


def test_source_pull_is_disposable_and_yields_watched_project(tmp_path):
    shared, setup = make_setup(tmp_path)
    assert setup.save({"github_token":TOKEN})
    with setup.project_source() as (config, token):
        assert token == TOKEN
        watched = list((config.vault_root/"10-Project").rglob("*.md"))
        assert len(watched) == 1
        assert watched[0].read_text().count("github_repo") == 1
        temporary = config.vault_root
        assert temporary.is_dir()
    assert not temporary.exists()
    assert not (setup.state/"mirror"/"10-Project").exists()


def test_real_collector_pipeline_with_fake_get_and_copied_project_fixture(tmp_path,monkeypatch):
    shared, setup = make_setup(tmp_path)
    assert setup.save({"github_token":TOKEN})
    monkeypatch.setattr(source,"_utc_now",lambda:DAY)
    with setup.project_source() as (config,token):
        assert token == TOKEN
        result = run_once(config,_FakeClient(visible_pr=True,include_commit=True),
                          cas_root=setup.state/"cas",
                          preview_root=setup.state/"preview",now=DAY)
        stage = config.vault_root
    assert not stage.exists()
    assert result["status"] == "preview_ready"
    assert result["date_jst"] == "2026-10-05"
    assert result["rendered_entity_count"] >= 1
    assert result["vault_modified"] is False
    assert result["publication_authorized"] is False
    assert TOKEN not in json.dumps(result)
    assert len(list((setup.state/"cas"/"capture").iterdir())) == 1
    assert len(list((setup.state/"preview").iterdir())) == 2


def test_failed_copy_or_no_watched_project_never_creates_capture(tmp_path):
    shared = SharedConnections(tmp_path)
    s = DailySetup(tmp_path/"daily",shared,probe=lambda _: True,
                   copier=lambda c,t:False)
    assert s.save({"github_token":TOKEN})
    with pytest.raises(DailySetupError,match="project_mirror_pull_failed"):
        with s.project_source():
            pass
    assert not list((s.state/"cas"/"capture").iterdir())
    s.copier = lambda c,t:True
    with pytest.raises(DailySetupError,match="no_watched_github_projects"):
        with s.project_source():
            pass


def test_invalid_or_failed_token_cannot_replace_previous_credential(tmp_path):
    checks=[]
    def probe(token):
        checks.append(token)
        return token==TOKEN
    shared=SharedConnections(tmp_path)
    setup=DailySetup(tmp_path/"daily",shared,probe=probe,copier=project_copy)
    with pytest.raises(DailySetupError):
        setup.save({"github_token":"bad token with spaces"})
    assert not checks
    assert not setup.save({"github_token":"OTHER_TEST_TOKEN_0123456789"})
    assert not (setup.state/"github.token").exists()
    assert setup.save({"github_token":TOKEN})
    assert not setup.save({"github_token":"OTHER_TEST_TOKEN_0123456789"})
    assert (setup.state/"github.token").read_text().strip()==TOKEN
    assert setup.configured()


def test_rclone_source_is_only_remote_read_into_local_disposable_folder(tmp_path,monkeypatch):
    shared=SharedConnections(tmp_path)
    output=tmp_path/"mirror"/"10-Project"
    output.mkdir(parents=True)
    args=[]
    class Result:
        returncode=0
    def fake_run(argv,**kwargs):
        args.append((argv,kwargs))
        return Result()
    monkeypatch.setattr(
        "obsidian_automation.github_daily_setup.subprocess.run",fake_run)
    assert pull_project_notes(shared.con,output)
    assert len(args)==1
    command,kwargs=args[0]
    assert command[:3]==["rclone","--config",str(shared.config)]
    assert command[3:6]==["copy","nextcloud:Vault/10-Project",str(output)]
    assert not any(part in command for part in
                   ("sync","move","delete","purge","copyto","rcat"))
    assert kwargs["timeout"]==120


def test_connections_http_setup_and_shared_snapshot_contract(tmp_path):
    shared,setup=make_setup(tmp_path)
    started=[]
    with DailyJob(tmp_path/"daily-job",
                  lambda:started.append(1) or {
                      "version":1,"status":"preview_ready","date_jst":"2026-10-05",
                      "capture_sha256":"a"*64,"digest_sha256":"b"*64,
                      "original_event_count":4,"rendered_entity_count":2,
                      "model_used":False,"publication_authorized":False,
                      "vault_modified":False,
                  },configured=setup.configured) as job:
        server=make_server(port=0,observer=lambda:[],ai_observer=lambda:{},
                           daily_job=job,daily_setup=setup)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        port=server.server_address[1]
        origin=f"http://127.0.0.1:{port}"

        def req(method,path,payload=None,orig=None,token=None):
            conn=http.client.HTTPConnection("127.0.0.1",port,timeout=4)
            headers={"Host":f"127.0.0.1:{port}"}
            if orig: headers["Origin"]=orig
            if token: headers["X-Obsidian-CSRF"]=token
            if payload is not None: headers["Content-Type"]="application/json"
            conn.request(method,path,
                         json.dumps(payload) if payload is not None else None,
                         headers=headers)
            response=conn.getresponse()
            val=response.status,response.read()
            conn.close()
            return val

        def csrf():
            return json.loads(req("GET","/api/v1/operation-session")[1])["csrf_token"]

        try:
            assert req("POST","/api/v1/daily/connections",{"github_token":TOKEN})[0]==403
            assert req("POST","/api/v1/daily/connections",
                       {"github_token":TOKEN},"http://evil.invalid",csrf())[0]==403
            assert req("POST","/api/v1/daily/connections",
                       {"github_token":TOKEN,"shell":"danger"},origin,csrf())[0]==400
            assert req("POST","/api/v1/daily/connections",
                       {"github_token":TOKEN},origin,csrf())[0]==200
            assert setup.configured()
            saved=json.loads(req("GET","/api/v1/daily/connections")[1])
            assert saved["configured"] and TOKEN not in json.dumps(saved)
            assert req("GET","/api/v1/workflows")[0]==200
            assert job.settings()["enabled"] is False
            assert started==[]
            # Review P2 injection: failed schedule reset cannot install
            # a new provider token while a previous schedule is On.
            job.update({"enabled":True,"interval_seconds":86400})
            before = (setup.state/"github.token").read_bytes()
            original_update = job.update
            def fail_update(_):
                raise OSError("synthetic_SQLite_error")
            job.update = fail_update
            assert req("POST","/api/v1/daily/connections",
                       {"github_token":"NEW_GITHUB_FIXTURE_TOKEN_01234567"},
                       origin,csrf())[0] == 503
            assert (setup.state/"github.token").read_bytes() == before
            assert job.settings()["enabled"] is True
            job.update = original_update
            # Invalid input cannot even pause the old working schedule.
            assert req("POST","/api/v1/daily/connections",
                       {"github_token":"contains spaces"},
                       origin,csrf())[0] == 400
            assert job.settings()["enabled"] is True
            assert 'id="daily-connections-form"' in HTML
            assert 'name="github_token"' in HTML
            assert 'dailyConnectionRefresh' in JS
            assert "innerHTML" not in JS
        finally:
            server.shutdown()
            thread.join(4)
            server.server_close()



def test_b2_nextcloud_settings_change_pauses_b4_until_reconfigured(tmp_path):
    """One shared WebDAV account, no implicit use of an old token/source."""
    from obsidian_automation.snapshot_setup import SetupStore
    from obsidian_automation.snapshot_job import SnapshotJob
    from test_snapshot_setup import sample

    state=tmp_path/"app"
    salts = iter(["encodedA","encodedB","encodedC"])
    store=SetupStore(state,probe=lambda _:True,obscure=lambda _:next(salts))
    assert store.save(sample())
    snapshot=SnapshotJob(state,lambda _:("no_changes","verify","c"*40),
                         configured=True,connection_generation=store.generation())
    setup=DailySetup(state/"github-daily",store,probe=lambda _:True,copier=project_copy)
    assert setup.save({"github_token":TOKEN})
    with DailyJob(state/"github-daily",lambda: {
        "version":1,"status":"preview_ready","date_jst":"2026-10-05",
        "capture_sha256":"a"*64,"digest_sha256":"b"*64,
        "original_event_count":4,"rendered_entity_count":2,
        "model_used":False,"publication_authorized":False,
        "vault_modified":False,
    },configured=setup.configured) as job:
        server=make_server(port=0,snapshot_job=snapshot,snapshot_setup=store,
                           daily_job=job,daily_setup=setup,
                           observer=lambda:[],ai_observer=lambda:{})
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        port=server.server_address[1]
        origin=f"http://127.0.0.1:{port}"

        def request(method,path,payload=None,csrf=None):
            conn=http.client.HTTPConnection("127.0.0.1",port,timeout=3)
            headers={"Host":f"127.0.0.1:{port}","Origin":origin}
            if csrf: headers["X-Obsidian-CSRF"]=csrf
            if payload is not None: headers["Content-Type"]="application/json"
            conn.request(method,path,json.dumps(payload) if payload is not None else None,
                         headers=headers)
            res=conn.getresponse()
            val=res.status,res.read()
            conn.close()
            return val

        def token():
            return json.loads(request("GET","/api/v1/operation-session")[1])["csrf_token"]

        try:
            job.update({"enabled":True,"interval_seconds":86400})
            assert job.settings()["enabled"]
            rotated=sample()
            rotated["git_token"]="ROTATED_GITEA_TOKEN"
            old_obscured=store.rclone.read_bytes()
            assert request("POST","/api/v1/snapshot/connections",rotated,token())[0]==200
            assert store.rclone.read_bytes() != old_obscured
            assert setup.configured() and job.settings()["enabled"]
            changed=sample()
            changed["vault_folder"]="AnotherVault"
            assert request("POST","/api/v1/snapshot/connections",changed,token())[0]==200
            assert not setup.configured()
            assert not job.settings()["enabled"]
            assert job.tick() is False
            assert job.status()["last_run"] is None
            assert setup.save({"github_token":TOKEN})
            assert setup.configured()
            assert job.settings()["enabled"] is False
        finally:
            server.shutdown()
            thread.join(3)
            server.server_close()
    snapshot.close()


def test_mirror_pull_detects_concurrent_shared_source_change_at_exit(tmp_path):
    shared,setup=make_setup(tmp_path)
    assert setup.save({"github_token":TOKEN})
    with pytest.raises(DailySetupError, match="source_connections_changed"):
        with setup.project_source() as (cfg, _):
            assert (cfg.vault_root/"10-Project").exists()
            shared.source_settings["vault_folder"] = "DifferentVault"
    assert not setup.configured()
    assert not list((setup.state/"cas"/"capture").iterdir())


def test_setup_does_not_require_real_provider_during_app_start(tmp_path):
    from obsidian_automation import snapshot_app
    import sys
    state=tmp_path/"fresh"
    observed=[]
    class FakeServer:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def serve_forever(self,**_):
            observed.append(self.daily_setup.status())
            assert self.daily_job.status()["settings"]["enabled"] is False
            assert self.daily_job.status()["settings"]["configured"] is False
            assert self.snapshot_job.settings()["enabled"] is False
    server=FakeServer()
    original=snapshot_app.make_server
    argv=sys.argv
    try:
        snapshot_app.make_server=lambda **_:server
        sys.argv=["obsidian-snapshot-app","--state",str(state)]
        assert snapshot_app.main()==0
    finally:
        snapshot_app.make_server=original
        sys.argv=argv
    assert len(observed)==1
    assert observed[0]["configured"] is False
