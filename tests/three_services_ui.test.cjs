const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const code = fs.readFileSync(
  "src/obsidian_automation/three_services/app.js", "utf8"
);

async function mount(jobs, configuration = {}, options = {}) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        id, textContent: "", value: "", checked: false, hidden: false,
        disabled: true, dataset: {}, listeners: {},
        addEventListener(event, callback) { this.listeners[event] = callback; }
      });
    }
    return elements.get(id);
  }
  const defaultFetch = async (path) => {
    const key = String(path);
    if (key === "/api/v1/vault-root")
      return {ok:true, async json(){return {
        version:1,active_label:"Vault",enrolled:true,provider:"verified",
        can_rollback:false};}};
    if (key === "/api/v1/vault-bindings")
      return {ok:true, async json(){return {
        version:1,state:"default",
        roles:{daily:"00-DailyNote",project:"10-Project",system:"98-System"},
        can_reset:false,can_rollback:false};}};
    if (key === "/api/v1/core-promotion/configuration") {
      return {
        ok: true,
        async json() {
          return {
            policy_sha256: "a".repeat(64),
            checkpoint_initialized: true,
            requires_reauthorization: false,
            ...configuration
          };
        }
      };
    }
    return {ok: true, async json() {return jobs[key];}};
  };
  const fetch = options.fetch || defaultFetch;
  const context = {
    document: {
      getElementById: element,
      activeElement: null,
      hidden: false
    },
    fetch, console,
    setInterval() {},
    Intl, Date, Number, String, Object, Promise
  };
  vm.runInNewContext(code, context, {timeout: 2000});
  // Two promises from the top-level IIFE must both complete.
  if (!options.noWait) {
    for (let i = 0; i < 4; i++)
      await new Promise((resolve) => setImmediate(resolve));
  }
  return element;
}

function snapshot(verified) {
  return {
    version: 1, job: "vault_snapshot", settings: {
      configured: true, enabled: false, interval_seconds: 3600,
      next_due: null, gitea_write: verified ? "verified" : "unverified"
    },
    last_run: {status: "saved", stage: "verify", created: 1000, completed: 1100}
  };
}
function daily(configured, sourceMatches = configured) {
  return {
    version: 1, job: "github_daily_preview", settings: {
      configured, enabled: false, interval_seconds: 86400, next_due: null
    },
    publication_enabled: true,
    last_run: {status: "published", created: 1000, completed: 1100,
               source_matches_current: sourceMatches}
  };
}
function core(sourceMatches) {
  return {
    version: 1, job: "core_promotion", settings: {
      configured: true, source_bound: true, enabled: false,
      interval_seconds: 3600, next_due: null
    },
    last_run: {
      status: "promoted", created: 1000, completed: 1100,
      receipt_sha256: "d".repeat(64),
      source_matches_current: sourceMatches
    }
  };
}
function jobs(s, d, c) {
  return {
    "/api/v1/snapshot": snapshot(s),
    "/api/v1/daily": daily(d),
    "/api/v1/core-promotion": core(c)
  };
}

test("old-target successful receipts are historical, not currently verified", async () => {
  const el = await mount(jobs(false, false, false), {requires_reauthorization: true});
  for (const id of ["snapshot", "daily", "core-promotion"]) {
    assert.equal(el(id + "-status").dataset.kind, "unknown");
  }
  assert.match(el("snapshot-status").textContent, /旧接続先/);
  assert.match(el("core-promotion-status").textContent, /旧接続先/);
  assert.match(el("core-promotion-message").textContent, /旧Checkpoint/);
});

test("success is shown only when current connection and generation verify", async () => {
  const el = await mount(jobs(true, true, true));
  for (const id of ["snapshot", "daily", "core-promotion"]) {
    assert.equal(el(id + "-status").dataset.kind, "ok");
  }
});


test("Daily success on Vault A stays historical after Vault B is reconfigured", async () => {
  const values = jobs(true, true, true);
  values["/api/v1/daily"] = daily(true, false);
  const el = await mount(values);
  assert.equal(el("daily-configured").textContent, "設定済み");
  assert.equal(el("daily-status").dataset.kind, "unknown");
  assert.match(el("daily-status").textContent, /旧接続先/);
});

test("slow pre-mutation refresh cannot overwrite the post-save result", async () => {
  const values = jobs(true, true, true);
  let resolveOld = null;
  let snapshotReads = 0;
  const reply = (value) => ({ok: true, async json() {return value;}});

  const fetch = async (path, options) => {
    if (path === "/api/v1/snapshot") {
      snapshotReads++;
      if (snapshotReads === 2)
        return await new Promise(resolve => {resolveOld = resolve;});
      return reply(snapshot(snapshotReads === 1));
    }
    if (path === "/api/v1/session") return reply({csrf: "synthetic"});
    if (path === "/api/v1/snapshot/settings")
      return reply({enabled: true, interval_seconds: 3600});
    if (path === "/api/v1/core-promotion/configuration")
      return reply({policy_sha256:"a".repeat(64), checkpoint_initialized:true});
    if (path === "/api/v1/vault-root")
      return reply({active_label:"Vault",enrolled:true,provider:"verified",can_rollback:false});
    if (path === "/api/v1/vault-bindings")
      return reply({state:"default",roles:{daily:"00-DailyNote",project:"10-Project",
                   system:"98-System"},can_reset:false,can_rollback:false});
    return reply(values[path]);
  };

  const el = await mount(values, {}, {fetch});
  assert.equal(el("snapshot-status").dataset.kind, "ok");

  // An older refresh begins, then the operator changes the connection or
  // settings and a newer refresh reflects unverified state.
  el("refresh").listeners.click();
  assert.equal(typeof resolveOld, "function");
  el("snapshot-enabled").checked = true;
  el("snapshot-interval").value = "3600";
  el("snapshot-save").listeners.click();
  for (let i = 0; i < 8; i++)
    await new Promise(resolve => setImmediate(resolve));
  assert.equal(el("snapshot-status").dataset.kind, "unknown");

  // Old GET resolves with a verified A receipt AFTER the B response.
  resolveOld(reply(snapshot(true)));
  for (let i = 0; i < 8; i++)
    await new Promise(resolve => setImmediate(resolve));
  assert.equal(el("snapshot-status").dataset.kind, "unknown");
});
