"""#303 V1/C read-only picker service and default-OFF Dashboard routes."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import threading

import pytest

from obsidian_automation import admin_dashboard
from obsidian_automation.admin_dashboard import HTML, make_server
from obsidian_automation.vault_root_selection import (
    HANDLE_TTL_S, TrustedRootContext, VaultRootSelectionService,
)
from test_303_v1b2_dav_verifier import ACCOUNT, PREFIX, VERIFIED, FakeNextcloud, oc_id

JS_PATH = (Path(admin_dashboard.__file__).parent / "webui_design_v1" / "vault_root.js")


def context(**kw):
    base = dict(account=ACCOUNT, active="Other", active_resource_id=oc_id(20),
                container_resource_id=oc_id(1), source_revision="rev1",
                jobs_in_flight=False)
    base.update(kw)
    return TrustedRootContext(**base)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def service(nc=None, *, proof=VERIFIED, ctx=None, clock=None):
    holder = {"ctx": ctx or context()}
    svc = VaultRootSelectionService(
        context=lambda: holder["ctx"], transport=nc if nc is not None else FakeNextcloud(),
        proof=proof, clock=clock or Clock())
    return svc, holder


def labels(body):
    return [c["label"] for c in body["candidates"]]


def test_default_service_is_unproven_and_makes_no_calls():
    nc = FakeNextcloud()
    svc = VaultRootSelectionService(context=lambda: context(), transport=nc)
    assert svc.status()["provider"] == "unproven"
    assert svc.candidates({}) == (200, {"version": 1, "status": "blocked",
                                        "reason": "provider_unproven", "candidates": []})
    assert svc.preview({"candidate": "x"})[1]["reason"] == "provider_unproven"
    assert nc.calls == []


def test_missing_binding_is_reported_without_io():
    nc = FakeNextcloud()
    svc, holder = service(nc)
    holder["ctx"] = None
    assert svc.candidates({})[1]["reason"] == "binding_unavailable"
    assert svc.status()["binding"] == "unavailable"
    assert nc.calls == []


def test_listing_omits_files_mounts_and_returns_only_labels_and_handles():
    svc, _ = service()
    code, body = svc.candidates({})
    assert code == 200 and body["status"] == "ok"
    assert labels(body) == ["Other", "Vault"]
    text = json.dumps(body)
    for leak in (PREFIX, "ocsynthetic01", oc_id(10), "rev1", "etag"):
        assert leak not in text


def test_nested_listing_and_preview_flow():
    nc = FakeNextcloud()
    svc, _ = service(nc)
    vault = next(c for c in svc.candidates({})[1]["candidates"] if c["label"] == "Vault")
    nested = svc.candidates({"parent": vault["handle"]})[1]
    assert labels(nested) == ["Notes"]
    code, result = svc.preview({"candidate": nested["candidates"][0]["handle"]})
    assert code == 200
    assert result == {"version": 1, "status": "candidate_valid",
                      "reason": "requires_revalidation_and_activation",
                      "writes_vault": False, "activation_allowed": False,
                      "requires_explicit_activation": True}


def test_preview_rejects_folder_recreated_after_listing():
    nc = FakeNextcloud()
    svc, _ = service(nc)
    vault = next(c for c in svc.candidates({})[1]["candidates"] if c["label"] == "Vault")
    nc.nodes["Vault"] = (77, True, "")
    nc.nodes["Vault/Notes"] = (78, True, "")
    nc.nodes["Vault/readme.md"] = (79, False, "")
    assert svc.preview({"candidate": vault["handle"]})[1]["reason"] == "identity_unverified"


def test_active_root_preview_is_no_change():
    svc, _ = service()
    other = next(c for c in svc.candidates({})[1]["candidates"] if c["label"] == "Other")
    assert svc.preview({"candidate": other["handle"]})[1]["status"] == "no_change"


def test_handles_expire_and_are_bound_to_source_revision():
    clock = Clock()
    svc, holder = service(clock=clock)
    handle = svc.candidates({})[1]["candidates"][0]["handle"]
    holder["ctx"] = context(source_revision="rev2")
    assert svc.preview({"candidate": handle})[1]["reason"] == "stale_source"
    assert svc.candidates({"parent": handle})[1]["reason"] == "stale_source"
    holder["ctx"] = context()
    clock.now += HANDLE_TTL_S + 1
    assert svc.preview({"candidate": handle})[1]["reason"] == "handle_expired"


def test_unknown_or_malformed_handles_and_payloads():
    svc, _ = service()
    assert svc.preview({"candidate": "forged"})[1]["reason"] == "handle_expired"
    assert svc.preview({"candidate": 5})[1]["reason"] == "handle_expired"
    assert svc.preview({"candidate": "x", "path": "Vault"})[0] == 400
    assert svc.preview([])[0] == 400
    assert svc.candidates({"path": "Vault"})[0] == 400


def test_jobs_in_flight_blocks_preview():
    svc, holder = service()
    handle = svc.candidates({})[1]["candidates"][1]["handle"]
    holder["ctx"] = context(jobs_in_flight=True)
    assert svc.preview({"candidate": handle})[1]["reason"] == "jobs_in_flight"


def test_service_without_actions_has_no_effectful_surface():
    public = {n for n in dir(VaultRootSelectionService) if not n.startswith("_")}
    assert public == {"status", "candidates", "preview", "enroll", "activate", "rollback"}
    nc = FakeNextcloud()
    svc, _ = service(nc)
    for call, payload in ((svc.enroll, {}), (svc.activate, {"candidate": "x", "confirm": True}),
                          (svc.rollback, {"confirm": True})):
        assert call(payload) == (405, {"error": "activation_not_available"})
    assert nc.calls == []
    assert svc.status()["activation"] == "not_available"


@pytest.fixture
def dashboard():
    servers = []

    def start(selection=None):
        server = make_server(port=0, observer=lambda: [], ai_observer=lambda: {},
                             vault_root_selection=selection)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        port = server.server_address[1]

        def request(method, path, data=None, *, csrf=None,
                    origin=f"http://127.0.0.1:{port}"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
            headers = {"Host": f"127.0.0.1:{port}"}
            if origin:
                headers["Origin"] = origin
            if csrf:
                headers["X-Obsidian-CSRF"] = csrf
            if data is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, json.dumps(data) if data is not None else None,
                         headers=headers)
            response = conn.getresponse()
            status, raw = response.status, response.read()
            conn.close()
            return status, json.loads(raw) if raw.startswith(b"{") else raw

        def token():
            return request("GET", "/api/v1/operation-session")[1]["csrf_token"]
        return request, token
    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_routes_are_off_by_default(dashboard):
    request, token = dashboard()
    assert request("GET", "/api/v1/vault-root") == request("GET", "/api/v1/unknown-route")
    assert request("GET", "/api/v1/vault-root") == (404, b"Not found")
    unknown = request("POST", "/api/v1/unknown-route", {}, csrf=token())
    for path in ("/api/v1/vault-root/candidates", "/api/v1/vault-root/previews"):
        assert request("POST", path, {}, csrf=token()) == unknown == (405, b"Method not allowed")


def test_main_does_not_enable_picker(monkeypatch):
    seen = {}

    class Stop(Exception):
        pass

    def fake_make_server(**kwargs):
        seen.update(kwargs)
        raise Stop
    monkeypatch.setattr(admin_dashboard, "make_server", fake_make_server)
    with pytest.raises(Stop):
        admin_dashboard.main(["--port", "0"])
    assert seen.get("vault_root_selection") is None


def test_enabled_routes_require_csrf_and_stay_read_only(dashboard):
    nc = FakeNextcloud()
    svc, _ = service(nc)
    request, token = dashboard(svc)
    status = request("GET", "/api/v1/vault-root")
    assert status == (200, {"version": 1, "enabled": True, "provider": "verified",
                            "binding": "available", "activation": "not_available",
                            "writes_vault": False})
    assert request("POST", "/api/v1/vault-root/candidates", {})[0] == 403
    for origin in ("http://evil.invalid", None):
        assert request("POST", "/api/v1/vault-root/candidates", {}, csrf=token(),
                       origin=origin)[0] == 403
    assert nc.calls == []
    code, body = request("POST", "/api/v1/vault-root/candidates", {}, csrf=token())
    assert code == 200 and labels(body) == ["Other", "Vault"]
    vault = next(c for c in body["candidates"] if c["label"] == "Vault")
    code, result = request("POST", "/api/v1/vault-root/previews",
                           {"candidate": vault["handle"]}, csrf=token())
    assert code == 200 and result["status"] == "candidate_valid"
    assert result["activation_allowed"] is False
    assert all(depth in ("0", "1") for _, depth in nc.calls)
    assert request("POST", "/api/v1/vault-root/previews", None, csrf=token())[0] == 400


def test_static_script_is_served_and_page_references_it(dashboard):
    request, _ = dashboard()
    assert '<script defer src="/static/vault-root.js"></script>' in HTML
    code, body = request("GET", "/static/vault-root.js")
    assert code == 200 and body == JS_PATH.read_bytes()


def test_script_renders_labels_as_text_and_switches_only_after_confirm():
    js = JS_PATH.read_text(encoding="utf-8")
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js
    assert "/api/v1/nextcloud/connections" not in js
    assert "if (!r.ok) return null;" in js and "if (!status) return;" in js  # OFF: inert
    posts = set()
    for line in js.splitlines():
        if 'post("/api/' in line:
            posts.add(line.split('post("', 1)[1].split('"', 1)[0])
    assert posts == {"/api/v1/vault-root/candidates", "/api/v1/vault-root/previews",
                     "/api/v1/vault-root/enrollment", "/api/v1/vault-root/activations",
                     "/api/v1/vault-root/rollbacks"}
    # effectful POSTs come after an explicit confirmation dialog in the same handler
    for path in ("/api/v1/vault-root/activations", "/api/v1/vault-root/rollbacks"):
        at = js.index('"' + path + '"')
        handler = js[js.rindex("addEventListener", 0, at):at]
        assert "window.confirm(" in handler and "if (!ok) return;" in handler or \
            "if (!window.confirm(" in handler
    assert "confirm: true" in js
    # the switch button appears only for a candidate_valid preview
    assert 'data.status === "candidate_valid" && status.activation === "available"' in js


def test_rate_limit_bounds_provider_reads():
    from obsidian_automation.vault_root_selection import RATE_LIMIT, RATE_WINDOW_S
    clock = Clock()
    nc = FakeNextcloud()
    svc, _ = service(nc, clock=clock)
    for _ in range(RATE_LIMIT):
        assert svc.candidates({})[1]["status"] == "ok"
    calls = len(nc.calls)
    assert svc.candidates({})[1]["reason"] == "rate_limited"
    assert svc.preview({"candidate": "x"})[1]["reason"] == "rate_limited"
    assert len(nc.calls) == calls
    clock.now += RATE_WINDOW_S
    assert svc.candidates({})[1]["status"] == "ok"


def test_restricted_service_requires_explicit_flag():
    from dataclasses import replace
    restricted = replace(VERIFIED, C="UNKNOWN")
    nc = FakeNextcloud()
    svc = VaultRootSelectionService(context=lambda: context(), transport=nc, proof=restricted)
    assert svc.candidates({})[1]["reason"] == "provider_unproven" and nc.calls == []
    svc = VaultRootSelectionService(context=lambda: context(), transport=nc,
                                    proof=restricted, restricted_coherence=True)
    assert svc.status()["provider"] == "restricted"
    vault = next(c for c in svc.candidates({})[1]["candidates"] if c["label"] == "Vault")
    assert svc.preview({"candidate": vault["handle"]})[1]["status"] == "candidate_valid"


def test_script_guards_double_click():
    js = JS_PATH.read_text(encoding="utf-8")
    assert "if (button.disabled) return;" in js and "rate_limited" in js
    for name in ("check", "enroll", "apply", "rollback"):
        assert f"once({name}," in js


def test_restricted_status_is_labelled_and_explained():
    from dataclasses import replace
    svc = VaultRootSelectionService(context=lambda: context(), transport=FakeNextcloud(),
                                    proof=replace(VERIFIED, C="UNKNOWN"),
                                    restricted_coherence=True)
    assert svc.status()["provider"] == "restricted"
    assert service()[0].status()["provider"] == "verified"
    js = JS_PATH.read_text(encoding="utf-8")
    assert "検出できません" in js and 'status.provider !== "unproven"' in js
    # option A (L scoped to Nextcloud's index): the caveat names what is not covered
    for phrase in ("他の端末でフォルダが変更", "サーバーに直接置かれたファイル",
                   "再スキャン前の外部ストレージ"):
        assert phrase in js
