const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const code = fs.readFileSync("src/obsidian_automation/webdav_admin/app.js", "utf8");

function node(tag) {
  return {
    tag, children: [], listeners: {}, hidden: false, value: "", disabled: false, _text: "",
    // like a real DOM node: assigning textContent replaces all children
    get textContent() { return this._text; },
    set textContent(value) { this._text = value; this.children = []; },
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(event, callback) { this.listeners[event] = callback; }
  };
}
function walk(root, visit) {
  visit(root);
  for (const child of root.children) walk(child, visit);
}
function findAll(root, predicate) { const hits = []; walk(root, (n) => predicate(n) && hits.push(n)); return hits; }
function allText(root) { return [root.textContent, ...root.children.map(allText)].join("|"); }
const tick = async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r)); };

const users = [
  {name: "vault-desktop", description: "デスクトップ用", protected: false, created: "2026-11-24T00:00:00Z", updated: "2026-11-24T00:00:00Z"},
  {name: "vault-oa", description: "", protected: true, created: null, updated: null}
];

async function mount({post, confirmAnswer = true, list = users} = {}) {
  const ids = {};
  for (const id of ["message", "users", "secret", "create", "create-name", "create-description", "create-password"]) {
    ids[id] = Object.assign(node(id === "create" ? "form" : "div"), {id});
  }
  ids.secret.hidden = true;
  const calls = {fetch: [], confirm: [], clipboard: []};
  const fetch = async (url, options = {}) => {
    calls.fetch.push({url: String(url), options});
    if (url === "/api/v1/session") return {ok: true, async json() { return {csrf: "tok"}; }};
    if (url === "/api/v1/users" && !options.method || options.method === "GET") {
      return {ok: true, async json() { return {users: list, protected: ["nc-vault-ui", "vault-oa"]}; }};
    }
    return post ? post(url, options) : {ok: true, async json() { return {name: "x", password: null}; }};
  };
  const context = {
    document: {getElementById: (id) => ids[id], createElement: node},
    fetch, confirm(message) { calls.confirm.push(message); return confirmAnswer; },
    navigator: {clipboard: {async writeText(value) { calls.clipboard.push(value); }}},
    console, JSON, Object, Math, String, Error, Promise
  };
  vm.runInNewContext(code, context, {timeout: 2000});
  await tick();
  return {ids, calls};
}
const posts = (calls) => calls.fetch.filter((c) => c.options.method === "POST" && c.url !== "/api/v1/session");
const button = (root, label) => findAll(root, (n) => n.tag === "button" && n.textContent === label)[0];

test("the list shows users with a protected badge and never a hash or password", async () => {
  const {ids} = await mount();
  const text = allText(ids.users);
  assert.match(text, /vault-desktop/);
  assert.match(text, /デスクトップ用/);
  assert.match(text, /vault-oa/);
  assert.equal(findAll(ids.users, (n) => n.textContent === " 保護").length, 1);
  assert.doesNotMatch(text, /\$2y|\$fake|password/i);
});

test("creating a user sends the CSRF token, shows a generated password once, and clears the form", async () => {
  const post = async () => ({ok: true, async json() { return {name: "vault-new", password: "Gen-Pass_abcdefghijkl"}; }});
  const {ids, calls} = await mount({post});
  ids["create-name"].value = " vault-new ";
  ids["create-description"].value = "テスト";
  let prevented = false;
  await ids.create.listeners.submit({preventDefault() { prevented = true; }});
  await tick();
  assert.equal(prevented, true);
  const sent = posts(calls)[0];
  assert.equal(sent.url, "/api/v1/users");
  assert.equal(sent.options.headers["X-Admin-CSRF"], "tok");
  assert.deepEqual(JSON.parse(sent.options.body), {name: "vault-new", description: "テスト"});
  assert.equal(ids.secret.hidden, false);
  assert.match(allText(ids.secret), /Gen-Pass_abcdefghijkl/);
  assert.match(allText(ids.secret), /この画面でしか表示されません/);
  assert.equal(ids["create-name"].value, "");
  button(ids.secret, "閉じる").listeners.click();
  assert.equal(ids.secret.hidden, true);
  assert.doesNotMatch(allText(ids.secret), /Gen-Pass/);
});

test("a typed password is sent but never shown back", async () => {
  const post = async () => ({ok: true, async json() { return {name: "vault-new", password: null}; }});
  const {ids, calls} = await mount({post});
  ids["create-name"].value = "vault-new";
  ids["create-password"].value = "my-own-long-password";
  await ids.create.listeners.submit({preventDefault() {}});
  await tick();
  assert.equal(JSON.parse(posts(calls)[0].options.body).password, "my-own-long-password");
  assert.equal(ids.secret.hidden, true);
  assert.equal(ids["create-password"].value, "");
});

test("deleting asks for confirmation naming the effect; cancelling sends nothing", async () => {
  const declined = await mount({confirmAnswer: false});
  button(declined.ids.users, "削除").listeners.click();
  await tick();
  assert.equal(posts(declined.calls).length, 0);
  assert.match(declined.calls.confirm[0], /すぐに接続できなくなります/);

  const accepted = await mount();
  button(accepted.ids.users, "削除").listeners.click();
  await tick();
  const sent = posts(accepted.calls)[0];
  assert.equal(sent.url, "/api/v1/users/vault-desktop/delete");
  assert.deepEqual(JSON.parse(sent.options.body), {confirm: "vault-desktop"});
});

test("resetting a password auto-generates by default and shows it once", async () => {
  const post = async () => ({ok: true, async json() { return {name: "vault-desktop", password: "Fresh-Generated-Pass-1"}; }});
  const {ids, calls} = await mount({post});
  button(ids.users, "パスワード再発行").listeners.click();
  button(ids.users, "再発行する").listeners.click();
  await tick();
  const sent = posts(calls)[0];
  assert.equal(sent.url, "/api/v1/users/vault-desktop/password");
  assert.deepEqual(JSON.parse(sent.options.body), {});
  assert.match(allText(ids.secret), /Fresh-Generated-Pass-1/);
});

test("editing the description posts only the description", async () => {
  const {ids, calls} = await mount();
  button(ids.users, "説明を編集").listeners.click();
  const input = findAll(ids.users, (n) => n.tag === "input")[0];
  input.value = "新しい説明";
  button(ids.users, "保存する").listeners.click();
  await tick();
  const sent = posts(calls)[0];
  assert.equal(sent.url, "/api/v1/users/vault-desktop/description");
  assert.deepEqual(JSON.parse(sent.options.body), {description: "新しい説明"});
});

test("server error codes are shown in Japanese", async () => {
  const post = async () => ({ok: false, async json() { return {error: "user_exists"}; }});
  const {ids} = await mount({post});
  ids["create-name"].value = "vault-desktop";
  await ids.create.listeners.submit({preventDefault() {}});
  await tick();
  assert.equal(ids.message.textContent, "同じ名前のユーザーがすでにあります");
  assert.equal(ids.secret.hidden, true);
});
