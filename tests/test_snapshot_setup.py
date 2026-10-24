import http.client
import json
import os
import stat
import subprocess
import threading
import uuid
from dataclasses import replace

from obsidian_automation.admin_dashboard import make_server
from obsidian_automation.snapshot_job import SnapshotJob
from obsidian_automation.snapshot_setup import SetupStore, git_askpass, probe_connections, obscure_password, private_repo
from test_snapshot_adapter import fixture, git


def sample():
    return dict(webdav_url="https://cloud.invalid/remote.php/dav/files/user/", vault_folder="Vault",
                nextcloud_user="user", nextcloud_password="NC-PRIVATE",
                git_url="https://git.invalid/user/repo.git", git_user="user", git_token="GIT-PRIVATE", branch="main")


def test_store_probe_failure_save_reload_and_helper(tmp_path):
    value = sample()
    store = SetupStore(tmp_path, probe=lambda _: False, obscure=lambda _: "encoded")
    assert store.status()["configured"] is False
    assert not store.save(value)
    assert not store.path.exists()
    store.probe = lambda _: True
    assert store.save(value)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.rclone.stat().st_mode) == 0o600
    assert "NC-PRIVATE" not in store.rclone.read_text()
    assert "GIT-PRIVATE" not in str(store.status())
    assert store.connections().git_credentials == store.path
    store.probe = lambda _: False
    modified = dict(value, git_token="BAD-PRIVATE")
    assert not store.save(modified)
    assert SetupStore(tmp_path).load() == value
    # Simulate power loss after replacing rclone.conf but before committing JSON.
    store.rclone.write_text("[nextcloud]\npass = different\n")
    assert SetupStore(tmp_path).connections() is None
    assert SetupStore(tmp_path).status()["configured"] is False
    store.rclone.write_text("[nextcloud]\ntype = webdav\nurl = " + value["webdav_url"] +
                            "\nvendor = nextcloud\nuser = user\npass = encoded\n")
    assert SetupStore(tmp_path).load() == value
    restarted = SnapshotJob(tmp_path, lambda _: ("failed", "sync", None),
                            configured=SetupStore(tmp_path).connections() is not None)
    try:
        assert restarted.settings()["configured"] and not restarted.settings()["enabled"]
        assert restarted.status()["last_run"] is None
    finally:
        restarted.close()
    env = dict(os.environ, SNAPSHOT_CREDENTIALS=str(store.path))
    for prompt, expected in [("Username for 'https://git.invalid':", "user"),
                             ("Password for 'https://git.invalid':", "GIT-PRIVATE")]:
        result = subprocess.run([str(git_askpass(tmp_path)), prompt], env=env, capture_output=True, text=True, check=True)
        assert result.stdout.strip() == expected
    assert "GIT-PRIVATE" not in str(store.connections())


def test_http_setup_guards_no_auto_run(tmp_path):
    store = SetupStore(tmp_path, probe=lambda _: True, obscure=lambda _: "encoded")
    started, release = threading.Event(), threading.Event()
    blocking = [False]
    def runner(_):
        if blocking[0]:
            started.set()
            assert release.wait(4)
        return "no_changes", "verify", "a" * 40
    job = SnapshotJob(tmp_path, runner, configured=False)
    server = make_server(port=0, snapshot_job=job, snapshot_setup=store, observer=lambda: [], ai_observer=lambda: {})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    origin = f"http://127.0.0.1:{port}"

    def req(method, path, payload=None, origin_header=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        headers = {"Host": f"127.0.0.1:{port}"}
        if origin_header:
            headers["Origin"] = origin_header
        if token:
            headers["X-Obsidian-CSRF"] = token
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=json.dumps(payload) if payload is not None else None, headers=headers)
        response = conn.getresponse()
        result = response.status, response.read()
        conn.close()
        return result

    def csrf():
        return json.loads(req("GET", "/api/v1/operation-session")[1])["csrf_token"]

    try:
        path = "/api/v1/snapshot/connections"
        assert req("GET", "/")[0] == 200
        assert req("GET", path)[1].find(b'"configured": false') >= 0
        assert req("POST", path, sample(), token=csrf())[0] == 403
        assert req("POST", path, sample(), "http://evil", csrf())[0] == 403
        assert req("POST", path, sample(), origin)[0] == 403
        assert req("POST", path, sample(), origin, csrf())[0] == 200
        assert job.status()["last_run"] is None and not job.settings()["enabled"]
        assert job.settings()["configured"]
        assert job.settings()["gitea_write"] == "unverified"
        settings = dict(enabled=True, interval_seconds=60, source_name="nextcloud", destination_name="gitea")
        assert req("POST", "/api/v1/snapshot/settings", settings, origin, csrf())[0] == 200
        before = job.settings()
        assert before["enabled"] and before["next_due"] is not None
        store.probe = lambda _: False
        changed = dict(sample(), git_token="NEW-PRIVATE")
        assert req("POST", path, changed, origin, csrf())[0] == 422
        assert job.settings() == before and store.load() == sample()
        store.probe = lambda _: True
        assert req("POST", path, changed, origin, csrf())[0] == 200
        assert not job.settings()["enabled"] and job.settings()["next_due"] is None
        assert job.status()["last_run"] is None
        assert b"PRIVATE" not in req("GET", path)[1]
        assert b"PRIVATE" not in req("GET", "/api/v1/snapshot")[1]
        blocking[0] = True
        code, data = req("POST", "/api/v1/snapshot/runs", {"request_id": uuid.uuid4().hex}, origin, csrf())
        assert code == 202
        assert started.wait(2)
        assert req("POST", path, sample(), origin, csrf())[0] == 409
        assert store.load() == changed
        release.set()
        job._worker.join(3)
        assert job.status()["last_run"]["status"] == "no_changes"
        assert job.settings()["gitea_write"] == "unverified"
        assert not job.settings()["enabled"]
    finally:
        release.set()
        server.shutdown()
        thread.join(3)
        server.server_close()
        job.close()


def test_first_branch_in_empty_bare_repo(tmp_path, monkeypatch):
    adapter, source, remote, _ = fixture(tmp_path, monkeypatch)
    empty = tmp_path / "empty.git"
    git("init", "--bare", empty, cwd=tmp_path)
    from dataclasses import replace
    adapter.config = replace(adapter.config, git_remote=empty.as_uri())
    status, stage, sha = adapter("7" * 32)
    assert (status, stage) == ("saved", "verify")
    assert git("--git-dir", empty, "rev-parse", "refs/heads/main", cwd=tmp_path) == sha
    assert (source / "note.md").read_text() == "old"


def test_probe_read_only_and_no_secret_in_git_argv(monkeypatch):
    import obsidian_automation.snapshot_setup as setup
    value = sample()
    calls = []

    class Response:
        status = 207
        def __enter__(self): return self
        def __exit__(self, *_): pass

    def fake_open(request, timeout):
        assert request.get_method() == "PROPFIND" and timeout == 8
        assert request.get_header("Depth") == "0"
        assert request.full_url.endswith("/Vault/")
        return Response()

    def fake_command(args, **kwargs):
        calls.append(args)
        assert kwargs["timeout"] == 10
        assert "GIT_ASKPASS" in kwargs["env"]
        assert "GIT-PRIVATE" not in str(args) + str(kwargs["env"])
        return True, ""

    monkeypatch.setattr(setup.urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(setup, "private_repo", lambda _: True)
    monkeypatch.setattr(setup, "_command", fake_command)
    assert probe_connections(value)
    assert calls == [["git", "ls-remote", value["git_url"]]]
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("NC-PRIVATE")))
    assert not probe_connections(value)


def test_rclone_obscure_stdin_only(monkeypatch):
    import obsidian_automation.snapshot_setup as setup
    def fake_run(args, **kwargs):
        assert args == ["rclone", "obscure", "-"]
        assert kwargs["input"] == "NC-PRIVATE\n"
        return subprocess.CompletedProcess(args, 0, "encoded\n", "")
    monkeypatch.setattr(setup.subprocess, "run", fake_run)
    assert obscure_password("NC-PRIVATE") == "encoded"


def test_private_repo_api_read_only_and_fail_closed(monkeypatch):
    import obsidian_automation.snapshot_setup as setup
    value = sample()
    value["git_url"] = "https://git.invalid/gitea/user/repo.git"
    responses = [(200, {"full_name": "user/repo", "private": True})]

    class Response:
        def __init__(self, code, body):
            self.status = code
            self.body = json.dumps(body).encode()
            self.headers = {"Content-Type": "application/json"}
        def read(self, amount):
            assert amount == 65537
            return self.body
        def __enter__(self): return self
        def __exit__(self, *_): pass

    class Opener:
        def open(self, request, timeout):
            assert request.get_method() == "GET" and timeout == 8
            assert request.full_url == "https://git.invalid/gitea/api/v1/repos/user/repo"
            assert request.get_header("Authorization") == "token GIT-PRIVATE"
            assert "PRIVATE" not in request.full_url
            return Response(*responses[-1])

    def fake_build_opener(handler):
        assert handler.redirect_request(None, None, 302, "redirect", {}, "https://other.invalid/") is None
        return Opener()
    monkeypatch.setattr(setup.urllib.request, "build_opener", fake_build_opener)
    assert private_repo(value)
    for code, body in [(200, {"full_name": "user/repo", "private": False}),
                       (200, {"full_name": "other/repo", "private": True}),
                       (404, {"full_name": "user/repo", "private": True}),
                       (200, {"full_name": "user/repo"})]:
        responses.append((code, body))
        assert not private_repo(value)


def test_public_after_setup_prevents_push(tmp_path, monkeypatch):
    import obsidian_automation.snapshot_adapter as module
    import obsidian_automation.snapshot_setup as setup
    adapter, source, remote, baseline = fixture(tmp_path, monkeypatch)
    (source / "note.md").write_text("would-be snapshot")
    store = SetupStore(adapter.config.state, probe=lambda _: True, obscure=lambda _: "encoded")
    assert store.save(sample())
    adapter.config = replace(adapter.config, git_remote=sample()["git_url"],
                             git_credentials=store.path, allow_local_fixture=False)
    original = module._command

    def fixture_transport(args, **kwargs):
        # Fake HTTPS transport only; an actual remote host is never contacted.
        kwargs["env"] = {**kwargs["env"], "GIT_ALLOW_PROTOCOL": "https:file"}  # test-only transport
        return original([remote.as_uri() if arg == sample()["git_url"] else arg for arg in args], **kwargs)

    monkeypatch.setattr(module, "_command", fixture_transport)
    monkeypatch.setattr(setup, "private_repo", lambda _: False)
    assert adapter("8" * 32) == ("failed", "push", None)
    assert git("--git-dir", remote, "rev-parse", "refs/heads/main", cwd=tmp_path) == baseline


def test_restart_after_config_commit_before_schedule_reset_fails_off(tmp_path):
    store = SetupStore(tmp_path, probe=lambda _: True, obscure=lambda _: "encoded")
    assert store.save(sample())
    job = SnapshotJob(tmp_path, lambda _: ("failed", "sync", None),
                      connection_generation=store.generation())
    settings = dict(enabled=True, interval_seconds=60, source_name="nextcloud", destination_name="gitea")
    job.update(settings)
    job.close()
    assert store.save(dict(sample(), git_token="replacement"))  # crash before job.connections_saved()
    recovered = SnapshotJob(tmp_path, lambda _: ("failed", "sync", None),
                            connection_generation=store.generation())
    try:
        assert recovered.settings()["configured"]
        assert not recovered.settings()["enabled"] and recovered.settings()["next_due"] is None
        assert recovered.settings()["gitea_write"] == "unverified"
    finally:
        recovered.close()


def test_git_refuses_http_redirect_from_private_to_public_repo(tmp_path):
    """Disposable loopback Git transport must reject cross-repository redirect."""
    import http.server
    from obsidian_automation.snapshot_adapter import git_environment

    requests = []

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{self.server.server_port}/public.git/info/refs?service=git-upload-pack",
            )
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = git_environment(False)
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "http.followRedirects"
        assert env["GIT_CONFIG_VALUE_0"] == "false"
        # Production allows HTTPS only; permit HTTP only for this local fixture.
        env["GIT_ALLOW_PROTOCOL"] = "http"
        result = subprocess.run(
            ["git", "ls-remote", f"http://127.0.0.1:{server.server_port}/private.git"],
            cwd=tmp_path, env=env, capture_output=True, text=True,
            timeout=8, check=False,
        )
        assert result.returncode != 0
        assert requests, "Git did not reach the private endpoint"
        assert all(url.startswith("/private.git/") for url in requests), requests
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()
