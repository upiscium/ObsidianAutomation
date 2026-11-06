"use strict";
const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const vm=require("node:vm");
const source=fs.readFileSync(path.resolve(__dirname,
  "../src/obsidian_automation/webui_design_v1/setup_readiness.js"),"utf8");
const fixtures={
 nextcloud:{version:1,configured:true,scope:"shared_nextcloud",writes_vault:false},
 snapshot:{version:1,configured:true,nextcloud:"configured",gitea:"read_verified_at_save"},
 daily:{version:1,configured:true,github:"configured",nextcloud_source:"configured",
   project_mirror:"pulled_per_run",vault_write:false},
};
const clone=value=>JSON.parse(JSON.stringify(value));
let n=0;function test(label,fn){fn();n++;console.log("PASS",label);}
function adapter(){
 const ctx=vm.createContext({});
 vm.runInContext(source,ctx);
 return {
   project(values){
    const raw=vm.runInContext("JSON.parse("+JSON.stringify(JSON.stringify(values))+")",ctx);
    return ctx.ObsidianSetupReadiness.project(raw);
   }
 };
}
test("exact current saved settings are not described as live healthy",()=>{
 const v=adapter().project([fixtures.nextcloud,fixtures.snapshot,fixtures.daily]);
 assert.deepEqual(Array.from(v,x=>x.kind),["configured","verified_at_save","configured"]);
 assert.match(v[0].label,/現在の疎通は未検証/);
 assert.match(v[1].label,/保存時に検証済み/);
 assert.ok(v.every(x=>!Object.hasOwn(x,"allowRun")&&!Object.hasOwn(x,"credential")));
});
test("unconfigured Nextcloud yields prerequisites rather than false readiness",()=>{
 const n={...fixtures.nextcloud,configured:false};
 const g={...fixtures.snapshot,configured:false,nextcloud:"missing",gitea:"missing"};
 const d={...fixtures.daily,configured:false,github:"missing",nextcloud_source:"missing"};
 assert.deepEqual(Array.from(adapter().project([n,g,d]),x=>x.kind),
   ["missing","requires_nextcloud","requires_nextcloud"]);
});
test("independent Gitea and GitHub absence is not a global blocker",()=>{
 const g={...fixtures.snapshot,configured:false,gitea:"missing"};
 const d={...fixtures.daily,configured:false,github:"missing"};
 assert.deepEqual(Array.from(adapter().project([fixtures.nextcloud,g,d]),x=>x.kind),
   ["configured","missing","missing"]);
});
test("source free text and extra fields cannot appear in displayed summary",()=>{
 const src=[clone(fixtures.nextcloud),clone(fixtures.snapshot),clone(fixtures.daily)];
 src[0].injected="SENSITIVE_SECRET_01";
 src[1].url="SENSITIVE_SECRET_02";
 src[2].github_token="SENSITIVE_SECRET_03";
 const result=adapter().project(src);
 assert.doesNotMatch(JSON.stringify(result),/SENSITIVE|github_token|url/);
});
test("malformed claims and cross-type statuses fail closed",()=>{
 const cases=[
   [null,null,null],
   [{...fixtures.nextcloud,scope:"PRIVATE"},fixtures.snapshot,fixtures.daily],
   [fixtures.nextcloud,{...fixtures.snapshot,configured:true,gitea:"missing"},fixtures.daily],
   [fixtures.nextcloud,fixtures.snapshot,{...fixtures.daily,vault_write:true}],
   [fixtures.nextcloud,fixtures.snapshot,{...fixtures.daily,github:"__proto__"}],
 ];
 for(const x of cases)assert.ok(adapter().project(x).some(row=>row.kind==="unavailable"));
 assert.throws(()=>adapter().project([]),/invalid_setup_observation/);
});
class Element{
 constructor(tag,id){this.tagName=tag;this.id=id;this.children=[];this.handlers={};
  this._text="";this.dataset={};this.disabled=false;this.hidden=false;}
 set textContent(s){this._text=String(s);this.children=[];}
 get textContent(){return this._text+this.children.map(x=>x.textContent).join(" ");}
 set innerHTML(_){throw Error("HTML sink forbidden");}
 append(...x){this.children.push(...x);}
 replaceChildren(...x){this._text="";this.children=x;}
 addEventListener(event,cb){this.handlers[event]=cb;}
 async fire(event){return this.handlers[event]?.();}
 setAttribute(key,val){this[key]=String(val);}
 querySelector(){return new Element("h2");}
 scrollIntoView(){this.scrolled=true;}
 focus(){this.focused=true;}
}
function deferred(){
 let resolve,reject;
 const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});
 return {resolve,reject,promise};
}
async function browser(){
 const names=["manager-setup-check","manager-setup-summary","manager-setup-results",
   "manager-setup-step-title","manager-setup-step-hint","manager-setup-step-state",
   "manager-setup-prev","manager-setup-open","manager-setup-next",
   "manager-global-nextcloud","manager-global-gitea","manager-global-github"];
 const nodes=Object.fromEntries(names.map(id=>[id,new Element("div",id)]));
 const requests=[];let ticks=0;const timers=new Map();
 const context=vm.createContext({
  document:{getElementById:id=>nodes[id]||null,createElement:tag=>new Element(tag)},
  AbortController,
  fetch(url,options){const pending=deferred();requests.push({url,options,...pending});return pending.promise;},
  setTimeout(fn,ms){assert.equal(ms,9500);const id=++ticks;timers.set(id,fn);return id;},
  clearTimeout(id){timers.delete(id);},
  localStorage:{setItem(){throw Error("storage disallowed");}},
  navigator:{sendBeacon(){throw Error("network side-effect disallowed");}}
 });
 vm.runInContext(source,context);
 return {nodes,requests,context,timers};
}
(async()=>{
 const b=await browser();
 test("browser is manual-only and does not request status on load",()=>{
  assert.equal(b.requests.length,0);
  assert.equal(b.nodes["manager-setup-results"].children.length,0);
 });
 const task=b.nodes["manager-setup-check"].fire("click");
 test("click fetches only three fixed same-origin status GETs",()=>{
  assert.equal(b.requests.length,3);
  for(const request of b.requests){
   assert.equal(request.options.method,"GET");
   assert.equal(request.options.credentials,"omit");
   assert.equal(request.options.redirect,"error");
   assert.equal(request.options.cache,"no-store");
   assert.equal(request.options.body,undefined);
   assert.ok(request.url.startsWith("/api/v1/"));
  }
  assert.deepEqual(b.requests.map(x=>x.url),[
   "/api/v1/nextcloud/connections","/api/v1/snapshot/connections",
   "/api/v1/daily/connections"]);
 });
 for(const [i,data] of [fixtures.nextcloud,fixtures.snapshot,fixtures.daily].entries()){
  const parsed=vm.runInContext("JSON.parse("+JSON.stringify(JSON.stringify(data))+")",b.context);
  b.requests[i].resolve({status:200,json:async()=>parsed});
 }
 await task;
 test("status renders text-only links and exposes no credential values",()=>{
  const list=b.nodes["manager-setup-results"];
  assert.equal(list.children.length,3);
  assert.equal(list.children[0].children[1].textContent,"設定を開く");
  assert.equal(b.nodes["manager-setup-summary"].textContent.startsWith("3 / 3"),true);
  assert.doesNotMatch(list.textContent,/token|password|https:/i);
  assert.equal(b.timers.size,0);
  assert.equal(b.nodes["manager-setup-check"].disabled,false);
 });
 await b.nodes["manager-setup-results"].children[1].children[1].fire("click");
 test("Gitea detail navigation focuses the existing Settings panel",()=>{
  assert.equal(b.nodes["manager-global-gitea"].scrolled,true);
 });
 const unavailable=await browser();
 const failed=unavailable.nodes["manager-setup-check"].fire("click");
 unavailable.requests[0].resolve({status:503,json(){throw Error("PRIVATE BODY");}});
 unavailable.requests[1].reject(Error("PRIVATE PATH"));
 unavailable.requests[2].resolve({status:200,json:async()=>({version:0,configured:true})});
 await failed;
 test("unavailable sources are redacted and do not claim success",()=>{
  const s=unavailable.nodes["manager-setup-summary"].textContent;
  assert.match(s,/0 \/ 3/);
  assert.equal(unavailable.nodes["manager-setup-results"].children.length,3);
  assert.doesNotMatch(unavailable.nodes["manager-setup-results"].textContent,/PRIVATE/);
 });
 const timeoutCase=await browser();
 const pending=timeoutCase.nodes["manager-setup-check"].fire("click");
 assert.equal(timeoutCase.requests.length,3);
 const promiseThatNeverSettles=new Promise(()=>{});
 timeoutCase.requests[0].resolve({status:200,json:()=>promiseThatNeverSettles});
 timeoutCase.requests[1].resolve({status:200,json:async()=>fixtures.snapshot});
 timeoutCase.requests[2].resolve({status:200,json:async()=>fixtures.daily});
 await Promise.resolve();
 const timer=[...timeoutCase.timers.values()][0];
 assert.equal(typeof timer,"function");
 timer();
 await pending;
 test("deadline includes stalled JSON decoding and ends in unknown",()=>{
  assert.equal(timeoutCase.nodes["manager-setup-check"].disabled,false);
  assert.equal(timeoutCase.timers.size,0);
  assert.ok(timeoutCase.nodes["manager-setup-summary"].textContent.startsWith("0 / 3"));
  assert.equal(timeoutCase.nodes["manager-setup-results"].children.length,3);
 });
 const guided=await browser();
 test("guide starts Nextcloud without auto-reading credentials or status",()=>{
  assert.match(guided.nodes["manager-setup-step-title"].textContent,/Step 1 \/ 3.*Nextcloud/);
  assert.equal(guided.nodes["manager-setup-prev"].disabled,true);
  assert.equal(guided.nodes["manager-setup-next"].disabled,false);
  assert.equal(guided.requests.length,0);
 });
 await guided.nodes["manager-setup-next"].fire("click");
 test("step 2 is optional Gitea and does not block progression",()=>{
  assert.match(guided.nodes["manager-setup-step-title"].textContent,/Step 2 \/ 3.*Gitea/);
  assert.match(guided.nodes["manager-setup-step-hint"].textContent,/必須ではありません/);
  assert.equal(guided.nodes["manager-setup-next"].disabled,false);
  assert.equal(guided.nodes["manager-setup-prev"].disabled,false);
 });
 await guided.nodes["manager-setup-open"].fire("click");
 test("step form-opening only scrolls to the original Gitea form",()=>{
  assert.equal(guided.nodes["manager-global-gitea"].scrolled,true);
  assert.equal(guided.requests.length,0);
 });
 await guided.nodes["manager-setup-next"].fire("click");
 test("step 3 is GitHub, and back navigation keeps all forms intact",()=>{
  assert.match(guided.nodes["manager-setup-step-title"].textContent,/Step 3 \/ 3.*GitHub/);
  assert.equal(guided.nodes["manager-setup-next"].disabled,true);
  assert.equal(guided.nodes["manager-setup-prev"].disabled,false);
  assert.equal(guided.requests.length,0);
  assert.ok(guided.nodes["manager-global-nextcloud"]);
  assert.ok(guided.nodes["manager-global-gitea"]);
  assert.ok(guided.nodes["manager-global-github"]);
 });
 console.log("setup_readiness:",n,"PASS (pure status + controlled browser transport)");
})().catch(error=>{console.error(error);process.exitCode=1;});
