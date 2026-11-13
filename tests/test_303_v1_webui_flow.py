"""#303 V1 end to end through the real Dashboard HTTP handler (synthetic provider).

Uses the production proof register (A/I/X/L/R VERIFIED for V1-S1, C unknown)
and an in-process fake Nextcloud. No network, no real Vault.
"""
from __future__ import annotations

import http.client
import json
import threading

import pytest

from obsidian_automation import vault_root_activation as act
from obsidian_automation import vault_root_enrollment as en
from obsidian_automation import vault_root_dav_verifier as v
from obsidian_automation.admin_dashboard import make_server
from obsidian_automation.snapshot_setup import SetupStore
from obsidian_automation.vault_root_webui import build_vault_root_ui, jobs_busy
from test_303_v1b2_dav_verifier import INSTANCE, PREFIX, FakeNextcloud
from test_303_v1d_vault_root_activation import NC, _Daily, _Formatter, _Job, _Snapshot


class FakeAccount(FakeNextcloud):
    """FakeNextcloud that also answers the enrollment read and OCS cloud/user."""

    def propfind(self, origin, href, depth, body, *, timeout, max_bytes):
        if body != en.ENROLL_BODY:
            return super().propfind(origin, href, depth, body,
                                    timeout=timeout, max_bytes=max_bytes)
        self.calls.append((href, "enroll"))
        xml = ('<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:" '
               'xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">'
               f'<d:response><d:href>{PREFIX}</d:href><d:propstat><d:prop>'
               '<d:resourcetype><d:collection/></d:resourcetype>'
               f'<oc:fileid>1</oc:fileid><oc:id>00000001{INSTANCE}</oc:id>'
               '<nc:mount-type></nc:mount-type></d:prop>'
               '<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>')
        return v.DavResponse(207, "application/xml; charset=utf-8", "", False, xml.encode())

    def ocs_current_user(self, origin, *, timeout, max_bytes):
        self.calls.append(("ocs", "get"))
        body = json.dumps({"ocs": {"meta": {"statuscode": 100},
                                   "data": {"id": "synthetic_user"}}}).encode()
        return v.DavResponse(200, "application/json", "", False, body)


@pytest.fixture
def app(tmp_path):
    store = SetupStore(tmp_path / "state", obscure=lambda _: "encoded")
    assert store.save_nextcloud(dict(NC), probed=True)
    nc = FakeAccount()
    server = make_server(port=0, snapshot_setup=store, observer=lambda: [],
                         ai_observer=lambda: {})
    jobs = dict(snapshot_job=_Snapshot(), daily_job=_Daily(), formatter_job=_Formatter(),
                b3_observer_job=_Job())
    for name, job in jobs.items():
        setattr(server, name, job)
    server.vault_root_selection = build_vault_root_ui(store, server,
                                                      transport_factory=lambda saved: nc)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def request(method, path, data=None, csrf=True):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        headers = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}"}
        if csrf and method == "POST":
            token = request("GET", "/api/v1/operation-session")[1]["csrf_token"]
            headers["X-Obsidian-CSRF"] = token
        if data is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, json.dumps(data) if data is not None else None, headers)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, json.loads(body) if body.startswith(b"{") else body
    yield request, store, nc, jobs
    server.shutdown()
    server.server_close()


def labels(body):
    return {c["label"]: c["handle"] for c in body["candidates"]}


def test_full_flow_enroll_preview_switch_rollback(app):
    request, store, nc, jobs = app
    status = request("GET", "/api/v1/vault-root")[1]
    assert status["enrolled"] is False and status["activation"] == "not_available"
    assert status["active_label"] == "Other" and status["provider"] == "restricted"
    assert nc.calls == []                                    # GET does no provider I/O

    assert request("POST", "/api/v1/vault-root/enrollment", {})[1]["status"] == "enrolled"
    status = request("GET", "/api/v1/vault-root")[1]
    assert status["enrolled"] is True and status["activation"] == "available"

    handles = labels(request("POST", "/api/v1/vault-root/candidates", {})[1])
    assert set(handles) == {"Other", "Vault"}
    preview = request("POST", "/api/v1/vault-root/previews", {"candidate": handles["Vault"]})[1]
    assert preview["status"] == "candidate_valid" and preview["activation_allowed"] is False

    assert request("POST", "/api/v1/vault-root/activations",
                   {"candidate": handles["Vault"]})[0] == 400       # no confirmation
    assert request("POST", "/api/v1/vault-root/activations",
                   {"candidate": handles["Vault"], "confirm": "yes"})[0] == 400
    done = request("POST", "/api/v1/vault-root/activations",
                   {"candidate": handles["Vault"], "confirm": True})[1]
    assert done == {"version": 1, "status": "activated", "reason": "requires_job_reenable",
                    "writes_vault": False}
    assert store.load_nextcloud()["vault_folder"] == "Vault"
    assert jobs["daily_job"].saved and jobs["formatter_job"].saved   # schedules paused
    assert act.check_root_pin(store, lambda path: "00000010" + INSTANCE) == "ok"
    status = request("GET", "/api/v1/vault-root")[1]
    assert status["active_label"] == "Vault" and status["can_rollback"] is True

    # every handle predates the new binding
    assert request("POST", "/api/v1/vault-root/previews",
                   {"candidate": handles["Other"]})[1]["reason"] == "handle_expired"

    assert request("POST", "/api/v1/vault-root/rollbacks", {})[0] == 400
    back = request("POST", "/api/v1/vault-root/rollbacks", {"confirm": True})[1]
    assert back["status"] == "rolled_back"
    assert store.load_nextcloud()["vault_folder"] == "Other"
    assert request("GET", "/api/v1/vault-root")[1]["can_rollback"] is False


def test_switch_refused_while_a_job_runs(app):
    request, store, nc, jobs = app
    assert request("POST", "/api/v1/vault-root/enrollment", {})[1]["status"] == "enrolled"
    handles = labels(request("POST", "/api/v1/vault-root/candidates", {})[1])
    jobs["daily_job"].db.execute("INSERT INTO runs VALUES ('running')")
    result = request("POST", "/api/v1/vault-root/activations",
                     {"candidate": handles["Vault"], "confirm": True})[1]
    assert result["status"] == "blocked" and result["reason"] == "jobs_in_flight"
    assert store.load_nextcloud()["vault_folder"] == "Other"


def test_effectful_routes_require_csrf(app):
    request, store, nc, jobs = app
    for path, body in (("/api/v1/vault-root/enrollment", {}),
                       ("/api/v1/vault-root/activations", {"candidate": "x", "confirm": True}),
                       ("/api/v1/vault-root/rollbacks", {"confirm": True})):
        assert request("POST", path, body, csrf=False)[0] == 403
    assert nc.calls == [] and store.load_nextcloud()["vault_folder"] == "Other"


def test_jobs_busy_fails_closed(app):
    class Broken:
        @property
        def snapshot_job(self):
            return self

        @property
        def db(self):
            raise RuntimeError("db gone")
    assert jobs_busy(Broken()) is True


def test_installer_adds_flag_only_when_asked():
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("installer_303", root / "tools/install_snapshot_lxc.py")
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    assert "--vault-root-ui" not in mod._service_unit()
    assert "--vault-root-ui" not in mod._service_unit("10.12.2.15")
    unit = mod._service_unit("10.12.2.15", vault_root_ui=True)
    assert "--state /var/lib/obsidian-snapshot --bind 10.12.2.15 --vault-root-ui\n" in unit
    assert mod._build_parser().parse_args([]).vault_root_ui is False


def test_app_flag_requires_state(monkeypatch, capsys):
    import sys
    from obsidian_automation import snapshot_app
    monkeypatch.setattr(sys, "argv", ["obsidian-snapshot-app", "--config", "/x.toml",
                                      "--vault-root-ui"])
    with pytest.raises(SystemExit):
        snapshot_app.main()
    assert "--vault-root-ui requires" in capsys.readouterr().err
