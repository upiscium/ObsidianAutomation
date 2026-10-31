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
    good(index, p = payload()) { requests[index].resolve({status: 200, json: async () => p}); },
    timeout() { for (const [id, fn] of [...timers]) { timers.delete(id); fn(); } }
  };
}
function unavailable(b) {
  assert.match(b.nodes["live-status"].textContent, /Observation unavailable/);
  assert.doesNotMatch(b.nodes["live-services"].textContent, /succeeded|Historical.*success|Connected adapter/);
  assert.equal(b.nodes["live-services"].children.length, 5);
  assert.doesNotMatch(b.text(), /PRIVATE|undefined|Error:|schemaVersion/);
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
