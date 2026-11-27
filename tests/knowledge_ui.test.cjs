const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const code = fs.readFileSync("src/obsidian_automation/three_services/knowledge.js", "utf8");

function node(tag) {
  const self = {
    tag, children: [], listeners: {}, hidden: false, textContent: "", value: "",
    disabled: false,
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(event, callback) { this.listeners[event] = callback; },
    querySelectorAll(selector) {
      const found = [];
      const walk = (root) => {
        for (const child of root.children) {
          if (child.tag === selector) found.push(child);
          walk(child);
        }
      };
      walk(this);
      return found;
    }
  };
  return self;
}
function find(root, id) {
  if (root.id === id) return root;
  for (const child of root.children) {
    const hit = find(child, id);
    if (hit) return hit;
  }
  return null;
}
function buttons(root) { return root.querySelectorAll("button"); }
function allText(root) { return [root.textContent, ...root.children.map(allText)].join("|"); }
const tick = async () => { for (let i = 0; i < 8; i++) await new Promise((r) => setImmediate(r)); };

const schedule = (extra = {}) => ({
  enabled: false, next_due: null, interval_seconds: 3600, max_per_day: 3,
  max_pending: 10, proposed_today: 1, last_skip: null, ...extra
});
const enabled = (extra = {}) => ({
  version: 1, enabled: true, model: "qwen-test", busy: false, recent_runs: [],
  vault_write: true, ...extra, schedule: schedule(extra.schedule)
});
const listing = (items) => ({version: 1, total: items.length, counts: {new: items.length},
  conversations: items, truncated: false});
const item = (extra = {}) => ({archive: "a".repeat(64), index: 0, provider: "claude", messages: 4,
  bytes: 2048, first_day: "2026-01-01", state: "new", ...extra});

async function mount({status, confirmAnswer = true, post} = {}) {
  const main = node("main");
  const calls = {fetch: [], confirm: [], intervals: 0, cleared: 0};
  const fetch = async (url, options = {}) => {
    calls.fetch.push({url: String(url), options});
    if (url === "/api/v1/session") return {ok: true, async json() { return {csrf: "tok"}; }};
    if (url === "/api/v1/knowledge" && !options.method) {
      if (!status) return {ok: false, async json() { return {error: "unavailable"}; }};
      return {ok: true, async json() { return status; }};
    }
    if (post) return post(url, options);
    return {ok: true, async json() { return {}; }};
  };
  const context = {
    document: {querySelector: (s) => s === "main" ? main : null, createElement: node},
    fetch, confirm(message) { calls.confirm.push(message); return confirmAnswer; },
    setInterval() { calls.intervals++; return 7; }, clearInterval() { calls.cleared++; },
    Date, console, JSON, Object, Math, String, Error, Promise
  };
  vm.runInNewContext(code, context, {timeout: 2000});
  await tick();
  return {main, calls, panel: find(main, "knowledge")};
}

test("panel stays hidden when the server did not enable it", async () => {
  const {panel} = await mount({status: null});
  assert.equal(panel.hidden, true);
});

test("enabled panel shows the schedule limits and never offers a title or text", async () => {
  const {panel} = await mount({status: enabled({
    schedule: {enabled: true, next_due: 1800000000, proposed_today: 2, last_skip: "daily_cap"},
    recent_runs: [{run_id: "r", trigger: "scheduled", status: "proposed", outcome: "created",
      file: "2026-note.md", error_code: null, created: 1800000000, completed: 1800000100}]
  })});
  assert.equal(panel.hidden, false);
  const text = allText(find(panel, "knowledge-status"));
  assert.match(text, /モデル\|qwen-test/);
  assert.match(text, /定期実行\|オン/);
  assert.match(text, /60 分 \/ 3 件 \/ 10 件/);
  assert.match(text, /今日の提案\|2 件/);
  assert.match(text, /直近の見送り\|1日の上限/);
  assert.match(allText(find(panel, "knowledge-runs")), /提案を作成 \/ 定期 \/ 2026-note\.md/);
  assert.match(allText(panel), /タイトルや本文はここには表示しません/);
});

test("loading the list posts with CSRF and renders count-only rows with a button only where useful", async () => {
  const sent = [];
  const post = async (url, options) => {
    sent.push([url, JSON.parse(options.body), options.headers["X-Obsidian-CSRF"]]);
    return {ok: true, async json() {
      return listing([item(), item({index: 1, state: "proposed"}), item({index: 2, state: "failed"}),
                      item({index: 3, state: "too_large"})]);
    }};
  };
  const {panel} = await mount({status: enabled(), post});
  find(panel, "knowledge-load").listeners.click();
  await tick();
  assert.deepEqual(sent, [["/api/v1/knowledge/conversations", {}, "tok"]]);
  const box = find(panel, "knowledge-list");
  const text = allText(box);
  assert.match(text, /会話 4 件/);
  assert.match(text, /未処理/);
  assert.match(text, /提案済み/);
  assert.match(text, /失敗（手動で再試行）/);
  assert.equal(buttons(box).filter((b) => b.textContent === "提案を作る").length, 2);
});

test("starting a run asks for confirmation; cancelling sends nothing", async () => {
  const sent = [];
  const post = async (url, options) => {
    sent.push([url, JSON.parse(options.body)]);
    if (url.endsWith("/conversations")) return {ok: true, async json() { return listing([item()]); }};
    return {ok: true, status: 202, async json() { return {outcome: "accepted", run_id: "r"}; }};
  };
  const declined = await mount({status: enabled(), confirmAnswer: false, post});
  find(declined.panel, "knowledge-load").listeners.click();
  await tick();
  buttons(find(declined.panel, "knowledge-list")).find((b) => b.textContent === "提案を作る").listeners.click();
  await tick();
  assert.equal(sent.filter(([url]) => url.endsWith("/runs")).length, 0);
  assert.match(declined.calls.confirm[0], /04-AI\/Proposals\/ に新規作成します/);

  sent.length = 0;
  const accepted = await mount({status: enabled(), post});
  find(accepted.panel, "knowledge-load").listeners.click();
  await tick();
  buttons(find(accepted.panel, "knowledge-list")).find((b) => b.textContent === "提案を作る").listeners.click();
  await tick();
  assert.deepEqual(sent.filter(([url]) => url.endsWith("/runs")),
    [["/api/v1/knowledge/runs", {archive: "a".repeat(64), index: 0}]]);
});

test("while a run is busy the controls are disabled and the panel polls", async () => {
  const {panel, calls} = await mount({status: enabled({busy: true})});
  assert.equal(find(panel, "knowledge-load").disabled, true);
  assert.equal(find(panel, "knowledge-schedule").disabled, true);
  assert.equal(calls.intervals, 1);
});

test("switching the schedule on needs confirmation and names the brakes", async () => {
  const sent = [];
  const post = async (url, options) => {
    sent.push([url, JSON.parse(options.body)]);
    return {ok: true, async json() { return {enabled: true, next_due: 1}; }};
  };
  const declined = await mount({status: enabled(), confirmAnswer: false, post});
  find(declined.panel, "knowledge-schedule").listeners.click();
  await tick();
  assert.equal(sent.length, 0);
  assert.match(declined.calls.confirm[0], /1日 3 件まで、未承認が 10 件以上なら停止/);

  const accepted = await mount({status: enabled(), post});
  find(accepted.panel, "knowledge-schedule").listeners.click();
  await tick();
  assert.deepEqual(sent, [["/api/v1/knowledge/settings", {enabled: true}]]);
  assert.match(find(accepted.panel, "knowledge-message").textContent, /定期実行をオンにしました/);
});

test("switching the schedule off needs no confirmation", async () => {
  const sent = [];
  const post = async (url, options) => {
    sent.push(JSON.parse(options.body));
    return {ok: true, async json() { return {enabled: false}; }};
  };
  const {panel, calls} = await mount({status: enabled({schedule: {enabled: true}}), post});
  find(panel, "knowledge-schedule").listeners.click();
  await tick();
  assert.deepEqual(sent, [{enabled: false}]);
  assert.equal(calls.confirm.length, 0);
});

test("server errors are shown in Japanese", async () => {
  const post = async () => ({ok: false, async json() { return {error: "knowledge_busy"}; }});
  const {panel} = await mount({status: enabled(), post});
  find(panel, "knowledge-load").listeners.click();
  await tick();
  assert.equal(find(panel, "knowledge-message").textContent, "別の提案を実行中です");
});

test("the limits form shows current values and saves them in one settings post", async () => {
  const sent = [];
  const post = async (url, options) => {
    sent.push([url, JSON.parse(options.body)]);
    return {ok: true, async json() { return {}; }};
  };
  const {panel} = await mount({status: enabled({schedule: {anchor_minute: 570, max_per_day: 5}}), post});
  assert.equal(find(panel, "knowledge-interval").value, "60");
  assert.equal(find(panel, "knowledge-per-day").value, "5");
  assert.equal(find(panel, "knowledge-pending").value, "10");
  assert.equal(find(panel, "knowledge-anchor").value, "09:30");
  assert.match(allText(find(panel, "knowledge-status")), /基準時刻（日本時間）\|09:30/);
  find(panel, "knowledge-interval").value = "120";
  find(panel, "knowledge-per-day").value = "8";
  find(panel, "knowledge-pending").value = "25";
  find(panel, "knowledge-anchor").value = "";
  find(panel, "knowledge-save").listeners.click();
  await tick();
  assert.deepEqual(sent, [["/api/v1/knowledge/settings",
    {interval_seconds: 7200, max_per_day: 8, max_pending: 25, anchor_minute: null}]]);
  find(panel, "knowledge-interval").value = "30";
  find(panel, "knowledge-save").listeners.click();
  await tick();
  assert.equal(sent.length, 1);  // below one hour: refused before sending
});
