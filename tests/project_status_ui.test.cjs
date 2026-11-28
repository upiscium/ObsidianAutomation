const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const code = fs.readFileSync("src/obsidian_automation/three_services/project-status.js", "utf8");

function node(tag) {
  return {
    tag, children: [], listeners: {}, hidden: false, textContent: "", disabled: false,
    appendChild(child) { this.children.push(child); return child; },
    setAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; },
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
             max_per_day: 30, cooldown_hours: 24, active_window_days: null, anchor_minute: null,
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

test("the enabled panel shows the state and count-only run summaries, without description or duplicated settings", async () => {
  const {panel} = await mount({status: enabled({
    schedule: {enabled: true, next_due: 1800000000},
    recent_runs: [{run_id: "r", trigger: "scheduled", mode: "apply", status: "ok", error_code: null,
      created: 1800000000, completed: 1800000100,
      counts: {mode: "apply", watched: 8, planned: 1, applied: 1, held: {unchanged: 7, cooldown: 2},
               failed: {note_changed: 1}, repo_errors: 0}}]
  })});
  assert.equal(panel.hidden, false);
  assert.equal(panel.tag, "section");
  assert.match(allText(panel), /Project updater/);
  const text = allText(find(panel, "project-status-status"));
  assert.match(text, /定期実行\|オン/);
  assert.match(text, /今日の変更\|1 件/);
  assert.doesNotMatch(text, /360 分|基準時刻/);  // settings live in the dialog
  assert.doesNotMatch(allText(panel), /GitHubの直近の動きから/);  // no description text
  const runs = allText(find(panel, "project-status-runs"));
  assert.match(runs, /完了 \/ 定期 \/ 書き込みあり \/ 対象 8 件 \/ 変更 1 件 \/ 24時間以内に変更済み 2 \/ note_changed 1/);
});

test("there is no dry-run button; Run once asks for confirmation and applies", async () => {
  const sent = [];
  const post = async (url, options) => { sent.push([url, JSON.parse(options.body)]); return {ok: true, async json() { return {}; }}; };
  const {panel, calls} = await mount({status: enabled(), post});
  assert.equal(find(panel, "project-status-check"), null);
  assert.equal(find(panel, "project-status-schedule"), null);
  const run = find(panel, "project-status-apply");
  assert.equal(run.textContent, "Run once");
  run.listeners.click();
  await tick();
  assert.equal(calls.confirm.length, 1);
  assert.deepEqual(sent, [["/api/v1/project-status/runs", {apply: true}]]);
});

test("cancelling the confirmation sends nothing, and Run once is disabled while busy", async () => {
  const sent = [];
  const post = async (url) => { sent.push(url); return {ok: true, async json() { return {}; }}; };
  const {panel} = await mount({status: enabled(), confirmAnswer: false, post});
  find(panel, "project-status-apply").listeners.click();
  await tick();
  assert.deepEqual(sent, []);
  const busy = await mount({status: enabled({busy: true})});
  assert.equal(find(busy.panel, "project-status-apply").disabled, true);
});

test("the schedule is a checkbox in the settings dialog: SAVE posts it with every other value", async () => {
  const sent = [];
  const post = async (url, options) => { sent.push([url, JSON.parse(options.body)]); return {ok: true, async json() { return {}; }}; };
  const {panel, calls} = await mount({status: enabled(), post});
  const dialog = find(panel, "project-status-dialog");
  assert.equal(dialog.tag, "dialog");
  find(panel, "project-status-config").listeners.click();
  assert.equal(dialog.open, true);
  const box = find(panel, "project-status-enabled");
  assert.equal(box.type, "checkbox");
  assert.equal(box.checked, false);
  box.checked = true;
  box.listeners.change();
  find(panel, "project-status-save").listeners.click();
  await tick();
  assert.equal(calls.confirm.length, 1);  // switching it on names the brakes
  assert.equal(sent[0][0], "/api/v1/project-status/settings");
  assert.equal(sent[0][1].enabled, true);
  assert.equal(sent[0][1].interval_seconds, 21600);
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
    {enabled: false, interval_seconds: 21600, max_changes: 10, max_per_day: 12, cooldown_hours: 24,
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
