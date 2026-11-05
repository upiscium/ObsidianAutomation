"""#304 R0: opt-in connection-readiness checklist; never executes a Job."""
from __future__ import annotations

from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
import re
import shutil
import subprocess
import threading

import pytest

from obsidian_automation.admin_dashboard import HTML, WORKFLOW_UI_ASSETS, make_server

ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "src/obsidian_automation/webui_design_v1/setup_readiness.js"


@contextmanager
def serving():
    app = make_server(port=0, observer=lambda: [], ai_observer=lambda: {})
    t = threading.Thread(
        target=app.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    t.start()
    try:
        yield app
    finally:
        app.shutdown()
        t.join(timeout=3)
        app.server_close()


def request(app, path, method="GET", *, host=None, origin=None):
    port = app.server_address[1]
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


def test_setup_guide_is_inside_global_settings_and_is_opt_in():
    assert '<title>Obsidian Status Manager</title>' in HTML
    assert 'id="manager-global"' in HTML
    assert 'id="manager-setup-readiness"' in HTML
    assert 'id="manager-setup-check" type="button"' in HTML
    assert 'id="manager-setup-results"' in HTML
    assert 'src="/static/setup-readiness.js"' in HTML
    assert HTML.index('id="manager-global"') < HTML.index('id="manager-setup-readiness"')
    assert HTML.index('id="manager-setup-readiness"') < HTML.index('id="manager-global-content"')
    assert "読み取り専用" in HTML
    ids = re.findall(r'\bid="([^"]+)"', HTML)
    assert len(ids) == len(set(ids))


def test_existing_connection_save_forms_and_operation_ports_are_unmodified():
    for name in (
        "nextcloud-form", "connections-form", "daily-connections-form",
        "snapshot-run", "snapshot-save", "daily-run", "daily-save",
        "formatter-run", "formatter-save", "manager-global-nextcloud",
        "manager-global-gitea", "manager-global-github",
    ):
        assert f'id="{name}"' in HTML
    assert len(re.findall('id="' + re.escape(name) + '"', HTML)) == 1


def test_new_static_asset_is_exact_allowlisted_get_head_only():
    uri = "/static/setup-readiness.js"
    mime = "text/javascript; charset=utf-8"
    assert WORKFLOW_UI_ASSETS[uri] == ("setup_readiness.js", mime)
    assert ASSET.is_file()
    with serving() as app:
        for verb in ("GET", "HEAD"):
            status, headers, body = request(app, uri, method=verb)
            assert status == 200
            assert headers["Content-Type"] == mime
            assert body == (ASSET.read_bytes() if verb == "GET" else b"")
            assert int(headers["Content-Length"]) == ASSET.stat().st_size
            assert "script-src 'self'" in headers["Content-Security-Policy"]
            assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
            assert headers["Cache-Control"] == "no-store"
        for verb in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            assert request(app, uri, method=verb)[0] == 405
        assert request(app, uri, host="evil.invalid")[0] == 403
        assert request(app, uri, origin="https://evil.invalid")[0] == 403
        for invalid in (
            uri+"?x=1", uri+"/", "/static/../setup-readiness.js",
            "/static/setup-readiness/index.html", "/static/%73etup-readiness.js"
        ):
            assert request(app, invalid)[0] == 404
        assert request(app, "/")[0] == 200


def test_setup_guide_source_does_not_create_new_write_transport():
    source = ASSET.read_text()
    assert "innerHTML" not in source
    assert "document.write" not in source
    assert "eval(" not in source
    assert "localStorage.setItem" not in source
    assert "XMLHttpRequest" not in source
    assert 'method:"POST"' not in source
    assert 'method:"PUT"' not in source
    for endpoint in (
        "/api/v1/nextcloud/connections",
        "/api/v1/snapshot/connections",
        "/api/v1/daily/connections",
    ):
        assert endpoint in source
    assert 'method:"GET"' in source
    assert 'redirect:"error"' in source
    assert 'credentials:"omit"' in source


def test_node_readiness_contract_and_fake_browser_transport():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable; Python/HTTP allowlist checks still pass")
    result = subprocess.run(
        [node, str(ROOT / "tests/setup_readiness.test.cjs")],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "setup_readiness: 11 PASS" in result.stdout
