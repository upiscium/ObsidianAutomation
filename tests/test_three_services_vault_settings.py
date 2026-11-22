"""Synthetic-only Vault Settings negative admission and fail-closed tests."""
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

from obsidian_automation.core_promotion_runtime import CorePromotionRuntime
from obsidian_automation.three_services_server import make_server
from obsidian_automation.three_services_vault_fence import MinimalVaultFence
from test_three_services_server import request


class FakeJob:
    def __init__(self, *, promotion=False):
        self._lock = threading.RLock()
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.execute("CREATE TABLE runs (status TEXT)")
        self._settings = {"enabled": True, "interval_seconds": 3600}
        self._effect_gate = threading.Lock()
        self.promotes = promotion
        self.bound_generations = []

    def settings(self):
        return dict(self._settings)

    def update(self, payload):
        self._settings = dict(payload)
        return self.settings()

    def connections_saved(self, generation):
        self._settings["enabled"] = False
        self.bound_generations.append(generation)

    def _unresolved(self):
        return self.db.execute(
            "SELECT 1 FROM runs WHERE status='indeterminate'"
        ).fetchone() is not None


class FakeStore:
    def __init__(self, state):
        self.state = state
        self.data = {"webdav_url": "https://test.invalid/dav/",
                     "nextcloud_user": "account",
                     "vault_folder": "Old"}
    def load_nextcloud(self):
        return dict(self.data)

    def generation(self):
        return "abc"


class FakeRuntime:
    def __init__(self):
        self.count = 0
    def quarantine_for_vault_structure_change(self):
        self.count += 1


def setup_fence(tmp_path):
    store = FakeStore(tmp_path)
    snapshot, daily, promotion = FakeJob(), FakeJob(), FakeJob(promotion=True)
    runtime = FakeRuntime()
    server = SimpleNamespace(snapshot_job=snapshot, daily_job=daily,
                             core_promotion_job=promotion,
                             core_promotion_runtime=runtime)
    return store, server, runtime, MinimalVaultFence(store, server)


def test_vault_change_serializes_all_three_and_quarantines_core(tmp_path):
    store, server, runtime, fence = setup_fence(tmp_path)
    with fence.quiesced() as busy:
        assert busy is False
        store.data["vault_folder"] = "New"
        fence.after_change()
    assert runtime.count == 1
    assert all(not j.settings()["enabled"] for j in
               (server.snapshot_job,server.daily_job,server.core_promotion_job))
    assert server.snapshot_job.bound_generations == ["abc"]


def test_same_target_mapping_does_not_quarantine_core(tmp_path, monkeypatch):
    store, server, runtime, fence = setup_fence(tmp_path)
    status = {"system": "98-System"}
    monkeypatch.setattr(
        "obsidian_automation.three_services_vault_fence.physical_folder",
        lambda state, role: status[role])
    with fence.quiesced() as busy:
        assert not busy
        fence.after_change()
    assert runtime.count == 0
    with fence.quiesced() as busy:
        assert not busy
        status["system"] = "99-System"
        fence.after_change()
    assert runtime.count == 1


def test_fence_rejects_active_promotion_and_unreconciled_effect(tmp_path):
    store, server, runtime, fence = setup_fence(tmp_path)
    p = server.core_promotion_job
    p.db.execute("INSERT INTO runs VALUES ('running')")
    with fence.quiesced() as busy:
        assert busy
    p.db.execute("DELETE FROM runs")
    p.db.execute("INSERT INTO runs VALUES ('indeterminate')")
    with fence.quiesced() as busy:
        assert busy
    p.db.execute("DELETE FROM runs")
    assert p._effect_gate.acquire(False)
    try:
        with fence.quiesced() as busy:
            assert busy
    finally:
        p._effect_gate.release()


def test_core_quarantine_marker_is_sticky(tmp_path):
    runtime = object.__new__(CorePromotionRuntime)
    runtime.target_binding_path = tmp_path / "target-binding.json"
    runtime.checkpoint_path = tmp_path / "checkpoint.json"
    runtime.quarantine_path = tmp_path / "quarantine"
    assert runtime.quarantine_for_vault_structure_change() is False
    runtime.checkpoint_path.write_text("synthetic")
    assert runtime.quarantine_for_vault_structure_change() is True
    assert runtime.quarantine_path.read_bytes() == b"vault_structure_changed\n"
    assert runtime.quarantine_for_vault_structure_change() is True


class FakeVaultService:
    def status(self):
        return {"version": 1, "state": "default", "roles": {"daily": "00-DailyNote"}}
    def preview(self, payload):
        if payload != {"roles": {"daily": "01-Daily"}}:
            return 400, {"error": "invalid_request"}
        return 200, {"status": "blocked", "reason": "synthetic_only"}


def test_vault_settings_fixed_routes_preserve_csrf_and_three_service_panels():
    with make_server(port=0) as server:
        server.vault_bindings_editor = FakeVaultService()
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            code, body = request(port, "GET", "/")
            assert code == 200
            assert body.count(b' class="service"') == 3
            assert b'id="vault-settings"' in body
            code, raw = request(port, "GET", "/api/v1/vault-bindings")
            assert code == 200
            assert json.loads(raw)["roles"]["daily"] == "00-DailyNote"
            assert request(port, "GET", "/api/v1/formatter")[0] == 404
            payload = {"roles": {"daily": "01-Daily"}}
            host = f"http://127.0.0.1:{port}"
            assert request(port, "POST", "/api/v1/vault-bindings/previews",
                           payload=payload, origin=host)[0] == 403
            token = json.loads(request(port,"GET","/api/v1/session")[1])["csrf"]
            code, raw = request(port,"POST","/api/v1/vault-bindings/previews",
                                payload=payload, csrf=token, origin=host)
            assert code == 200 and json.loads(raw)["reason"] == "synthetic_only"
            assert request(port,"POST","/api/v1/vault-bindings/previews",
                           payload=payload, csrf=token, origin=host)[0] == 403
            assert request(port,"POST","/api/v1/vault-bindings/arbitrary",
                           payload=payload, origin=host)[0] == 405
        finally:
            server.shutdown()
            thread.join(timeout=3)


def test_real_vault_services_readonly_status_without_provider(tmp_path):
    from obsidian_automation.snapshot_setup import SetupStore
    from obsidian_automation.vault_root_webui import (
        build_vault_bindings_ui, build_vault_root_ui,
    )
    store = SetupStore(tmp_path / "state", probe=lambda _: True)
    server = SimpleNamespace(snapshot_job=FakeJob(),
                             daily_job=FakeJob(),
                             core_promotion_job=FakeJob(promotion=True),
                             core_promotion_runtime=FakeRuntime())
    root = build_vault_root_ui(store, server)
    mapping = build_vault_bindings_ui(store, server)
    status = root.status()
    assert status["enabled"] is True
    assert status["enrolled"] is False
    assert status["writes_vault"] is False
    binding_status = mapping.status()
    assert binding_status["state"] == "default"
    assert binding_status["roles"]["daily"] == "00-DailyNote"
    assert binding_status["editable_roles"] == ["daily", "project"]
    assert mapping.preview({"roles": {"system": "99-System"}})[1]["reason"] == "system_mapping_not_supported"
    # No credentials/enrollment => no usable provider preview.
    code, response = root.candidates({})
    assert code == 200 and response["status"] == "blocked"
    assert response["candidates"] == []
