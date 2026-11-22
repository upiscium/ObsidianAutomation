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
    confirm(){return true;},
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


test("each connection value has its own checkmark and one-field Save", async () => {
  const values=jobs(true,true,true);
  const state={
    nextcloud:{fields:{
      webdav_url:"verified",vault_folder:"verified",
      nextcloud_user:"verified",nextcloud_password:"missing"
    }},
    gitea:{fields:{git_url:"saved",git_user:"missing",git_token:"missing",branch:"missing"}},
    github:{fields:{github_token:"verified"}}
  };
  let submitted=null;
  const reply=value=>({ok:true,async json(){return value;}});
  const fake=async(path,options)=>{
    if(path==="/api/v1/session")return reply({csrf:"fixture"});
    if(path==="/api/v1/core-promotion/configuration")
      return reply({policy_sha256:"a".repeat(64),checkpoint_initialized:true});
    if(path==="/api/v1/vault-root")
      return reply({active_label:"Vault",enrolled:true,provider:"verified",can_rollback:false});
    if(path==="/api/v1/vault-bindings")
      return reply({state:"default",roles:{daily:"00-DailyNote",project:"10-Project",
                   system:"98-System"},can_reset:false,can_rollback:false});
    if(path==="/api/v1/nextcloud/connections")return reply(state.nextcloud);
    if(path==="/api/v1/snapshot/connections")return reply(state.gitea);
    if(path==="/api/v1/daily/connections")return reply(state.github);
    if(path==="/api/v1/nextcloud/connections/field"){
      submitted=JSON.parse(options.body);
      state.nextcloud.fields[submitted.field]="verified";
      return reply({field_result:"verified",fields:state.nextcloud.fields});
    }
    return reply(values[path]);
  };
  const el=await mount(values,{}, {fetch:fake});
  assert.match(el("status-nextcloud-webdav_url").textContent,/✓ 保存済み/);
  assert.equal(el("status-nextcloud-nextcloud_password").textContent,"未設定");
  assert.match(el("status-gitea-git_url").textContent,/一時保存/);
  assert.match(el("status-github-github_token").textContent,/✓ 保存済み/);

  const field=el("field-nextcloud-nextcloud_password");
  field.value="fixture-passphrase";
  field.listeners.input();
  assert.match(el("status-nextcloud-nextcloud_password").textContent,/変更中/);
  el("save-nextcloud-nextcloud_password").listeners.click();
  for(let i=0;i<10;i++)await new Promise(resolve=>setImmediate(resolve));
  assert.equal(JSON.stringify(submitted),JSON.stringify({
    field:"nextcloud_password",value:"fixture-passphrase"
  }));
  assert.equal(field.value,"");
  assert.match(el("status-nextcloud-nextcloud_password").textContent,/✓ 保存済み/);
  assert.match(el("status-nextcloud-vault_folder").textContent,/✓ 保存済み/);
});


test("folder role Preview/Save changes only selected role", async () => {
  const values=jobs(true,true,true);
  const status={
    state:"ok",can_reset:true,can_rollback:false,
    system_fixed_under_promotion:true,
    roles:{daily:"00-DailyNote",project:"10-Project",system:"98-System"}
  };
  let previewPayload=null,savePayload=null;
  const reply=v=>({ok:true,async json(){return v;}});
  const fetch=async(path,options)=>{
    if(path==="/api/v1/session")return reply({csrf:"fixture"});
    if(path==="/api/v1/core-promotion/configuration")
      return reply({policy_sha256:"a".repeat(64),checkpoint_initialized:true});
    if(path==="/api/v1/vault-root")
      return reply({active_label:"Vault",enrolled:true,provider:"verified",can_rollback:false});
    if(path==="/api/v1/vault-bindings")return reply(status);
    if(path==="/api/v1/vault-bindings/previews"){
      previewPayload=JSON.parse(options.body);
      return reply({status:"candidate_valid",handle:"fixture-handle"});
    }
    if(path==="/api/v1/vault-bindings/saves"){
      savePayload=JSON.parse(options.body);
      status.roles.daily="01-Daily";
      return reply({status:"saved",reason:"requires_job_reenable"});
    }
    return reply(values[path]);
  };
  const el=await mount(values,{}, {fetch});
  assert.match(el("vault-bindings-project-status").textContent,/✓ 保存済み/);
  assert.equal(el("vault-bindings-system-status").textContent,"✓ 固定");
  el("vault-bindings-daily").value="01-Daily";
  el("vault-bindings-daily").listeners.input();
  assert.match(el("vault-bindings-daily-status").textContent,/未保存/);
  el("vault-bindings-daily-preview").listeners.click();
  for(let i=0;i<10;i++)await new Promise(resolve=>setImmediate(resolve));
  assert.equal(JSON.stringify(previewPayload),JSON.stringify({
    roles:{daily:"01-Daily"}
  }));
  assert.equal(el("vault-bindings-daily-save").disabled,false);
  el("vault-bindings-daily-save").listeners.click();
  for(let i=0;i<10;i++)await new Promise(resolve=>setImmediate(resolve));
  assert.equal(JSON.stringify(savePayload),JSON.stringify({
    handle:"fixture-handle",confirm:true
  }));
  assert.equal(status.roles.project,"10-Project");
  assert.match(el("vault-bindings-daily-status").textContent,/✓ 保存済み/);
});


test("a quarantined Core checkpoint and invalid System path show no green check", async () => {
  const values=jobs(true,true,true);
  const baseline=values["/api/v1/core-promotion"];
  baseline.settings.configured=false;
  const el=await mount(values,{
    checkpoint_initialized:true,configured:false,
    requires_reauthorization:true,target_binding_valid:false
  },{
    fetch: async path=>{
      const reply=v=>({ok:true,async json(){return v;}});
      if(path==="/api/v1/core-promotion/configuration")
        return reply({policy_sha256:"a".repeat(64),checkpoint_initialized:true,
          configured:false,requires_reauthorization:true,target_binding_valid:false});
      if(path==="/api/v1/vault-root")
        return reply({active_label:"Vault",enrolled:true,provider:"verified",can_rollback:false});
      if(path==="/api/v1/vault-bindings")
        return reply({state:"ok",roles:{daily:"00-DailyNote",
          project:"10-Project",system:"99-System"},
          system_fixed_under_promotion:false,can_reset:true,can_rollback:false});
      return reply(values[path]);
    }
  });
  assert.match(el("core-promotion-checkpoint-status").textContent,/要再確認/);
  assert.notEqual(el("core-promotion-checkpoint-status").dataset.state,"verified");
  assert.match(el("vault-bindings-system-status").textContent,/要確認/);
  assert.notEqual(el("vault-bindings-system-status").dataset.state,"fixed");
});
