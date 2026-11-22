/* Minimal three-service controls. Acknowledgment is not verified completion. */
(() => {
  "use strict";
  const names=["snapshot","core-promotion","daily"];
  const $=id=>document.getElementById(id);
  const data={};
  const previewSpecs={},scheduleDirty={},scheduleTouched={};
  let busy=false;
  let policy=null;
  // Reject out-of-order GET responses and those started before a mutation.
  let refreshGeneration=0;
  let configurationGeneration=0;
  let vaultGeneration=0;
  let connectionGeneration=0;
  let previewRootHandle=null;
  const previewBindings={},bindingsDirty=new Set(),connectionDirty=new Set();
  const connectionErrors=new Map();
  function invalidatePendingReads(){
    refreshGeneration++;
    configurationGeneration++;
    vaultGeneration++;
    connectionGeneration++;
  }
  const errors={
    unconfigured:"接続または初期設定が未完了です",
    busy:"別の実行が進行中です",
    job_busy:"実行中の処理があるため変更できません",
    promotion_busy:"Core Promotion実行中のため変更できません",
    mapping_present:"Vaultフォルダマッピングを先に確認してください",
    connection_probe_failed:"接続先を検証できませんでした",
    github_probe_failed:"GitHub Tokenを検証できませんでした",
    csrf_rejected:"操作セッションが期限切れです",
    origin_rejected:"アクセス元を検証できませんでした",
    checkpoint_already_initialized:"Checkpointは初期化済みです",
    invalid_request:"入力内容が不正です",
    needs_reconciliation:"未確認のCore Promotionが存在します。CheckpointとVaultの状態を照合するまで再実行できません",
    operation_unavailable:"結果不明です。再送せず、状態を確認してください",
    invalid_field:"項目の値が正しくありません",
    unreadable_field_draft:"一時保存した設定を読み取れません。管理者による確認が必要です",
    invalid_schedule:"起点時刻・間隔が不正です。間隔は60秒の倍数かつ24時間を割り切る値にしてください",
    stale_settings:"別の画面で設定が変わりました。再読込してPreviewし直してください",
    schedule_revision_required:"旧形式からの設定変更は拒否されました。新しいUIからPreviewしてください"
  };
  const msg=(id,s)=>{$(id).textContent=s;};
  function fieldState(id,state,label){
    const target=$(id);
    target.dataset.state=state;
    target.textContent=label;
  }
  const describe=s=>errors[s]||("操作できませんでした ("+String(s||"unknown")+")");
  function dateText(v) {
    if(typeof v!=="number"||!Number.isFinite(v)||v<0)return "—";
    const d=new Date(v*1000);
    return !Number.isFinite(d.getTime())?"—":
      new Intl.DateTimeFormat("ja-JP",{timeZone:"Asia/Tokyo",dateStyle:"short",timeStyle:"medium"}).format(d);
  }
  async function get(url){
    const r=await fetch(url,{credentials:"same-origin",cache:"no-store"});
    const x=await r.json();
    if(!r.ok)throw Error(x.error||"read_unavailable");
    return x;
  }
  async function post(url,body){
    const csrf=(await get("/api/v1/session")).csrf;
    const r=await fetch(url,{method:"POST",credentials:"same-origin",mode:"same-origin",
      headers:{"Content-Type":"application/json","X-Obsidian-CSRF":csrf},
      body:JSON.stringify(body)});
    let x;
    try{x=await r.json();}catch(e){throw Error("operation_unavailable");}
    if(!r.ok)throw Error(x.error||x.outcome||"operation_unavailable");
    return x;
  }
  function requestId(){
    if(!globalThis.crypto||!crypto.getRandomValues)throw Error("secure_random_unavailable");
    const b=new Uint8Array(16);
    crypto.getRandomValues(b);
    return Array.from(b,x=>x.toString(16).padStart(2,"0")).join("");
  }
  function view(n,x){
    if(!x||x.version!==1||!x.settings||typeof x.settings.configured!=="boolean")return {kind:"unknown",label:"状態未確認"};
    const s=x.last_run?.status;
    if(s==="queued"||s==="running")return {kind:"running",label:"実行中"};
    if(n==="snapshot"&&(s==="saved"||s==="no_changes"))
      return x.settings.gitea_write==="verified"&&x.last_run.stage==="verify"
        ?{kind:"ok",label:"保存・検証済み"}
        :{kind:"unknown",label:"旧接続先の履歴・現接続先は未検証"};
    if(n==="daily"&&s==="published")
      return x.settings.configured===true&&x.last_run.source_matches_current===true
        ?{kind:"ok",label:"Daily反映済み"}
        :{kind:"unknown",label:"旧接続先の履歴・現接続先は未検証"};
    if(n==="core-promotion"&&(s==="promoted"||s==="up_to_date")){
      if(x.settings.configured!==true||x.last_run.source_matches_current!==true)
        return {kind:"unknown",label:"旧接続先の履歴・現接続先は未検証"};
      if(s==="promoted")
        return x.last_run.receipt_sha256?{kind:"ok",label:"反映・検証済み"}
                                        :{kind:"unknown",label:"結果未検証"};
      return {kind:"ok",label:"最新・Live照合済み"};
    }
    if(["failed","publish_failed","conflict","live_drift"].includes(s))return {kind:"error",label:s==="live_drift"?"Live driftを検出":"失敗・競合"};
    if(["indeterminate","unconfirmed"].includes(s))return {kind:"unknown",label:"結果不明・要確認"};
    if(s==="preview_ready")return {kind:"unknown",label:"Previewのみ・未反映"};
    if(!x.settings.configured)return {kind:"unknown",label:"未設定"};
    if(n==="core-promotion"&&x.settings.source_bound===false)return {kind:"unknown",label:"接続再設定が必要"};
    return {kind:"idle",label:x.settings.enabled?"待機中":"停止中"};
  }
  function render(n,x){
    data[n]=x;
    const v=view(n,x),settings=x.settings,r=x.last_run;
    $(n+"-status").dataset.kind=v.kind;
    msg(n+"-status",v.label);
    msg(n+"-configured",settings.configured?"設定済み":"未設定");
    msg(n+"-last",r?(r.status+" · "+dateText(r.completed??r.created)):"実行履歴なし");
    msg(n+"-next",settings.enabled?dateText(settings.next_due):"自動実行Off");
    $(n+"-enabled").disabled=false;
    $(n+"-interval").disabled=false;
    if(!scheduleDirty[n]){
      if(document.activeElement!==$(n+"-enabled"))$(n+"-enabled").checked=settings.enabled;
      if(document.activeElement!==$(n+"-interval"))$(n+"-interval").value=settings.interval_seconds;
      if(document.activeElement!==$(n+"-anchor"))
        $(n+"-anchor").value=settings.schedule?.anchor_local_time??"12:00";
    }
    $(n+"-phase-mode").hidden=settings.schedule?.mode==="anchored";
    msg(n+"-phase-mode",settings.schedule?.mode==="anchored"?
      "":"旧Interval方式（Preview→保存で固定時刻へ移行）");
    for(const kind of ["enabled","interval","anchor"]){
      const marker=n+"-"+kind+"-state";
      if(scheduleTouched[n]?.has(kind)){
        fieldState(marker,"editing","● 未保存");
      }else if(kind==="anchor"&&settings.schedule?.mode!=="anchored"){
        fieldState(marker,"missing","未設定");
      }else{
        const actual=kind==="enabled"?$(n+"-enabled").checked:
          kind==="interval"?Number($(n+"-interval").value):
          $(n+"-anchor").value;
        const stored=kind==="enabled"?settings.enabled:
          kind==="interval"?settings.interval_seconds:
          settings.schedule.anchor_local_time;
        fieldState(marker,actual===stored?"verified":"editing",
                   actual===stored?"✓ 保存済み":"● 未保存");
      }
    }
    $(n+"-save").disabled=busy;
    $(n+"-run").disabled=busy||!settings.configured||(n==="core-promotion"&&settings.source_bound===false);
    if(n==="daily"&&x.publication_enabled===false)
      msg("daily-message","現在はPreview専用です。反映にはサーバーの --daily-publish が必要です。");
  }
  async function refresh(){
    if(busy)return;
    const generation=++refreshGeneration;
    const rs=await Promise.allSettled(names.map(n=>get("/api/v1/"+n)));
    if(generation!==refreshGeneration)return;
    rs.forEach((r,i)=>{
      const n=names[i];
      if(r.status==="fulfilled")render(n,r.value);
      else{
        delete data[n];
        $(n+"-status").dataset.kind="unknown";
        msg(n+"-status","状態取得失敗");
        msg(n+"-configured","—");msg(n+"-last","確認できません");msg(n+"-next","—");
        for(const s of ["enabled","interval","save","run"])$(n+"-"+s).disabled=true;
      }
    });
  }
  async function configuration(){
    if(busy)return;
    const generation=++configurationGeneration;
    try{
      const d=await get("/api/v1/core-promotion/configuration");
      if(generation!==configurationGeneration)return;
      policy=d.policy_sha256;
      $("core-promotion-policy").value=policy;
      $("core-promotion-bootstrap").disabled=busy||d.checkpoint_initialized||
        typeof policy!=="string"||!/^[0-9a-f]{64}$/.test(policy);
      $("core-promotion-bootstrap-section").hidden=false;
      const checkpointOk=d.checkpoint_initialized===true&&
        d.target_binding_valid===true&&d.requires_reauthorization!==true&&
        d.configured===true;
      fieldState("core-promotion-checkpoint-status",
                 checkpointOk?"verified":d.checkpoint_initialized?"failed":"missing",
                 checkpointOk?"✓ 使用可能":d.checkpoint_initialized?
                   "要再確認・再承認":"未初期化");
      if(d.requires_reauthorization===true)
        msg("core-promotion-message",
            "接続先Vaultが変更されたため、旧Checkpointでの実行を拒否しています。新しい基準の独立照合が必要です。");
    }catch(e){
      if(generation!==configurationGeneration)return;
      policy=null;$("core-promotion-bootstrap").disabled=true;
    }
  }
  function scheduleRequest(n){
    const rev=data[n]?.settings?.schedule?.revision??0;
    const body={enabled:$(n+"-enabled").checked,
      interval_seconds:Number($(n+"-interval").value),
      timezone:"Asia/Tokyo",anchor_local_time:$(n+"-anchor").value,
      expected_revision:rev};
    if(n==="snapshot"){
      body.source_name="nextcloud";
      body.destination_name="gitea";
    }
    return body;
  }
  async function schedulePreview(n){
    if(busy)return;
    if(!data[n]){msg(n+"-message","状態を取得してからPreviewしてください。");return;}
    const body=scheduleRequest(n),sig=JSON.stringify(body);
    busy=true;invalidatePendingReads();
    try{
      const result=await post("/api/v1/"+n+"/schedule-preview",{
        timezone:body.timezone,anchor_local_time:body.anchor_local_time,
        interval_seconds:body.interval_seconds
      });
      if(!Array.isArray(result.next_slots)||result.next_slots.length!==3)
        throw Error("operation_unavailable");
      if(JSON.stringify(scheduleRequest(n))!==sig)
        throw Error("stale_settings");
      previewSpecs[n]=sig;
      msg(n+"-preview-result","次の3回："+result.next_slots.map(dateText).join(" / "));
      msg(n+"-message","Preview成功。現在の設定で保存できます。");
    }catch(e){
      delete previewSpecs[n];
      msg(n+"-message",describe(e.message));
    }finally{busy=false;await refresh();await configuration();}
  }
  async function action(n,kind){
    if(busy)return;
    if(kind==="settings"&&previewSpecs[n]!==JSON.stringify(scheduleRequest(n))){
      msg(n+"-message","保存する前に現在の設定をPreviewしてください。");
      return;
    }
    busy=true;
    invalidatePendingReads();
    try{
      if(kind==="settings"){
        const body=scheduleRequest(n);
        await post("/api/v1/"+n+"/settings",body);
        scheduleDirty[n]=false;
        if(scheduleTouched[n])scheduleTouched[n].clear();
        delete previewSpecs[n];
        msg(n+"-message","");
      }else{
        const result=await post("/api/v1/"+n+"/runs",{request_id:requestId()});
        msg(n+"-message","受付 "+result.outcome+" · Run ID "+result.run_id+"。実行結果は別途照合します。");
      }
    }catch(e){msg(n+"-message",describe(e.message));}
    finally{busy=false;await refresh();await configuration();}
  }
  // Independent connection updates: no client-side copy of a saved secret,
  // no all-fields-at-once form, and no checkmark for unverified staged input.
  const connectionGroups={
    nextcloud:{url:"/api/v1/nextcloud/connections",
      fields:["webdav_url","vault_folder","nextcloud_user","nextcloud_password"]},
    gitea:{url:"/api/v1/snapshot/connections",
      fields:["git_url","git_user","git_token","branch"]},
    github:{url:"/api/v1/daily/connections",fields:["github_token"]}
  };
  // A constant-length placeholder is intentionally NOT an input value.
  // Password/token fields remain empty so the mask can never be submitted
  // to the provider as a replacement credential.
  const secretFields=new Set(["nextcloud_password","git_token","github_token"]);
  const secretMask="●".repeat(8);
  function statusField(group,field,state){
    const id="status-"+group+"-"+field;
    if(connectionErrors.has(group+"-"+field)){
      const why=connectionErrors.get(group+"-"+field);
      fieldState(id,why==="operation_unavailable"?"staged":"failed",
                 why==="operation_unavailable"?"? 結果不明":"✕ 検証失敗");
    }else if(connectionDirty.has(group+"-"+field)){
      fieldState(id,"editing","● 変更中");
    }else if(state==="verified"){
      fieldState(id,"verified","✓ 保存済み");
    }else if(state==="saved"){
      fieldState(id,"staged","◌ 一時保存（未検証）");
    }else if(state==="stale"){
      fieldState(id,"staged","◌ 保存済み・要再確認");
    }else if(state==="missing"){
      fieldState(id,"missing","未設定");
    }else{
      fieldState(id,"failed","確認できません");
    }
  }
  async function connectionRefresh(){
    if(busy)return;
    const generation=++connectionGeneration;
    const entries=Object.entries(connectionGroups);
    const replies=await Promise.allSettled(entries.map(([_,config])=>get(config.url)));
    if(generation!==connectionGeneration)return;
    entries.forEach(([group,config],i)=>{
      const response=replies[i].status==="fulfilled"?
        replies[i].value:null;
      const fields=response?.fields;
      const values=response?.values;
      for(const field of config.fields){
        const key=group+"-"+field;
        const input=$("field-"+key);
        if(secretFields.has(field)){
          // Never copy the credential or a stand-in into input.value.
          // Set the placeholder only when the server confirms its existence.
          input.placeholder=["verified","saved","stale"].includes(fields?.[field])
            ?secretMask:"";
        }else if(!connectionDirty.has(key)&&document.activeElement!==input){
          const stored=values?.[field];
          input.value=typeof stored==="string"?stored:"";
        }
        statusField(group,field,fields?.[field]);
      }
      $("verify-"+group).disabled=response?.ready_to_verify!==true;
    });
  }
  async function connectionField(group,field){
    if(busy)return;
    const config=connectionGroups[group];
    if(!config||!config.fields.includes(field))return;
    const input=$("field-"+group+"-"+field);
    const value=input.value;
    if(!value.trim()){
      msg(group+"-message","この項目の新しい値を入力してください。空欄は既存値を保持します。");
      return;
    }
    busy=true;invalidatePendingReads();
    fieldState("status-"+group+"-"+field,"editing","確認中…");
    msg(group+"-message","この項目を保存・確認しています…");
    try{
      const result=await post(config.url+"/field",{field,value});
      if(secretFields.has(field)){
        input.value="";  // no retained password/token or mask-as-value
      }else{
        const stored=result?.values?.[field];
        if(typeof stored==="string")input.value=stored;
      }
      connectionDirty.delete(group+"-"+field);
      connectionErrors.delete(group+"-"+field);
      msg(group+"-message",result.field_result==="staged"?
        "この項目を一時保存しました。必要な値が揃ったら接続検証します。":
        result.field_result==="unchanged"?"保存済みの値から変更ありません。":
        "項目を保存し、接続を検証しました。");
    }catch(e){
      connectionErrors.set(group+"-"+field,e.message);
      msg(group+"-message",describe(e.message));
    }
    finally{
      busy=false;
      await Promise.all([refresh(),configuration(),vaultRefresh(),connectionRefresh()]);
    }
  }
  async function verifyStaged(group){
    if(busy||!connectionGroups[group])return;
    busy=true;invalidatePendingReads();
    msg(group+"-message","保存済みの項目で接続を確認しています…");
    try{
      const result=await post(connectionGroups[group].url+"/verify",{});
      msg(group+"-message",result.field_result==="verified"?
        "接続を確認しました。項目の横にチェックマークが付きます。":
        "設定の確認結果："+String(result.field_result??"unknown"));
    }catch(e){msg(group+"-message",describe(e.message));}
    finally{
      busy=false;
      await Promise.all([refresh(),configuration(),vaultRefresh(),connectionRefresh()]);
    }
  }
  async function bootstrap(){
    if(busy||!policy)return;
    const sha=$("core-promotion-baseline").value.trim();
    if(!/^[0-9a-f]{40}$/.test(sha)){
      msg("core-promotion-message","基準SHAは40文字の小文字hexで入力してください。");return;
    }
    if(!confirm("指定コミットでCheckpointを初期化します。Vaultは変更しません。続行しますか？"))return;
    busy=true;
    invalidatePendingReads();
    try{
      const r=await post("/api/v1/core-promotion/bootstrap",{core_commit:sha,policy_sha256:policy});
      msg("core-promotion-message","Checkpoint初期化済み。基準 "+r.baseline_commit+" / 最新 "+r.head_commit);
    }catch(e){msg("core-promotion-message",describe(e.message));}
    finally{busy=false;await refresh();await configuration();}
  }
  // Vault Settings use the existing bounded/root-pinned services. Preview
  // handles are one-use and never treated as permission to auto-activate.
  const mappingRoles=["daily","project"];
  async function vaultRefresh(){
    if(busy)return;
    const generation=++vaultGeneration;
    const rs=await Promise.allSettled([
      get("/api/v1/vault-root"),get("/api/v1/vault-bindings")
    ]);
    if(generation!==vaultGeneration)return;
    if(rs[0].status==="fulfilled"){
      const r=rs[0].value;
      msg("vault-root-active",r.active_label??"未設定");
      fieldState("vault-root-status",r.active_label?"verified":"missing",
                 r.active_label?"✓ 設定済み":"未設定");
      msg("vault-root-enrolled",r.enrolled?"確認済み":"未確認");
      fieldState("vault-enrollment-status",r.enrolled?"verified":"missing",
                 r.enrolled?"✓ 検証済み":"未検証");
      const generic=r.provider==="generic_https_path"||
        r.provider==="generic_unverified";
      msg("vault-root-provider",r.provider==="generic_https_path"?
        "HTTPSパス（ID保証なし）":r.provider==="generic_unverified"?
        "HTTPSパス（未登録）":r.provider??"不明");
      $("vault-root-enroll").textContent=generic?
        "HTTPSパスを検証・登録":"アカウントを検証";
      // Only Nextcloud supplies OC-ID/OCS-backed root browse/switch.
      for(const action of ["list","enter","preview","activate"])
        $("vault-root-"+action).disabled=generic;
      $("vault-root-candidate").disabled=generic;
      $("vault-root-rollback").disabled=generic||!r.can_rollback;
    }else{
      msg("vault-root-active","取得できません");
      fieldState("vault-root-status","failed","確認できません");
      fieldState("vault-enrollment-status","failed","確認できません");
      $("vault-root-rollback").disabled=true;
    }
    if(rs[1].status==="fulfilled"){
      const b=rs[1].value;
      msg("vault-bindings-state",b.state??"不明");
      const systemValid=b.system_fixed_under_promotion===true;
      fieldState("vault-bindings-system-status",
        systemValid?"fixed":"failed",
        systemValid?"✓ 固定":"要確認・標準Systemへ戻してください");
      $("vault-bindings-reset").disabled=b.can_reset!==true;
      $("vault-bindings-rollback").disabled=b.can_rollback!==true;
      mappingRoles.forEach(role=>{
        const input=$("vault-bindings-"+role);
        if(!bindingsDirty.has(role)&&b.roles&&typeof b.roles==="object"){
          if(document.activeElement!==input)input.value=b.roles[role]??"";
        }
        const id="vault-bindings-"+role+"-status";
        if(bindingsDirty.has(role))fieldState(id,"editing","● 未保存");
        else if(b.state==="ok")fieldState(id,"verified","✓ 保存済み");
        else if(b.state==="default")fieldState(id,"verified","✓ 標準設定");
        else fieldState(id,"failed","要確認");
      });
    }else{
      msg("vault-bindings-state","取得できません");
      $("vault-bindings-reset").disabled=true;
      $("vault-bindings-rollback").disabled=true;
      fieldState("vault-bindings-system-status","failed","確認できません");
      mappingRoles.forEach(role=>fieldState(
        "vault-bindings-"+role+"-status","failed","確認できません"));
    }
  }
  async function rootAction(kind){
    if(busy)return;
    const pick=$("vault-root-candidate").value;
    if(["enter","preview","activate"].includes(kind)&&!pick){
      msg("vault-root-message","候補フォルダを選択してください。");return;
    }
    if(kind==="activate"&&previewRootHandle!==pick){
      msg("vault-root-message","切替前に同じ候補をPreviewしてください。");return;
    }
    if(["activate","rollback"].includes(kind)&&
       !confirm("Vaultの参照先を変更します。Vault内のファイルは移動せず、各JobをOffにします。既存のCore Promotion Checkpointは再検証が必要です。続行しますか？"))return;
    busy=true;invalidatePendingReads();
    try{
      const spec={
        enroll:["/api/v1/vault-root/enrollment",{}],
        list:["/api/v1/vault-root/candidates",{}],
        enter:["/api/v1/vault-root/candidates",{parent:pick}],
        preview:["/api/v1/vault-root/previews",{candidate:pick}],
        activate:["/api/v1/vault-root/activations",{candidate:pick,confirm:true}],
        rollback:["/api/v1/vault-root/rollbacks",{confirm:true}]
      };
      const [url,payload]=spec[kind];
      const result=await post(url,payload);
      const status=result.status??"unknown";
      if(kind==="list"||kind==="enter"){
        previewRootHandle=null;
        $("vault-root-activate").disabled=true;
        const select=$("vault-root-candidate");
        select.replaceChildren(new Option("候補を選択", ""));
        for(const c of result.candidates??[]){
          if(typeof c.label==="string"&&typeof c.handle==="string")
            select.add(new Option(c.label,c.handle));
        }
        msg("vault-root-message",status==="ok"?
          "フォルダ候補を取得しました。検証する候補を選んでください。":
          "候補を取得できませんでした: "+String(result.reason??status));
      }else if(kind==="preview"){
        previewRootHandle=status==="candidate_valid"?pick:null;
        $("vault-root-activate").disabled=previewRootHandle===null;
        msg("vault-root-message",status==="candidate_valid"?
          "検証成功。明示的にRoot切替できます。":
          "Preview: "+String(result.reason??status));
      }else{
        previewRootHandle=null;
        $("vault-root-activate").disabled=true;
        msg("vault-root-message",status+" · "+String(result.reason??""));
      }
    }catch(e){msg("vault-root-message",describe(e.message));}
    finally{busy=false;await refresh();await configuration();await vaultRefresh();}
  }
  async function bindingsAction(kind,role=null){
    if(busy)return;
    if(["preview","save"].includes(kind)&&!mappingRoles.includes(role))return;
    const value=role===null?null:$("vault-bindings-"+role).value.trim();
    const saved=role===null?null:previewBindings[role];
    if(kind==="save"&&(!saved||saved.value!==value)){
      msg("vault-bindings-message","この項目をPreviewし直してください。");return;
    }
    if(["save","reset","rollback"].includes(kind)&&
       !confirm("選択した設定を保存し、必要なJobをOffにします。Vaultのノートは移動しません。続行しますか？"))return;
    busy=true;invalidatePendingReads();
    try{
      const spec={
        preview:["/api/v1/vault-bindings/previews",{roles:{[role]:value}}],
        save:["/api/v1/vault-bindings/saves",{handle:saved?.handle,confirm:true}],
        reset:["/api/v1/vault-bindings/resets",{confirm:true}],
        rollback:["/api/v1/vault-bindings/rollbacks",{confirm:true}]
      };
      const [url,payload]=spec[kind];
      const result=await post(url,payload);
      if(kind==="preview"){
        const handle=typeof result.handle==="string"?result.handle:null;
        if(handle)previewBindings[role]={handle,value};
        else delete previewBindings[role];
        $("vault-bindings-"+role+"-save").disabled=!handle;
      }else{
        // Save consumes the preview handle even if the server blocks it.
        for(const key of mappingRoles){
          delete previewBindings[key];
          $("vault-bindings-"+key+"-save").disabled=true;
        }
        if(["saved","reset","rolled_back"].includes(result.status)){
          if(role!==null)bindingsDirty.delete(role);
          if(kind!=="save")bindingsDirty.clear();
        }
      }
      msg("vault-bindings-message",String(result.status??"unknown")+
          " · "+String(result.reason??""));
    }catch(e){msg("vault-bindings-message",describe(e.message));}
    finally{busy=false;await Promise.all([refresh(),configuration(),vaultRefresh(),connectionRefresh()]);}
  }
  $("vault-root-candidate").addEventListener("change",()=>{
    previewRootHandle=null;$("vault-root-activate").disabled=true;
  });
  for(const kind of ["enroll","list","enter","preview","activate","rollback"]){
    $("vault-root-"+kind).addEventListener("click",()=>void rootAction(kind));
  }
  for(const role of mappingRoles){
    for(const kind of ["preview","save"]){
      $("vault-bindings-"+role+"-"+kind).addEventListener(
        "click",()=>void bindingsAction(kind,role));
    }
    $("vault-bindings-"+role).addEventListener("input",()=>{
      bindingsDirty.add(role);
      delete previewBindings[role];
      $("vault-bindings-"+role+"-save").disabled=true;
      fieldState("vault-bindings-"+role+"-status","editing","● 未保存");
    });
  }
  for(const kind of ["reset","rollback"]){
    $("vault-bindings-"+kind).addEventListener("click",()=>void bindingsAction(kind));
  }
  names.forEach(n=>{
    $(n+"-save").addEventListener("click",()=>void action(n,"settings"));
    $(n+"-run").addEventListener("click",()=>void action(n,"runs"));
    $(n+"-preview").addEventListener("click",()=>void schedulePreview(n));
    for(const kind of ["enabled","interval","anchor"]){
      for(const event of ["input","change"]){
        $(n+"-"+kind).addEventListener(event,()=>{
          scheduleDirty[n]=true;
          if(!scheduleTouched[n])scheduleTouched[n]=new Set();
          scheduleTouched[n].add(kind);
          fieldState(n+"-"+kind+"-state","editing","● 未保存");
          delete previewSpecs[n];
          msg(n+"-preview-result","変更後の次回3件をPreviewしてください。");
        });
      }
    }
  });
  $("refresh").addEventListener("click",()=>void Promise.all([
    refresh(),configuration(),vaultRefresh(),connectionRefresh()
  ]));
  $("core-promotion-bootstrap").addEventListener("click",()=>void bootstrap());
  for(const [group,config] of Object.entries(connectionGroups)){
    $("verify-"+group).addEventListener("click",()=>void verifyStaged(group));
    for(const field of config.fields){
      $("save-"+group+"-"+field).addEventListener(
        "click",()=>void connectionField(group,field));
      $("field-"+group+"-"+field).addEventListener("input",()=>{
        connectionErrors.delete(group+"-"+field);
        connectionDirty.add(group+"-"+field);
        fieldState("status-"+group+"-"+field,"editing","● 変更中");
      });
    }
  }
  void Promise.all([refresh(),configuration(),vaultRefresh(),connectionRefresh()]);
  setInterval(()=>{if(!busy&&!document.hidden)void refresh();},6000);
})();
