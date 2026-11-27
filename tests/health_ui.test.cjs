const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const code = fs.readFileSync("src/obsidian_automation/three_services/health.js", "utf8");

function node(tag) {
  return {
    tag, children: [], hidden: false, textContent: "", attributes: {},
    get firstChild() { return this.children[0] || null; },
    appendChild(child) { this.children.push(child); return child; },
    insertBefore(child, ref) { this.children.splice(this.children.indexOf(ref), 0, child); return child; },
    setAttribute(name, value) { this.attributes[name] = value; }
  };
}
function find(root, id) {
  if (root.id === id) return root;
  for (const child of root.children) { const hit = find(child, id); if (hit) return hit; }
  return null;
}
const text = (root) => [root.textContent, ...root.children.map(text)].join("|");
const tick = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r)); };

async function mount(health, ok = true) {
  const main = node("main");
  main.appendChild(node("section"));
  const calls = {fetch: [], intervals: []};
  const context = {
    document: {querySelector: () => main, createElement: node},
    fetch: async (url) => { calls.fetch.push(String(url));
      return {ok, async json() { return health; }}; },
    setInterval(fn, ms) { calls.intervals.push(ms); return 1; },
    Date, Object, console
  };
  vm.runInNewContext(code, context, {timeout: 2000});
  await tick();
  return {main, calls, banner: find(main, "health-alert")};
}

const svc = (extra = {}) => ({service: "snapshot", consecutive_failures: 17, failing_since: 1795000000,
  last_success: 1794990000, threshold: 3, alert: true, ...extra});

test("the banner is the first element of the page and stays hidden while everything is healthy", async () => {
  const {main, banner, calls} = await mount({version: 1, alert: false,
    services: [svc({alert: false, consecutive_failures: 1})]});
  assert.equal(main.children[0], banner);
  assert.equal(banner.hidden, true);
  assert.equal(banner.attributes.role, "alert");
  assert.deepEqual(calls.fetch, ["/api/v1/health"]);
  assert.deepEqual(calls.intervals, [60000]);
});

test("a failing service is named with its streak, start and last success", async () => {
  const {banner} = await mount({version: 1, alert: true, services: [
    svc(), svc({service: "knowledge", consecutive_failures: 3, last_success: null}),
    svc({service: "daily", alert: false})]});
  assert.equal(banner.hidden, false);
  const shown = text(banner);
  assert.match(shown, /連続で失敗しているサービスがあります/);
  assert.match(shown, /Snapshot（バックアップ）\ が 17 回連続で失敗しています/);
  assert.match(shown, /知識提案 が 3 回連続で失敗しています/);
  assert.match(shown, /最後の成功: 記録なし/);
  assert.doesNotMatch(shown, /GitHub Daily/);
});

test("a failed read leaves the banner as it was and never throws", async () => {
  const {banner} = await mount({}, false);
  assert.equal(banner.hidden, true);
});
