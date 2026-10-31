"use strict";
// Authored live renderer + controllable asynchronous transport, no network,
// browser dependencies, credentials or real clock delays. Not browser E2E.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const base = path.resolve(__dirname, "../src/obsidian_automation/webui_design_v1");
const html = fs.readFileSync(path.join(base, "live.html"), "utf8");
const model = fs.readFileSync(path.join(base, "model.js"), "utf8");
const live = fs.readFileSync(path.join(base, "live.js"), "utf8");
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.handlers = {}; this._text = ""; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(" "); }
  set innerHTML(_) { throw Error("HTML sink forbidden"); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ""; this.children = children; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(type, handler) { this.handlers[type] = handler; }
  fire(type) { return this.handlers[type](); }
  all() { return this.children.flatMap(c => [c, ...c.all()]); }
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
const flush = async () => { for (let i = 0; i < 20; ++i) await Promise.resolve(); };
function browser() {
  const nodes = {};
  for (const match of html.matchAll(/<([a-z0-9-]+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) nodes[match[2]] = new Element(match[1]);
  nodes["ai-health-status"].textContent = html.match(/<p id="ai-health-status"[^>]*>([^<]+)<\/p>/)[1];
  const requests = [], timers = new Map(); let nextTimer = 0;
  const forbidden = () => { throw Error("Unexpected side effect"); };
  const context = vm.createContext({
    document: {getElementById: id => nodes[id], createElement: tag => new Element(tag)},
    AbortController, Date,
    fetch(url, options) { const d = deferred(); requests.push({...d, url, options}); return d.promise; },
    setTimeout(fn, delay) { assert.equal(delay, 10000); const id = ++nextTimer; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); }, setInterval: forbidden,
    XMLHttpRequest: forbidden, WebSocket: forbidden, localStorage: {setItem: forbidden}, navigator: {sendBeacon: forbidden}
  });
  vm.runInContext(model, context); vm.runInContext(live, context);
  // This fixture is deliberately synthetic; make its adapter modes read_only
  // to test transport provenance independently from mock source semantics.
  function payload() {
    const p = JSON.parse(JSON.stringify(context.ObsidianDesign.mockControlCatalog()));
    p.workflows.forEach(w => { w.adapterMode = "read_only"; }); return p;
  }
  return {nodes, requests, timers, payload, context,
    text: () => Object.values(nodes).map(n => n.textContent).join(" "),
    refresh: () => nodes["refresh-observation"].fire("click"),
    aiRead: () => nodes["ai-health-refresh"].fire("click"),
    good(index, p = payload()) { requests[index].resolve({status: 200, json: async () => p}); },
    aiGood(index, p = aiPayload()) {
      // Native response.json creates objects in the renderer's realm. Recreate
      // that boundary, rather than passing host-realm prototypes into the VM.
      const decoded = vm.runInContext("JSON.parse(" + JSON.stringify(JSON.stringify(p)) + ")", context);
      requests[index].resolve({status: 200, json: async () => decoded});
    },
    timeout() { for (const [id, fn] of [...timers]) { timers.delete(id); fn(); } }
  };
}
function unavailable(b) {
  assert.match(b.nodes["live-status"].textContent, /Observation unavailable/);
  assert.doesNotMatch(b.nodes["live-services"].textContent, /succeeded|Historical.*success|Connected adapter/);
  assert.equal(b.nodes["live-services"].children.length, 5);
  assert.doesNotMatch(b.text(), /PRIVATE|undefined|Error:|schemaVersion/);
}
// Controlled synthetic status fixture, never a real producer/provider read.
function aiPayload() {
  return {version: 1, authority: "observation_only", ai_pipeline: {
    freshness: "fresh", health: "ok", review_count: 17, operational_failures: 23,
    pending_retry_failures: 31, deterministic_rejections: 47,
    backpressure: true, review_reminder_due: false
  }};
}
function aiText(b) {
  return b.nodes["ai-health-status"].textContent + " " + b.nodes["ai-health-details"].textContent;
}
function aiEmpty(b, expected = "AI producer observation unavailable") {
  assert.equal(b.nodes["ai-health-status"].textContent, expected);
  assert.equal(b.nodes["ai-health-details"].children.length, 0);
  assert.doesNotMatch(aiText(b), /PRIVATE|undefined|Error:|review_count|17|23|31|47/);
}
function controlText(b) {
  return ["live-status", "live-summary", "live-services"].map(id => b.nodes[id].textContent).join(" ");
}
let count = 0;
async function test(name, fn) { await fn(); ++count; console.log("PASS " + name); }
(async () => {
  await test("load and manual refresh only, exact noncredential GET, no polling", async () => {
    const b = browser(); assert.equal(b.requests.length, 1);
    assert.match(b.nodes["live-status"].textContent, /Loading/);
    const r = b.requests[0]; assert.equal(r.url, "/api/v1/workflow-control");
    for (const [key, value] of Object.entries({method: "GET", mode: "same-origin", credentials: "omit", cache: "no-store", redirect: "error"})) assert.equal(r.options[key], value);
    assert.equal(r.options.body, undefined); b.good(0); await flush();
    assert.equal(b.timers.size, 0); assert.equal(b.requests.length, 1);
    b.refresh(); assert.equal(b.requests.length, 2); b.good(1); await flush(); assert.equal(b.timers.size, 0);
  });
  await test("real renderer labels HTTP not synthetic, UTC times and no controls", async () => {
    const b = browser(); b.good(0); await flush();
    assert.match(b.nodes["live-status"].textContent, /LIVE.*2026-10-27T00:00:00.000Z \(UTC\)/);
    const grid = b.nodes["live-services"];
    assert.equal(grid.children.length, 5); assert.equal(grid.attributes["aria-busy"], "false");
    assert.match(grid.textContent, /HTTP-derived read-only/); assert.doesNotMatch(grid.textContent, /synthetic|1793059200/);
    assert.ok(!grid.all().some(n => ["button", "input", "a", "form"].includes(n.tagName)));
    assert.match(grid.children[3].textContent, /Latest attempt running.*Historical.*success.*not Vault publication/);
    assert.doesNotMatch(grid.children[4].textContent, /Next due|Interval/);
    assert.match(b.nodes["live-summary"].textContent, /not proof of product success/);
    const p = b.payload(); assert.equal(b.context.ObsidianDesign.fromControlCatalog(p)[1].synthetic, true);
    assert.equal(b.context.ObsidianDesign.fromControlCatalog(p, "same-origin-http")[1].synthetic, false);
  });
  await test("mock HTTP source never masquerades as production evidence", async () => {
    const b = browser(); const p = b.payload(); p.workflows[1].adapterMode = "mock";
    b.good(0, p); await flush();
    assert.match(b.nodes["live-services"].children[1].textContent, /Mock adapter observation · not production evidence/);
    assert.equal(b.context.ObsidianDesign.fromControlCatalog(p, "same-origin-http")[1].synthetic, true);
    assert.match(b.nodes["live-summary"].textContent, /1 connected adapters/);
  });
  await test("refresh immediately clears previous good results then 503 fails closed", async () => {
    const b = browser(); b.good(0); await flush(); assert.match(b.text(), /succeeded/);
    b.refresh(); assert.doesNotMatch(b.nodes["live-services"].textContent, /succeeded/);
    b.requests[1].resolve({status: 503, json() { throw Error("PRIVATE body must not be parsed"); }});
    await flush(); unavailable(b); assert.equal(b.timers.size, 0);
  });
  await test("rejected transport and JSON promises never leak exceptions", async () => {
    for (const jsonFailure of [false, true]) {
      const b = browser();
      if (jsonFailure) b.requests[0].resolve({status: 200, json: async () => { throw Error("PRIVATE JSON"); }});
      else b.requests[0].reject(Error("PRIVATE rejection"));
      await flush(); unavailable(b); assert.equal(b.timers.size, 0);
    }
  });
  await test("real async malformed, missing/duplicate/reordered IDs and version fail closed", async () => {
    for (const mutate of [p => { p.schemaVersion = "v9"; }, p => p.workflows.pop(),
      p => { p.workflows[0].id = "b2"; }, p => p.workflows.reverse(),
      p => { p.workflows[1].configuration = "PRIVATE"; }, p => { p.extra = "PRIVATE"; }]) {
      const b = browser(); b.good(0); await flush(); b.refresh();
      const p = b.payload(); mutate(p); b.good(1, p); await flush(); unavailable(b);
    }
  });
  await test("deadline aborts hanging fetch and late success cannot resurrect health", async () => {
    const b = browser(); b.timeout(); await flush(); unavailable(b);
    assert.equal(b.requests[0].options.signal.aborted, true);
    b.good(0); await flush(); unavailable(b); assert.equal(b.timers.size, 0);
  });
  await test("deadline includes stalled JSON decoding and late rejection is handled", async () => {
    const b = browser(); const body = deferred();
    b.requests[0].resolve({status: 200, json: () => body.promise}); await flush();
    b.timeout(); await flush(); unavailable(b);
    body.reject(Error("PRIVATE late body")); await flush(); unavailable(b);
  });
  await test("older success after newest failure cannot resurrect old health", async () => {
    const b = browser(); b.refresh(); b.requests[1].reject(Error("PRIVATE newest"));
    await flush(); unavailable(b); b.good(0); await flush(); unavailable(b);
  });
  await test("older failure/success/timeout after newest success cannot replace it", async () => {
    for (const old of ["failure", "success", "timeout"]) {
      const b = browser(); b.refresh(); const p = b.payload(); p.observedAt += 10;
      b.good(1, p); await flush(); const before = b.text();
      if (old === "failure") b.requests[0].reject(Error("PRIVATE older"));
      else if (old === "success") b.good(0); else b.timeout();
      await flush(); assert.equal(b.text(), before); assert.match(b.nodes["live-status"].textContent, /LIVE/);
    }
  });
  await test("newer request with older catalog/workflow times rejected even across failure", async () => {
    for (const workflow of [false, true]) {
      const b = browser(); b.good(0); await flush(); b.refresh();
      b.requests[1].reject(Error("PRIVATE gap")); await flush(); b.refresh();
      const p = b.payload();
      if (workflow) p.workflows[0].observedAt -= 1;
      else { p.observedAt -= 1; p.workflows.forEach(w => { w.observedAt -= 1; }); }
      b.good(2, p); await flush(); unavailable(b);
      b.refresh(); b.good(3); await flush(); assert.match(b.nodes["live-status"].textContent, /LIVE/);
    }
  });
  await test("unavailable evidence discards history, labels cannot inject, huge times do not throw", async () => {
    const b = browser(), p = b.payload();
    p.observedAt = 1e100; p.workflows[0].observedAt = 1e100;
    p.workflows[0].label = '<img onerror="PRIVATE">'; p.workflows[1].integration = "unavailable";
    b.good(0, p); await flush();
    assert.match(b.nodes["live-status"].textContent, /outside display range/);
    assert.doesNotMatch(b.text(), /PRIVATE|undefined|Error:/);
    assert.doesNotMatch(b.nodes["live-services"].children[1].textContent, /succeeded|snapshot_ref|Historical.*success/);
  });
  await test("AI advisory accessible and manual only, fixed same-origin credential-free GET", async () => {
    assert.match(html, /<section aria-labelledby="ai-health-heading">/);
    assert.match(html, /<button id="ai-health-refresh" type="button">Read AI pipeline status<\/button>/);
    assert.match(html, /id="ai-health-status" role="status" aria-live="polite"/);
    assert.match(html, /<dl id="ai-health-details"><\/dl>/);
    assert.ok(html.indexOf('id="live-summary"') < html.indexOf('id="ai-health-heading"'));
    assert.ok(html.indexOf('id="ai-health-details"') < html.indexOf('id="live-services"'));
    assert.match(html, /not confirmation of provider readiness, note generation, Review approval, or Vault effect/);
    assert.doesNotMatch(html, /<(?:form|input)\b/i);
    const b = browser(); assert.match(aiText(b), /^Not requested/);
    assert.equal(b.requests.length, 1); b.good(0); await flush();
    assert.equal(b.requests.length, 1); assert.equal(b.timers.size, 0);
    assert.match(aiText(b), /^Not requested/);
    const before = controlText(b); const done = b.aiRead();
    assert.equal(b.requests.length, 2); assert.equal(controlText(b), before);
    const r = b.requests[1]; assert.equal(r.url, "/api/v1/status");
    for (const [key, value] of Object.entries({method: "GET", mode: "same-origin", credentials: "omit", cache: "no-store", redirect: "error"})) assert.equal(r.options[key], value);
    assert.equal(r.options.headers.Accept, "application/json");
    assert.deepEqual(Object.keys(r.options.headers), ["Accept"]);
    assert.equal(r.options.body, undefined); assert.ok(r.options.signal instanceof AbortSignal);
    assert.equal(r.options.signal.aborted, false); assert.equal(b.timers.size, 1);
    b.aiGood(1); await done; assert.equal(b.timers.size, 0);
    await flush(); assert.equal(b.requests.length, 2);
    assert.equal(controlText(b), before);
  });
  await test("AI fresh producer enums, safe counts and booleans never mean workflow success", async () => {
    for (const health of ["ok", "warning", "critical"]) {
      const b = browser(); b.good(0); await flush(); const before = controlText(b);
      b.aiRead(); const p = aiPayload(); p.ai_pipeline.health = health;
      b.aiGood(1, p); await flush();
      assert.match(aiText(b), new RegExp("fresh · producer health: " + health));
      assert.match(aiText(b), /not workflow success or connected B1 evidence/);
      const rows = b.nodes["ai-health-details"].children;
      assert.deepEqual(rows.map(row => row.children[1].textContent), ["17", "23", "31", "47", "Active", "Not due"]);
      assert.match(aiText(b), /Awaiting human review \(not approval\)/);
      assert.match(aiText(b), /pending retry \(budget unconfirmed\)/);
      assert.match(aiText(b), /Validation rejections \(not an outage\)/);
      assert.equal(controlText(b), before);
      b.aiRead(); p.ai_pipeline.review_count = 0;
      p.ai_pipeline.deterministic_rejections = Number.MAX_SAFE_INTEGER;
      p.ai_pipeline.backpressure = false; p.ai_pipeline.review_reminder_due = true;
      b.aiGood(2, p); await flush();
      assert.deepEqual(b.nodes["ai-health-details"].children.map(row => row.children[1].textContent),
        ["0", "23", "31", String(Number.MAX_SAFE_INTEGER), "Not active", "Due"]);
    }
  });
  await test("AI subset ignores all free text, service details, incidents and HTML", async () => {
    const b = browser(); b.good(0); await flush(); b.aiRead(); const p = aiPayload();
    p.generated_at = "PRIVATE time"; p.services = [{service: "PRIVATE service"}];
    p.actions = {trigger: true}; p.error = "PRIVATE error";
    Object.assign(p.ai_pipeline, {generated_at: "PRIVATE timestamp", age_seconds: "PRIVATE age",
      source_path: "/PRIVATE/path", provider: "PRIVATE provider", url: "https://PRIVATE.invalid",
      incidents: [{code: "PRIVATE incident"}], extra: '<img src="PRIVATE" onerror="PRIVATE">'});
    b.aiGood(1, p); await flush();
    assert.match(aiText(b), /producer health: ok/); assert.doesNotMatch(b.text(), /PRIVATE|<img|generated_at|observation_only/);
    assert.equal(b.nodes["ai-health-details"].children.length, 6);
  });
  await test("AI malformed envelopes, objects, enums, unsafe integers and flags fail closed", async () => {
    const mutations = [
      () => null, () => [], () => "PRIVATE", p => ({...p, version: "1"}), p => ({...p, version: 2}),
      p => ({...p, authority: "PRIVATE"}), p => { delete p.version; return p; },
      p => { delete p.ai_pipeline; return p; },
      ...[null, [], "PRIVATE"].map(value => p => ({...p, ai_pipeline: value})),
      ...[undefined, "PRIVATE", "__proto__", "toString"].map(value => p => { p.ai_pipeline.freshness = value; return p; }),
      ...[undefined, "unknown", "PRIVATE", "__proto__", "toString"].map(value => p => { p.ai_pipeline.health = value; return p; })
    ];
    for (const key of ["review_count", "operational_failures", "pending_retry_failures", "deterministic_rejections"]) {
      for (const value of [undefined, null, true, "17", -1, 0.5, Number.MAX_SAFE_INTEGER + 1, Infinity, NaN, {}]) {
        mutations.push(p => { p.ai_pipeline[key] = value; return p; });
      }
    }
    for (const key of ["backpressure", "review_reminder_due"]) {
      for (const value of [undefined, null, 0, 1, "false", {}]) mutations.push(p => { p.ai_pipeline[key] = value; return p; });
    }
    for (const mutate of mutations) {
      const b = browser(); b.good(0); await flush(); b.aiRead(); b.aiGood(1); await flush();
      b.aiRead(); assert.equal(b.nodes["ai-health-details"].children.length, 0);
      b.aiGood(2, mutate(aiPayload())); await flush(); aiEmpty(b); assert.equal(b.timers.size, 0);
    }
    // Use the renderer realm so these fail for custom prototypes, not merely
    // a host-realm prototype mismatch. Also test nonfinite decoded numbers
    // directly: JSON.stringify in aiGood would otherwise turn them into null.
    for (const expression of ["Object.create(p)", "({...p, ai_pipeline: Object.create(p.ai_pipeline)})",
      "({...p, ai_pipeline: {...p.ai_pipeline, review_count: Infinity}})",
      "({...p, ai_pipeline: {...p.ai_pipeline, review_count: NaN}})"]) {
      const b = browser(); b.good(0); await flush(); b.aiRead();
      const p = vm.runInContext("((p) => " + expression + ")(" + JSON.stringify(aiPayload()) + ")", b.context);
      b.good(1, p); await flush(); aiEmpty(b);
    }
  });
  await test("AI stale invalid unavailable never display supplied historical counts or text", async () => {
    for (const freshness of ["stale", "invalid", "unavailable"]) {
      const b = browser(); b.good(0); await flush(); b.aiRead(); b.aiGood(1); await flush();
      b.aiRead(); const p = aiPayload(); Object.assign(p.ai_pipeline, {freshness,
        health: "PRIVATE", generated_at: "PRIVATE", incidents: [{code: "PRIVATE"}]});
      b.aiGood(2, p); await flush();
      aiEmpty(b, "AI producer observation " + freshness + (freshness === "unavailable" ? "" : " · no current counters."));
      b.aiRead(); b.aiGood(3, {version: 1, authority: "observation_only", ai_pipeline: {freshness}});
      await flush(); assert.equal(b.nodes["ai-health-details"].children.length, 0);
    }
  });
  await test("AI fetch HTTP and JSON failures do not erase the valid control catalog", async () => {
    for (const failure of ["fetch", "json", 302, 403, 503]) {
      const b = browser(); b.good(0); await flush(); const before = controlText(b);
      b.aiRead(); b.aiGood(1); await flush(); b.aiRead();
      if (failure === "fetch") b.requests[2].reject(Error("PRIVATE transport"));
      else if (failure === "json") b.requests[2].resolve({status: 200, json: async () => { throw Error("PRIVATE JSON"); }});
      else b.requests[2].resolve({status: failure, json() { throw Error("PRIVATE must not decode HTTP error"); }});
      await flush(); aiEmpty(b); assert.equal(controlText(b), before); assert.equal(b.timers.size, 0);
      assert.equal(b.requests.length, 3); // No redirect-follow, fallback, retry or POST.
    }
  });
  await test("AI 10-second deadline aborts fetch and handles either late outcome", async () => {
    for (const late of ["success", "failure"]) {
      const b = browser(); b.good(0); await flush(); b.aiRead(); b.timeout(); await flush(); aiEmpty(b);
      assert.equal(b.requests[1].options.signal.aborted, true);
      if (late === "success") b.aiGood(1); else b.requests[1].reject(Error("PRIVATE late fetch"));
      await flush(); aiEmpty(b); assert.equal(b.timers.size, 0);
    }
  });
  await test("AI same deadline includes delayed JSON and handles late resolve or reject", async () => {
    for (const late of ["success", "failure"]) {
      const b = browser(); b.good(0); await flush(); b.aiRead(); const body = deferred();
      b.requests[1].resolve({status: 200, json: () => body.promise}); await flush();
      assert.equal(b.timers.size, 1); b.timeout(); await flush(); aiEmpty(b);
      assert.equal(b.requests[1].options.signal.aborted, true);
      if (late === "success") body.resolve(vm.runInContext("(" + JSON.stringify(aiPayload()) + ")", b.context));
      else body.reject(Error("PRIVATE late body"));
      await flush(); aiEmpty(b); assert.equal(b.timers.size, 0);
    }
  });
  await test("AI older success failure or timeout cannot replace newest success", async () => {
    for (const old of ["success", "failure", "timeout"]) {
      const b = browser(); b.good(0); await flush(); b.aiRead();
      const oldTimer = [...b.timers.values()][0]; b.aiRead();
      assert.equal(b.requests[1].options.signal.aborted, true);
      const p = aiPayload(); p.ai_pipeline.review_count = 59; p.ai_pipeline.health = "warning";
      b.aiGood(2, p); await flush(); const before = aiText(b);
      if (old === "success") b.aiGood(1);
      else if (old === "failure") b.requests[1].reject(Error("PRIVATE old"));
      else { oldTimer(); b.requests[1].reject(Error("PRIVATE timed out")); }
      await flush(); assert.equal(aiText(b), before); assert.match(before, /warning/);
      assert.equal(b.nodes["ai-health-details"].children[0].children[1].textContent, "59");
      assert.equal(b.timers.size, 0);
    }
  });
  await test("AI older success or rejection cannot resurrect results after newest failure", async () => {
    for (const old of ["success", "failure"]) {
      const b = browser(); b.good(0); await flush(); b.aiRead(); b.aiRead();
      b.requests[2].reject(Error("PRIVATE newest")); await flush(); aiEmpty(b);
      if (old === "success") b.aiGood(1); else b.requests[1].reject(Error("PRIVATE older"));
      await flush(); aiEmpty(b); assert.equal(b.timers.size, 0);
    }
  });
  await test("AI new click clears counts and old delayed JSON cannot finish newer pending read", async () => {
    for (const old of ["success", "failure"]) {
      const b = browser(); b.good(0); await flush(); b.aiRead(); b.aiGood(1); await flush();
      b.aiRead(); assert.equal(b.nodes["ai-health-details"].children.length, 0);
      const body = deferred(); b.requests[2].resolve({status: 200, json: () => body.promise}); await flush();
      b.aiRead(); const pending = aiText(b); assert.match(pending, /^Reading AI producer/);
      if (old === "success") body.resolve(vm.runInContext("(" + JSON.stringify(aiPayload()) + ")", b.context));
      else body.reject(Error("PRIVATE old JSON"));
      await flush(); assert.equal(aiText(b), pending);
      b.aiGood(3); await flush(); assert.match(aiText(b), /producer health: ok/);
      assert.equal(b.timers.size, 0);
    }
  });
  await test("control catalog failure and refresh never erase independent AI advisory", async () => {
    const b = browser(); b.aiRead(); b.aiGood(1); await flush(); const before = aiText(b);
    b.requests[0].reject(Error("PRIVATE catalog")); await flush(); unavailable(b);
    assert.equal(aiText(b), before); b.refresh(); assert.equal(aiText(b), before);
    b.good(2); await flush(); assert.equal(aiText(b), before);
    b.refresh(); b.timeout(); await flush(); unavailable(b); assert.equal(aiText(b), before);
    b.good(3); await flush(); assert.equal(aiText(b), before); assert.equal(b.requests.length, 4);
  });
  await test("AI HTTP provenance does not convert mock sources or pending B1 to connected", async () => {
    for (const mock of [true, false]) {
      const b = browser(); const p = b.payload();
      if (mock) p.workflows.forEach(w => { w.adapterMode = "mock"; });
      b.good(0, p); await flush(); const before = controlText(b);
      assert.match(b.nodes["live-services"].children[0].textContent, /Pending integration/);
      b.aiRead(); b.aiGood(1); await flush(); assert.equal(controlText(b), before);
      assert.match(aiText(b), /producer health: ok.*not workflow success or connected B1 evidence/);
      if (mock) assert.match(b.nodes["live-services"].textContent, /Mock adapter observation · not production evidence/);
      else assert.match(b.nodes["live-services"].textContent, /HTTP-derived read-only observation/);
      assert.equal(b.context.ObsidianDesign.fromControlCatalog(p, "same-origin-http")[0].synthetic, mock);
      assert.equal(b.nodes["live-services"].all().some(n => ["button", "form", "input"].includes(n.tagName)), false);
    }
  });
  if (process.argv.includes("--catalog-stdin")) {
    await test("actual loopback GET catalog passes strict model and live DOM with truthful absence", async () => {
      const b = browser(); b.good(0, JSON.parse(fs.readFileSync(0, "utf8"))); await flush();
      assert.match(b.nodes["live-status"].textContent, /LIVE/);
      assert.match(b.nodes["live-summary"].textContent, /0 connected adapters/);
      assert.match(b.nodes["live-services"].children[0].textContent, /Pending integration/);
      assert.match(b.nodes["live-services"].children[1].textContent, /Not attached/);
      assert.match(b.nodes["live-services"].children[3].textContent, /Not attached/);
      assert.doesNotMatch(b.nodes["live-services"].textContent, /succeeded|synthetic|Historical.*success/);
    });
  }
  console.log("webui_live_v1: " + count + " checks passed; controllable transport, no network");
})().catch(error => { console.error(error); process.exitCode = 1; });
