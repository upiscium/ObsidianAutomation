"""#303 V1/D Job pre-run root pin guard and the shared-save reset."""
from __future__ import annotations

import ast
from contextlib import closing
from dataclasses import replace
import http.client
import inspect
import json
import threading

import pytest

from obsidian_automation import snapshot_app
from obsidian_automation import vault_root_activation as act
from obsidian_automation import vault_root_dav_verifier as v
from obsidian_automation.admin_dashboard import make_server
from obsidian_automation.snapshot_job import SnapshotJob
from obsidian_automation.snapshot_setup import SetupStore
from test_303_v1b2_dav_verifier import ACCOUNT, VERIFIED, FakeNextcloud, oc_id
from test_303_v1d_vault_root_activation import NC, RESTRICTED, FakeFence, activate


@pytest.fixture
def store(tmp_path):
    s = SetupStore(tmp_path, obscure=lambda _: "encoded")
    assert s.save_nextcloud(dict(NC), probed=True)
    return s


class Factory:
    def __init__(self, nc):
        self.nc = nc
        self.calls = 0

    def __call__(self, saved):
        assert saved["nextcloud_password"] == "NC-PRIVATE"
        self.calls += 1
        return self.nc


def guard(store, nc, proof=RESTRICTED):
    factory = Factory(nc)
    return act.JobRootGuard(store, transport_factory=factory, proof=proof), factory


def test_no_activation_allows_without_any_provider_io(store):
    nc = FakeNextcloud()
    g, factory = guard(store, nc, proof=v.CURRENT_PROVIDER_PROOF)
    assert g.status() == "legacy_unpinned"
    g()  # does not raise
    assert factory.calls == 0 and nc.calls == []


def test_pinned_root_is_rechecked_and_mismatch_refuses(store):
    nc = FakeNextcloud()
    assert activate(store, nc).status == "activated"
    nc.calls.clear()
    g, factory = guard(store, nc)
    assert g.status() == "ok"
    assert nc.calls == [("/remote.php/dav/files/synthetic_user/Vault/Notes/", "0")]
    nc.nodes["Vault/Notes"] = (99, True, "")          # recreated under the same name
    with pytest.raises(act.RootPinRefused):
        g()


@pytest.mark.parametrize("change", ["unproven", "no_factory", "alias", "gone"])
def test_pinned_root_unreadable_refuses(store, change):
    nc = FakeNextcloud()
    assert activate(store, nc).status == "activated"
    proof = v.CURRENT_PROVIDER_PROOF if change == "unproven" else RESTRICTED
    g = act.JobRootGuard(store, transport_factory=None if change == "no_factory"
                         else Factory(nc), proof=proof)
    if change == "alias":
        nc.nodes["Vault/Notes"] = (11, True, "shared")
    if change == "gone":
        del nc.nodes["Vault/Notes"]
    with pytest.raises(act.RootPinRefused):
        g()


def test_guard_wrap_runs_check_before_function(store):
    nc = FakeNextcloud()
    assert activate(store, nc).status == "activated"
    nc.nodes["Vault/Notes"] = (99, True, "")
    g, _ = guard(store, nc)
    ran = []
    with pytest.raises(act.RootPinRefused):
        g.wrap(lambda *a: ran.append(a))("x")
    assert ran == []


def test_read_root_identity_contract():
    nc = FakeNextcloud()
    assert v.read_root_identity(ACCOUNT, nc, "Vault", proof=RESTRICTED) == oc_id(10)
    assert v.read_root_identity(ACCOUNT, nc, "Shared", proof=RESTRICTED) is None
    assert v.read_root_identity(ACCOUNT, nc, "a.md", proof=RESTRICTED) is None
    assert v.read_root_identity(ACCOUNT, nc, "../x", proof=RESTRICTED) is None
    calls = len(nc.calls)
    assert v.read_root_identity(ACCOUNT, nc, "Vault") is None   # production register
    assert len(nc.calls) == calls


def test_reset_is_noop_without_activation(store):
    assert act.reset_pin_after_connection_save(store) is True
    assert not (store.state / act.BINDING_FILE).exists()
    assert not (store.state / act.PINNING_MARKER).exists()


def test_reset_after_operator_save_returns_to_unpinned(store):
    assert activate(store, FakeNextcloud()).status == "activated"
    assert store.save_nextcloud({**NC, "vault_folder": "Typed/By/Hand"}, probed=True)
    assert act.check_root_pin(store, lambda _: "x") == "mismatch"   # stale pin
    assert act.reset_pin_after_connection_save(store) is True
    data = json.loads((store.state / act.BINDING_FILE).read_text())
    assert data["current"] == {"path": "Typed/By/Hand", "chain": None, "instance_id": None}
    assert data["outcome"] == "connection_saved"
    assert act.check_root_pin(store, lambda _: pytest.fail("no read")) == "legacy_unpinned"
    assert act.rollback_vault_root(enabled=True, store=store,
                                   fence=FakeFence()).reason == "nothing_to_roll_back"


def test_connection_saved_outcome_never_accepts_a_chain(store):
    assert activate(store, FakeNextcloud()).status == "activated"
    path = store.state / act.BINDING_FILE
    data = json.loads(path.read_text())
    data["outcome"] = "connection_saved"
    path.write_text(json.dumps(data))
    assert act.check_root_pin(store, lambda _: oc_id(11)) == "mismatch"


def test_dashboard_shared_save_resets_pin_under_locks(store, tmp_path):
    store.nextcloud_probe = lambda _: True
    assert activate(store, FakeNextcloud()).status == "activated"
    with closing(SnapshotJob(tmp_path / "snap", lambda _: ("no_changes", "verify", "c" * 40),
                             configured=False)) as snapshot:
        server = make_server(port=0, snapshot_job=snapshot, snapshot_setup=store,
                             observer=lambda: [], ai_observer=lambda: {})
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]

        def request(method, path, data=None, csrf=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
            headers = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}"}
            if csrf:
                headers["X-Obsidian-CSRF"] = csrf
            if data is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, json.dumps(data) if data is not None else None, headers)
            r = conn.getresponse()
            body = r.read()
            conn.close()
            return r.status, json.loads(body) if body.startswith(b"{") else body
        try:
            token = request("GET", "/api/v1/operation-session")[1]["csrf_token"]
            status, _ = request("POST", "/api/v1/nextcloud/connections",
                                {**NC, "vault_folder": "Typed"}, csrf=token)
            assert status == 200
        finally:
            server.shutdown()
            server.server_close()
    data = json.loads((store.state / act.BINDING_FILE).read_text())
    assert data["outcome"] == "connection_saved" and data["current"]["path"] == "Typed"
    assert act.check_root_pin(store, lambda _: "x") == "legacy_unpinned"


def test_snapshot_app_guards_every_vault_reader():
    tree = ast.parse(inspect.getsource(snapshot_app.main))
    src = inspect.getsource(snapshot_app.main)
    assert "JobRootGuard(store, transport_factory=https_transport_factory)" in src
    funcs = {n.name: ast.get_source_segment(src, n) for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef)}
    # Snapshot, B4 and Formatter check before their source read.
    assert funcs["runner"].index("root_guard") < funcs["runner"].index("SnapshotAdapter")
    assert funcs["daily_runner"].index("root_guard()") < funcs["daily_runner"].index(
        "collect_b4_preview")
    assert funcs["formatter_runner"].index("root_guard()") < funcs[
        "formatter_runner"].index("formatter_source()")
    assert "FormatterJob(formatter_state, formatter_runner," in src
    # B3 normal and phase collectors are both wrapped.
    assert src.count("root_guard.wrap(") == 2


def test_never_activated_allows_even_without_saved_connection(tmp_path):
    # Jobs keep their own "not configured" handling; the guard adds nothing.
    empty = SetupStore(tmp_path / "empty")
    g = act.JobRootGuard(empty, transport_factory=lambda _: pytest.fail("no I/O"))
    assert g.status() == "legacy_unpinned"
    g()


def test_reset_tolerates_store_without_private_state():
    class Duck:
        pass
    assert act.reset_pin_after_connection_save(Duck()) is True
