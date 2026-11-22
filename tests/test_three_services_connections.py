"""Synthetic HTTP setup regression; never reaches Nextcloud, Git or Gitea."""
import json
import threading

from obsidian_automation.snapshot_job import SnapshotJob
from obsidian_automation.snapshot_setup import SetupStore
from obsidian_automation.three_services_server import make_server
from test_three_services_server import request


def save(port, path, data):
    csrf = json.loads(request(port, "GET", "/api/v1/session")[1])["csrf"]
    return request(port, "POST", path, payload=data, csrf=csrf,
                   origin="http://127.0.0.1:" + str(port))


def test_three_services_connections_can_rotate_nextcloud_with_gitea_preserved(tmp_path):
    store = SetupStore(
        tmp_path / "private", probe=lambda _: True, obscure=lambda _: "encoded",
    )
    store.nextcloud_probe = lambda _: True
    job = SnapshotJob(tmp_path / "jobs", lambda _: ("no_changes", "verify", "a"*40),
                      configured=False)
    nextcloud = {
        "webdav_url": "https://cloud.invalid/remote.php/dav/files/user/",
        "vault_folder": "Vault", "nextcloud_user": "user",
        "nextcloud_password": "SYNTHETIC-NEXTCLOUD",
    }
    gitea = {
        "git_url": "https://gitea.invalid/u/ObsidianVault.git",
        "git_user": "user", "git_token": "SYNTHETIC-GITEA", "branch": "main",
    }
    try:
        with make_server(port=0, snapshot_setup=store) as server:
            server.snapshot_job = job
            port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                assert save(port, "/api/v1/nextcloud/connections", nextcloud)[0] == 200
                assert store.load_nextcloud() == nextcloud
                assert store.load() is None
                assert save(port, "/api/v1/snapshot/connections", gitea)[0] == 200
                assert store.load() == {**nextcloud, **gitea}
                updated = {**nextcloud, "nextcloud_password": "SYNTHETIC-ROTATED"}
                assert save(port, "/api/v1/nextcloud/connections", updated)[0] == 200
                assert store.load() == {**updated, **gitea}
                assert job.settings()["enabled"] is False
                for path in ("/api/v1/nextcloud/connections",
                             "/api/v1/snapshot/connections"):
                    code, body = request(port, "GET", path)
                    assert code == 200
                    assert b"SYNTHETIC-NEXTCLOUD" not in body
                    assert b"SYNTHETIC-ROTATED" not in body
                    assert b"SYNTHETIC-GITEA" not in body
            finally:
                server.shutdown()
                thread.join(timeout=3)
    finally:
        job.close()
