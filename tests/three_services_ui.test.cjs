const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const code = fs.readFileSync(
  "src/obsidian_automation/three_services/app.js", "utf8"
);

async function mount(jobs, configuration = {}) {
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
  const fetch = async (path) => {
    const key = String(path);
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
  for (let i = 0; i < 4; i++) await new Promise((resolve) => setImmediate(resolve));
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
function daily(configured) {
  return {
    version: 1, job: "github_daily_preview", settings: {
      configured, enabled: false, interval_seconds: 86400, next_due: null
    },
    publication_enabled: true,
    last_run: {status: "published", created: 1000, completed: 1100}
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
