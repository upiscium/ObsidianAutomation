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


class StubGuard:
    def __init__(self, verdict):
        self.verdict = verdict
        self.checks = 0

    def status(self):
        self.checks += 1
        return self.verdict

    def __call__(self):
        if self.status() not in ("ok", "legacy_unpinned"):
            raise act.RootPinRefused("vault_root_pin_refused")


@pytest.mark.parametrize("verdict", ["legacy_unpinned", "ok"])
def test_guarded_runners_reach_the_source_when_allowed(verdict):
    g = StubGuard(verdict)
    assert snapshot_app.guard_snapshot_runner(g, lambda rid: ("ok", rid, None))("r1") == \
        ("ok", "r1", None)
    assert snapshot_app.guard_daily_runner(g, lambda: {"d": 1})() == {"d": 1}
    assert snapshot_app.guard_formatter_runner(g, lambda: {"p": 1})() == {"p": 1}
    assert g.checks == 3


@pytest.mark.parametrize("verdict", ["mismatch", "unavailable"])
def test_guarded_runners_refuse_before_the_source(verdict):
    from obsidian_automation.github_daily_job import DailyStageFailure
    from obsidian_automation.vault_format_source import FormatterSourceError
    g = StubGuard(verdict)
    touched = []
    assert snapshot_app.guard_snapshot_runner(g, lambda rid: touched.append(rid))("r1") == \
        ("failed", "sync", None)
    with pytest.raises(DailyStageFailure) as daily:
        snapshot_app.guard_daily_runner(g, lambda: touched.append("daily"))()
    assert (daily.value.stage, daily.value.code) == ("source_admission", "source_unavailable")
    with pytest.raises(FormatterSourceError):
        snapshot_app.guard_formatter_runner(g, lambda: touched.append("fmt"))()
    assert touched == []
    snapshot_app.guard_snapshot_runner(None, lambda rid: touched.append(rid))("legacy-config")
    assert touched == ["legacy-config"]


def test_daily_refusal_never_reaches_publisher(tmp_path):
    import uuid
    from obsidian_automation.github_daily_job import DailyJob
    published = []
    runner = snapshot_app.guard_daily_runner(StubGuard("mismatch"), lambda: pytest.fail("ran"))
    with DailyJob(tmp_path / "daily", runner, configured=lambda: True,
                  publisher=lambda data: published.append(data)) as job:
        kind, receipt = job.submit(uuid.uuid4().hex)
        assert kind == "accepted"
        job._worker.join(3)
        row = job.run(receipt["run_id"])
    assert row["status"] == "failed"
    assert (row["failure_stage"], row["failure_code"]) == (
        "source_admission", "source_unavailable")
    assert published == []


def test_snapshot_app_main_uses_every_guard():
    src = inspect.getsource(snapshot_app.main)
    tree = ast.parse(src)
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert {"JobRootGuard", "guard_snapshot_runner", "guard_daily_runner",
            "guard_formatter_runner", "root_guard.wrap"} <= calls
    assert "FormatterJob(formatter_state, formatter_runner," in src
    wraps = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "root_guard.wrap"]
    assert len(wraps) == 2  # B3 normal and phase collectors


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


def test_credential_only_save_keeps_a_valid_pin(store):
    assert activate(store, FakeNextcloud()).status == "activated"
    previous = store.load_nextcloud()
    assert store.save_nextcloud({**previous, "nextcloud_password": "rotated"}, probed=True)
    assert act.reset_pin_after_connection_save(store) is True
    data = json.loads((store.state / act.BINDING_FILE).read_text())
    assert data["outcome"] == "activated" and data["current"]["chain"]


def test_uppercase_host_is_read_with_a_lowercase_origin(store):
    nc = FakeNextcloud()
    upper = NC["webdav_url"].replace("nextcloud.invalid", "NextCloud.INVALID")
    assert store.save_nextcloud({**NC, "webdav_url": upper}, probed=True)
    assert activate(store, nc).status == "activated"
    g, _ = guard(store, nc)          # FakeNextcloud asserts the lowercase origin
    assert g.status() == "ok"
    assert act.https_transport_factory(store.load_nextcloud())._origin == \
        "https://nextcloud.invalid"


@pytest.mark.parametrize("fault", ["transport_error", "wrong_instance"])
def test_pinned_read_faults_refuse(store, fault):
    nc = FakeNextcloud()
    assert activate(store, nc).status == "activated"
    if fault == "transport_error":
        def boom(n):
            raise v.DavTransportError("timeout")
        nc.on_call = boom
    else:
        nc.row_hook = lambda path, t: t.replace(oc_id(11), "00000011ocother")
    g, _ = guard(store, nc)
    with pytest.raises(act.RootPinRefused):
        g()


def test_full_snapshot_save_route_also_resets_pin(tmp_path):
    full = dict(NC, git_url="https://git.invalid/user/repo.git", git_user="user",
                git_token="GIT-PRIVATE", branch="main")
    s = SetupStore(tmp_path / "app", probe=lambda _: True, obscure=lambda _: "encoded")
    assert s.save(dict(full))
    assert activate(s, FakeNextcloud()).status == "activated"
    with closing(SnapshotJob(tmp_path / "snap", lambda _: ("no_changes", "verify", "c" * 40),
                             configured=True)) as snapshot:
        server = make_server(port=0, snapshot_job=snapshot, snapshot_setup=s,
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
            return r.status, body
        try:
            token = json.loads(request("GET", "/api/v1/operation-session")[1])["csrf_token"]
            # credential-only change: the pin survives
            assert request("POST", "/api/v1/snapshot/connections",
                           {**full, "vault_folder": "Vault/Notes",
                            "git_token": "GIT-ROTATED"}, csrf=token)[0] == 200
            assert json.loads((s.state / act.BINDING_FILE).read_text())["outcome"] == "activated"
            token = json.loads(request("GET", "/api/v1/operation-session")[1])["csrf_token"]
            assert request("POST", "/api/v1/snapshot/connections",
                           {**full, "vault_folder": "Typed"}, csrf=token)[0] == 200
        finally:
            server.shutdown()
            server.server_close()
    data = json.loads((s.state / act.BINDING_FILE).read_text())
    assert data["outcome"] == "connection_saved" and data["current"]["path"] == "Typed"
