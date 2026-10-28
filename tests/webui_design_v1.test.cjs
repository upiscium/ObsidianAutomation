"use strict";
// No jsdom, browser package, network, service, credentials or backend imports.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const base = path.resolve(__dirname, "../src/obsidian_automation/webui_design_v1");
const html = fs.readFileSync(path.join(base, "index.html"), "utf8");
const css = fs.readFileSync(path.join(base, "styles.css"), "utf8");
const source = fs.readFileSync(path.join(base, "model.js"), "utf8");
const app = fs.readFileSync(path.join(base, "app.js"), "utf8");
const clone = value => JSON.parse(JSON.stringify(value));
let count = 0;
function test(name, callback) { callback(); ++count; console.log("PASS " + name); }
const modelContext = vm.createContext({URL});
vm.runInContext(source, modelContext);
const M = modelContext.ObsidianDesign;

test("template has separate assets, navigation, accessible forms and safety CSP", () => {
  assert.match(html, /Obsidian Service Status Manager/);
  for (const id of ["overview-view", "detail-view", "connections-view", "legend", "menu-toggle", "app-status"]) assert.ok(html.includes('id="' + id + '"'));
  assert.match(html, /connect-src 'none'; form-action 'none'/);
  assert.match(html, /script src="model.js" defer/);
  assert.match(html, /script src="app.js" defer/);
  assert.ok(!/<script(?![^>]*\bsrc=)/.test(html));
  assert.ok(!/\son\w+=/.test(html));
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
  assert.equal(new Set(ids).size, ids.length);
  for (const match of html.matchAll(/<input\b([^>]+)>/g)) {
    const id = /\bid="([^"]+)"/.exec(match[1])[1];
    assert.ok(html.includes('for="' + id + '"'), id + " must have a label");
    if (match[1].includes('type="password"')) {
      assert.ok(match[1].includes('autocomplete="new-password"'));
      assert.ok(!match[1].includes("value="));
    }
  }
});
test("responsive mobile-first 1/2/3 columns and visible focus/touch minimum", () => {
  assert.match(css, /grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(css, /@media \(min-width: 640px\)[\s\S]*repeat\(2, minmax\(0, 1fr\)\)/);
  assert.match(css, /@media \(min-width: 1024px\)[\s\S]*repeat\(3, minmax\(0, 1fr\)\)/);
  assert.match(css, /:focus-visible/); assert.match(css, /min-height: 44px/);
});
test("five canonical products and safe current catalog fixture", () => {
  const services = M.fromCatalog(M.mockCatalog());
  assert.deepEqual(clone(services.map(s => s.id)), ["b1", "b2", "b3", "b4", "b5"]);
  assert.ok(services.every(s => s.integration !== "connected" && s.execution === "Unknown" && s.lastRun === null && s.nextRun === null));
  assert.equal(services[4].schedule, "not_applicable");
  assert.equal(services[1].integration, "not_attached");
});
test("catalog adapter accepts reordered real v1 data but copies no authority or private text", () => {
  const payload = clone(M.mockCatalog());
  Object.assign(payload.workflows[1], {integration: "connected", configuration: "configured", schedule: "on", last_result: "saved", control_available: true,
    label: '<img src=x onerror="SECRET">', note: "PRIVATE TOKEN", next_run: "fabricated", last_run: "fabricated"});
  payload.workflows.reverse();
  const services = M.fromCatalog(payload);
  assert.equal(services[1].integration, "connected");
  assert.equal(services[1].result, "saved");
  assert.equal(services[1].execution, "Unknown");
  assert.equal(services[1].nextRun, null);
  assert.doesNotMatch(JSON.stringify(services), /SECRET|PRIVATE|fabricated|control_available/);
});
test("strict invalid catalog rejection, duplicates, prototypes, future contract and manual B5", () => {
  const fixture = M.mockCatalog();
  const bad = [null, [], {}, {...fixture, version: 2}, {...fixture, authority: "control"}, {...fixture, workflows: fixture.workflows.slice(1)}];
  for (const [key, value] of [["id", "b6"], ["id", "b2"], ["integration", "__proto__"], ["configuration", "constructor"], ["schedule", "enabled"], ["last_result", "SECRET"], ["control_available", "true"]]) {
    const p = clone(fixture); p.workflows[0][key] = value; bad.push(p);
  }
  for (const [index, schedule] of [[4, "on"], [4, "off"], [0, "not_applicable"]]) {
    const p = clone(fixture); p.workflows[index].schedule = schedule; bad.push(p);
  }
  for (const p of bad) assert.throws(() => M.fromCatalog(p));
});
test("outcomes never confuse preview, queued or running with verified success", () => {
  for (const result of ["unknown", "unconfirmed", "queued", "running", "preview_ready"]) assert.equal(M.outcome({result})[1], "unknown");
  for (const result of ["saved", "no_changes", "published"]) assert.equal(M.outcome({result})[1], "success");
  for (const result of ["failed", "publish_failed"]) assert.equal(M.outcome({result})[1], "failure");
  const p = clone(M.mockCatalog());
  Object.assign(p.workflows[1], {integration: "unavailable", last_result: "saved", configuration: "configured", schedule: "on"});
  const s = M.fromCatalog(p)[1];
  assert.equal(s.result, "unknown"); assert.equal(s.configuration, "not_observed");
  assert.equal(M.outcome({result: "__proto__"})[1], "unknown");
});
test("illustrative timestamps and idle/disabled states are synthetic, not a wire contract", () => {
  const demo = M.scenario("demonstration");
  assert.ok(demo.services.every(s => s.synthetic && s.integration !== "connected"));
  assert.equal(demo.services[0].execution, "Running");
  assert.equal(demo.services[2].execution, "Idle");
  assert.equal(demo.services[2].schedule, "off");
  assert.equal(demo.services[2].nextRun, null);
  assert.equal(demo.services[4].nextRun, null);
  assert.equal(demo.services[4].schedule, "not_applicable");
  assert.equal(M.scenario("catalog").services[0].lastRun, null);
  assert.throws(() => M.fromCatalog(demo));
});
test("mock commands do not fabricate success or mutate input; B2/B4 navigation fixed", () => {
  const s = M.scenario("catalog").services[0];
  const before = JSON.stringify(s);
  const run = M.simulate(s, "run");
  assert.equal(run.service.execution, "Queued"); assert.equal(run.service.result, "unknown");
  assert.equal(JSON.stringify(s), before);
  assert.match(run.message, /no command sent/);
  assert.equal(M.simulate(s, "toggle").service.schedule, "on");
  assert.equal(M.controlsLink("b2"), "/#snapshot-controls");
  assert.equal(M.controlsLink("b4"), "/#github-daily");
  assert.equal(M.controlsLink("__proto__"), null);
  for (const index of [1, 3]) assert.equal(M.simulate(M.scenario("catalog").services[index], "run").service.synthetic, false);
  const b5 = M.scenario("catalog").services[4];
  assert.equal(M.simulate(b5, "toggle").service.schedule, "not_applicable");
  assert.equal(M.simulate(b5, "import").service.schedule, "not_applicable");
});
test("connection validation is local, errors contain no submitted secrets or raw URL", () => {
  const errors = M.validateFields({webdav_url: "https://user:PRIVATE@host.example/path?token=PRIVATE", nextcloud_password: "PRIVATE", gitea_token: "PRIVATE", vault_folder: "../PRIVATE"});
  assert.ok(errors.some(e => e.field === "webdav_url"));
  assert.ok(errors.some(e => e.field === "nextcloud_account"));
  assert.ok(errors.some(e => e.field === "gitea_repo"));
  assert.ok(errors.some(e => e.field === "vault_folder"));
  assert.doesNotMatch(JSON.stringify(errors), /PRIVATE/);
  assert.equal(M.validateFields({webdav_url: "https://cloud.example/dav/", nextcloud_account: "dummy", gitea_repo: "https://git.example/u/v.git", gitea_account: "dummy", gitea_branch: "main"}).length, 0);
  assert.equal(M.validateFields({}).length, 0); // Optional services aren't forced.
});

// Minimal DOM fixture executes the authored handlers. It is not a layout engine.
class Element {
  constructor(tag = "div") { this.tagName = tag; this.children = []; this.attributes = {}; this.handlers = {}; this.dataset = {}; this.hidden = false; this.disabled = false; this.value = ""; this.type = ""; this.name = ""; this._text = ""; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(" "); }
  set innerHTML(value) { throw new Error("Raw HTML forbidden"); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ""; this.children = children; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(type, handler) { this.handlers[type] = handler; }
  focus() { if (!this.disabled) Element.focused = this; }
  querySelectorAll(selector) {
    const all = this.children.flatMap(child => [child, ...child.querySelectorAll("*")]);
    if (selector === "*") return all;
    return all.filter(node => selector === "input" ? node.tagName === "input" : selector === 'input[type="password"]' ? node.tagName === "input" && node.type === "password" : selector.startsWith('[data-action="') ? node.dataset.action === selector.slice(14, -2) : false);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  fire(type, props = {}) { if (this.disabled && type === "click") return; return this.handlers[type]?.({currentTarget: this, preventDefault() {}, ...props}); }
  reset() { this.querySelectorAll("input").forEach(input => { input.value = ""; }); }
}
function browser() {
  const nodes = {};
  for (const match of html.matchAll(/<([a-z0-9-]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const node = new Element(match[1]);
    node.id = match[3]; node.type = /\btype="([^"]+)"/.exec(match[2])?.[1] || "";
    node.name = /\bname="([^"]+)"/.exec(match[2])?.[1] || "";
    node.hidden = /\bhidden\b/.test(match[2]); nodes[node.id] = node;
  }
  nodes.scenario.value = "catalog";
  const inputs = Object.values(nodes).filter(node => node.tagName === "input");
  nodes["connections-form"].append(...inputs);
  nodes["connections-form"].elements = {namedItem: name => inputs.find(node => node.name === name)};
  const events = {}; const docEvents = {};
  const location = {hash: "#overview"}; let network = 0;
  const forbidden = () => { ++network; throw new Error("Network/storage access forbidden"); };
  const context = vm.createContext({
    URL, location, document: {getElementById: id => nodes[id], createElement: tag => new Element(tag), addEventListener: (type, fn) => { docEvents[type] = fn; }},
    addEventListener: (type, fn) => { events[type] = fn; },
    FormData: class { constructor() { this.values = inputs.map(node => [node.name, node.value]); } [Symbol.iterator]() { return this.values[Symbol.iterator](); } },
    fetch: forbidden, XMLHttpRequest: forbidden, WebSocket: forbidden, localStorage: {setItem: forbidden}, sessionStorage: {setItem: forbidden}, navigator: {sendBeacon: forbidden}
  });
  vm.runInContext(source, context); vm.runInContext(app, context);
  return {nodes, context, events, docEvents, network: () => network,
    route: hash => { location.hash = hash; events.hashchange(); },
    scenario: name => { nodes.scenario.value = name; nodes.scenario.fire("change"); }};
}
test("real renderer creates five products and visually distinct global summary", () => {
  const b = browser(); const cards = b.nodes["service-grid"].children;
  assert.equal(cards.length, 6);
  assert.equal(cards.filter(c => c.tagName === "article").length, 5);
  assert.equal(cards[5].tagName, "aside"); assert.match(cards[5].className, /health-summary/);
  assert.match(cards[5].textContent, /Unknown/);
  assert.ok(cards.every(c => !c.textContent.includes("Connected adapter")));
  assert.equal(b.network(), 0);
});
test("actual detail handlers use navigation only for B2/B4 and separate lifecycle facts", () => {
  const b = browser();
  for (const [id, href] of [["b2", "/#snapshot-controls"], ["b4", "/#github-daily"]]) {
    b.route("#service/" + id);
    const content = b.nodes["detail-content"];
    assert.match(content.textContent, /Configuration.*Execution.*Schedule.*Last run timestamp.*Last verified result.*Next run/);
    const actions = content.children.find(n => n.className === "actions");
    assert.ok(actions.children.some(n => n.href === href));
    assert.ok(actions.children.every(n => n.tagName !== "button"));
  }
  assert.equal(b.network(), 0);
});
test("B1/B3 local simulated controls remain unknown and restore keyboard focus", () => {
  const b = browser();
  for (const [id, label] of [["b1", "Simulate Run once"], ["b3", "Simulate Sync now"]]) {
    b.route("#service/" + id);
    let button = b.nodes["detail-content"].querySelector('[data-action="run"]');
    assert.equal(button.textContent, label); button.fire("click");
    assert.match(b.nodes["detail-content"].textContent, /Queued/);
    assert.match(b.nodes["detail-content"].textContent, /Unknown/);
    assert.match(b.nodes["command-message"].textContent, /no command sent/);
    assert.equal(Element.focused, b.nodes["detail-content"].querySelector('[data-action="run"]'));
  }
  assert.equal(b.network(), 0);
});
test("B5 has no next-run, timer toggle or file read; import only simulates admission", () => {
  const b = browser(); b.route("#service/b5");
  let content = b.nodes["detail-content"];
  assert.doesNotMatch(content.textContent, /Next run|Simulate Enable|Simulate Disable/);
  assert.match(content.textContent, /Manual ZIP Import/);
  let button = content.querySelector('[data-action="import"]'); assert.equal(button.disabled, true);
  const input = content.querySelectorAll("input")[0];
  input.files = [{name: "PRIVATE.zip", text() { throw new Error("Do not read ZIP"); }, arrayBuffer() { throw new Error("Do not read ZIP"); }}];
  input.fire("change"); assert.equal(button.disabled, false); button.fire("click");
  assert.match(b.nodes["command-message"].textContent, /no effect verified/);
  assert.doesNotMatch(b.nodes["detail-content"].textContent, /PRIVATE|Succeeded/);
  assert.equal(Element.focused, b.nodes["detail-title"]);
  assert.equal(b.network(), 0);
});
test("loading, unavailable and stale fail closed and replacing states clears old demo success", () => {
  const b = browser();
  for (const phase of ["loading", "unavailable", "stale"]) {
    b.scenario("demonstration"); assert.match(b.nodes["service-grid"].textContent, /Succeeded/);
    b.scenario(phase); b.route("#service/b1");
    assert.doesNotMatch(b.nodes["service-grid"].textContent, /Succeeded/);
    assert.equal(b.nodes["detail-content"].querySelector('[data-action="run"]').disabled, true);
    assert.match(b.nodes["app-status"].textContent, /unavailable|Unavailable|Stale|Loading/);
  }
  assert.equal(b.network(), 0);
});
test("form errors, secret clearing, no false save and navigation/page exit cleanup", () => {
  const b = browser(); b.route("#connections");
  b.nodes["webdav-url"].value = "https://PRIVATE:PRIVATE@host.example";
  for (const id of ["nextcloud-password", "gitea-token", "github-token", "ai-token"]) b.nodes[id].value = "PRIVATE";
  b.nodes["connections-form"].fire("submit");
  assert.equal(b.nodes["webdav-url"].attributes["aria-invalid"], "true");
  for (const id of ["nextcloud-password", "gitea-token", "github-token", "ai-token"]) assert.equal(b.nodes[id].value, "");
  assert.match(b.nodes["connection-message"].textContent, /Validation failed/);
  assert.doesNotMatch(b.nodes["connection-message"].textContent, /PRIVATE/);
  b.nodes["connections-form"].reset(); b.nodes["connections-form"].fire("submit");
  assert.match(b.nodes["connection-message"].textContent, /NOT verified.*Nothing sent or saved/);
  b.nodes["webdav-url"].value = "http://invalid.example"; b.nodes["connections-form"].fire("submit");
  assert.equal(b.nodes["webdav-url"].attributes["aria-invalid"], "true");
  b.nodes["reset-demo"].fire("click");
  assert.equal(b.nodes["webdav-url"].attributes["aria-invalid"], undefined);
  assert.equal(b.nodes["webdav-url"].attributes["aria-describedby"], undefined);
  b.nodes["github-token"].value = "PRIVATE"; b.route("#overview"); assert.equal(b.nodes["github-token"].value, "");
  b.nodes["github-token"].value = "PRIVATE"; b.events.pagehide(); assert.equal(b.nodes["github-token"].value, "");
  assert.equal(b.network(), 0);
});
test("menu keyboard escape, route focus, malformed hashes and reset are safe", () => {
  const b = browser();
  b.nodes["menu-toggle"].fire("click"); assert.equal(b.nodes.navigation.hidden, false);
  assert.equal(b.nodes["menu-toggle"].attributes["aria-expanded"], "true");
  b.docEvents.keydown({key: "Escape"}); assert.equal(b.nodes.navigation.hidden, true);
  assert.equal(Element.focused, b.nodes["menu-toggle"]);
  b.route("#connections"); assert.equal(Element.focused, b.nodes["connections-title"]);
  b.route('#service/<img onerror="PRIVATE">'); assert.equal(b.nodes["overview-view"].hidden, false);
  b.nodes["reset-demo"].fire("click"); assert.equal(b.network(), 0);
});
test("production I/O, injection sinks, uploads and persistence absent from shipped scripts", () => {
  const scripts = source + app;
  assert.doesNotMatch(scripts, /\b(fetch|XMLHttpRequest|WebSocket|sendBeacon|localStorage|sessionStorage|indexedDB|FileReader)\b/);
  assert.doesNotMatch(scripts, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\s*\(|new Function/);
  assert.doesNotMatch(scripts, /\bPOST\b|\.arrayBuffer\s*\(|\.text\s*\(/);
});
test("#480 normative synthetic catalog projects exact ordered v1 states without authority", () => {
  const contract = fs.readFileSync(path.resolve(base, "../../../docs/webui-workflow-control-contract-v1.md"), "utf8");
  const fixture = JSON.parse(contract.split("## 5. Five-workflow JSON fixture")[1].match(/```json\n([\s\S]*?)\n```/)[1]);
  for (const payload of [fixture, M.mockControlCatalog()]) {
    const before = JSON.stringify(payload);
    const services = M.fromControlCatalog(payload);
    assert.deepEqual(clone(services.map(s => s.id)), ["b1", "b2", "b3", "b4", "b5"]);
    assert.ok(services.every(s => s.readOnly && s.synthetic && s.availableActions.length === 0));
    assert.equal(services[1].lastAttempt.effect.changed, false);
    assert.equal(services[3].execution, "Running");
    assert.equal(services[3].lastAttempt.outcome, null);
    assert.equal(services[3].lastVerifiedOutcome.effect.kind, "daily_preview");
    assert.equal(services[4].schedule, "not_applicable"); assert.equal(services[4].nextRun, null);
    assert.equal(JSON.stringify(payload), before);
    assert.doesNotMatch(JSON.stringify(services), /requestId|revision|actionReasons|label/);
    services[1].lastAttempt.effect.changed = true;
    assert.equal(payload.workflows[1].lastAttempt.effect.changed, false);
    assert.throws(() => M.fromCatalog(payload));
  }
  assert.throws(() => M.fromControlCatalog(M.mockCatalog()));
});
function rejectsControl(mutations) {
  for (const mutate of mutations) {
    const payload = clone(M.mockControlCatalog()); mutate(payload);
    assert.throws(() => M.fromControlCatalog(payload), {message: "Invalid workflow observation"});
  }
}
test("control DTO rejects missing, reordered, duplicate, unknown IDs/keys/versions and accessors", () => {
  for (const value of [null, [], {}, "{}", true]) assert.throws(() => M.fromControlCatalog(value));
  rejectsControl([
    p => { p.schemaVersion = "webui-workflow-control/v9"; }, p => { p.authority = "control"; },
    p => p.workflows.pop(), p => p.workflows.reverse(), p => { p.workflows[0].id = "b2"; },
    p => { p.workflows[0].id = "b6"; }, p => { delete p.observedAt; }, p => { p.argv = ["PRIVATE"]; },
    p => { p.workflows[0].schemaVersion = "webui-workflow-control/v2"; },
    p => { p.workflows[0].configuration.extra = "PRIVATE"; },
    p => Object.defineProperty(p.workflows[0], "label", {get() { throw new Error("PRIVATE"); }}),
    p => { p.workflows[0][Symbol("PRIVATE")] = "PRIVATE"; },
    p => Object.setPrototypeOf(p.workflows[0], {private: "PRIVATE"})
  ]);
  for (const key of ["integration", "adapterMode", "revision"]) for (const value of ["__proto__", "constructor", "PRIVATE", null, true])
    rejectsControl([p => { p.workflows[0][key] = value; }]);
});
test("control DTO fails closed on all nested unknown enums and malformed times/booleans", () => {
  for (const value of ["__proto__", "constructor", "PRIVATE", null, false]) rejectsControl([
    p => { p.workflows[1].configuration.state = value; }, p => { p.workflows[1].schedule.state = value; },
    p => { p.workflows[1].execution.state = value; }, p => { p.workflows[1].lastAttempt.state = value; },
    p => { p.workflows[1].lastAttempt.outcome = value; }, p => { p.workflows[1].lastVerifiedOutcome.status = value; },
    p => { p.workflows[1].lastAttempt.effect.kind = value; }, p => { p.workflows[1].lastAttempt.effect.state = value; }
  ]);
  for (const value of [-1, NaN, Infinity, true, "1793059200", undefined]) rejectsControl([
    p => { p.observedAt = value; }, p => { p.workflows[0].observedAt = value; },
    p => { p.workflows[3].execution.startedAt = value; }, p => { p.workflows[1].lastAttempt.completedAt = value; },
    p => { p.workflows[1].lastVerifiedOutcome.completedAt = value; }, p => { p.workflows[1].lastAttemptAt = value; },
    p => { p.workflows[3].schedule.nextDueAt = value; }
  ]);
  for (const value of ["false", 0, {}, undefined]) rejectsControl([p => { p.workflows[1].lastAttempt.effect.changed = value; }]);
  rejectsControl([p => { p.workflows[0].observedAt = p.observedAt + 1; },
    p => { p.workflows[3].lastAttemptAt = p.observedAt + 1; },
    p => { p.workflows[1].lastAttempt.completedAt = 1; }]);
});
test("B5 manual invariant and undecided B1/B3 schedules cannot be broadened", () => {
  rejectsControl([
    ...["enabled", "disabled", "unknown"].map(state => p => { p.workflows[4].schedule.state = state; }),
    p => { p.workflows[4].schedule.nextDueAt = 0; }, p => { p.workflows[4].schedule.intervalSeconds = 300; },
    p => { p.workflows[4].schedule.reason = null; },
    ...[0, 2].map(i => p => { p.workflows[i].schedule = {state: "disabled", intervalSeconds: null, nextDueAt: null, reason: null}; }),
    p => { p.workflows[1].schedule.nextDueAt = 1; }, p => { p.workflows[1].schedule.intervalSeconds = true; },
    p => { p.workflows[3].schedule.intervalSeconds = 3599; }, p => { p.workflows[3].schedule.intervalSeconds = 86401; },
    p => { p.workflows[3].schedule.nextDueAt = null; }, p => { p.workflows[0].schedule.reason = null; }
  ]);
  const p = clone(M.mockControlCatalog()); p.workflows[3].schedule.nextDueAt = null;
  p.workflows[3].schedule.reason = "due_unavailable";
  assert.equal(M.fromControlCatalog(p)[3].nextRun, null);
});
test("action universe/partition/reasons fail closed; connected guidance still grants no UI authority", () => {
  rejectsControl([
    p => { p.workflows[0].availableActions = ["generate", "generate"]; },
    p => { p.workflows[0].availableActions = ["__proto__"]; },
    p => { delete p.workflows[0].actionReasons.generate; },
    p => { p.workflows[0].actionReasons.generate = null; },
    p => { p.workflows[0].actionReasons.generate = "PRIVATE"; },
    p => { p.workflows[0].actionReasons.argv = "mock_only"; },
    p => { p.workflows[0].availableActions = ["generate"]; delete p.workflows[0].actionReasons.generate; },
    p => { p.workflows[4].actionReasons.stage_archive = "mock_only"; }
  ]);
  const p = clone(M.mockControlCatalog()); const w = p.workflows[1];
  w.adapterMode = "connected"; w.availableActions = ["run_once"]; delete w.actionReasons.run_once;
  const s = M.fromControlCatalog(p)[1];
  assert.deepEqual(clone(s.availableActions), []);
  assert.equal(M.simulate(s, "run").service.execution, "Idle");
  assert.match(M.simulate(s, "run").message, /Read-only/);
  w.adapterMode = "read_only"; assert.throws(() => M.fromControlCatalog(p));
});
test("attempt/effect coherence never promotes active, unknown or preview to Vault success", () => {
  rejectsControl([
    p => { p.workflows[3].lastAttempt.effect.state = "verified"; },
    p => { p.workflows[3].lastAttempt.outcome = "success"; },
    p => { p.workflows[3].lastAttempt.requestId = "c".repeat(32); },
    p => { p.workflows[3].execution.state = "idle"; p.workflows[3].execution.requestId = null; },
    p => { p.workflows[3].execution.startedAt = 1; },
    p => { p.workflows[1].lastAttempt.effect.state = "unknown"; },
    p => { p.workflows[1].lastVerifiedOutcome.effect.state = "unknown"; },
    p => { p.workflows[1].lastAttempt.effect.kind = "daily_vault"; },
    p => { p.workflows[1].lastVerifiedOutcome.completedAt = p.observedAt; },
    p => { p.workflows[3].lastVerifiedOutcome.completedAt = p.workflows[3].lastAttemptAt + 1; },
    p => { p.workflows[1].lastVerifiedOutcome.completedAt = p.workflows[1].lastAttemptAt + 1; },
    p => { p.workflows[1].lastVerifiedOutcome.status = "failure"; },
    p => { p.workflows[1].lastVerifiedOutcome.effect.changed = true; }
  ]);
  const p = clone(M.mockControlCatalog()); const w = p.workflows[3];
  w.execution = {state: "idle", requestId: null, startedAt: null};
  w.lastAttempt.state = "completed"; w.lastAttempt.outcome = "failure"; w.lastAttempt.completedAt = p.observedAt;
  const s = M.fromControlCatalog(p)[3];
  assert.match(M.outcome(s)[0], /Latest attempt failed/); assert.equal(M.outcome(s)[1], "failure");
  assert.equal(s.lastVerifiedOutcome.status, "success");
  assert.match(M.effectText(s.lastVerifiedOutcome.effect), /not Vault publication/);
});
test("secret-like free text is not projected and invalid data yields fixed non-echoed errors", () => {
  const p = clone(M.mockControlCatalog()); p.workflows[0].label = '<img src=x onerror="SECRET_SENTINEL">';
  assert.doesNotMatch(JSON.stringify(M.fromControlCatalog(p)), /SECRET_SENTINEL|onerror/);
  for (const mutate of [p => { p.workflows[0].configuration.reason = "SECRET_SENTINEL"; },
    p => { p.workflows[1].lastAttempt.effect.kind = "SECRET_SENTINEL"; },
    p => { p.workflows[0].credentials = "SECRET_SENTINEL"; }]) {
    const payload = clone(M.mockControlCatalog()); mutate(payload);
    try { M.fromControlCatalog(payload); assert.fail("Must reject"); }
    catch (e) { assert.equal(e.message, "Invalid workflow observation"); }
  }
  const q = clone(M.mockControlCatalog()); q.workflows[1].integration = "unavailable";
  const s = M.fromControlCatalog(q)[1];
  assert.equal(s.execution, "Unknown"); assert.equal(s.configuration, "not_observed");
  assert.equal(s.lastAttempt, null); assert.equal(s.lastVerifiedOutcome, null); assert.equal(s.nextRun, null);
});
test("generation guard rejects slow old success/failure, malformed newest and duplicate settlement", () => {
  const reader = M.controlReader();
  let old = reader.begin(), newest = reader.begin();
  assert.equal(reader.settle(newest, null), true); assert.equal(reader.snapshot().phase, "unavailable");
  assert.equal(reader.settle(old, M.mockControlCatalog()), false); assert.equal(reader.snapshot().phase, "unavailable");
  old = reader.begin(); newest = reader.begin();
  assert.equal(reader.settle(newest, M.mockControlCatalog()), true);
  assert.equal(reader.settle(old, null), false); assert.equal(reader.snapshot().phase, "ready");
  assert.equal(reader.settle(newest, null), false); assert.equal(reader.snapshot().phase, "ready");
  const snapshot = reader.snapshot(); snapshot.services[1].lastVerifiedOutcome.status = "failure";
  assert.equal(reader.snapshot().services[1].lastVerifiedOutcome.status, "success");
  const token = reader.begin(); assert.equal(reader.snapshot().services[1].lastVerifiedOutcome, undefined);
  const bad = clone(M.mockControlCatalog()); bad.workflows[0].integration = "PRIVATE";
  reader.settle(token, bad); assert.equal(reader.snapshot().phase, "unavailable");
  assert.ok(reader.snapshot().services.every(s => s.execution === "Unknown" && s.result === "unknown"));
});
test("read-only renderer separates latest from historical, blocks all workflow controls and file inputs", () => {
  const b = browser(); b.scenario("control-v1");
  assert.match(b.nodes["service-grid"].textContent, /Synthetic read-only/);
  for (const id of ["b1", "b2", "b3", "b4", "b5"]) {
    b.route("#service/" + id); const content = b.nodes["detail-content"];
    assert.equal(content.querySelectorAll("input").length, 0);
    assert.ok(!content.querySelectorAll("*").some(e => e.tagName === "button"));
    assert.match(content.textContent, /Observation and available actions are not admission authority/);
    assert.doesNotMatch(content.textContent, /undefined|PRIVATE/);
  }
  b.route("#service/b4");
  assert.match(b.nodes["detail-content"].textContent, /Latest attempt.*running.*Historical.*success.*Daily preview \(not Vault publication\)/);
  b.route("#service/b5"); assert.doesNotMatch(b.nodes["detail-content"].textContent, /Next run|Enabled|Simulate/);
  b.scenario("unavailable"); assert.doesNotMatch(b.nodes["service-grid"].textContent, /Historical success/);
  assert.equal(b.network(), 0);
});
console.log("webui_design_v1: " + count + " checks passed; no network or backend invoked");
