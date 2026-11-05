/* #298/#481 wireframe-root manager. Reuses existing guarded B2/B4/Formatter controls. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const M = globalThis.ObsidianDesign;
  const reader = M && M.controlReader("same-origin-http");
  const ITEMS = Object.freeze([
    {id:"b1",name:"AI Note Generation",sub:"AIノート生成・Pre-review",panel:"pipeline"},
    {id:"b2",name:"Vault Snapshot",sub:"Nextcloud → private Gitea",panel:"snapshot-controls"},
    {id:"b3",name:"Project Sync",sub:"GitHub / Project → Vault",panel:null},
    {id:"b4",name:"GitHub Daily",sub:"GitHub進捗 → Daily Note",panel:"github-daily"},
    {id:"b5",name:"ChatGPT Import",sub:"手動ZIPインポート",panel:null},
    {id:"formatter",name:"Vault Formatter",sub:"98-System · Dry-run / Formatting",panel:"vault-formatter"}
  ]);
  const ID = Object.freeze(Object.fromEntries(ITEMS.map(x => [x.id,x])));
  const JOB_ENDPOINT = Object.freeze({
    b2:["/api/v1/snapshot","vault_snapshot"],
    b4:["/api/v1/daily","github_daily_preview"],
    formatter:["/api/v1/formatter","vault_formatter"]
  });
  const SUCCESS = new Set(["saved","no_changes","published","applied"]);
  const FAILURE = new Set(["failed","publish_failed","conflict"]);
  const IN_PROGRESS = new Set(["queued","running"]);
  const JOB_LABEL = Object.freeze({
    queued:"受付済み",running:"実行中",saved:"Snapshot保存を確認",
    no_changes:"変更なし",published:"Daily Note反映を確認",
    preview_ready:"Preview生成済み（Vault未反映）",
    publish_failed:"反映失敗",failed:"失敗",conflict:"競合",
    would_change:"Dry-run：変更候補あり",applied:"Formatter適用確認",
    unconfirmed:"結果不明",indeterminate:"結果不明"
  });
  // A valid-looking status for a different Job is invalid here. In
  // particular, Daily 'saved' must never masquerade as confirmed publication.
  const JOB_STATUSES = Object.freeze({
    b2:new Set(["queued","running","saved","no_changes","failed","unconfirmed"]),
    b4:new Set(["queued","running","preview_ready","published","publish_failed","failed","unconfirmed"]),
    formatter:new Set(["queued","running","would_change","no_changes","applied","conflict","failed","indeterminate","unconfirmed"])
  });
  const VIEW = Object.freeze({home:"manager-home",detail:"manager-detail",global:"manager-global"});
  const state = {selected:null,view:"home",phase:"loading",services:[],jobs:Object.create(null),generation:0};
  // Native iOS/Android edge-swipe Back traverses History API entries. We do
  // not synthesize touch gestures, which would compete with browser/OS input.
  const HISTORY_KEY = "__obsidianStatusManager";
  const HISTORY_VERSION = 1;
  let historyDepth = 0;
  let historyReady = false;
  let mountedDetail = null;
  const legacy = $("manager-legacy-root");

  function node(tag,text,className) {
    const el=document.createElement(tag);
    if (text !== undefined) el.textContent=text;
    if (className) el.className=className;
    return el;
  }
  function validDate(value) {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "—";
    const d=new Date(value*1000);
    if (!Number.isFinite(d.getTime())) return "—";
    return new Intl.DateTimeFormat("ja-JP",{timeZone:"Asia/Tokyo",
      year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",
      second:"2-digit",hour12:false}).format(d);
  }
  function validJob(raw,id) {
    const spec=JOB_ENDPOINT[id];
    if (!spec || !raw || raw.version!==1 || raw.job!==spec[1]) return null;
    if (!raw.settings || typeof raw.settings!=="object" ||
        Array.isArray(raw.settings)) return null;
    const st=raw.settings;
    if (id==="formatter") {
      if (!["off","dry-run","auto-apply"].includes(st.mode) ||
          typeof st.configured!=="boolean") return null;
    } else {
      if (typeof st.configured!=="boolean" ||
          typeof st.enabled!=="boolean") return null;
    }
    const last=raw.last_run;
    if (last!==null && (typeof last!=="object" || Array.isArray(last) ||
        typeof last.status!=="string" || !JOB_STATUSES[id].has(last.status))) return null;
    return {settings:st,last};
  }
  function serviceFor(id) {
    return state.services.find(s=>s.id===id) || null;
  }
  function jobFor(id) { return state.jobs[id] || null; }
  function displayStatus(id) {
    const job=jobFor(id);
    if (job) {
      const last=job.last, status=last?.status;
      if (status && IN_PROGRESS.has(status)) return {kind:"running",text:"実行中"};
      if (status && FAILURE.has(status)) return {kind:"failure",text:"直近の実行で失敗"};
      if (status && SUCCESS.has(status)) {
        // A Daily preview alone is NOT proof of a write to the Vault.
        if (id==="formatter" && !["no_changes","applied"].includes(status))
          return {kind:"unknown",text:"結果未検証"};
        if (id==="b2" && last.stage!=="verify")
          return {kind:"unknown",text:"結果未検証"};
        return {kind:"success",text:id==="b4"?"前回の反映を確認":"直近の処理に成功"};
      }
      if (id==="b4" && status==="preview_ready")
        return {kind:"unknown",text:"Preview完了（未反映）"};
      if (id==="formatter" && status==="would_change")
        return {kind:"unknown",text:"Dry-run：変更候補あり"};
      if (status==="unconfirmed" || status==="indeterminate")
        return {kind:"unknown",text:"結果不明"};
      if (job.settings.configured===false) return {kind:"unknown",text:"未設定"};
      const enabled=id==="formatter" ? job.settings.mode!=="off" : job.settings.enabled;
      return {kind:"unknown",text:enabled?"待機中":"停止・無効"};
    }
    const s=serviceFor(id);
    if (!s || state.phase!=="ready") return {kind:"unknown",text:"状態未確認"};
    if (s.integration!=="connected" || s.adapterMode==="mock")
      return {kind:"unknown",text:s.integration==="pending"?"統合待ち":"未接続・未確認"};
    if (s.execution==="Running" || s.execution==="Queued")
      return {kind:"running",text:s.execution==="Running"?"実行中":"受付済み"};
    // A historical verified success is not the result of a newer unknown
    // attempt. Never color the card green unless the *latest* attempt has
    // its own verified successful effect.
    if (s.lastAttempt?.outcome==="failure") return {kind:"failure",text:"直近の実行で失敗"};
    if (s.lastAttempt?.outcome==="unknown")
      return {kind:"unknown",text:"直近の実行結果は不明"};
    if (s.lastAttempt?.outcome==="success" && s.lastAttempt.effect?.state==="verified")
      return {kind:"success",text:"直近の結果を検証済み"};
    if (s.lastVerifiedOutcome?.status==="success")
      return {kind:"unknown",text:"過去の成功記録のみ"};
    return {kind:"unknown",text:"結果未確認"};
  }
  function fact(label,value) {
    const el=node("div",undefined,"manager-fact");
    el.append(node("span",label),node("span",value));
    return el;
  }
  function renderCard(entry) {
    const status=displayStatus(entry.id);
    const card=node("article",undefined,"manager-service-card");
    const title=node("h3",entry.name);
    const subtitle=node("p",entry.sub,"manager-service-subtitle");
    const line=node("div",undefined,"manager-service-state");
    const dot=node("span",undefined,"manager-signal");
    dot.setAttribute("aria-hidden","true");
    dot.dataset.kind=status.kind;
    line.append(dot,node("span","Status · "+status.text));
    const action=node("button","Settings","manager-settings-button");
    action.type="button";
    action.setAttribute("aria-label",entry.name+" の状態と設定を開く");
    action.addEventListener("click",()=>navigate("detail",entry.id));
    card.append(title,subtitle,line,action);
    return card;
  }
  function renderHome() {
    const grid=$("manager-card-grid");
    grid.replaceChildren(...ITEMS.map(renderCard));
    grid.setAttribute("aria-busy",state.phase==="loading"?"true":"false");
    const ready=state.phase==="ready";
    const count=ITEMS.filter(x=>displayStatus(x.id).kind==="failure").length;
    $("manager-summary").textContent=
      (ready?"共通Workflowの状態を取得済み":"共通Workflowの状態は未確認")+
      " · "+ITEMS.length+"サービス · 確認済み失敗 "+count+
      "件。緑の表示は直近の検証済み結果であり、サービス全体の健全性を保証しません。";
  }
  function intervalLabel(s,job,id) {
    const interval=job?.settings?.interval_seconds ?? s?.intervalSeconds;
    return Number.isSafeInteger(interval) && interval>=60 && interval<=86400
      ? interval+" 秒" : id==="b5"?"手動（スケジュールなし）":"—";
  }
  function detailFacts(id) {
    const s=serviceFor(id),job=jobFor(id);
    const last=job?.last || null;
    const st=displayStatus(id),data=[];
    const schedule=job?(id==="formatter"
      ? job.settings.mode==="off"?"無効":job.settings.mode==="dry-run"?"Dry-run":"Auto apply"
      :job.settings.enabled?"有効":"無効")
      :s?(s.schedule==="on"?"有効":s.schedule==="off"?"無効":
        s.schedule==="not_applicable"?"手動":"未確認"):"未確認";
    const configuration=job?job.settings.configured?"設定済み":"未設定":
      s?s.configuration==="configured"?"設定済み":
        s.configuration==="missing"?"未設定":"未確認":"未確認";
    data.push(["Status",st.text],["接続・設定",configuration],
      ["定期実行",schedule]);
    const execution=last?.status && IN_PROGRESS.has(last.status)
      ?last.status:s?.execution||"Unknown";
    const runAt=last?.completed??last?.created??s?.lastAttempt?.completedAt??s?.lastAttemptAt??null;
    const due=job?.settings?.next_due??s?.nextRun??null;
    data.push(["現在の実行状態",execution==="running"?"実行中":
      execution==="queued"?"受付済み":
      execution==="Idle"?"待機中":execution==="Unknown"?"未確認":execution]);
    data.push(["最終実行",validDate(runAt)]);
    const knownOutcome=last?.status ? (
      id==="b2" && ["saved","no_changes"].includes(last.status) && last.stage!=="verify"
        ? "保存結果未検証" : JOB_LABEL[last.status]
    ) : s?.lastVerifiedOutcome ? (s.lastVerifiedOutcome.status==="success"
      ? "過去の検証済み成功":"過去の確認済み失敗") : "未確認";
    data.push(["最終結果",knownOutcome],
      ["次回実行",id==="b5"?"手動":validDate(due)],
      ["設定間隔",intervalLabel(s,job,id)]);
    return data;
  }
  function restorePanel() {
    if (mountedDetail) {
      legacy.append(mountedDetail);
      mountedDetail=null;
    }
    $("manager-detail-slot").replaceChildren();
  }
  function renderDetail() {
    if (!state.selected || !ID[state.selected]) return;
    const entry=ID[state.selected],st=displayStatus(entry.id);
    $("manager-detail-title").textContent=entry.name;
    $("manager-detail-type").textContent=entry.id==="formatter"?
      "VAULT FORMATTER SETTINGS":entry.id.toUpperCase()+" · SERVICE SETTINGS";
    $("manager-detail-status").textContent=st.text;
    $("manager-detail-status").dataset.kind=st.kind;
    $("manager-detail-facts").replaceChildren(
      ...detailFacts(entry.id).map(([k,v])=>fact(k,v)));
    const advice=$("manager-detail-advice");
    const slot=$("manager-detail-slot");
    if (entry.id==="b1") {
      advice.textContent="AIの状況を表示します。共通UIからの生成・承認操作はまだ接続されていません。";
    } else if (entry.id==="b2" || entry.id==="b4" || entry.id==="formatter") {
      advice.textContent="下の操作は既存のサービス専用APIを使用します。未設定・実行中・異常時の拒否条件は変更していません。";
    } else if (entry.id==="b3") {
      advice.textContent="Project Syncの実行操作はまだ共通WebUIに接続されていません。実行中・成果確認は独立したJob証拠に依存します。";
    } else {
      advice.textContent="ChatGPT Exportは手動取込です。共通WebUIからのZIP取込操作は未接続です。";
    }
    restorePanel();
    if (entry.panel) {
      const panel=$(entry.panel);
      if (panel) {
        mountedDetail=panel;
        slot.append(panel);
      }
    }
    if (!mountedDetail) {
      slot.append(node("p","このサービスの操作は現在利用できません。状態表示のみです。","manager-summary"));
    }
  }
  function validRoute(view,id) {
    return Object.hasOwn(VIEW,view) && (view!=="detail" || Object.hasOwn(ID,id));
  }
  function renderView(view,id) {
    if (!validRoute(view,id)) return;
    if (view!=="global" && !$("manager-global").hidden) {
      // Browser Back/Forward and the in-page Home button both clear
      // unsaved write-only secrets when leaving Global System Settings.
      for (const field of $("manager-global").querySelectorAll('input[type="password"]')) {
        field.value="";
      }
    }
    if (view!=="detail") restorePanel();
    state.view=view;
    state.selected=view==="detail"?id:null;
    for (const [key,target] of Object.entries(VIEW)) $(target).hidden=key!==view;
    if (view==="detail") renderDetail();
    if (view==="home") renderHome();
    $("manager-menu").hidden=true;
    $("manager-menu-toggle").setAttribute("aria-expanded","false");
    $("manager-menu-toggle").setAttribute("aria-label","メニューを開く");
    const focus=view==="home"?$("manager-home-title"):
      view==="global"?$("manager-global-title"):$("manager-detail-title");
    focus.setAttribute("tabindex","-1");
    focus.focus({preventScroll:true});
    if (typeof window.scrollTo==="function") window.scrollTo(0,0);
  }
  function readHistory(value) {
    const item=value && typeof value==="object" && value[HISTORY_KEY];
    if (!item || item.version!==HISTORY_VERSION ||
        !Number.isSafeInteger(item.depth) || item.depth<0 || item.depth>100 ||
        !validRoute(item.view,item.id)) return null;
    return item;
  }
  function historyState(view,id,depth) {
    const previous=window.history.state;
    const preserved=previous && typeof previous==="object" && !Array.isArray(previous)
      ? previous : {};
    return {...preserved,
      [HISTORY_KEY]:{version:HISTORY_VERSION,view,id:view==="detail"?id:null,depth}};
  }
  function setupHistory() {
    if (!window.history || typeof window.history.pushState!=="function" ||
        typeof window.history.replaceState!=="function") return;
    const existing=readHistory(window.history.state);
    try {
      if (!existing) window.history.replaceState(historyState("home",null,0),"");
      historyReady=true;
      historyDepth=existing?.depth ?? 0;
      window.addEventListener("popstate",event=>{
        const route=readHistory(event.state);
        historyDepth=route?.depth ?? 0;
        renderView(route?.view ?? "home",route?.id ?? null);
      });
      if (existing) renderView(existing.view,existing.id);
    } catch {
      historyReady=false;
      historyDepth=0;
    }
  }
  function backToHome() {
    if (state.view==="home") {
      renderView("home",null);
      return;
    }
    // Returning Home must not PUSH a new Home entry: otherwise the next
    // edge-swipe would reopen the Settings screen the user just left.
    if (historyReady && historyDepth>0) {
      window.history.go(-historyDepth);
      return;
    }
    renderView("home",null);
  }
  function navigate(view,id) {
    if (!validRoute(view,id)) return;
    if (view==="home") {
      backToHome();
      return;
    }
    if (state.view===view && (view!=="detail" || state.selected===id)) {
      renderView(view,id);
      return;
    }
    if (historyReady) {
      try {
        const depth=historyDepth+1;
        window.history.pushState(historyState(view,id,depth),"");
        historyDepth=depth;
      } catch {
        historyReady=false;
      }
    }
    renderView(view,id);
  }
  function moveGlobalPanels() {
    const container=$("manager-global-content");
    for (const id of ["manager-global-nextcloud","manager-global-gitea"]) {
      const panel=$(id);
      if (panel) container.append(panel);
    }
    const github=$("manager-global-github");
    for (const id of ["daily-connection-state","daily-connections-form","daily-connection-message"]) {
      const element=$(id);
      if (element) github.append(element);
    }
    // Present the settings in the same order as the initial wireframe:
    // common Nextcloud, Gitea Snapshot destination, then GitHub.
    container.append(github);
    const advanced=$("manager-advanced-slot");
    for (const id of ["services","operation-preview"]) {
      const panel=$(id);
      if (panel) advanced.append(panel);
    }
  }
  async function getJson(url) {
    const controller=new AbortController();
    const timeout=setTimeout(()=>controller.abort(),9500);
    try {
      const r=await fetch(url,{method:"GET",mode:"same-origin",
        credentials:"omit",cache:"no-store",redirect:"error",
        headers:{Accept:"application/json"},signal:controller.signal});
      if (r.status!==200) return null;
      return await r.json();
    } catch { return null; }
    finally { clearTimeout(timeout); }
  }
  async function refresh() {
    const generation=++state.generation;
    state.phase="loading"; state.services=[];
    state.jobs=Object.create(null);
    renderHome();
    if (state.selected) renderDetail();
    if (!reader) {
      state.phase="unavailable"; renderHome(); return;
    }
    const token=reader.begin();
    const [catalog,snapshot,daily,formatter]=await Promise.all([
      getJson("/api/v1/workflow-control"),getJson(JOB_ENDPOINT.b2[0]),
      getJson(JOB_ENDPOINT.b4[0]),getJson(JOB_ENDPOINT.formatter[0])
    ]);
    if (generation!==state.generation) return;
    reader.settle(token,catalog);
    const result=reader.snapshot();
    state.phase=result.phase;
    state.services=result.services;
    for (const [id,value] of [["b2",snapshot],["b4",daily],["formatter",formatter]]) {
      state.jobs[id]=validJob(value,id);
    }
    renderHome();
    if (state.selected) renderDetail();
  }
  $("manager-menu-toggle").addEventListener("click",()=>{
    const menu=$("manager-menu");
    menu.hidden=!menu.hidden;
    $("manager-menu-toggle").setAttribute("aria-expanded",String(!menu.hidden));
    $("manager-menu-toggle").setAttribute("aria-label",menu.hidden?"メニューを開く":"メニューを閉じる");
  });
  for (const button of $("manager-menu").querySelectorAll("[data-manager-route]")) {
    button.addEventListener("click",()=>navigate(button.dataset.managerRoute));
  }
  document.addEventListener("keydown",event=>{
    if (event.key==="Escape"&&!$("manager-menu").hidden) {
      $("manager-menu").hidden=true;
      $("manager-menu-toggle").setAttribute("aria-expanded","false");
      $("manager-menu-toggle").focus();
    }
  });
  $("manager-detail-back").addEventListener("click",()=>navigate("home"));
  $("manager-global-back").addEventListener("click",()=>navigate("home"));
  $("manager-refresh").addEventListener("click",refresh);
  moveGlobalPanels();
  renderHome();
  setupHistory();
  refresh();
})();
