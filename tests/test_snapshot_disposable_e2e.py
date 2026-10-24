"""P3-B: one disposable HTTP -> job -> fake WebDAV/rclone -> local bare Git path.

HTTPS Gitea and Nextcloud are deliberately NOT exercised. Privacy and Git
read-only probes are injected; the real adapter still performs local Git I/O.
"""
import http.client
import json
import os
import subprocess
import threading
import uuid
from dataclasses import replace

from obsidian_automation.admin_dashboard import make_server
from obsidian_automation.snapshot_adapter import SnapshotAdapter
from obsidian_automation.snapshot_job import SnapshotJob
from obsidian_automation.snapshot_setup import SetupStore
from test_snapshot_adapter import fixture, git
from test_snapshot_setup import sample


def test_disposable_dashboard_to_restore_and_restart(tmp_path, monkeypatch):
    _, source, _, _ = fixture(tmp_path, monkeypatch)  # fake rclone executable; source never written by adapter
    before_source = (source / "note.md").read_bytes()
    remote = tmp_path / "fresh.git"
    git("init", "--bare", remote, cwd=tmp_path)
    state = tmp_path / "state"
    store = SetupStore(state, probe=lambda _: True, obscure=lambda _: "encoded")

    def runner(run_id):
        c = store.connections()
        # Explicit fixture-only substitution; production never allows file://.
        return SnapshotAdapter(replace(c, git_remote=remote.as_uri(), allow_local_fixture=True))(run_id)

    def launch():
        job = SnapshotJob(state, runner, configured=store.connections() is not None,
                          connection_generation=store.generation())
        server = make_server(port=0, snapshot_job=job, snapshot_setup=store,
                             observer=lambda: [], ai_observer=lambda: {})
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        return job, server, thread, port

    def request(port, method, path, body=None, token=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Host": f"127.0.0.1:{port}"}
        if method == "POST":
            headers.update({"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"})
        if token:
            headers["X-Obsidian-CSRF"] = token
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def csrf(port):
        return json.loads(request(port, "GET", "/api/v1/operation-session")[1])["csrf_token"]

    def post(port, path, body):
        return request(port, "POST", path, body, csrf(port))

    def run(port, job):
        code, payload = post(port, "/api/v1/snapshot/runs", {"request_id": uuid.uuid4().hex})
        assert code == 202, payload
        run_id = json.loads(payload)["run_id"]
        job._worker.join(6)
        assert not job._worker.is_alive()
        return json.loads(request(port, "GET", "/api/v1/snapshot/runs/" + run_id)[1])

    def stop(job, server, thread):
        server.shutdown()
        thread.join(3)
        server.server_close()
        job.close()

    job, server, thread, port = launch()
    try:
        assert request(port, "GET", "/")[0] == 200
        initial = json.loads(request(port, "GET", "/api/v1/snapshot")[1])
        assert not initial["settings"]["configured"] and not initial["settings"]["enabled"]
        assert post(port, "/api/v1/snapshot/connections", sample())[0] == 200
        assert job.status()["last_run"] is None and not job.settings()["enabled"]
        assert b"PRIVATE" not in request(port, "GET", "/api/v1/snapshot/connections")[1]
        assert b"PRIVATE" not in request(port, "GET", "/api/v1/snapshot")[1]
        settings = dict(enabled=True, interval_seconds=60, source_name="nextcloud", destination_name="gitea")
        assert post(port, "/api/v1/snapshot/settings", settings)[0] == 200
        assert job.settings()["enabled"]
        first = run(port, job)
        assert first["status"] == "saved" and first["stage"] == "verify"
        assert job.settings()["gitea_write"] == "verified"
        assert git("--git-dir", remote, "rev-parse", "refs/heads/main", cwd=tmp_path) == first["remote_ref"]
        restored = subprocess.run(["git", "--git-dir", str(remote), "show", "refs/heads/main:note.md"],
                                  cwd=tmp_path, check=True, capture_output=True).stdout
        assert restored == before_source == (source / "note.md").read_bytes()
        second = run(port, job)
        assert second["status"] == "no_changes" and second["remote_ref"] == first["remote_ref"]
        assert post(port, "/api/v1/snapshot/settings", {**settings, "enabled": False})[0] == 200
        assert not job.settings()["enabled"] and not job.tick()
        assert run(port, job)["status"] == "no_changes"  # manual Run while paused
        last = job.status()["last_run"]["run_id"]
    finally:
        stop(job, server, thread)

    job, server, thread, port = launch()
    try:
        assert job.status()["last_run"]["run_id"] == last
        assert job.settings()["gitea_write"] == "verified" and not job.settings()["enabled"]
        monkeypatch.setenv("FAIL_RCLONE", "sync")
        failure = run(port, job)
        assert (failure["status"], failure["stage"]) == ("failed", "sync")
        assert git("--git-dir", remote, "rev-parse", "refs/heads/main", cwd=tmp_path) == first["remote_ref"]
        assert (source / "note.md").read_bytes() == before_source
    finally:
        stop(job, server, thread)
