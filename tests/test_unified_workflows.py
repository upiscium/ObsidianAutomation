"""Read-only five-workflow MVI catalog: legacy state is not integration proof."""
from __future__ import annotations

import http.client
import json
import threading
import uuid
from contextlib import contextmanager

import pytest

from obsidian_automation.admin_dashboard import HTML, JS, make_server
from obsidian_automation.snapshot_job import SnapshotJob
from obsidian_automation.github_daily_job import DailyJob
from obsidian_automation.unified_workflows import workflow_catalog


@contextmanager
def server_with(snapshot_job=None, daily_job=None):
    def unexpected_observer():
        raise AssertionError("catalog must not read legacy systemd/AI observers")
    server = make_server(
        port=0,
        observer=unexpected_observer,
        ai_observer=unexpected_observer,
        snapshot_job=snapshot_job,
        daily_job=daily_job,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()


def request(server, path="/api/v1/workflows", method="GET", host=None, origin=None):
    port = server.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    headers = {"Host": host if host is not None else f"127.0.0.1:{port}"}
    if origin is not None:
        headers["Origin"] = origin
    try:
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def entry(catalog, identifier):
    return next(w for w in catalog["workflows"] if w["id"] == identifier)


def test_unattached_product_five_not_legacy_service_five():
    catalog = workflow_catalog(None)
    assert set(catalog) == {"version", "authority", "workflows"}
    assert catalog["version"] == 1 and catalog["authority"] == "observation_only"
    assert [w["id"] for w in catalog["workflows"]] == ["b1", "b2", "b3", "b4", "b5"]
    for identifier in ("b1", "b3", "b4", "b5"):
        item = entry(catalog, identifier)
        assert item["integration"] == "pending"
        assert item["configuration"] == "not_observed"
        assert item["last_result"] is None
        assert item["control_available"] is False
        # No unsupported claim that legacy jobs are stopped or installed.
        assert item["schedule"] in ("not_observed", "not_applicable")
    assert entry(catalog, "b5")["schedule"] == "not_applicable"
    assert entry(catalog, "b2")["integration"] == "not_attached"
    assert entry(catalog, "b2")["control_available"] is False
    assert "未接続" in entry(catalog, "b4")["note"]
    assert "Run Onceは利用できません" in entry(catalog, "b4")["note"]
    body = json.dumps(catalog)
    assert "obsidian-ai-vault-pull.service" not in body
    assert "obsidian-github-daily-apply.service" not in body


def test_b2_snapshot_has_live_settings_without_running_or_exposing_receipts(tmp_path):
    calls = []

    def fake_runner(run_id):
        calls.append(run_id)
        return "saved", "verify", "a" * 40

    job = SnapshotJob(tmp_path, fake_runner, configured=False)
    try:
        empty = entry(workflow_catalog(job), "b2")
        assert (empty["integration"], empty["configuration"], empty["schedule"]) == (
            "connected", "missing", "off",
        )
        assert not empty["control_available"] and empty["last_result"] is None
        assert calls == []
        job.connections_saved("generation-one")
        ready = entry(workflow_catalog(job), "b2")
        assert ready["configuration"] == "configured" and ready["schedule"] == "off"
        assert ready["control_available"] and ready["last_result"] is None
        assert calls == []
        job.update({"enabled": True, "interval_seconds": 60,
                    "source_name": "nextcloud", "destination_name": "gitea"})
        assert entry(workflow_catalog(job), "b2")["schedule"] == "on"
        kind, record = job.submit(uuid.uuid4().hex)
        assert kind == "accepted" and record is not None
        job._worker.join(3)
        result = entry(workflow_catalog(job), "b2")
        assert result["last_result"] == "saved"
        assert calls == [record["run_id"]]
        payload = json.dumps(workflow_catalog(job))
        assert record["run_id"] not in payload
        assert "a" * 40 not in payload
        assert "connections.json" not in payload
        for identifier in ("b1", "b3", "b4", "b5"):
            assert entry(workflow_catalog(job), identifier)["integration"] == "pending"
    finally:
        job.close()


def test_malformed_and_failed_snapshot_observation_cannot_leak_secrets():
    class Explodes:
        def status(self):
            raise RuntimeError("very-private-gitea-token")
    class Unknown:
        def status(self):
            return {
                "version": 1, "job": "vault_snapshot",
                "settings": {"configured": True, "enabled": True},
                "last_run": {"status": "token=PRIVATE"},
            }
    class Invalid:
        def status(self):
            return {"version": 2, "job": "invalid", "settings": {"configured": True}}
    for observer in (Explodes(), Invalid()):
        item = entry(workflow_catalog(observer), "b2")
        assert item["integration"] == "unavailable"
        assert item["control_available"] is False
        assert item["last_result"] is None
        assert "very-private-gitea-token" not in json.dumps(item)
    item = entry(workflow_catalog(Unknown()), "b2")
    assert item["last_result"] == "unknown"
    assert "token=PRIVATE" not in json.dumps(item)


def test_catalog_http_is_independent_read_only_and_origin_guarded():
    with server_with() as server:
        status, headers, raw = request(server)
        assert status == 200
        assert headers["Cache-Control"] == "no-store"
        assert "Access-Control-Allow-Origin" not in headers
        value = json.loads(raw)
        assert len(value["workflows"]) == 5
        assert entry(value, "b2")["integration"] == "not_attached"
        assert request(server, method="HEAD")[0] == 200
        assert request(server, method="HEAD")[2] == b""
        assert request(server, method="POST")[0] == 405
        assert request(server, host="evil.invalid")[0] == 403
        assert request(server, origin="https://evil.invalid")[0] == 403
        assert request(server, path="/api/v1/workflows/private")[0] == 404


def test_snapshot_catalog_http_updates_without_old_status_cache(tmp_path):
    job = SnapshotJob(tmp_path, lambda _: ("no_changes", "verify", "b" * 40),
                      configured=True)
    try:
        with server_with(job) as server:
            def b2():
                status, _, raw = request(server)
                assert status == 200
                return entry(json.loads(raw), "b2")
            assert b2()["schedule"] == "off"
            job.update({"enabled": True, "interval_seconds": 60,
                        "source_name": "nextcloud", "destination_name": "gitea"})
            assert b2()["schedule"] == "on"
            job.update({"enabled": False, "interval_seconds": 60,
                        "source_name": "nextcloud", "destination_name": "gitea"})
            assert b2()["schedule"] == "off"
    finally:
        job.close()


def test_ui_displays_product_workflows_without_fake_actions():
    assert 'id="workflow-catalog"' in HTML
    assert 'id="workflow-summary"' in HTML
    assert 'id="snapshot-controls"' in HTML
    assert 'aria-label="既存systemdサービス一覧"' in HTML
    assert 'fetch("/api/v1/workflows"' in JS
    assert 'function workflowCard(w)' in JS
    assert 'w.control_available === true' in JS
    assert 'link.href = "#snapshot-controls"' in JS
    assert '$("workflow-catalog").replaceChildren()' in JS
    assert "innerHTML" not in JS



def test_browser_catalog_ignores_out_of_order_success_and_failure():
    """Execute the actual catalog JS against a tiny disposable DOM stub."""
    import os
    import shutil
    import subprocess

    node = os.environ.get("NODE_BINARY") or shutil.which("node")
    if not node:
        import pytest
        pytest.skip("optional Node.js UI regression runner is not installed")

    start = JS.index("const workflowLabels = {")
    end = JS.index("function renderAi(p) {", start)
    fragment = JS[start:end]
    prefix = r"""
"use strict";
const assert = require("node:assert/strict");
const vm = require("node:vm");
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.textContent = ""; }
  set innerHTML(value) { throw new Error("HTML injection is forbidden"); }
  append(...children) {
    this.children.push(...children);
    this.lastElementChild = this.children.at(-1);
  }
  replaceChildren(...children) { this.children = children; }
}
const nodes = {
  "workflow-catalog": new Element("div"),
  "workflow-summary": new Element("p"),
};
const waiting = [];
const sandbox = {
  document: {createElement: (type) => new Element(type)},
  $: (id) => nodes[id],
  line: () => {
    const div = new Element("div");
    div.append(new Element("span"), new Element("span"));
    return div;
  },
  fetch: () => new Promise((resolve, reject) => waiting.push({resolve, reject})),
};
"""
    suffix = r"""
vm.runInNewContext(source + "\nthis.begin = workflowCatalogRefresh;", sandbox);
const ids = ["b1", "b2", "b3", "b4", "b5"];
const reply = {
  version: 1, authority: "observation_only",
  workflows: ids.map(id => ({
    id, label: id, note: "public synthetic record",
    integration: id === "b2" ? "connected" : "pending",
    configuration: id === "b2" ? "configured" : "not_observed",
    schedule: id === "b2" ? "off" : "not_observed",
    last_result: null, control_available: id === "b2",
  })),
};
function assertCounts(expected) {
  const summary = nodes["workflow-summary"].textContent;
  const matches = [...summary.matchAll(/(\d+)(?:\/5)?機能/g)];
  const counts = matches.map(match => Number(match[1]));
  assert.deepEqual(counts, expected);
  assert.equal(counts.reduce((sum, count) => sum + count, 0), 5);
  assert.match(summary, /WebUI接続済み .*共通Job未接続 .*このサーバーでは未接続 .*状態取得不可 /);
}
async function respond(value) {
  const done = sandbox.begin();
  waiting.at(-1).resolve({ok: true, json: async () => value});
  await done;
}
async function exercise() {
  // A slow old success must not re-populate cards after new B failed.
  const first = sandbox.begin();
  const second = sandbox.begin();
  waiting[1].resolve({ok: false});
  await second;
  assert.equal(nodes["workflow-catalog"].children.length, 0);
  assert.match(nodes["workflow-summary"].textContent, /取得できません/);
  waiting[0].resolve({ok: true, json: async () => reply});
  await first;
  assert.equal(nodes["workflow-catalog"].children.length, 0);
  assert.match(nodes["workflow-summary"].textContent, /取得できません/);

  // A late old failure must not wipe a newer successful five-card result.
  const third = sandbox.begin();
  const fourth = sandbox.begin();
  waiting[3].resolve({ok: true, json: async () => reply});
  await fourth;
  assert.equal(nodes["workflow-catalog"].children.length, 5);
  assert.match(nodes["workflow-summary"].textContent, /5機能/);
  waiting[2].reject(new Error("old response with private data"));
  await third;
  assert.equal(nodes["workflow-catalog"].children.length, 5);
  assert.match(nodes["workflow-summary"].textContent, /5機能/);
  assertCounts([1, 4, 0, 0]);

  // The actual no-job backend projection includes B2 not_attached, not pending.
  await respond(unattached);
  assertCounts([0, 4, 1, 0]);
  assert.equal(nodes["workflow-catalog"].children.length, 5);
  assert.ok(nodes["workflow-catalog"].children.every(card =>
    card.children.every(child => child.tag !== "a")));

  const mixed = JSON.parse(JSON.stringify(reply));
  ["connected", "pending", "not_attached", "unavailable", "pending"].forEach(
    (state, i) => { mixed.workflows[i].integration = state; });
  const injection = '<img src=x onerror="throw 1"><script>throw 2</script>';
  mixed.workflows[0].label = injection;
  mixed.workflows[0].note = injection;
  await respond(mixed);
  assertCounts([1, 2, 1, 1]);
  const card = nodes["workflow-catalog"].children[0];
  assert.equal(card.children[0].textContent, "B1 " + injection);
  assert.equal(card.children[1].textContent, injection);
  assert.ok(card.children.every(child => !["img", "script"].includes(child.tag)));

  // Every invalid newest response clears both prior cards and summary counts.
  const invalid = [null, {...reply, version: 2}, {...reply, authority: "control"},
    {...reply, workflows: reply.workflows.slice(1)}];
  for (const [field, value] of [
    ["id", "b2"], ["id", "b6"], ["integration", "configured"],
    ["integration", "unexpected"], ["integration", "__proto__"],
  ]) {
    const bad = JSON.parse(JSON.stringify(reply));
    bad.workflows[0][field] = value;
    invalid.push(bad);
  }
  for (const bad of invalid) {
    await respond(mixed);
    assertCounts([1, 2, 1, 1]);
    const old = sandbox.begin();
    await respond(bad);
    assert.equal(nodes["workflow-catalog"].children.length, 0);
    assert.match(nodes["workflow-summary"].textContent, /取得できません/);
    assert.doesNotMatch(nodes["workflow-summary"].textContent, /\d+機能/);
    waiting.at(-2).resolve({ok: true, json: async () => reply});
    await old;
    assert.equal(nodes["workflow-catalog"].children.length, 0);
    assert.match(nodes["workflow-summary"].textContent, /取得できません/);
  }
  // An invalid old response must not clear a newer valid summary either.
  const oldInvalid = sandbox.begin();
  await respond(mixed);
  waiting.at(-2).resolve({ok: true, json: async () => invalid[0]});
  await oldInvalid;
  assertCounts([1, 2, 1, 1]);
  console.log("latest_response_wins=PASS");
  console.log("exhaustive_counts_validation_and_text_safety=PASS");
}
exercise().catch(e => { console.error(e); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-"], input=(prefix + "const source = " + json.dumps(fragment) + ";\n"
                            + "const unattached = " + json.dumps(workflow_catalog(None))
                            + ";\n" + suffix),
        capture_output=True, text=True, check=False, timeout=12,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "latest_response_wins=PASS" in result.stdout
    assert "exhaustive_counts_validation_and_text_safety=PASS" in result.stdout



def test_b4_job_catalog_shows_preview_only_without_claiming_vault_publication(tmp_path):
    available = [False]
    def preview():
        return {
            "version":1, "status":"preview_ready", "date_jst":"2026-10-24",
            "capture_sha256":"a"*64, "digest_sha256":"b"*64,
            "original_event_count":21, "rendered_entity_count":5,
            "publication_authorized":False, "vault_modified":False,
            "model_used":False,
        }

    with DailyJob(tmp_path / "b4", preview, configured=lambda: available[0]) as job:
        pending = entry(workflow_catalog(None), "b4")
        assert pending["integration"] == "pending"
        before = entry(workflow_catalog(None, job), "b4")
        assert before["integration"] == "connected"
        assert before["configuration"] == "missing"
        assert before["schedule"] == "off"
        assert before["control_available"] is False
        assert "設定が必要" in before["note"]
        assert "Run Onceは実行できません" in before["note"]
        available[0] = True
        ready = entry(workflow_catalog(None, job), "b4")
        assert ready["control_available"] and ready["last_result"] is None
        assert "Run Onceは共通WebUIから利用可能" in ready["note"]
        job.update({"enabled": True, "interval_seconds": 86400})
        assert entry(workflow_catalog(None, job), "b4")["schedule"] == "on"
        kind, receipt = job.submit(uuid.uuid4().hex)
        assert kind == "accepted"
        job._worker.join(3)
        result = entry(workflow_catalog(None, job), "b4")
        assert result["last_result"] == "preview_ready"
        assert "Preview" in result["note"]
        assert "Preview生成はVaultへの反映ではありません" in result["note"]
        payload = json.dumps(workflow_catalog(None, job))
        assert receipt["run_id"] not in payload
        assert "a"*64 not in payload
        assert "b"*64 not in payload

        with server_with(daily_job=job) as server:
            status, _, raw = request(server)
            assert status == 200
            value = entry(json.loads(raw), "b4")
            assert value["integration"] == "connected"
            assert value["last_result"] == "preview_ready"


def test_b4_catalog_rejects_unknown_status_and_redacts_exceptions():
    class Explodes:
        def status(self):
            raise RuntimeError("SECRET-GITHUB-TOKEN")
    class Bad:
        def status(self):
            return {"version":1,"job":"github_daily_preview",
                    "settings":{"configured":True,"enabled":True},
                    "last_run":{"status":"token=SECRET"}}
    item = entry(workflow_catalog(None, Explodes()), "b4")
    assert item["integration"] == "unavailable"
    assert item["last_result"] is None
    assert item["control_available"] is False
    assert "Run Onceは利用できません" in item["note"]
    assert "SECRET" not in json.dumps(item)
    item = entry(workflow_catalog(None, Bad()), "b4")
    assert item["integration"] == "connected"
    assert item["last_result"] == "unknown"
    assert "SECRET" not in json.dumps(item)
    assert 'link.href = "#github-daily"' in JS
    assert "preview_ready:" in JS


@pytest.mark.parametrize("observation", [
    None, [], {"version": 2, "job": "github_daily_preview"},
    {"version": 1, "job": "other", "settings": {}},
    {"version": 1, "job": "github_daily_preview", "settings": None},
    *({"version": 1, "job": "github_daily_preview",
       "publication_enabled": True, "private": "SECRET", "settings": settings}
      for settings in ({}, {"configured": "SECRET", "enabled": True},
                       {"configured": True, "enabled": 1})),
    # Even a failure after initial validation must discard actionable guidance.
    {"version": 1, "job": "github_daily_preview", "publication_enabled": True,
     "settings": {"configured": True, "enabled": True},
     "last_run": {"status": ["SECRET"]}},
])
def test_b4_malformed_observation_never_advertises_actions(observation):
    class Fake:
        def status(self):
            return observation

    item = entry(workflow_catalog(None, Fake()), "b4")
    assert item["integration"] == "unavailable"
    assert item["configuration"] == item["schedule"] == "not_observed"
    assert item["last_result"] is None and item["control_available"] is False
    assert "状態を取得できません" in item["note"]
    assert "Run Onceは利用できません" in item["note"]
    assert "Writer有効" not in item["note"]
    assert "SECRET" not in json.dumps(item)


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("publication", [None, False, True, 1, "true"])
def test_b4_guidance_uses_validated_configuration_and_exact_writer_flag(configured, publication):
    class Fake:
        def status(self):
            return {"version": 1, "job": "github_daily_preview",
                    "settings": {"configured": configured, "enabled": True},
                    "publication_enabled": publication}

    item = entry(workflow_catalog(None, Fake()), "b4")
    assert item["integration"] == "connected"
    assert item["control_available"] is configured
    assert item["configuration"] == ("configured" if configured else "missing")
    assert item["schedule"] == ("on" if configured else "off")
    if configured:
        assert "Run Onceは共通WebUIから利用可能" in item["note"]
    else:
        assert "設定が必要" in item["note"]
        assert "Run Onceは実行できません" in item["note"]
    if publication is True:
        assert "Writer有効。" in item["note"]
        assert "検証済み結果で確認" in item["note"]
    else:
        assert "Writer有効。" not in item["note"]
        assert "Preview生成はVaultへの反映ではありません" in item["note"]



def test_b4_browser_actions_reconcile_both_panel_and_unified_catalog():
    """B4 settings and async Run now both refresh the five-workflow catalog."""
    start = JS.index('$("daily-save").addEventListener')
    end = JS.index('let previewGeneration', start)
    handlers = JS[start:end]
    save = handlers[:handlers.index('async function pollDailyResult')]
    run = handlers[handlers.index('$("daily-run").addEventListener'):]
    poll = handlers[handlers.index('async function pollDailyResult'):
                    handlers.index('$("daily-run").addEventListener')]
    assert 'await dailyRefresh();' in save
    assert 'await workflowCatalogRefresh();' in save
    assert 'await pollDailyResult(runId);' in run
    assert 'await workflowCatalogRefresh();' in poll
    assert 'for (let attempt = 0; attempt < 90; ++attempt)' in poll
    assert '["queued","running"].includes(receipt.status)' in poll
    assert 'result === "preview_ready"' in poll
    assert "preview_ready:" in JS



def test_real_b4_browser_javascript_polls_until_terminal_and_refreshes_catalog():
    """Execute the authored JS handler, not a Python imitation of its async flow."""
    import os
    import shutil
    import subprocess
    import pytest

    node = os.environ.get("NODE_BINARY") or shutil.which("node")
    if not node:
        pytest.skip("Node.js optional UI runtime is absent")

    start = JS.index("async function dailyRefresh()")
    end = JS.index("let previewGeneration", start)
    fragment = JS[start:end]
    script = r"""
"use strict";
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
class Node {
  constructor() {
    this.textContent = ""; this.disabled = false; this.checked = false;
    this.value = "86400"; this.handlers = {};
  }
  addEventListener(type, callback) { this.handlers[type] = callback; }
}
const names = [
  "daily-state", "daily-last", "daily-due", "daily-enabled",
  "daily-interval", "daily-save", "daily-run", "daily-refresh", "daily-message"
];
const nodes = Object.fromEntries(names.map(id => [id, new Node()]));
const runId = "a".repeat(32);
let runStatus = null;
let pollCount = 0;
let catalogCalls = 0;
let postCount = 0;
const sandbox = {
  $: id => nodes[id],
  setTimeout: callback => callback(),
  crypto: {randomUUID: () => "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"},
  snapshotPost: async (path, data) => {
    assert.equal(path, "/api/v1/daily/runs");
    assert.match(data.request_id, /^[a-f0-9]{32}$/);
    ++postCount; runStatus = "queued";
    return {run_id: runId};
  },
  workflowCatalogRefresh: async () => {++catalogCalls;},
  fetch: async path => {
    if (path === "/api/v1/daily") {
      return {ok:true, json:async () => ({
        version:1, job:"github_daily_preview",
        settings:{enabled:false, configured:true,
                  interval_seconds:86400, next_due:null},
        last_run:runStatus ? {status:runStatus, date_jst:"2026-10-24"} : null
      })};
    }
    assert.equal(path, "/api/v1/daily/runs/" + runId);
    ++pollCount;
    runStatus = pollCount >= 3 ? "preview_ready" : "running";
    return {ok:true,json:async()=>({run_id:runId,status:runStatus})};
  }
};
vm.runInNewContext(fs.readFileSync(0,"utf8"), sandbox);
assert.equal(typeof nodes["daily-run"].handlers.click,"function");
Promise.resolve(nodes["daily-run"].handlers.click()).then(() => {
  assert.equal(postCount,1);
  assert.equal(pollCount,3);
  assert.ok(catalogCalls>=2);
  assert.ok(nodes["daily-last"].textContent.includes("preview_ready"));
  assert.ok(nodes["daily-message"].textContent.includes("Vault"));
  assert.equal(runStatus,"preview_ready");
  assert.equal(typeof nodes["daily-refresh"].handlers.click,"function");
  const beforeRefresh = catalogCalls;
  return Promise.resolve(nodes["daily-refresh"].handlers.click()).then(() => {
    assert.ok(catalogCalls > beforeRefresh);
    assert.equal(postCount,1); // Refresh never re-submits the job.
    assert.ok(nodes["daily-last"].textContent.includes("preview_ready"));
  });
}).catch(err => { console.error(err); process.exitCode=1; });
"""
    result = subprocess.run([node, "-e", script], input=fragment,
                            text=True, capture_output=True, timeout=12)
    assert result.returncode == 0, result.stderr
