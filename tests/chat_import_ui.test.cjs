const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const code = fs.readFileSync("src/obsidian_automation/three_services/chat-import.js", "utf8");

function node(tag) {
  return {
    tag, children: [], listeners: {}, hidden: false, textContent: "", value: "",
    disabled: false, files: null,
    appendChild(child) { this.children.push(child); return child; },
    setAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; },
    addEventListener(event, callback) { this.listeners[event] = callback; }
  };
}
function find(root, id) {
  if (root.id === id) return root;
  for (const child of root.children) {
    const hit = find(child, id);
    if (hit) return hit;
  }
  return null;
}
function allText(root) {
  return [root.textContent, ...root.children.map(allText)].join("|");
}
const tick = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r)); };

async function mount({status, xhr, confirmAnswer = true, post} = {}) {
  const main = node("main");
  const calls = {fetch: [], xhr: [], confirm: 0};
  const fetch = async (url, options = {}) => {
    calls.fetch.push({url: String(url), options});
    if (url === "/api/v1/session") return {ok: true, async json() { return {csrf: "tok"}; }};
    if (url === "/api/v1/chat-import") {
      if (!status) return {ok: false, async json() { return {error: "unavailable"}; }};
      return {ok: true, async json() { return status; }};
    }
    if (post) return post(url, options);
    return {ok: true, async json() { return {}; }};
  };
  class XMLHttpRequest {
    constructor() { this.headers = {}; this.upload = {}; calls.xhr.push(this); }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(k, v) { this.headers[k] = v; }
    send(body) {
      this.body = body;
      const outcome = xhr || {status: 200, body: {}};
      if (this.upload.onprogress) this.upload.onprogress({lengthComputable: true, loaded: 1, total: 2});
      this.status = outcome.status;
      this.responseText = JSON.stringify(outcome.body);
      setImmediate(() => this.onload());
    }
  }
  const context = {
    document: {
      querySelector: (selector) => selector === "main" ? main : null,
      createElement: node
    },
    fetch, XMLHttpRequest, confirm() { calls.confirm++; return confirmAnswer; },
    console, JSON, Object, Math, String, Error, Promise
  };
  vm.runInNewContext(code, context, {timeout: 2000});
  await tick();
  return {main, calls, panel: find(main, "chat-import")};
}

const enabled = (extra = {}) => ({
  version: 1, enabled: true, providers: ["chatgpt", "claude"],
  max_upload_bytes: 2 * 1024 ** 3, ready_archives: 0, scopes: {}, ...extra
});

test("panel stays hidden when the server did not enable chat import", async () => {
  const {panel} = await mount({status: null});
  assert.equal(panel.hidden, true);
});

test("enabled panel shows count-only summary and never offers a Vault write", async () => {
  const {panel} = await mount({status: enabled({
    ready_archives: 2,
    scopes: {"private-claude": {known_conversations: 7, committed_runs: 1,
      last_run: {status: "ok", added: 7, changed: 0, unchanged: 0,
                 absent_not_deleted: 0, quarantined_slots: 1, retained_messages: 30}}}
  })});
  assert.equal(panel.hidden, false);
  const text = allText(find(panel, "chat-import-summary"));
  assert.match(text, /保管済みZIP\|2/);
  assert.match(text, /Claude\|会話 7 件/);
  assert.match(text, /ChatGPT\|未取り込み/);
  assert.match(allText(panel), /Chat import/);
  assert.doesNotMatch(allText(panel), /Vault・WebDAV・外部サービスへは何も書き込まず/);  // no description text
  assert.equal(panel.tag, "section");
});

test("upload sends the ZIP with CSRF and zip content type, then selects the detected provider", async () => {
  const result = {provider: "claude", inspection: "inspected",
    counts: {conversations: 3, messages: 12, empty_slots: 0},
    publish: "published", bytes: 10, vault_write: false};
  const {panel, calls} = await mount({status: enabled(), xhr: {status: 200, body: result}});
  const input = find(panel, "chat-import-file");
  const file = {name: "export.zip", size: 10};
  input.files = [file];
  input.listeners.change();
  const upload = find(panel, "chat-import-upload");
  assert.equal(upload.disabled, false);
  upload.listeners.click();
  await tick();
  const request = calls.xhr[0];
  assert.equal(request.url, "/api/v1/chat-import/upload");
  assert.equal(request.headers["Content-Type"], "application/zip");
  assert.equal(request.headers["X-Obsidian-CSRF"], "tok");
  assert.equal(request.body, file);
  assert.equal(find(panel, "chat-import-provider").value, "claude");
  assert.match(find(panel, "chat-import-message").textContent,
    /Claude のエクスポートとして保管しました（会話 3 件 \/ 発言 12 件/);
});

test("upload errors are shown in Japanese and re-enable the controls", async () => {
  const {panel} = await mount({status: enabled(),
    xhr: {status: 413, body: {error: "upload_too_large"}}});
  const input = find(panel, "chat-import-file");
  input.files = [{name: "big.zip", size: 1}];
  find(panel, "chat-import-upload").listeners.click();
  await tick();
  assert.equal(find(panel, "chat-import-message").textContent, "ファイルが大きすぎます");
  assert.equal(find(panel, "chat-import-preview").disabled, false);
});

test("commit asks for confirmation; cancelling sends nothing", async () => {
  const sent = [];
  const post = async (url, options) => {
    sent.push([url, JSON.parse(options.body)]);
    return {ok: true, async json() {
      return {provider: "chatgpt", status: "committed_inventory_only", runs: [
        {status: "ok", added: 2, changed: 0, unchanged: 0, quarantined_slots: 0,
         retained_messages: 8}], generation_jobs_queued: 0, vault_write: false};
    }};
  };
  const declined = await mount({status: enabled(), confirmAnswer: false, post});
  find(declined.panel, "chat-import-commit").listeners.click();
  await tick();
  assert.equal(sent.length, 0);

  const accepted = await mount({status: enabled(), post});
  find(accepted.panel, "chat-import-provider").value = "chatgpt";
  find(accepted.panel, "chat-import-commit").listeners.click();
  await tick();
  assert.deepEqual(sent, [["/api/v1/chat-import/inventory", {provider: "chatgpt", commit: true}]]);
  assert.match(find(accepted.panel, "chat-import-message").textContent,
    /確定しました: 追加 2 \/ 変更 0/);
});
