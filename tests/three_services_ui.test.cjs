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
  let holdOne = false;
  let mutated = false;
  const reply = (value) => ({ok: true, async json() {return value;}});

  const fetch = async (path, options) => {
    if (path === "/api/v1/snapshot") {
      if (holdOne) {
        holdOne=false;
        return await new Promise(resolve => {resolveOld = resolve;});
      }
      return reply(snapshot(!mutated));
    }
    if (path === "/api/v1/session") return reply({csrf: "synthetic"});
    if (path === "/api/v1/snapshot/schedule-preview")
      return reply({version:1,mode:"anchored",timezone:"Asia/Tokyo",
                    anchor_local_time:"12:00",interval_seconds:3600,
                    next_slots:[1000,4600,8200]});
    if (path === "/api/v1/snapshot/settings") {
      mutated=true;
      return reply({enabled:true,interval_seconds:3600});
    }
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
  el("snapshot-enabled").checked = true;
  el("snapshot-interval").value = "3600";
  el("snapshot-anchor").value = "12:00";
  el("snapshot-enabled").listeners.change(); // browser marks unsaved draft
  // The phase-preview is an explicit prerequisite of settings mutation.
  el("snapshot-preview").listeners.click();
  for (let i = 0; i < 8; i++)
    await new Promise(resolve => setImmediate(resolve));
  assert.match(el("snapshot-message").textContent, /Preview成功/);
  assert.match(el("snapshot-preview-result").textContent, /次の3回/);

  // An older refresh begins, then the operator saves. The newer GET
  // correctly reports that the old source's receipt is unverified.
  holdOne=true;
  el("refresh").listeners.click();
  assert.equal(typeof resolveOld, "function");
  el("snapshot-save").listeners.click();
  for (let i = 0; i < 8; i++)
    await new Promise(resolve => setImmediate(resolve));
  assert.equal(mutated, true);
  assert.equal(el("snapshot-message").textContent, "");
  assert.equal(el("snapshot-status").dataset.kind, "unknown");

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

test("saving without phase-preview never changes configured job", async () => {
  const values=jobs(true,true,true);
  let mutated=false;
  const reply = (value) => ({ok:true, async json() {return value;}});
  const fetch = async path => {
    if(path==="/api/v1/snapshot/settings")mutated=true;
    if(path==="/api/v1/vault-root")
      return reply({active_label:"Vault",enrolled:true,provider:"verified",can_rollback:false});
    if(path==="/api/v1/vault-bindings")
      return reply({state:"default",roles:{daily:"00-DailyNote",project:"10-Project",
                    system:"98-System"},can_reset:false,can_rollback:false});
    if(path==="/api/v1/core-promotion/configuration")
      return reply({policy_sha256:"a".repeat(64), checkpoint_initialized:true});
    return reply(values[path]);
  };
  const el=await mount(values,{}, {fetch});
  el("snapshot-enabled").checked=true;
  el("snapshot-save").listeners.click();
  for (let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));
  assert.equal(mutated,false);
  assert.match(el("eva-warning-text").textContent,/Previewしてください/);
});


test("schedule values individually mark saved versus changed while Save stays atomic", async () => {
  const values=jobs(true,true,true);
  const s=values["/api/v1/snapshot"].settings;
  s.schedule={mode:"anchored",timezone:"Asia/Tokyo",
              anchor_local_time:"12:00",revision:3};
  const el=await mount(values);
  for(const kind of ["enabled","interval","anchor"]){
    assert.equal(el("snapshot-"+kind+"-state").dataset.state,"verified");
    assert.match(el("snapshot-"+kind+"-state").textContent,/✓ 保存済み/);
  }
  assert.equal(el("daily-anchor-state").textContent,"未設定");
  el("snapshot-anchor").value="13:00";
  el("snapshot-anchor").listeners.input();
  assert.equal(el("snapshot-anchor-state").dataset.state,"editing");
  assert.match(el("snapshot-anchor-state").textContent,/未保存/);
  assert.equal(el("snapshot-interval-state").dataset.state,"verified");
  assert.equal(el("snapshot-enabled-state").dataset.state,"verified");
  assert.equal(el("snapshot-preview-result").textContent, "");  // no instruction text: a missing Preview shows the in-page warning on SAVE
});


test("public connection values are visible, while saved secrets use an eight-circle placeholder", async () => {
  const models=jobs(true,true,true);
  // A generated noncredential sentinel tests the forbidden-server-value case
  // without including password-shaped literals in the repository.
  const forbiddenValue=Array.from({length:5},(_,i)=>String(i)).join("_");
  const responses={
    "/api/v1/nextcloud/connections":{
      fields:{webdav_url:"verified",vault_folder:"verified",nextcloud_user:"verified",
              nextcloud_password:"verified"},
      values:{webdav_url:"https://cloud.invalid/remote.php/dav/files/user/",
              vault_folder:"ActualVault",nextcloud_user:"fixture-user",
              // Even a faulty provider response must not fill a secret input.
              nextcloud_password:forbiddenValue}
    },
    "/api/v1/snapshot/connections":{
      fields:{git_url:"verified",git_user:"verified",git_token:"verified",branch:"verified"},
      values:{git_url:"https://git.invalid/team/Vault.git",git_user:"gitbot",branch:"main",
              git_token:forbiddenValue}
    },
    "/api/v1/daily/connections":{
      fields:{github_token:"verified"},values:{github_token:forbiddenValue}
    }
  };
  const reply=v=>({ok:true,async json(){return v;}});
  const fetch=async path=>{
    if(path==="/api/v1/core-promotion/configuration")
      return reply({policy_sha256:"a".repeat(64),checkpoint_initialized:true});
    if(path==="/api/v1/vault-root")
      return reply({active_label:"ActualVault",enrolled:true,provider:"verified",can_rollback:false});
    if(path==="/api/v1/vault-bindings")
      return reply({state:"default",roles:{daily:"00-DailyNote",project:"10-Project",
                    system:"98-System"},can_reset:false,can_rollback:false});
    return reply(responses[path]??models[path]);
  };
  const el=await mount(models,{}, {fetch});
  for(const [id,value] of Object.entries({
    "field-nextcloud-webdav_url":"https://cloud.invalid/remote.php/dav/files/user/",
    "field-nextcloud-vault_folder":"ActualVault",
    "field-nextcloud-nextcloud_user":"fixture-user",
    "field-gitea-git_url":"https://git.invalid/team/Vault.git",
    "field-gitea-git_user":"gitbot",
    "field-gitea-branch":"main"
  })){
    assert.equal(el(id).value,value,id);
  }
  for(const id of ["field-nextcloud-nextcloud_password",
                   "field-gitea-git_token","field-github-github_token"]){
    assert.equal(el(id).value,"",id+" secret value must remain empty");
    assert.equal(el(id).placeholder,"●".repeat(8),id);
    assert.equal(el(id).placeholder.length,8);
    assert.match(el(id.replace(/^field-/,"status-")).textContent,/✓ 保存済み/);
  }

  // Typing a new public value must survive automatic refresh. Once the
  // operator saves, only that field is transmitted and persisted.
  const input=el("field-nextcloud-vault_folder");
  input.value="ManualEdit";
  input.listeners.input();
  el("refresh").listeners.click();
  for(let i=0;i<9;i++)await new Promise(resolve=>setImmediate(resolve));
  assert.equal(input.value,"ManualEdit");
  assert.match(el("status-nextcloud-vault_folder").textContent,/変更中/);
});


test("missing credentials have no mask, staged credentials have a fixed mask but no submitted value", async () => {
  const values=jobs(true,true,true);
  let sent=false;
  const reply=v=>({ok:true,async json(){return v;}});
  const fetch=async path=>{
    if(path==="/api/v1/session")return reply({csrf:"fixture"});
    if(path==="/api/v1/core-promotion/configuration")
      return reply({policy_sha256:"a".repeat(64),checkpoint_initialized:true});
    if(path==="/api/v1/vault-root")return reply({active_label:"",enrolled:false,provider:"verified"});
    if(path==="/api/v1/vault-bindings")
      return reply({state:"default",roles:{daily:"00-DailyNote",project:"10-Project",
                    system:"98-System"},can_reset:false,can_rollback:false});
    if(path==="/api/v1/nextcloud/connections")return reply({
      fields:{webdav_url:"saved",vault_folder:"saved",nextcloud_user:"saved",nextcloud_password:"saved"},
      values:{webdav_url:"https://fixture.invalid/remote.php/dav/files/user/",
              vault_folder:"StagedVault",nextcloud_user:"staged-owner"}
    });
    if(path==="/api/v1/snapshot/connections")return reply({
      fields:{git_url:"missing",git_user:"missing",git_token:"missing",branch:"missing"},values:{}
    });
    if(path==="/api/v1/daily/connections")return reply({
      fields:{github_token:"missing"},values:{}
    });
    if(path.endsWith("/field"))sent=true;
    return reply(values[path]);
  };
  const el=await mount(values,{}, {fetch});
  assert.equal(el("field-nextcloud-vault_folder").value,"StagedVault");
  assert.equal(el("field-nextcloud-nextcloud_password").placeholder,"●".repeat(8));
  assert.equal(el("field-nextcloud-nextcloud_password").value,"");
  for(const id of ["field-gitea-git_token","field-github-github_token"]){
    assert.equal(el(id).placeholder,"");
    assert.equal(el(id).value,"");
  }
  el("save-nextcloud-nextcloud_password").listeners.click();
  for(let i=0;i<9;i++)await new Promise(resolve=>setImmediate(resolve));
  assert.equal(sent,false,"visual mask must not be submitted as a credential");
  assert.match(el("nextcloud-message").textContent,/入力してください/);
});

test("UI copy matches operator request without extra footer or schedule success prose", async () => {
  const html=fs.readFileSync("src/obsidian_automation/three_services/index.html","utf8");
  assert.equal(html.match(/起点時刻<input/g)?.length,3);
  assert.equal(html.includes("Asia/Tokyo"),false);
  assert.equal(html.match(/実行間隔\(秒\)</g)?.length,3);
  assert.equal(html.includes("（秒）"),false);
  for(const gone of ["Vault → private Gitea","GitHub ObsidianCore → Vault","GitHub Issue / PR → 前日JSTのDaily Note",
    "起点時刻を保存する前にPreview","設定を保存","次の3回をプレビュー","最終実行","次回予定"])
    assert.equal(html.includes(gone),false,gone);
  for(const label of ["Status","Last run","Next run","Preview","SAVE","Run Once"])assert.ok(html.includes(label),label);
  assert.equal(html.match(/<dialog class="eva-dialog" id="(snapshot|core-promotion|daily)-dialog"/g)?.length,3);
  assert.equal(html.match(/class="outline gear"/g)?.length,3);
  assert.ok(html.includes("RootやSystemの変更後は再検証が必要です。変更した機能の自動実行はOffになります。"));
  for(const deleted of [
    "Vault内のファイルは移動・編集しません。",
    "Core Promotionは旧Checkpointを再利用できず、再検証が必要です。",
    "最終結果が不明な処理は自動再試行しません。"
  ])assert.equal(html.includes(deleted),false);
  assert.equal(html.includes("<footer>"),false);
  assert.equal(code.includes("起点時刻の設定を保存しました。実行中のJobは取り消しません。"),false);
  assert.equal(code.includes("JSTの壁時計で固定発火"),false);
  assert.equal(html.match(/ class="service"/g)?.length,3);
  const models=jobs(true,true,true);
  models["/api/v1/snapshot"].settings.schedule={
    mode:"anchored",timezone:"Asia/Tokyo",anchor_local_time:"12:00",revision:3
  };
  const el=await mount(models);
  assert.equal(el("snapshot-phase-mode").textContent,"");
  assert.equal(el("snapshot-phase-mode").hidden,true);
  assert.match(el("daily-phase-mode").textContent,/旧Interval方式/);
});



test("generic HTTPS root registration does not advertise Nextcloud picker or inode identity", async () => {
  const vals=jobs(true,true,true);
  const reply=v=>({ok:true,async json(){return v;}});
  const fetch=async path=>{
    if(path==="/api/v1/vault-root")return reply({
      active_label:"Vault",enrolled:true,provider:"generic_https_path",
      activation:"not_available",can_rollback:false
    });
    if(path==="/api/v1/vault-bindings")return reply({
      state:"default",roles:{daily:"00-DailyNote",
        project:"10-Project",system:"98-System"},can_reset:false,
      can_rollback:false,system_fixed_under_promotion:true
    });
    if(path==="/api/v1/core-promotion/configuration")return reply({
      policy_sha256:"a".repeat(64),checkpoint_initialized:true
    });
    return reply(vals[path]);
  };
  const el=await mount(vals,{}, {fetch});
  assert.match(el("vault-root-provider").textContent,/ID保証なし/);
  assert.match(el("vault-root-enroll").textContent,/HTTPSパスを検証/);
  for(const name of ["list","enter","preview","activate","rollback"])
    assert.equal(el("vault-root-"+name).disabled,true,name);
  assert.equal(el("vault-root-candidate").disabled,true);
  assert.equal(el("vault-enrollment-status").dataset.state,"verified");
});
