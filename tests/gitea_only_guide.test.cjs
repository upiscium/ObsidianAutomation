"use strict";
// #304 R1/C: execute actual shipped inline JS functions in an isolated fake DOM.
const assert=require("node:assert/strict");
const fs=require("node:fs");
const vm=require("node:vm");
const source=fs.readFileSync(0,"utf8");
const start=source.indexOf("let snapshotSettings = null;");
const stop=source.indexOf('$("nextcloud-form").addEventListener("submit"',start);
const submitStart=source.indexOf('$("connections-form").addEventListener("submit"',stop);
const submitStop=source.indexOf("async function snapshotRefresh()",submitStart);
assert.ok(start>=0 && stop>start && submitStart>stop && submitStop>submitStart);
const code=source.slice(start,stop)+"\n"+source.slice(submitStart,submitStop);
const NC="/api/v1/nextcloud/connections";
const GITEA="/api/v1/snapshot/connections";
const valid={version:1,scope:"shared_nextcloud",configured:true,writes_vault:false};
const missing={...valid,configured:false};
let passed=0;
function check(label,body){body();passed++;process.stdout.write("PASS "+label+"\n");}
function deferred(){
 let resolve,reject;
 const promise=new Promise((r,j)=>{resolve=r;reject=j;});
 return {promise,resolve,reject};
}
function make({deferredFetch=false,answer=valid,savePending=false}={}){
 const requests=[];
 const writes=[];
 const pending=[];
 const button={disabled:true};
 const elements={
  "nextcloud-state":{textContent:""},
  "gitea-nextcloud-prerequisite":{textContent:""},
  "connection-message":{textContent:""},
  "connections-form":{
    querySelector(name){assert.equal(name,"button");return button;},
    addEventListener(name,cb){assert.equal(name,"submit");this.onSubmit=cb;},
    reset(){this.resetCount=(this.resetCount||0)+1;}
  }
 };
 const save=deferred();
 const context=vm.createContext({
  document:{getElementById(id){assert.ok(id in elements,id);return elements[id];}},
  fetch(url,opts){
   assert.equal(url,NC);
   assert.equal(opts.cache,"no-store");
   assert.equal(opts.credentials,"same-origin");
   requests.push(url);
   if(deferredFetch){
    const d=deferred();pending.push(d);return d.promise;
   }
   return Promise.resolve({ok:true,json:async()=>answer});
  },
  FormData:function(form){
   assert.equal(form,elements["connections-form"]);
   return new Map([
     ["git_url","https://gitea.invalid/org/repo.git"],
     ["git_user","synthetic"],
     ["git_token","SYNTHETIC_NOT_SECRET"],
     ["branch","main"]
   ]);
  },
  snapshotPost(path,body){
   writes.push({path,body});
   assert.equal(path,GITEA);
   return savePending ? save.promise : Promise.resolve({version:1,configured:true});
  },
  connectionRefresh:async()=>{},snapshotRefresh:async()=>{},
  dailyConnectionRefresh:async()=>{},dailyRefresh:async()=>{},
  formatterRefresh:async()=>{},workflowCatalogRefresh:async()=>{}
 });
 vm.runInContext("const $ = id => document.getElementById(id);\n"+code+
  "\nglobalThis.__oa = {nextcloudRefresh,renderGiteaPrerequisite};",context);
 return {api:context.__oa,button,elements,requests,writes,pending,save,
   submit:()=>elements["connections-form"].onSubmit({
      preventDefault(){},
      currentTarget:elements["connections-form"]
   })
 };
}
(async()=>{
 const m=make({answer:missing});
 check("initial form is disabled before status GET",()=>assert.equal(m.button.disabled,true));
 await m.submit();
 check("unsaved source rejects Gitea submit without network writes",()=>{
  assert.equal(m.writes.length,0);
  assert.match(m.elements["connection-message"].textContent,/Nextcloud/);
 });
 await m.api.nextcloudRefresh();
 check("missing shared Nextcloud blocks Gitea",()=>{
  assert.equal(m.button.disabled,true);
  assert.match(m.elements["gitea-nextcloud-prerequisite"].textContent,/先に/);
 });
 const yes=make();
 await yes.api.nextcloudRefresh();
 check("configured shared source enables Gitea-only form without credential readback",()=>{
  assert.equal(yes.button.disabled,false);
  assert.match(yes.elements["gitea-nextcloud-prerequisite"].textContent,/再入力は不要/);
  assert.match(yes.elements["gitea-nextcloud-prerequisite"].textContent,/疎通は未確認/);
 });
 await yes.submit();
 check("Gitea save sends only four destination fields via existing API",()=>{
  assert.equal(yes.writes.length,1);
  assert.deepEqual(Object.keys(yes.writes[0].body).sort(),
    ["branch","git_token","git_url","git_user"]);
  assert.equal(yes.elements["connections-form"].resetCount,1);
  assert.equal(yes.button.disabled,false);
  assert.deepEqual(yes.requests,[NC,NC]);
 });
 const slow=make({deferredFetch:true});
 const first=slow.api.nextcloudRefresh();
 assert.equal(slow.pending.length,1);
 const newest=slow.api.nextcloudRefresh();
 assert.equal(slow.pending.length,2);
 slow.pending[1].resolve({ok:true,json:async()=>valid});
 await newest;
 slow.pending[0].resolve({ok:true,json:async()=>missing});
 await first;
 check("older status response cannot disable newer confirmed source",()=>{
  assert.equal(slow.button.disabled,false);
  assert.match(slow.elements["gitea-nextcloud-prerequisite"].textContent,/再入力は不要/);
 });
 const invalid=make({answer:{...valid,writes_vault:true}});
 await invalid.api.nextcloudRefresh();
 check("malformed status fails closed, never grants Gitea save",()=>{
  assert.equal(invalid.button.disabled,true);
  assert.match(invalid.elements["gitea-nextcloud-prerequisite"].textContent,/確認できません/);
 });
 const failed=make({deferredFetch:true});
 const broken=failed.api.nextcloudRefresh();
 failed.pending[0].reject(Error("FAKE_TOKEN_MUST_NOT_ECHO"));
 await broken;
 check("failed GET leaves Gitea disabled without secret echo",()=>{
  assert.equal(failed.button.disabled,true);
  assert.doesNotMatch(failed.elements["gitea-nextcloud-prerequisite"].textContent,/FAKE_TOKEN/);
 });
 const busy=make({savePending:true});
 await busy.api.nextcloudRefresh();
 const ongoing=busy.submit();
 check("saving Gitea disables repeat submit",()=>assert.equal(busy.button.disabled,true));
 await busy.submit();
 check("double submit cannot issue a second Gitea POST",()=>assert.equal(busy.writes.length,1));
 busy.save.resolve({version:1,configured:true});
 await ongoing;
 check("successful Gitea save releases disabled state after status recheck",()=>{
  assert.equal(busy.button.disabled,false);
  assert.equal(busy.elements["connections-form"].resetCount,1);
 });
 const rejected=make({savePending:true});
 await rejected.api.nextcloudRefresh();
 const rejectedSave=rejected.submit();
 check("pending Gitea save remains disabled until rejected",()=>{
  assert.equal(rejected.button.disabled,true);
  assert.equal(rejected.writes.length,1);
 });
 rejected.save.reject(Error("SYNTHETIC_SECRET_MUST_NOT_ECHO"));
 await rejectedSave;
 check("rejected Gitea POST recovers without reset or secret echo",()=>{
  assert.equal(rejected.button.disabled,false);
  assert.equal(rejected.elements["connections-form"].resetCount,undefined);
  assert.equal(rejected.writes.length,1);
  assert.deepEqual(rejected.requests,[NC,NC]);
  assert.doesNotMatch(rejected.elements["connection-message"].textContent,/SYNTHETIC_SECRET/);
  assert.match(rejected.elements["connection-message"].textContent,/失敗/);
 });
 console.log("gitea_only_guide:",passed,"PASS (isolated inline JS, no real network or secrets)");
})().catch(e=>{console.error(e);process.exitCode=1;});
