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
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
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


def test_top_status_rejects_historical_success_when_latest_attempt_unknown():
    """Execute the actual dashboard status classifier: older green must not hide unknown."""
    import shutil
    import subprocess
    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable; other HTML/HTTP contract tests remain active")
    script = r"""
const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const start=source.indexOf('  function displayStatus(id) {');
const end=source.indexOf('  function fact(',start);
assert.ok(start>0 && end>start,'status implementation must be present');
const context=vm.createContext({
  state:{phase:'ready',services:[{
    id:'b1',integration:'connected',adapterMode:'read_only',execution:'Idle',
    lastAttempt:{state:'completed',outcome:'unknown',
      effect:{kind:'review_artifact',state:'unknown',changed:null}},
    lastVerifiedOutcome:{status:'success',
      effect:{kind:'review_artifact',state:'verified',changed:true}}
  }],jobs:{}},
  IN_PROGRESS:new Set(['queued','running']),
  FAILURE:new Set(['failed','publish_failed','conflict']),
  SUCCESS:new Set(['saved','no_changes','published','applied'])
});
vm.runInContext(
  "const jobFor=id=>state.jobs[id]||null;"+
  "const serviceFor=id=>state.services.find(x=>x.id===id)||null;"+
  source.slice(start,end)+";globalThis.classify=displayStatus;",context
);
const first=context.classify('b1');
assert.equal(first.kind,'unknown','latest unknown must never inherit historical green');
context.state.services[0].lastAttempt.outcome='failure';
context.state.services[0].lastAttempt.effect.state='unknown';
assert.equal(context.classify('b1').kind,'failure');
context.state.services[0].lastAttempt.outcome='success';
context.state.services[0].lastAttempt.effect.state='verified';
assert.equal(context.classify('b1').kind,'success');
"""
    result = subprocess.run(
        [node, "-e", script, str(ASSET_ROOT / "status_manager.js")],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_status_manager_refuses_cross_job_terminal_states():
    """Never project an unrelated Job's terminal state as verified success."""
    import shutil
    import subprocess
    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable; other HTML/HTTP tests remain active")
    script = r"""
const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const sets=source.slice(
  source.indexOf('  const JOB_STATUSES = Object.freeze('),
  source.indexOf('  const VIEW = Object.freeze('));
const start=source.indexOf('  function validJob(raw,id) {');
const end=source.indexOf('  function serviceFor(id) {',start);
assert.ok(sets.length>100 && start>0 && end>start);
const context=vm.createContext({
  JOB_ENDPOINT:{b2:['/api/v1/snapshot','vault_snapshot'],
    b4:['/api/v1/daily','github_daily_preview'],
    formatter:['/api/v1/formatter','vault_formatter']}
});
vm.runInContext(sets+source.slice(start,end)+';globalThis.parse=validJob;',context);
const make=(job,status)=>({version:1,job,
  settings:{configured:true,enabled:true,mode:'dry-run'},
  last_run:{status,stage:'verify'}
});
const real=[
 ['b2','vault_snapshot','saved'],
 ['b4','github_daily_preview','preview_ready'],
 ['formatter','vault_formatter','would_change'],
];
for (const [id,job,status] of real) assert.ok(context.parse(make(job,status),id));
for (const [id,job,status] of [
 ['b2','vault_snapshot','published'],
 ['b4','github_daily_preview','saved'],
 ['formatter','vault_formatter','published'],
 ['formatter','vault_formatter','preview_ready']
]) assert.equal(context.parse(make(job,status),id),null,
  'wrong Job terminal must not show historical success');
"""
    result = subprocess.run(
        [node, "-e", script, str(ASSET_ROOT / "status_manager.js")],
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
