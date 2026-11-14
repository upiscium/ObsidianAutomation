"""#303 Phase 2 / P2-4: save, reset and roll back the role mapping (synthetic)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation import vault_bindings as vb
from obsidian_automation import vault_bindings_editor as ed
from obsidian_automation import vault_root_activation as act
from obsidian_automation.snapshot_setup import SetupStore
from obsidian_automation.vault_layout import DEFAULT_ROLE_FOLDERS
from obsidian_automation.vault_root_activation import RootPin
from test_303_p2_1_vault_bindings import ROLE_IDS, ctx, vault_nc
from test_303_v1b2_dav_verifier import INSTANCE, oc_id
from test_303_v1c_vault_root_selection import dashboard  # noqa: F401 (fixture)
from test_303_v1d_vault_root_activation import NC, RESTRICTED, FakeFence


@pytest.fixture
def store(tmp_path):
    s = SetupStore(tmp_path, obscure=lambda _: "encoded")
    assert s.save_nextcloud({**NC, "vault_folder": "ObsidianVault"}, probed=True)
    assert act._write_binding(s.state, RootPin("ObsidianVault", (oc_id(70),), INSTANCE),
                              None, "activated", lambda: 1)
    return s


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def service(store, nc=None, fence=None, clock=None):
    nc = nc or vault_nc()
    fence = fence or FakeFence()
    svc = ed.VaultBindingsService(
        store, context=lambda: ctx(source_revision=store.nextcloud_revision()),
        transport=nc, fence_factory=lambda: fence, proof=RESTRICTED,
        clock=clock or Clock())
    return svc, nc, fence


def preview_and_save(svc, roles, accept=None):
    payload = {"roles": roles}
    if accept is not None:
        payload["accept_recreated"] = accept
    code, result = svc.preview(payload)
    assert code == 200 and result["status"] == "valid", result
    return svc.save({"handle": result["handle"], "confirm": True})[1]


def record(store) -> dict:
    return json.loads((store.state / vb.BINDINGS_FILE).read_text())


# --- save -------------------------------------------------------------------------

def test_status_starts_at_todays_names(store):
    svc, _, _ = service(store)
    status = svc.status()
    assert status["state"] == "default" and status["roles"] == dict(DEFAULT_ROLE_FOLDERS)
    assert status["editable_roles"] == ["daily", "project", "system"]
    assert status["can_rollback"] is False and status["can_reset"] is False


def test_save_writes_the_previewed_record_under_the_fence(store):
    svc, nc, fence = service(store)
    result = preview_and_save(svc, {"daily": "Journal"})
    assert (result["status"], result["reason"]) == ("saved", "requires_job_reenable")
    assert fence.changes == 1
    saved = record(store)
    assert saved["roles"]["daily"] == "Journal" and saved["role_ids"]["daily"] == oc_id(200)
    assert saved["root_pin"] == [oc_id(70)]
    assert not (store.state / vb.PENDING_FILE).exists()
    assert oct((store.state / vb.BINDINGS_FILE).stat().st_mode & 0o777) == "0o600"
    assert svc.status()["state"] == "ok" and svc.status()["can_rollback"] is True
    assert vb.physical_folder(store.state, "daily") == "Journal"


def test_non_editable_roles_always_keep_todays_names(store):
    svc, _, _ = service(store)
    assert svc.preview({"roles": {"knowledge": "Journal"}})[0] == 400
    preview_and_save(svc, {"system": "Journal"})
    assert record(store)["roles"]["knowledge"] == "11-Knowledge"


@pytest.mark.parametrize("payload", [
    None, [], {}, {"roles": []}, {"roles": {"daily": 1}}, {"roles": {}, "extra": 1},
    {"roles": {}, "accept_recreated": "daily"}, {"roles": {}, "accept_recreated": ["ai"]},
    {"roles": {}, "accept_recreated": ["daily", "daily"]},
])
def test_malformed_previews_are_rejected(store, payload):
    svc, nc, _ = service(store)
    assert svc.preview(payload) == (400, {"error": "invalid_request"})
    assert nc.calls == []


def test_save_needs_confirm_and_a_live_handle(store):
    clock = Clock()
    svc, _, _ = service(store, clock=clock)
    handle = svc.preview({"roles": {"daily": "Journal"}})[1]["handle"]
    assert svc.save({"handle": handle, "confirm": False})[0] == 400
    assert svc.save({"handle": handle})[0] == 400
    clock.now += ed.HANDLE_TTL_S + 1
    assert svc.save({"handle": handle, "confirm": True})[1]["reason"] == "handle_expired"
    assert not (store.state / vb.BINDINGS_FILE).exists()
    handle = svc.preview({"roles": {"daily": "Journal"}})[1]["handle"]
    assert svc.save({"handle": handle, "confirm": True})[1]["status"] == "saved"
    assert svc.save({"handle": handle, "confirm": True})[1]["reason"] == "handle_expired"


def test_blocked_preview_gives_no_handle(store):
    svc, _, _ = service(store)
    code, result = svc.preview({"roles": {"daily": "Missing"}})
    assert result["status"] == "blocked" and "handle" not in result


def test_save_refuses_while_jobs_run(store):
    fence = FakeFence(busy=True)
    svc, _, _ = service(store, fence=fence)
    assert preview_and_save(svc, {"daily": "Journal"})["reason"] == "jobs_in_flight"
    assert not (store.state / vb.BINDINGS_FILE).exists() and fence.changes == 0


def test_save_refuses_a_folder_that_changed_after_the_preview(store):
    svc, nc, _ = service(store)
    handle = svc.preview({"roles": {"daily": "Journal"}})[1]["handle"]
    nc.nodes["ObsidianVault/Journal"] = (299, True, "")          # recreated
    assert svc.save({"handle": handle, "confirm": True})[1]["reason"] == "stale_preview"
    assert not (store.state / vb.BINDINGS_FILE).exists()


def test_saving_todays_names_without_a_record_is_a_no_op(store):
    svc, _, fence = service(store)
    assert preview_and_save(svc, {})["reason"] == "already_default"
    assert fence.changes == 0


# --- crash safety -----------------------------------------------------------------

@pytest.mark.parametrize("step", ["previous", "record", "pause", "finish"])
def test_every_crash_point_leaves_jobs_refusing(store, monkeypatch, step):
    svc, _, fence = service(store)
    handle = svc.preview({"roles": {"daily": "Journal"}})[1]["handle"]
    real_atomic = ed._atomic

    def failing_atomic(path, text):
        if (step == "previous" and path.name == ed.PREVIOUS_FILE) or (
                step == "record" and path.name == vb.BINDINGS_FILE):
            raise OSError("disk")
        return real_atomic(path, text)
    monkeypatch.setattr(ed, "_atomic", failing_atomic)
    if step == "pause":
        fence.after_change = lambda: (_ for _ in ()).throw(RuntimeError("x"))
    if step == "finish":
        real_unlink = Path.unlink

        def unlink(self, *a, **kw):
            if self.name == vb.PENDING_FILE:
                raise OSError("busy")
            return real_unlink(self, *a, **kw)
        monkeypatch.setattr(Path, "unlink", unlink)
    result = svc.save({"handle": handle, "confirm": True})[1]
    monkeypatch.undo()
    fence.after_change = FakeFence.after_change.__get__(fence)
    if step == "previous":
        assert result["status"] == "blocked"          # cleanly undone
        assert not (store.state / vb.PENDING_FILE).exists()
        return
    assert result["status"] == "uncertain"
    assert (store.state / vb.PENDING_FILE).exists()
    assert svc.status()["state"] == "pending"
    root = type("R", (), {"state": store.state, "status": lambda self: "ok",
                          "pin": lambda self: RootPin("ObsidianVault", (oc_id(70),), INSTANCE),
                          "read_identity": lambda self, p: None})()
    assert vb.RoleGuard(root, ("system",)).status() == "mismatch"
    # The way out: reset finishes or drops the change.
    assert svc.reset({"confirm": True})[1]["status"] == "reset"
    assert svc.status()["state"] == "default"


# --- re-pin -----------------------------------------------------------------------

def test_recreated_folder_needs_an_explicit_re_pin(store):
    svc, nc, _ = service(store)
    preview_and_save(svc, {"daily": "Journal"})
    nc.nodes["ObsidianVault/Journal"] = (299, True, "")
    blocked = svc.preview({"roles": {"daily": "Journal"}})[1]
    assert blocked["roles"]["daily"]["status"] == "identity_changed" and "handle" not in blocked
    # Re-pinning a role that was not recreated is refused.
    assert svc.preview({"roles": {"daily": "Journal"},
                        "accept_recreated": ["system"]})[1]["reason"] == "invalid_accept"
    result = preview_and_save(svc, {"daily": "Journal"}, accept=["daily"])
    assert result["status"] == "saved" and record(store)["role_ids"]["daily"] == oc_id(299)


def test_re_pin_never_hides_another_recreated_role(store):
    svc, nc, _ = service(store)
    preview_and_save(svc, {"daily": "Journal"})
    nc.nodes["ObsidianVault/Journal"] = (299, True, "")
    nc.nodes["ObsidianVault/98-System"] = (298, True, "")
    result = svc.preview({"roles": {"daily": "Journal"}, "accept_recreated": ["daily"]})[1]
    assert result["status"] == "blocked"
    assert result["roles"]["system"]["status"] == "identity_changed"


# --- reset and rollback -----------------------------------------------------------

def test_rollback_restores_the_previous_state_one_step(store):
    nc = vault_nc()
    nc.nodes["ObsidianVault/Ops"] = (205, True, "")
    svc, _, fence = service(store, nc)
    preview_and_save(svc, {"daily": "Journal"})
    preview_and_save(svc, {"daily": "Journal", "system": "Ops"})
    assert svc.rollback({"confirm": True})[1]["status"] == "rolled_back"
    assert record(store)["roles"]["system"] == "98-System"
    assert record(store)["roles"]["daily"] == "Journal"
    assert svc.rollback({"confirm": True})[1]["reason"] == "nothing_to_roll_back"
    assert svc.rollback({"confirm": False})[0] == 400


def test_rollback_of_the_first_save_returns_to_no_record(store):
    svc, _, _ = service(store)
    preview_and_save(svc, {"daily": "Journal"})
    assert svc.rollback({"confirm": True})[1]["status"] == "rolled_back"
    assert not (store.state / vb.BINDINGS_FILE).exists()
    assert svc.status()["state"] == "default"


def test_reset_drops_the_record_and_can_be_rolled_back(store):
    svc, _, _ = service(store)
    preview_and_save(svc, {"daily": "Journal"})
    assert svc.reset({"confirm": True})[1]["status"] == "reset"
    assert svc.status()["state"] == "default"
    assert svc.reset({"confirm": True})[1]["reason"] == "already_default"
    assert svc.rollback({"confirm": True})[1]["status"] == "rolled_back"
    assert record(store)["roles"]["daily"] == "Journal"


def test_reset_drops_an_unreadable_record(store):
    (store.state / vb.BINDINGS_FILE).write_text("{")
    (store.state / vb.BINDINGS_FILE).chmod(0o600)
    svc, _, _ = service(store)
    assert svc.status()["state"] == "unreadable"
    assert svc.reset({"confirm": True})[1]["status"] == "reset"
    assert svc.status()["state"] == "default" and svc.status()["can_rollback"] is False


def test_reset_and_rollback_refuse_while_jobs_run(store):
    svc, _, _ = service(store)
    preview_and_save(svc, {"daily": "Journal"})
    busy, _, _ = service(store, fence=FakeFence(busy=True))
    assert busy.reset({"confirm": True})[1]["reason"] == "jobs_in_flight"
    assert busy.rollback({"confirm": True})[1]["reason"] == "jobs_in_flight"


# --- D2-A: root switch -------------------------------------------------------------

def test_root_switch_is_refused_while_a_mapping_exists(store):
    svc, nc, _ = service(store)
    preview_and_save(svc, {"daily": "Journal"})
    nc.nodes["Other"] = (20, True, "")
    result = act.activate_vault_root(
        enabled=True, store=store, fence=FakeFence(), account=ctx().account, transport=nc,
        requested="ObsidianVault/Journal", expected_leaf_id=oc_id(200),
        expected_revision=store.nextcloud_revision(), container_resource_id=oc_id(1),
        active_resource_id=oc_id(70), proof=RESTRICTED)
    assert (result.status, result.reason) == ("blocked", "mapping_present")
    assert act._write_binding(store.state, RootPin("ObsidianVault", (oc_id(70),), INSTANCE),
                              RootPin("Other", (oc_id(20),), INSTANCE), "activated", lambda: 2)
    assert act.rollback_vault_root(enabled=True, store=store,
                                   fence=FakeFence()).reason == "mapping_present"


def test_stale_root_is_shown_and_reset_is_the_way_out(store):
    svc, _, _ = service(store)
    preview_and_save(svc, {"daily": "Journal"})
    assert act._write_binding(store.state, RootPin("ObsidianVault", (oc_id(71),), INSTANCE),
                              None, "activated", lambda: 2)   # e.g. via the connection form
    assert svc.status()["state"] == "stale_root"
    assert svc.preview({"roles": {"daily": "Journal"}})[1]["reason"] in (
        "root_changed", "root_pin_mismatch")
    assert svc.reset({"confirm": True})[1]["status"] == "reset"
    assert svc.status()["state"] == "default"
    # Rolling the reset back would restore a mapping for the old root: refused.
    assert svc.rollback({"confirm": True})[1]["reason"] == "root_changed"
    assert svc.status()["state"] == "default"


# --- HTTP routes --------------------------------------------------------------------

def test_routes_are_off_without_the_flag(dashboard):
    request, token = dashboard()
    assert request("GET", "/api/v1/vault-bindings") == (404, b"Not found")
    for path in ed_paths():
        assert request("POST", path, {}, csrf=token())[0] in (404, 405)


def ed_paths():
    from obsidian_automation.admin_dashboard import VAULT_BINDINGS_POST_PATHS
    return VAULT_BINDINGS_POST_PATHS


def test_routes_serve_the_service_with_origin_and_csrf(store, tmp_path):
    import http.client
    import threading
    from obsidian_automation.admin_dashboard import make_server
    svc, _, _ = service(store)
    server = make_server(port=0, observer=lambda: [], ai_observer=lambda: {},
                         vault_bindings_editor=svc)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def request(method, path, data=None, *, csrf=None, origin=True):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Host": f"127.0.0.1:{port}"}
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{port}"
        if csrf:
            headers["X-Obsidian-CSRF"] = csrf
        if data is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, json.dumps(data) if data is not None else None, headers)
        r = conn.getresponse()
        status, raw = r.status, r.read()
        conn.close()
        return status, json.loads(raw) if raw.startswith(b"{") else raw

    def token():
        return request("GET", "/api/v1/operation-session")[1]["csrf_token"]
    try:
        code, status = request("GET", "/api/v1/vault-bindings")
        assert code == 200 and status["state"] == "default"
        assert request("POST", "/api/v1/vault-bindings/previews",
                       {"roles": {"daily": "Journal"}})[0] == 403          # no CSRF
        assert request("POST", "/api/v1/vault-bindings/previews",
                       {"roles": {"daily": "Journal"}}, csrf=token(), origin=False)[0] == 403
        code, result = request("POST", "/api/v1/vault-bindings/previews",
                               {"roles": {"daily": "Journal"}}, csrf=token())
        assert code == 200 and result["status"] == "valid"
        text = json.dumps(result)
        assert INSTANCE not in text and "remote.php" not in text
        code, saved = request("POST", "/api/v1/vault-bindings/saves",
                              {"handle": result["handle"], "confirm": True}, csrf=token())
        assert saved["status"] == "saved"
        assert request("POST", "/api/v1/vault-bindings/rollbacks",
                       {"confirm": True}, csrf=token())[1]["status"] == "rolled_back"
        code, body = request("GET", "/static/vault-bindings.js")
        assert code == 200 and b"vault-bindings" in body
    finally:
        server.shutdown()
        server.server_close()


def test_page_loads_the_script_and_it_renders_text_only():
    from obsidian_automation.admin_dashboard import HTML
    assert '<script defer src="/static/vault-bindings.js"></script>' in HTML
    js = (Path(ed.__file__).parent / "webui_design_v1" / "vault_bindings.js").read_text()
    assert "innerHTML" not in js and "window.confirm(" in js
    assert '"/api/v1/vault-bindings/saves", {handle, confirm: true}' in js


# --- review follow-ups ----------------------------------------------------------

def test_retrying_an_interrupted_reset_keeps_the_rollback_point(store):
    svc, _, fence = service(store)
    preview_and_save(svc, {"daily": "Journal"})
    fence.after_change = lambda: (_ for _ in ()).throw(RuntimeError("x"))
    assert svc.reset({"confirm": True})[1]["status"] == "uncertain"
    fence.after_change = FakeFence.after_change.__get__(fence)
    assert svc.reset({"confirm": True})[1]["status"] == "reset"
    assert svc.rollback({"confirm": True})[1]["status"] == "rolled_back"
    assert record(store)["roles"]["daily"] == "Journal"


def test_reset_over_an_interrupted_save_keeps_the_older_rollback_point(store):
    nc = vault_nc()
    nc.nodes["ObsidianVault/Ops"] = (205, True, "")
    svc, _, fence = service(store, nc)
    preview_and_save(svc, {"daily": "Journal"})                  # A
    handle = svc.preview({"roles": {"daily": "Journal", "system": "Ops"}})[1]["handle"]
    fence.after_change = lambda: (_ for _ in ()).throw(RuntimeError("x"))
    assert svc.save({"handle": handle, "confirm": True})[1]["status"] == "uncertain"
    fence.after_change = FakeFence.after_change.__get__(fence)
    assert svc.reset({"confirm": True})[1]["status"] == "reset"
    # Rollback returns to A, the last finished record, never the unfinished B.
    assert svc.rollback({"confirm": True})[1]["status"] == "rolled_back"
    assert record(store)["roles"]["system"] == "98-System"


def test_mapping_needs_a_settled_root(store):
    svc, nc, _ = service(store)
    assert act._write_binding(store.state, RootPin("ObsidianVault", (oc_id(70),), INSTANCE),
                              None, "pending_activated", lambda: 2)
    result = svc.preview({"roles": {"daily": "Journal"}})[1]
    assert result["reason"] == "root_unsettled" and nc.calls == []
    assert act._write_binding(store.state, RootPin("ObsidianVault", (oc_id(70),), INSTANCE),
                              None, "activated", lambda: 3)
    handle = svc.preview({"roles": {"daily": "Journal"}})[1]["handle"]
    assert act._write_binding(store.state, RootPin("ObsidianVault", (oc_id(70),), INSTANCE),
                              None, "pending_activated", lambda: 4)
    assert svc.save({"handle": handle, "confirm": True})[1]["reason"] == "root_unsettled"


def test_unreadable_record_is_reported_by_preview(store):
    (store.state / vb.BINDINGS_FILE).write_text("{")
    (store.state / vb.BINDINGS_FILE).chmod(0o600)
    svc, _, _ = service(store)
    code, result = svc.preview({"roles": {"daily": "Journal"}})
    assert code == 200 and result["reason"] == "unreadable_record"


def test_connection_form_root_change_shows_stale_root_and_jobs_refuse(store):
    svc, _, _ = service(store)
    preview_and_save(svc, {"daily": "Journal"})
    assert store.save_nextcloud({**NC, "vault_folder": "Other"}, probed=True)
    assert act._write_binding(store.state, RootPin("Other", None, None), None,
                              "connection_saved", lambda: 5)
    assert svc.status()["state"] == "stale_root"
    root = type("R", (), {"state": store.state, "status": lambda self: "legacy_unpinned",
                          "pin": lambda self: RootPin("Other", None, None),
                          "read_identity": lambda self, p: None})()
    assert vb.RoleGuard(root, ("project",)).status() == "mismatch"


def test_writes_are_rate_limited(store):
    svc, _, _ = service(store)
    for _ in range(ed.RATE_LIMIT):
        svc._rate_limited()
    assert svc.reset({"confirm": True})[1]["reason"] == "rate_limited"
    assert svc.rollback({"confirm": True})[1]["reason"] == "rate_limited"


def test_a_save_drops_other_preview_handles(store):
    svc, _, _ = service(store)
    first = svc.preview({"roles": {"daily": "Journal"}})[1]["handle"]
    second = svc.preview({"roles": {"daily": "Journal"}})[1]["handle"]
    assert svc.save({"handle": first, "confirm": True})[1]["status"] == "saved"
    assert svc.save({"handle": second, "confirm": True})[1]["reason"] == "handle_expired"


def test_rollback_to_a_record_needs_a_settled_root(store):
    svc, _, _ = service(store)
    preview_and_save(svc, {"daily": "Journal"})
    assert svc.reset({"confirm": True})[1]["status"] == "reset"
    assert act._write_binding(store.state, RootPin("ObsidianVault", (oc_id(70),), INSTANCE),
                              None, "pending_rolled_back", lambda: 6)
    assert svc.rollback({"confirm": True})[1]["reason"] == "root_unsettled"
    assert not (store.state / vb.BINDINGS_FILE).exists()


def test_rate_limited_save_keeps_the_handle(store):
    svc, _, _ = service(store)
    handle = svc.preview({"roles": {"daily": "Journal"}})[1]["handle"]
    saved = list(svc._requests)
    svc._requests = [svc._clock()] * ed.RATE_LIMIT
    assert svc.save({"handle": handle, "confirm": True})[1]["reason"] == "rate_limited"
    svc._requests = saved
    assert svc.save({"handle": handle, "confirm": True})[1]["status"] == "saved"


# --- O1-A: connection forms while a mapping exists -------------------------------

def test_connection_form_cannot_move_the_root_while_a_mapping_exists(store, tmp_path):
    import http.client
    import threading
    from contextlib import closing
    from obsidian_automation.admin_dashboard import make_server
    from obsidian_automation.snapshot_job import SnapshotJob
    svc, _, _ = service(store)
    preview_and_save(svc, {"daily": "Journal"})
    store.nextcloud_probe = lambda _: True
    with closing(SnapshotJob(tmp_path / "snap", lambda _: ("no_changes", "verify", "c" * 40),
                             configured=False)) as snapshot:
        server = make_server(port=0, snapshot_job=snapshot, snapshot_setup=store,
                             observer=lambda: [], ai_observer=lambda: {})
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]

        def post(data):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            token_conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            hdr = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}"}
            token_conn.request("GET", "/api/v1/operation-session", headers=hdr)
            token = json.loads(token_conn.getresponse().read())["csrf_token"]
            token_conn.close()
            conn.request("POST", "/api/v1/nextcloud/connections", json.dumps(data),
                         {**hdr, "X-Obsidian-CSRF": token, "Content-Type": "application/json"})
            r = conn.getresponse()
            body = r.read()
            conn.close()
            return r.status, json.loads(body)
        base = {**NC, "vault_folder": "ObsidianVault"}
        try:
            for change in ({"vault_folder": "Other"}, {"nextcloud_user": "someone"},
                           {"webdav_url": "https://other.invalid/remote.php/dav/files/x/"}):
                assert post({**base, **change}) == (409, {"error": "mapping_present"})
            assert store.load_nextcloud()["vault_folder"] == "ObsidianVault"
            # A password change is still allowed.
            status, _ = post({**base, "nextcloud_password": "NEW-SECRET"})
            assert status == 200
            assert store.load_nextcloud()["nextcloud_password"] == "NEW-SECRET"
            # After a reset the Vault folder can change again.
            assert svc.reset({"confirm": True})[1]["status"] == "reset"
            assert post({**base, "vault_folder": "Other"})[0] == 200
        finally:
            server.shutdown()
            server.server_close()


def test_connection_form_message_is_shown_for_mapping_present():
    from obsidian_automation.admin_dashboard import HTML, JS
    page = HTML + JS
    assert 'error.message === "mapping_present"' in page
    assert "MAPPING_PRESENT_MESSAGE" in page


def _post(port, path, data):
    import http.client
    hdr = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}"}
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", "/api/v1/operation-session", headers=hdr)
    token = json.loads(c.getresponse().read())["csrf_token"]
    c.close()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", path, json.dumps(data),
              {**hdr, "X-Obsidian-CSRF": token, "Content-Type": "application/json"})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, json.loads(body) if body.startswith(b"{") else body


def test_snapshot_route_and_pending_marker_also_refuse(store, tmp_path):
    import threading
    from contextlib import closing
    from obsidian_automation.admin_dashboard import make_server
    from obsidian_automation.snapshot_job import SnapshotJob
    (store.state / vb.PENDING_FILE).write_text("1\n")      # only a pending change
    with closing(SnapshotJob(tmp_path / "snap", lambda _: ("no_changes", "verify", "c" * 40),
                             configured=False)) as snapshot:
        server = make_server(port=0, snapshot_job=snapshot, snapshot_setup=store,
                             observer=lambda: [], ai_observer=lambda: {})
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        try:
            full = {**NC, "vault_folder": "Other", "git_url": "https://git.invalid/a/b.git",
                    "git_user": "u", "git_token": "T", "branch": "main"}
            assert _post(port, "/api/v1/snapshot/connections", full) == (
                409, {"error": "mapping_present"})
            assert _post(port, "/api/v1/nextcloud/connections",
                         {**NC, "vault_folder": "Other"}) == (409, {"error": "mapping_present"})
            assert store.load_nextcloud()["vault_folder"] == "ObsidianVault"
        finally:
            server.shutdown()
            server.server_close()


def test_unreadable_connection_with_a_mapping_refuses(store, monkeypatch):
    from obsidian_automation.admin_dashboard import _mapping_blocks_change
    svc, _, _ = service(store)
    preview_and_save(svc, {"daily": "Journal"})
    monkeypatch.setattr(store, "load_nextcloud", lambda: None)
    assert _mapping_blocks_change(store, {"vault_folder": "Other"}) is True
