"use strict";
// Isolated, deterministic evaluation of the actual Status Manager B3 detail.
// Neither phase activation nor a Provider call is performed by this fixture.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.resolve(__dirname,
  "../src/obsidian_automation/webui_design_v1/status_manager.js"), "utf8");

class Element {
  constructor(tag, id="") {
    this.tagName=tag; this.id=id; this.children=[]; this.parentNode=null;
    this.events={}; this.dataset={}; this.hidden=false; this._text="";
  }
  get isConnected(){ return true; }
  set textContent(value){ this._text=String(value); this.replaceChildren(); }
  get textContent(){ return this._text+this.children.map(c=>c.textContent).join(" "); }
  set innerHTML(_value){ throw Error("unexpected innerHTML"); }
  append(...nodes){
    for(const child of nodes){
      if(child.parentNode){
        const i=child.parentNode.children.indexOf(child);
        if(i!==-1) child.parentNode.children.splice(i,1);
      }
      child.parentNode=this; this.children.push(child);
    }
  }
  replaceChildren(...nodes){
    for(const child of this.children) child.parentNode=null;
    this.children=[];this.append(...nodes);
  }
  setAttribute(){}
  addEventListener(event, listener){ this.events[event]=listener; }
  fire(event){ assert.ok(this.events[event]); return this.events[event](); }
  querySelectorAll(){ return []; }
  focus(){}
  scrollIntoView(){}
}

async function showB3({
  legacyEnabled, phaseDesiredEnabled, cacheStatus=200, catalogSchedule="off"
}){
  const ids=[
    "manager-legacy-root","manager-home","manager-detail","manager-global",
    "manager-card-grid","manager-summary","manager-detail-title",
    "manager-detail-type","manager-detail-status","manager-detail-facts",
    "manager-detail-advice","manager-detail-slot","manager-menu",
    "manager-menu-toggle","manager-home-title","manager-global-title",
    "manager-global-content","manager-global-nextcloud",
    "manager-global-gitea","manager-global-github","daily-connection-state",
    "daily-connections-form","daily-connection-message",
    "manager-advanced-slot","services","operation-preview","pipeline",
    "snapshot-controls","github-daily","vault-formatter",
    "manager-detail-back","manager-global-back","manager-refresh"
  ];
  const nodes=Object.fromEntries(ids.map(id=>[id,new Element("div",id)]));
  const calls=[];
  const routes={
    "/api/v1/workflow-control":{status:200,body:{}},
    "/api/v1/snapshot":{status:503,body:null},
    "/api/v1/daily":{status:503,body:null},
    "/api/v1/formatter":{status:503,body:null},
    "/api/v1/b3/observer":{
      status:200,
      body:{
        settings: {enabled:legacyEnabled},
        // Synthetic-only hint: this legacy endpoint MUST NOT be trusted as a
        // phase/effective schedule status projection, even if extended.
        unverified_phase: {enabled:phaseDesiredEnabled},
        last_run:{status:"observed",observations:9}
      }
    },
    "/api/v1/b3/projects":{
      status:cacheStatus,
      body:{schemaVersion:"b3-project-watch-list/v1",projectCount:0,projects:[]}
    }
  };
  const fetch=async (url, options)=>{
    calls.push({url,options});
    assert.ok(Object.hasOwn(routes,url),"Unexpected endpoint: "+url);
    assert.equal(options.method,"GET");
    assert.equal(options.credentials,"omit");
    assert.equal(options.mode,"same-origin");
    assert.equal(options.cache,"no-store");
    return {status:routes[url].status,json:async()=>routes[url].body};
  };
  // Synthetic control reader models a stale/partial catalog claiming B3
  // schedule OFF (or ON) while phase state is independently unknown.
  const mockReader={begin(){return 1;},settle(){},snapshot(){
    return {phase:"ready",services:[{
      id:"b3",integration:"connected",configuration:"configured",
      schedule:catalogSchedule,execution:"Unknown",lastAttempt:null,
      lastVerifiedOutcome:null,nextRun:1762596000,intervalSeconds:3600
    }]};
  }};
  vm.runInNewContext(source,{
    window:{scrollTo(){}},
    document:{
      getElementById:id=>nodes[id]??null,
      createElement:tag=>new Element(tag),
      addEventListener(){}
    },
    globalThis:{ObsidianDesign:{controlReader(){return mockReader;}}},
    AbortController,Date,Intl,fetch,
    setTimeout(){return 1;},clearTimeout(){}
  });
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.length,4,"bootstrap uses only fixed GET reads");
  nodes["manager-card-grid"].children[2].children[3].fire("click");
  await new Promise(resolve=>setImmediate(resolve));
  const section=nodes["manager-detail-slot"].children.find(c=>c.className==="manager-b3-readonly");
  assert.ok(section,"read-only B3 panel rendered");
  const facts=nodes["manager-detail-facts"].children.map(row=>row.textContent);
  return {summary:section.children[1].textContent,calls,facts};
}

async function main(){
  const cases=[
    {legacyEnabled:false,phaseDesiredEnabled:true,cacheStatus:200,catalogSchedule:"off"},
    {legacyEnabled:false,phaseDesiredEnabled:true,cacheStatus:503,catalogSchedule:"off"},
    {legacyEnabled:false,phaseDesiredEnabled:false,cacheStatus:200,catalogSchedule:"off"},
    {legacyEnabled:true,phaseDesiredEnabled:false,cacheStatus:200,catalogSchedule:"on"},
    {legacyEnabled:"off",phaseDesiredEnabled:true,cacheStatus:200,catalogSchedule:"unknown"}
  ];
  for(const scenario of cases){
    const {summary,calls,facts}=await showB3(scenario);
    const relative=typeof scenario.legacyEnabled==="boolean"
      ?(scenario.legacyEnabled?"ON":"OFF"):"未確認";
    assert.ok(summary.includes("B3旧相対間隔スケジュール: "+relative));
    assert.ok(summary.includes("時刻基準Phaseスケジュール: 未確認（別管理）"));
    assert.ok(summary.includes("Vault Writer: 未接続"));
    assert.ok(summary.includes("GitHub観測 9件"));
    assert.ok(!summary.includes("B3 Observer: OFF"),
      "Legacy OFF must never imply whole Observer OFF");
    assert.ok(!summary.includes("時刻基準Phaseスケジュール: OFF"),
      "Phase OFF is unverified by the legacy-only endpoint");
    assert.ok(facts.includes("定期実行 未確認（旧相対間隔・時刻基準Phaseは別管理）"));
    assert.ok(facts.includes("次回実行 —"),"B3 due is not globally authoritative");
    assert.ok(facts.includes("設定間隔 —"),"relative interval is not phase interval");
    assert.ok(!facts.includes("定期実行 無効"));
    assert.deepEqual(calls.map(c=>c.url),[
      "/api/v1/workflow-control","/api/v1/snapshot",
      "/api/v1/daily","/api/v1/formatter",
      "/api/v1/b3/observer","/api/v1/b3/projects"
    ]);
    console.log("PASS legacy="+relative+" phaseSynthetic="+
      scenario.phaseDesiredEnabled+" cache="+scenario.cacheStatus);
  }
  console.log("status_manager_b3_phase_status: 5 synthetic cases passed, GET-only");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
