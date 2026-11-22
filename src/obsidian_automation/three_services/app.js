/* Minimal three-service controls. Acknowledgment is not verified completion. */
(() => {
  "use strict";
  const names=["snapshot","core-promotion","daily"];
  const $=id=>document.getElementById(id);
  const data={};
  let busy=false;
  let policy=null;
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
    operation_unavailable:"結果不明です。再送せず、状態を確認してください"
  };
  const msg=(id,s)=>{$(id).textContent=s;};
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
      return x.last_run.stage==="verify"?{kind:"ok",label:"保存・検証済み"}:{kind:"unknown",label:"結果未検証"};
    if(n==="daily"&&s==="published")return {kind:"ok",label:"Daily反映済み"};
    if(n==="core-promotion"&&s==="promoted")
      return x.last_run.receipt_sha256?{kind:"ok",label:"反映・検証済み"}:{kind:"unknown",label:"結果未検証"};
    if(n==="core-promotion"&&s==="up_to_date")return {kind:"ok",label:"最新・Live照合済み"};
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
    if(document.activeElement!==$(n+"-enabled"))$(n+"-enabled").checked=settings.enabled;
    if(document.activeElement!==$(n+"-interval"))$(n+"-interval").value=settings.interval_seconds;
    $(n+"-save").disabled=busy;
    $(n+"-run").disabled=busy||!settings.configured||(n==="core-promotion"&&settings.source_bound===false);
    if(n==="daily"&&x.publication_enabled===false)
      msg("daily-message","現在はPreview専用です。反映にはサーバーの --daily-publish が必要です。");
  }
  async function refresh(){
    const rs=await Promise.allSettled(names.map(n=>get("/api/v1/"+n)));
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
    try{
      const d=await get("/api/v1/core-promotion/configuration");
      policy=d.policy_sha256;
      $("core-promotion-policy").value=policy;
      $("core-promotion-bootstrap").disabled=busy||d.checkpoint_initialized||
        typeof policy!=="string"||!/^[0-9a-f]{64}$/.test(policy);
      $("core-promotion-bootstrap-section").hidden=d.checkpoint_initialized;
    }catch(e){policy=null;$("core-promotion-bootstrap").disabled=true;}
  }
  async function action(n,kind){
    if(busy)return;
    busy=true;
    try{
      if(kind==="settings"){
        const body={enabled:$(n+"-enabled").checked,interval_seconds:Number($(n+"-interval").value)};
        if(n==="snapshot"){body.source_name="nextcloud";body.destination_name="gitea";}
        await post("/api/v1/"+n+"/settings",body);
        msg(n+"-message","設定を保存しました。実行中のJobは取り消しません。");
      }else{
        const result=await post("/api/v1/"+n+"/runs",{request_id:requestId()});
        msg(n+"-message","受付 "+result.outcome+" · Run ID "+result.run_id+"。実行結果は別途照合します。");
      }
    }catch(e){msg(n+"-message",describe(e.message));}
    finally{busy=false;await refresh();await configuration();}
  }
  async function connection(url,form,message){
    if(busy)return;
    busy=true;msg(message,"接続を検証しています…");
    try{
      await post(url,Object.fromEntries(new FormData($(form))));
      msg(message,"保存しました。影響する自動実行は再確認が必要です。");
      $(form).reset();
    }catch(e){msg(message,describe(e.message));}
    finally{busy=false;await refresh();await configuration();}
  }
  async function bootstrap(){
    if(busy||!policy)return;
    const sha=$("core-promotion-baseline").value.trim();
    if(!/^[0-9a-f]{40}$/.test(sha)){
      msg("core-promotion-message","基準SHAは40文字の小文字hexで入力してください。");return;
    }
    if(!confirm("指定コミットでCheckpointを初期化します。Vaultは変更しません。続行しますか？"))return;
    busy=true;
    try{
      const r=await post("/api/v1/core-promotion/bootstrap",{core_commit:sha,policy_sha256:policy});
      msg("core-promotion-message","Checkpoint初期化済み。基準 "+r.baseline_commit+" / 最新 "+r.head_commit);
    }catch(e){msg("core-promotion-message",describe(e.message));}
    finally{busy=false;await refresh();await configuration();}
  }
  names.forEach(n=>{
    $(n+"-save").addEventListener("click",()=>void action(n,"settings"));
    $(n+"-run").addEventListener("click",()=>void action(n,"runs"));
  });
  $("refresh").addEventListener("click",()=>void refresh());
  $("core-promotion-bootstrap").addEventListener("click",()=>void bootstrap());
  [["nextcloud-form","/api/v1/nextcloud/connections","nextcloud-message"],
   ["gitea-form","/api/v1/snapshot/connections","gitea-message"],
   ["github-form","/api/v1/daily/connections","github-message"]].forEach(([f,u,m])=>{
      $(f).addEventListener("submit",e=>{e.preventDefault();void connection(u,f,m);});
  });
  void Promise.all([refresh(),configuration()]);
  setInterval(()=>{if(!busy&&!document.hidden)void refresh();},6000);
})();
