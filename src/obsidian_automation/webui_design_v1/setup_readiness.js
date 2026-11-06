/* #304 R0: optional read-only initial-setup checklist. No credential or Job operations. */
(function (root) {
  "use strict";
  const ITEMS = Object.freeze([
    Object.freeze({name:"Nextcloud",path:"/api/v1/nextcloud/connections",target:"manager-global-nextcloud"}),
    Object.freeze({name:"Gitea Snapshot",path:"/api/v1/snapshot/connections",target:"manager-global-gitea"}),
    Object.freeze({name:"GitHub Daily",path:"/api/v1/daily/connections",target:"manager-global-github"})
  ]);
  const record = value => value !== null && typeof value === "object" &&
    !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype;

  // Status-only. Never copy source-provided endpoint, username, token, message,
  // URL, saved secret, or arbitrary text into the UI.
  function classify(index, value) {
    if (!record(value) || value.version !== 1) return "unavailable";
    if (index === 0) {
      if (typeof value.configured !== "boolean" ||
          value.scope !== "shared_nextcloud" || value.writes_vault !== false)
        return "unavailable";
      return value.configured ? "configured" : "missing";
    }
    if (index === 1) {
      if (typeof value.configured !== "boolean" ||
          !["configured","missing"].includes(value.nextcloud) ||
          !["read_verified_at_save","missing"].includes(value.gitea) ||
          (value.configured !== (value.nextcloud === "configured" &&
                                 value.gitea === "read_verified_at_save")))
        return "unavailable";
      return value.configured ? "verified_at_save" :
        value.nextcloud === "missing" ? "requires_nextcloud" : "missing";
    }
    if (index === 2) {
      if (typeof value.configured !== "boolean" ||
          !["configured","missing"].includes(value.github) ||
          !["configured","missing"].includes(value.nextcloud_source) ||
          value.project_mirror !== "pulled_per_run" ||
          value.vault_write !== false ||
          (value.configured !== (value.github === "configured" &&
                                 value.nextcloud_source === "configured")))
        return "unavailable";
      return value.configured ? "configured" :
        value.nextcloud_source === "missing" ? "requires_nextcloud" : "missing";
    }
    return "unavailable";
  }
  const LABELS = Object.freeze({
    configured:"設定済み（現在の疎通は未検証）",
    verified_at_save:"保存時に検証済み（現在の疎通は未確認）",
    missing:"未設定",
    requires_nextcloud:"先にNextcloudの設定が必要",
    unavailable:"状態を確認できません"
  });
  function project(payloads) {
    if (!Array.isArray(payloads) || payloads.length !== ITEMS.length)
      throw new Error("invalid_setup_observation");
    return ITEMS.map((item,index)=>{
      const kind=classify(index,payloads[index]);
      return {name:item.name,target:item.target,kind,label:LABELS[kind]};
    });
  }
  root.ObsidianSetupReadiness = Object.freeze({project});
  if (typeof document === "undefined") return;

  const button=document.getElementById("manager-setup-check");
  const label=document.getElementById("manager-setup-summary");
  const list=document.getElementById("manager-setup-results");
  if (!button || !label || !list) return;

  // The guide never performs a save or submits a form. Original connection
  // forms remain authoritative, including their validation and write gates.
  const guide={
    title:document.getElementById("manager-setup-step-title"),
    hint:document.getElementById("manager-setup-step-hint"),
    status:document.getElementById("manager-setup-step-state"),
    back:document.getElementById("manager-setup-prev"),
    open:document.getElementById("manager-setup-open"),
    next:document.getElementById("manager-setup-next")
  };
  const GUIDE_HINTS=Object.freeze([
    "共有Nextcloudの接続設定。保存後は「接続設定の状態を確認」で更新してください。",
    "GiteaはVault Snapshot利用時のみ必要です。GitHub Dailyには必須ではありません。",
    "GitHub Dailyの進捗取込を利用する場合に接続します。"
  ]);
  let step=0;
  let observed=null;
  let generation=0;
  let previous=null;
  const node=(tag,text)=>{
    const element=document.createElement(tag);
    if (text!==undefined) element.textContent=text;
    return element;
  };
  function openSettings(targetName) {
    const target=document.getElementById(targetName);
    if (target && typeof target.scrollIntoView==="function")
      target.scrollIntoView({block:"start"});
    const heading=target?.querySelector("h2,h3");
    if (heading) {
      heading.setAttribute("tabindex","-1");
      heading.focus({preventScroll:true});
    }
  }
  function renderGuide({focus=false}={}) {
    if (!Object.values(guide).every(Boolean)) return;
    const item=ITEMS[step];
    const row=observed?.[step];
    guide.title.textContent="Step "+(step+1)+" / "+ITEMS.length+" · "+item.name;
    guide.hint.textContent=GUIDE_HINTS[step];
    guide.status.textContent=row
      ? row.label+"。設定変更後は確認ボタンで再取得してください。"
      : "状態未確認。確認ボタンで取得してください。";
    guide.status.dataset.kind=row?.kind??"unavailable";
    guide.back.disabled=step===0;
    guide.next.disabled=step===ITEMS.length-1;
    if (focus) guide.title.focus({preventScroll:true});
  }
  if (Object.values(guide).every(Boolean)) {
    guide.back.addEventListener("click",()=>{
      step=Math.max(0,step-1);
      renderGuide({focus:true});
    });
    guide.next.addEventListener("click",()=>{
      step=Math.min(ITEMS.length-1,step+1);
      renderGuide({focus:true});
    });
    guide.open.addEventListener("click",()=>openSettings(ITEMS[step].target));
    renderGuide();
  }
  function render(payloads) {
    const rows=project(payloads);
    observed=rows;
    renderGuide();
    list.replaceChildren();
    for (const item of rows) {
      const li=node("li");
      const status=node("span",item.name+" — "+item.label);
      status.className="manager-setup-state";
      status.dataset.kind=item.kind;
      const goto=node("button","設定を開く");
      goto.type="button";
      goto.addEventListener("click",()=>openSettings(item.target));
      li.append(status,goto);
      list.append(li);
    }
    const confirmed=rows.filter(item=>["configured","verified_at_save"].includes(item.kind)).length;
    label.textContent=confirmed+" / 3 接続設定を確認しました。接続の現在の正常性やB1〜B5の実行可否は証明しません。";
  }
  async function read(url,signal) {
    try {
      const res=await fetch(url,{
        method:"GET",mode:"same-origin",credentials:"omit",cache:"no-store",
        redirect:"error",headers:{Accept:"application/json"},signal
      });
      if (res.status!==200) return null;
      const raw=await res.json();
      return record(raw) ? raw : null;
    } catch { return null; }
  }
  button.addEventListener("click",async()=>{
    const mine=++generation;
    if (previous) previous.abort();
    const controller=new AbortController();
    previous=controller;
    label.textContent="接続設定を確認しています。";
    list.replaceChildren();
    button.disabled=true;
    // The deadline covers JSON decoding too. Abort alone cannot bound a
    // custom/stalled response.json() promise that ignores cancellation.
    let timeout;
    const deadline=new Promise(resolve=>{
      timeout=setTimeout(()=>{
        controller.abort();
        resolve([null,null,null]);
      },9500);
    });
    try {
      const observations=await Promise.race([
        Promise.all(ITEMS.map(item=>read(item.path,controller.signal))),deadline
      ]);
      if (mine===generation) render(observations);
    } catch {
      if (mine===generation) render([null,null,null]);
    } finally {
      clearTimeout(timeout);
      if (mine===generation) {
        button.disabled=false;
        previous=null;
      }
    }
  });
})(globalThis);
