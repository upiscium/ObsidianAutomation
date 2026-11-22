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


def test_http_target_rotation_quarantines_existing_promotion_even_after_revert(
    tmp_path, monkeypatch,
):
    from types import SimpleNamespace

    from obsidian_automation import core_promotion_runtime as module
    from obsidian_automation.core_promotion_job import CorePromotionJob
    from obsidian_automation.core_promotion_runtime import CorePromotionRuntime
    from obsidian_automation.core_promotion import CORE_REPOSITORY, PROMOTION_POLICY_VERSION

    store = SetupStore(tmp_path / "private", probe=lambda _: True,
                       obscure=lambda _: "synthetic")
    store.nextcloud_probe = lambda _: True
    start = {
        "webdav_url": "https://cloud.invalid/remote.php/dav/files/user/",
        "vault_folder": "Original",
        "nextcloud_user": "user", "nextcloud_password": "SYNTHETIC-ONLY",
    }
    assert store.save_nextcloud(start)
    runtime = CorePromotionRuntime(store.state, store, lambda: None)
    runtime.policy_path.write_bytes(runtime.policy_bytes)
    runtime._ensure_target_binding(runtime._target_identity(), "a" * 40)
    runtime.checkpoint_path.write_bytes(b"fake-checkpoint-not-a-real-receipt")
    monkeypatch.setattr(module, "load_checkpoint", lambda _: SimpleNamespace(
        source_repository=CORE_REPOSITORY,
        policy_version=PROMOTION_POLICY_VERSION,
        policy_sha256=runtime.policy_sha256,
    ))
    assert runtime.configured() is True
    snapshot = SnapshotJob(tmp_path / "jobs", lambda _: ("no_changes", "verify", "a"*40),
                           configured=False)
    with CorePromotionJob(tmp_path / "promotion-jobs", lambda: {},
                          configured=runtime.configured,
                          source_generation=runtime.source_generation) as promotion:
        try:
            with make_server(port=0, snapshot_setup=store) as server:
                server.snapshot_job = snapshot
                server.core_promotion_runtime = runtime
                server.core_promotion_job = promotion
                port = server.server_port
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    changed = {**start, "vault_folder": "NewVault"}
                    assert save(port, "/api/v1/nextcloud/connections", changed)[0] == 200
                    assert runtime.quarantine_path.is_file()
                    assert runtime.configured() is False
                    assert promotion.submit("f"*32) == ("unconfigured", None)
                    assert save(port, "/api/v1/nextcloud/connections", start)[0] == 200
                    assert store.load_nextcloud() == start
                    assert runtime.configured() is False
                finally:
                    server.shutdown()
                    thread.join(timeout=3)
        finally:
            snapshot.close()
