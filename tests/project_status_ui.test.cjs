const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const code = fs.readFileSync("src/obsidian_automation/three_services/project-status.js", "utf8");

function node(tag) {
  return {
    tag, children: [], listeners: {}, hidden: false, textContent: "", disabled: false,
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(event, callback) { this.listeners[event] = callback; }
  };
}
function find(root, id) {
  if (root.id === id) return root;
  for (const child of root.children) { const hit = find(child, id); if (hit) return hit; }
  return null;
}
function allText(root) { return [root.textContent, ...root.children.map(allText)].join("|"); }
const tick = async () => { for (let i = 0; i < 8; i++) await new Promise((r) => setImmediate(r)); };

const enabled = (extra = {}) => ({
  version: 1, enabled: true, busy: false, recent_runs: [], vault_write: true, ...extra,
  schedule: {enabled: false, next_due: null, interval_seconds: 21600, max_changes: 10,
             changed_today: 1, last_skip: null, ...(extra.schedule || {})}
});

async function mount({status, confirmAnswer = true, post} = {}) {
  const main = node("main");
  const calls = {fetch: [], confirm: []};
  const fetch = async (url, options = {}) => {
    calls.fetch.push({url: String(url), options});
    if (url === "/api/v1/session") return {ok: true, async json() { return {csrf: "tok"}; }};
    if (url === "/api/v1/project-status" && !options.method) {
      if (!status) return {ok: false, async json() { return {error: "unavailable"}; }};
      return {ok: true, async json() { return status; }};
    }
    if (post) return post(url, options);
    return {ok: true, async json() { return {}; }};
  };
  const context = {
    document: {querySelector: (s) => s === "main" ? main : null, createElement: node},
    fetch, confirm(message) { calls.confirm.push(message); return confirmAnswer; },
    setInterval() { return 7; }, clearInterval() {},
    Date, console, JSON, Object, Math, String, Error, Promise
  };
  vm.runInNewContext(code, context, {timeout: 2000});
  await tick();
  return {main, calls, panel: find(main, "project-status")};
}

test("the panel stays hidden when the server did not enable it", async () => {
  assert.equal((await mount({status: null})).panel.hidden, true);
});

test("the enabled panel shows limits and count-only run summaries", async () => {
  const {panel} = await mount({status: enabled({
    schedule: {enabled: true, next_due: 1800000000},
    recent_runs: [{run_id: "r", trigger: "scheduled", mode: "apply", status: "ok", error_code: null,
      created: 1800000000, completed: 1800000100,
      counts: {mode: "apply", watched: 8, planned: 1, applied: 1, held: {unchanged: 7, cooldown: 2},
               failed: {note_changed: 1}, repo_errors: 0}}]
  })});
  assert.equal(panel.hidden, false);
  const text = allText(find(panel, "project-status-status"));
  assert.match(text, /定期実行\|オン/);
  assert.match(text, /360 分 \/ 10 件/);
  assert.match(text, /今日の変更\|1 件/);
  const runs = allText(find(panel, "project-status-runs"));
  assert.match(runs, /完了 \/ 定期 \/ 書き込みあり \/ 対象 8 件 \/ 変更 1 件 \/ 24時間以内に変更済み 2 \/ note_changed 1/);
});

test("a check-only run needs no confirmation; an applying run and the schedule do", async () => {
  const sent = [];
  const post = async (url, options) => { sent.push([url, JSON.parse(options.body)]); return {ok: true, async json() { return {}; }}; };
  const {panel, calls} = await mount({status: enabled(), post});
  find(panel, "project-status-check").listeners.click();
  await tick();
  assert.deepEqual(sent, [["/api/v1/project-status/runs", {apply: false}]]);
  assert.equal(calls.confirm.length, 0);
  find(panel, "project-status-apply").listeners.click();
  await tick();
  assert.equal(calls.confirm.length, 1);
  assert.deepEqual(sent[1], ["/api/v1/project-status/runs", {apply: true}]);
  find(panel, "project-status-schedule").listeners.click();
  await tick();
  assert.equal(calls.confirm.length, 2);
  assert.deepEqual(sent[2], ["/api/v1/project-status/settings", {enabled: true}]);
});

test("cancelling the confirmation sends nothing, and buttons are disabled while busy", async () => {
  const sent = [];
  const post = async (url, options) => { sent.push(url); return {ok: true, async json() { return {}; }}; };
  const {panel} = await mount({status: enabled(), confirmAnswer: false, post});
  find(panel, "project-status-apply").listeners.click();
  find(panel, "project-status-schedule").listeners.click();
  await tick();
  assert.deepEqual(sent, []);
  const busy = await mount({status: enabled({busy: true})});
  for (const id of ["project-status-check", "project-status-apply", "project-status-schedule"]) {
    assert.equal(find(busy.panel, id).disabled, true);
  }
});

test("settings fields mirror the schedule and saving posts the edited values", async () => {
  const sent = [];
  const post = async (url, options) => { sent.push([url, JSON.parse(options.body)]); return {ok: true, async json() { return {}; }}; };
  const {panel} = await mount({status: enabled({schedule: {max_per_day: 30, cooldown_hours: 24,
    active_window_days: null, anchor_minute: 570, max_changes: 10, interval_seconds: 21600}}), post});
  assert.equal(find(panel, "project-status-interval").value, "360");
  assert.equal(find(panel, "project-status-per-run").value, "10");
  assert.equal(find(panel, "project-status-per-day").value, "30");
  assert.equal(find(panel, "project-status-cooldown").value, "24");
  assert.equal(find(panel, "project-status-window").value, "");
  assert.equal(find(panel, "project-status-anchor").value, "09:30");
  find(panel, "project-status-per-day").value = "12";
  find(panel, "project-status-per-day").listeners.input();
  find(panel, "project-status-window").value = "14";
  find(panel, "project-status-save").listeners.click();
  await tick();
  assert.deepEqual(sent[0], ["/api/v1/project-status/settings",
    {interval_seconds: 21600, max_changes: 10, max_per_day: 12, cooldown_hours: 24,
     anchor_minute: 570, active_window_days: 14}]);
});

test("a value that is not a whole number or an interval under 60 minutes is not sent", async () => {
  const sent = [];
  const post = async (url) => { sent.push(url); return {ok: true, async json() { return {}; }}; };
  const {panel} = await mount({status: enabled(), post});
  find(panel, "project-status-interval").value = "30";
  find(panel, "project-status-save").listeners.click();
  await tick();
  assert.deepEqual(sent, []);
  assert.match(find(panel, "project-status-message").textContent, /値を確認してください/);
});

test("the panel is a section with a heading, not a collapsible, and mounts into #panels", async () => {
  const {panel} = await mount({status: enabled()});
  assert.equal(panel.tag, "section");
  assert.match(allText(panel), /Projectの状態を自動更新/);
});
