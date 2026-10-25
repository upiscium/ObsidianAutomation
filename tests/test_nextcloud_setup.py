"""B4-only WebDAV Connections without Gitea, sharing B2 when later enabled."""
from __future__ import annotations

import http.client
import json
import stat
import subprocess
import threading
import uuid

import pytest

from obsidian_automation.admin_dashboard import HTML, JS, make_server
from obsidian_automation.github_daily_job import DailyJob
from obsidian_automation.github_daily_setup import DailySetup, pull_project_notes
from obsidian_automation.nextcloud_setup import (
    NextcloudSetup, NextcloudInUse, validate_nextcloud, probe_nextcloud,
)
from obsidian_automation.snapshot_job import SnapshotJob
from obsidian_automation.snapshot_setup import SetupStore
from test_snapshot_setup import sample
from test_github_daily_setup import TOKEN, project_copy


def nextcloud():
    return {key: sample()[key] for key in (
        "webdav_url", "vault_folder", "nextcloud_user", "nextcloud_password",
    )}


def stores(tmp_path):
    full = SetupStore(tmp_path, probe=lambda _: True, obscure=lambda _: "encoded")
    source = NextcloudSetup(full, probe=lambda _: True, obscure=lambda _: "encoded")
    return full, source


def test_save_nextcloud_only_and_reload_without_gitea(tmp_path):
    full, source = stores(tmp_path)
    assert source.status() == {"version": 1, "configured": False,
                               "nextcloud": "missing", "gitea_required": False}
    assert source.save(nextcloud())
    assert full.load() is None
    assert full.connections() is None
    assert not full.path.exists()  # No fake Gitea repository or credential.
    assert source.status()["configured"] and not source.status()["gitea_required"]
    local = source.connections()
    assert local is not None
    local.validate()
    assert local.rclone_remote == "nextcloud:Vault"
    assert local.rclone_config == source.rclone
    for path in (source.path, source.rclone):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert nextcloud()["nextcloud_password"] not in source.rclone.read_text()
    assert nextcloud()["nextcloud_password"] not in json.dumps(source.status())
    restored = NextcloudSetup(SetupStore(tmp_path))
    assert restored.load() == nextcloud()
    assert restored.connections().rclone_remote == "nextcloud:Vault"
    assert full.generation() is None


def test_b4_source_can_preview_without_gitea(tmp_path):
    full, source = stores(tmp_path)
    assert source.save(nextcloud())
    d = DailySetup(tmp_path / "github-daily", source,
                   probe=lambda _: True, copier=project_copy)
    assert d.save({"github_token": TOKEN})
    assert d.configured()
    with d.project_source() as (cfg, token):
        assert token == TOKEN
        assert (cfg.vault_root / "10-Project" / "Nested" / "Example.md").is_file()
        ephemeral = cfg.vault_root
    assert not ephemeral.exists()
    assert full.connections() is None
    assert full.path.exists() is False


def test_nextcloud_source_pulls_read_only_without_git(monkeypatch, tmp_path):
    _, source = stores(tmp_path)
    assert source.save(nextcloud())
    target = tmp_path / "pull" / "10-Project"
    target.mkdir(parents=True)
    calls = []

    def fake_subprocess(argv, **kwargs):
        calls.append(argv)
        assert argv[:5] == ["rclone", "--config", str(source.rclone), "copy",
                            "nextcloud:Vault/10-Project"]
        assert argv[5] == str(target)
        assert kwargs["timeout"] == 120
        assert "sync" not in argv and "delete" not in argv and "push" not in argv
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr("obsidian_automation.github_daily_setup.subprocess.run", fake_subprocess)
    assert pull_project_notes(source.connections(), target)
    assert len(calls) == 1


def test_validation_and_probe_dont_require_gitea(monkeypatch):
    valid = nextcloud()
    assert validate_nextcloud(valid) == valid
    assert "git_url" not in valid
    for extra in ("git_url", "gitea"):
        with pytest.raises(ValueError):
            validate_nextcloud({**valid, extra: "unexpected"})
    for key, bad in (("webdav_url", "http://insecure/remote.php/dav/files/user/"),
                     ("vault_folder", "../secrets"), ("vault_folder", ".Vault"),
                     ("nextcloud_user", "bad:name"),
                     ("nextcloud_password", "")):
        with pytest.raises(ValueError):
            validate_nextcloud({**valid, key: bad})

    class ReadResponse:
        status = 207
        def __enter__(self): return self
        def __exit__(self, *_): pass

    observed = []
    def fake_open(request, timeout):
        observed.append(request)
        assert timeout == 8
        assert request.get_method() == "PROPFIND"
        assert request.get_header("Depth") == "0"
        assert request.full_url.endswith("/Vault/")
        return ReadResponse()
    monkeypatch.setattr("obsidian_automation.nextcloud_setup.urllib.request.urlopen", fake_open)
    assert probe_nextcloud(valid) and len(observed) == 1
    assert "NC-PRIVATE" not in str(observed[0].full_url)


def test_standalone_corruption_or_failed_commit_never_validated(tmp_path, monkeypatch):
    full, source = stores(tmp_path)
    assert source.save(nextcloud())
    original_config = source.rclone.read_bytes()
    original_values = source.load()
    source.rclone.write_text("partial config corruption")
    assert source.connections() is None
    assert source.status()["configured"] is False
    source.rclone.write_bytes(original_config)
    assert source.load() == original_values

    def fail_pause():
        raise OSError("sqlite_pause_failed")
    with pytest.raises(OSError):
        source.save({**nextcloud(), "vault_folder": "DifferentVault"},
                    before_commit=fail_pause)
    assert source.load() == original_values
    assert source.rclone.read_bytes() == original_config

    source.probe = lambda _: False
    assert not source.save({**nextcloud(), "vault_folder": "DifferentVault"})
    assert source.load() == original_values


def test_webui_nextcloud_only_then_github_and_later_snapshot(tmp_path):
    full, source = stores(tmp_path)
    job = SnapshotJob(tmp_path, lambda _: ("failed", "sync", None), configured=False)
    daily_setup = DailySetup(tmp_path / "github-daily", source,
                             probe=lambda _: True, copier=project_copy)
    with DailyJob(tmp_path / "github-daily", lambda: {
        "version": 1, "status": "preview_ready", "date_jst": "2026-10-05",
        "capture_sha256": "a" * 64, "digest_sha256": "b" * 64,
        "original_event_count": 4, "rendered_entity_count": 2,
        "model_used": False, "publication_authorized": False,
        "vault_modified": False,
    }, configured=daily_setup.configured) as daily:
        server = make_server(
            port=0, snapshot_job=job, snapshot_setup=full,
            nextcloud_setup=source, daily_job=daily, daily_setup=daily_setup,
            observer=lambda: [], ai_observer=lambda: {},
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        origin = f"http://127.0.0.1:{port}"

        def req(method, path, body=None, *, auth=True, extra=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
            headers = {"Host": f"127.0.0.1:{port}"}
            if auth: headers["Origin"] = origin
            if body is not None: headers["Content-Type"] = "application/json"
            if extra: headers.update(extra)
            conn.request(method, path,
                         json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse()
            result = response.status, response.read()
            conn.close()
            return result

        def csrf():
            return json.loads(req("GET", "/api/v1/operation-session")[1])["csrf_token"]

        def post(path, payload):
            return req("POST", path, payload, extra={"X-Obsidian-CSRF": csrf()})

        try:
            nc = "/api/v1/nextcloud/connections"
            assert req("GET", nc)[0] == 200
            assert json.loads(req("GET", nc)[1])["configured"] is False
            assert req("POST", nc, nextcloud(), auth=False)[0] == 403
            assert req("POST", nc, {**nextcloud(), "git_url": "unexpected"},
                       extra={"X-Obsidian-CSRF": csrf()})[0] == 400
            source.probe = lambda _: False
            assert post(nc, nextcloud())[0] == 422
            assert source.load() is None and not full.load()
            source.probe = lambda _: True
            assert post(nc, nextcloud())[0] == 200
            assert json.loads(req("GET", nc)[1])["configured"] is True
            assert job.settings()["configured"] is False
            assert job.settings()["enabled"] is False
            assert daily.settings()["configured"] is False
            assert post("/api/v1/daily/connections", {"github_token": TOKEN})[0] == 200
            assert daily_setup.configured()
            assert daily.settings()["configured"] is True
            assert daily.settings()["enabled"] is False
            assert full.connections() is None

            on = {"enabled": True, "interval_seconds": 86400}
            assert post("/api/v1/daily/settings", on)[0] == 200
            assert daily.settings()["enabled"]
            old_nc = source.load()
            # Reject a folder that execution would reject before WebDAV
            # probe, schedule pause, or any persistent source modification.
            assert post(nc, {**nextcloud(), "vault_folder": ".Vault"})[0] == 400
            assert source.load() == old_nc and daily.settings()["enabled"]
            changed_nc = {**nextcloud(), "vault_folder": "OtherVault"}
            assert post(nc, changed_nc)[0] == 200
            assert not daily.settings()["enabled"]
            assert not daily_setup.configured()
            assert daily.submit(uuid.uuid4().hex)[0] == "unconfigured"
            assert not job.settings()["configured"]
            # Rebind GitHub token to new Nextcloud source. No Gitea needed.
            assert post("/api/v1/daily/connections", {"github_token": TOKEN})[0] == 200
            assert daily_setup.configured()
            assert post("/api/v1/daily/settings", on)[0] == 200
            assert daily.settings()["enabled"]

            # Later enabling B2 with the same four source fields reuses one
            # account; B4 remains configured/on despite obscured rclone bytes.
            configured_b2 = {**sample(), **changed_nc}
            assert post("/api/v1/snapshot/connections", configured_b2)[0] == 200
            assert job.settings()["configured"] is True
            assert source.load() == changed_nc
            assert daily_setup.configured() and daily.settings()["enabled"]
            assert not source.path.exists() and not source.rclone.exists()
            assert post(nc, changed_nc)[0] == 200  # Same shared account is OK
            assert daily.settings()["enabled"]
            assert post(nc, old_nc)[0] == 409  # No second Nextcloud account
            assert source.load() == changed_nc
            assert daily.settings()["enabled"]
            assert b"NC-PRIVATE" not in req("GET", nc)[1]
            assert b"GIT-PRIVATE" not in req("GET", nc)[1]
            assert b"GIT-PRIVATE" not in req("GET", "/api/v1/daily/connections")[1]
            assert 'id="nextcloud-connections-form"' in HTML
            assert 'name="github_token"' in HTML
            assert 'fetch("/api/v1/nextcloud/connections"' in JS
        finally:
            server.shutdown()
            thread.join(3)
            server.server_close()
    job.close()


def test_webui_pause_failure_does_not_replace_previous_source(tmp_path):
    full, source = stores(tmp_path)
    assert source.save(nextcloud())
    job = SnapshotJob(tmp_path, lambda _: ("failed", "sync", None), configured=False)
    daily_setup = DailySetup(tmp_path / "github-daily", source, probe=lambda _: True)
    with DailyJob(tmp_path / "github-daily", lambda: {}, configured=daily_setup.configured) as daily:
        assert daily_setup.save({"github_token": TOKEN})
        daily.update({"enabled": True, "interval_seconds": 86400})
        server = make_server(port=0, snapshot_job=job, snapshot_setup=full,
                             nextcloud_setup=source, daily_job=daily,
                             daily_setup=daily_setup, observer=lambda: [],
                             ai_observer=lambda: {})
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port=server.server_address[1]
        origin=f"http://127.0.0.1:{port}"
        original = daily.update
        try:
            daily.update=lambda _: (_ for _ in ()).throw(OSError("synthetic_db_failure"))
            conn=http.client.HTTPConnection("127.0.0.1",port,timeout=3)
            conn.request("GET","/api/v1/operation-session",headers={"Host":f"127.0.0.1:{port}"})
            response=conn.getresponse()
            token=json.loads(response.read())["csrf_token"]
            conn.close()
            conn=http.client.HTTPConnection("127.0.0.1",port,timeout=3)
            conn.request("POST","/api/v1/nextcloud/connections",
                         json.dumps({**nextcloud(),"vault_folder":"Changed"}),
                         headers={"Host":f"127.0.0.1:{port}","Origin":origin,
                                  "Content-Type":"application/json","X-Obsidian-CSRF":token})
            res=conn.getresponse()
            assert res.status==503, res.read()
            conn.close()
            assert source.load() == nextcloud()
            assert daily.settings()["enabled"]
            assert daily_setup.configured()
        finally:
            daily.update=original
            server.shutdown()
            thread.join(3)
            server.server_close()
    job.close()

def test_source_pair_partial_commit_failure_restores_prior_binding(tmp_path, monkeypatch):
    from obsidian_automation import nextcloud_setup as module
    _, source = stores(tmp_path)
    assert source.save(nextcloud())
    original = source.load()
    old_rclone = source.rclone.read_bytes()
    real_atomic = module._atomic
    changes = []

    def fail_json(path, content):
        if path == source.path:
            raise OSError("synthetic_mid_pair_commit_failure")
        return real_atomic(path, content)

    monkeypatch.setattr(module, "_atomic", fail_json)
    with pytest.raises(OSError, match="synthetic_mid_pair_commit_failure"):
        source.save({**nextcloud(), "vault_folder": "NewVault"},
                    before_commit=lambda: changes.append("schedule_paused"))
    assert changes == ["schedule_paused"]
    assert source.load() == original
    assert source.rclone.read_bytes() == old_rclone


def test_concurrent_nextcloud_post_and_daily_get_keep_one_lock_order(tmp_path):
    """Real HTTP interleaving: GET must not acquire DailySetup before POST
    takes its shared lock, or both requests would wait on each other.
    """
    import time

    full, source = stores(tmp_path)
    assert source.save(nextcloud())
    daily_setup = DailySetup(tmp_path / "github-daily", source, probe=lambda _: True)
    assert daily_setup.save({"github_token": TOKEN})
    snapshot = SnapshotJob(tmp_path, lambda _: ("failed", "sync", None), configured=False)
    with DailyJob(tmp_path / "github-daily", lambda: {}, configured=daily_setup.configured) as daily:
        daily.update({"enabled": True, "interval_seconds": 86400})
        entered = threading.Event()
        release = threading.Event()
        held_daily_setup = []

        def blocking_probe(_payload):
            # CPython RLock ownership is a deterministic lock-order assertion.
            held_daily_setup.append(daily_setup._lock._is_owned())
            entered.set()
            assert release.wait(4)
            return True

        source.probe = blocking_probe
        server = make_server(port=0, snapshot_job=snapshot, snapshot_setup=full,
                             nextcloud_setup=source, daily_job=daily,
                             daily_setup=daily_setup,
                             observer=lambda: [], ai_observer=lambda: {})
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        port=server.server_address[1]
        origin=f"http://127.0.0.1:{port}"
        responses = {}

        def request(method, path, body=None, csrf=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {"Host": f"127.0.0.1:{port}", "Origin": origin}
            if csrf is not None: headers["X-Obsidian-CSRF"] = csrf
            if body is not None: headers["Content-Type"] = "application/json"
            try:
                conn.request(method, path, json.dumps(body) if body is not None else None,
                             headers=headers)
                response = conn.getresponse()
                return response.status, response.read()
            finally:
                conn.close()

        try:
            code, raw = request("GET", "/api/v1/operation-session")
            assert code==200
            token=json.loads(raw)["csrf_token"]

            def post():
                responses["post"] = request(
                    "POST", "/api/v1/nextcloud/connections",
                    {**nextcloud(), "vault_folder": "ChangedVault"}, token)
            def read_daily():
                responses["get"] = request("GET", "/api/v1/daily/connections")

            posting = threading.Thread(target=post, daemon=True)
            posting.start()
            assert entered.wait(3), "POST never reached the held shared source lock"
            reading = threading.Thread(target=read_daily, daemon=True)
            reading.start()
            time.sleep(0.08)
            # With the fixed order the POST thread already holds DailySetup
            # before shared. GET cannot sneak in to invert these locks.
            assert held_daily_setup == [True]
            release.set()
            posting.join(4)
            reading.join(4)
            assert not posting.is_alive() and not reading.is_alive()
            assert responses["post"][0] == 200, responses
            assert responses["get"][0] == 200, responses
            assert json.loads(responses["get"][1])["configured"] is False
            assert not daily.settings()["enabled"]
            assert source.load()["vault_folder"] == "ChangedVault"
            assert not daily_setup.configured()
        finally:
            release.set()
            server.shutdown()
            t.join(3)
            server.server_close()
    snapshot.close()
