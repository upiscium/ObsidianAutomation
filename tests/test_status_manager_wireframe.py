"""#298 wireframe-root status manager: safe GET assets and preserved operator controls.

This test constructs no job, credential, Vault, Nextcloud or Git provider client.
"""
from __future__ import annotations

from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
import re
import threading

from obsidian_automation.admin_dashboard import HTML, WORKFLOW_UI_ASSETS, make_server


ASSET_ROOT = Path(__file__).resolve().parents[1] / "src/obsidian_automation/webui_design_v1"


@contextmanager
def server():
    local = make_server(port=0, observer=lambda: [], ai_observer=lambda: {})
    t = threading.Thread(target=local.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    try:
        yield local
    finally:
        local.shutdown()
        t.join(timeout=3)
        local.server_close()


def read(local, path, *, method="GET", host=None, origin=None):
    port = local.server_address[1]
    conn = HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if origin is not None:
        headers["Origin"] = origin
    try:
        conn.request(method, path, headers=headers)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def test_root_is_wireframe_obisidian_status_manager_not_link_only():
    assert "<title>Obsidian Status Manager</title>" in HTML
    assert "<h1>Obsidian Status Manager</h1>" in HTML
    assert "Global System Settings" in HTML
    assert "manager-card-grid" in HTML
    assert "manager-detail-facts" in HTML
    assert "manager-detail-slot" in HTML
    assert "manager-menu-toggle" in HTML
    assert 'src="/static/workflow-control/model.js"' in HTML
    assert 'src="/static/status-manager.js"' in HTML
    assert 'href="/static/status-manager.css"' in HTML
    ids = re.findall(r'\bid="([^"]+)"', HTML)
    assert len(ids) == len(set(ids)), "dashboard IDs must remain unique"


def test_original_operational_controls_and_connection_forms_preserved():
    for form_or_control in (
        "nextcloud-form", "connections-form", "daily-connections-form",
        "snapshot-enabled", "snapshot-save", "snapshot-run",
        "daily-enabled", "daily-save", "daily-run",
        "formatter-mode", "formatter-save", "formatter-run",
        "operation-preview", "preview-check", "preview-confirm",
        "workflow-catalog", "services",
    ):
        assert f'id="{form_or_control}"' in HTML
    assert "manager-global-nextcloud" in HTML
    assert "manager-global-gitea" in HTML
    assert "manager-global-github" in HTML
    assert 'id="manager-legacy-root"' in HTML
    assert "manager-advanced-slot" in HTML


def test_only_two_explicit_new_static_assets_no_generic_directory_exposure():
    routes = {
        "/static/status-manager.js": ("status_manager.js", "text/javascript; charset=utf-8"),
        "/static/status-manager.css": ("status_manager.css", "text/css; charset=utf-8"),
    }
    for url, (name, mime) in routes.items():
        assert WORKFLOW_UI_ASSETS[url] == (name, mime)
    with server() as s:
        for url, (name, mime) in routes.items():
            data = (ASSET_ROOT / name).read_bytes()
            for method in ("GET", "HEAD"):
                code, headers, body = read(s, url, method=method)
                assert code == 200
                assert body == (data if method == "GET" else b"")
                assert headers["Content-Type"] == mime
                assert int(headers["Content-Length"]) == len(data)
                assert headers["Cache-Control"] == "no-store"
                assert "script-src 'self'" in headers["Content-Security-Policy"]
                assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
            assert read(s, url, host="evil.invalid")[0] == 403
            assert read(s, url, origin="https://evil.invalid")[0] == 403
            assert read(s, url, method="POST")[0] == 405
        for path in ("/static/status-manager.js?x=1", "/static/status-manager.css/extra",
                     "/static/../status-manager.js", "/static/status-manager/index.html"):
            assert read(s, path)[0] == 404
        code, _, root = read(s, "/")
        assert code == 200 and b"Obsidian Status Manager" in root


def test_wireframe_js_has_safe_fixed_endpoints_and_no_new_mutation_transport():
    script = (ASSET_ROOT / "status_manager.js").read_text()
    css = (ASSET_ROOT / "status_manager.css").read_text()
    assert "innerHTML" not in script and "eval(" not in script
    assert "document.write" not in script
    assert 'method:"GET"' in script and 'redirect:"error"' in script
    for path in ("/api/v1/workflow-control","/api/v1/snapshot",
                 "/api/v1/daily","/api/v1/formatter"):
        assert path in script
    assert "fetch(" in script
    assert 'method:"POST"' not in script and 'method:"PUT"' not in script
    assert "manager-global-nextcloud" in script
    assert "manager-global-gitea" in script
    assert "daily-connections-form" in script
    assert "snapshot-controls" in script and "github-daily" in script
    assert "vault-formatter" in script
    assert 'grid-template-columns:repeat(3,minmax(0,1fr))' in css
    assert 'grid-template-columns:repeat(2,minmax(0,1fr))' in css
    assert 'grid-template-columns:1fr' in css
    assert "manager-signal" in css
    assert "prefers-reduced-motion" in css
