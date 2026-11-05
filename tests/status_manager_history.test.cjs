"use strict";
// This test executes the actual status_manager.js in a controlled DOM with
// native browser-history semantics. No service requests or mutation endpoints.
const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const vm=require("node:vm");
const script=fs.readFileSync(path.resolve(__dirname,
  "../src/obsidian_automation/webui_design_v1/status_manager.js"),"utf8");
class Element{
 constructor(tag,id=""){
  this.tagName=tag;this.id=id;this.children=[];this.parentNode=null;this.events={};
  this.attributes={};this.dataset={};this.hidden=false;this.value="";this._text="";
 }
 set textContent(value){this._text=String(value);this.replaceChildren();}
 get textContent(){return this._text+this.children.map(c=>c.textContent).join(" ");}
 set innerHTML(_){throw Error("unsafe HTML");}
 append(...nodes){
  for(const child of nodes){
   if(child.parentNode){
    const siblings=child.parentNode.children;
    const n=siblings.indexOf(child);if(n!==-1)siblings.splice(n,1);
   }
   child.parentNode=this;this.children.push(child);
  }
 }
 replaceChildren(...nodes){for(const child of this.children)child.parentNode=null;this.children=[];this.append(...nodes);}
 setAttribute(name,value){this.attributes[name]=String(value);}
 addEventListener(event,fn){this.events[event]=fn;}
 fire(event){assert.ok(this.events[event],this.id+" "+event);return this.events[event]();}
 focus(){this.focused=true;}
 scrollIntoView(){}
 querySelectorAll(selector){
  if(this.id==="manager-menu"&&selector==="[data-manager-route]")return this.menuItems;
  if(this.id==="manager-global"&&selector==='input[type="password"]')return this.secretFields;
  return [];
 }
}
function browser(saved){
 const ids=[
  "manager-legacy-root","manager-home","manager-detail","manager-global",
  "manager-card-grid","manager-summary","manager-detail-title","manager-detail-type",
  "manager-detail-status","manager-detail-facts","manager-detail-advice","manager-detail-slot",
  "manager-menu","manager-menu-toggle","manager-home-title","manager-global-title",
  "manager-global-content","manager-global-nextcloud","manager-global-gitea",
  "manager-global-github","daily-connection-state","daily-connections-form",
  "daily-connection-message","manager-advanced-slot","services","operation-preview",
  "pipeline","snapshot-controls","github-daily","vault-formatter","manager-detail-back",
  "manager-global-back","manager-refresh"
 ];
 const nodes=Object.fromEntries(ids.map(id=>[id,new Element("div",id)]));
 nodes["manager-detail"].hidden=true;nodes["manager-global"].hidden=true;
 const forms=[];
 for(let i=0;i<4;i++){const input=new Element("input");forms.push(input);}
 nodes["manager-global"].secretFields=forms;
 const menuHome=new Element("button"),menuGlobal=new Element("button");
 menuHome.dataset.managerRoute="home";
 menuGlobal.dataset.managerRoute="global";
 nodes["manager-menu"].menuItems=[menuHome,menuGlobal];
 const listeners={};
 const entries=saved?.entries?structuredClone(saved.entries):[null];
 let current=saved?.current??0,scrolls=0;
 const calls={push:0,replace:0,go:[],fetch:0};
 const history={
  get state(){return entries[current];},
  replaceState(value){calls.replace++;entries[current]=structuredClone(value);},
  pushState(value){calls.push++;entries.splice(current+1);entries.push(structuredClone(value));current++;},
  go(delta){
   calls.go.push(delta);
   const next=current+delta;
   assert.ok(next>=0&&next<entries.length,"cannot leave test's in-app history");
   current=next;
   listeners.popstate?.({state:structuredClone(entries[current])});
  },
  back(){this.go(-1);},
  forward(){this.go(1);}
 };
 const window={
  history,
  addEventListener(name,fn){listeners[name]=fn;},
  scrollTo(){scrolls++;}
 };
 const context=vm.createContext({
  window,
  document:{
   getElementById:id=>nodes[id]||null,
   createElement:tag=>new Element(tag),
   addEventListener(name,fn){listeners["document_"+name]=fn;}
  },
  // Reader unavailable means the async refresh is read-only and no fetches.
  AbortController,
  Intl,Date,
  setTimeout(){throw Error("unexpected timer");},
  clearTimeout(){},
  fetch(){calls.fetch++;throw Error("unexpected fetch");},
  globalThis:{ObsidianDesign:null}
 });
 vm.runInContext(script,context);
 const screen=()=>Object.keys({home:1,detail:1,global:1})
   .find(view=>!nodes["manager-"+view].hidden);
 const select=id=>{
  const ids=["b1","b2","b3","b4","b5","formatter"];
  const index=ids.indexOf(id);assert.ok(index>=0);
  nodes["manager-card-grid"].children[index].children[3].fire("click");
 };
 return {
  nodes,forms,history,calls,window,entries,context,screen,select,menuHome,menuGlobal,
  navigateGlobal(){nodes["manager-menu-toggle"].fire("click");menuGlobal.fire("click");},
  goHome(){nodes["manager-detail-back"].fire("click");},
  save(){return {entries:structuredClone(entries),current};},
  get scrolls(){return scrolls;}
 };
}
let count=0;
function check(name,fn){fn();count++;console.log("PASS",name);}
const b=browser();
check("initial root is replaced not pushed; no external calls",()=>{
 assert.equal(b.screen(),"home");assert.equal(b.calls.replace,1);
 assert.equal(b.calls.push,0);assert.equal(b.calls.fetch,0);
 assert.equal(b.history.state.__obsidianStatusManager.view,"home");
 assert.equal(b.history.state.__obsidianStatusManager.depth,0);
});
b.select("b2");
check("Settings pushes a same-document history entry",()=>{
 assert.equal(b.screen(),"detail");assert.equal(b.nodes["manager-detail-title"].textContent,"Vault Snapshot");
 assert.equal(b.calls.push,1);assert.equal(b.history.state.__obsidianStatusManager.depth,1);
 assert.ok(b.nodes["manager-detail-slot"].children.includes(b.nodes["snapshot-controls"]));
});
b.history.back();
check("native browser Back returns overview",()=>{
 assert.equal(b.screen(),"home");
 assert.equal(b.calls.push,1);
 assert.equal(b.history.state.__obsidianStatusManager.depth,0);
});
b.history.forward();
check("native browser Forward restores Settings and original panel",()=>{
 assert.equal(b.screen(),"detail");
 assert.equal(b.nodes["manager-detail-title"].textContent,"Vault Snapshot");
 assert.ok(b.nodes["manager-detail-slot"].children.includes(b.nodes["snapshot-controls"]));
});
b.navigateGlobal();
check("Global pushed from Detail; Back restores prior Detail",()=>{
 assert.equal(b.screen(),"global");
 assert.equal(b.calls.push,2);
 assert.equal(b.history.state.__obsidianStatusManager.depth,2);
 assert.ok(b.nodes["manager-global-gitea"].parentNode===b.nodes["manager-global-content"]);
});
b.forms.forEach(x=>x.value="SYNTHETIC_UNSAVED_SECRET");
b.history.back();
check("native Back from Global clears unsaved secrets and restores Detail",()=>{
 assert.equal(b.screen(),"detail");
 assert.ok(b.forms.every(x=>x.value===""));
 assert.equal(b.calls.push,2);
});
b.history.forward();
check("native Forward to Global does not resurrect old unsaved secrets",()=>{
 assert.equal(b.screen(),"global");
 assert.ok(b.forms.every(x=>x.value===""));
});
b.nodes["manager-global-back"].fire("click");
check("explicit Home returns to root without pushing history loop",()=>{
 assert.equal(b.screen(),"home");
 assert.equal(b.calls.push,2);
 assert.deepEqual(b.calls.go.slice(-1),[-2]);
 assert.equal(b.history.state.__obsidianStatusManager.depth,0);
});
b.select("formatter");
check("new navigation after Back truncates stale forward entries",()=>{
 assert.equal(b.screen(),"detail");
 assert.equal(b.nodes["manager-detail-title"].textContent,"Vault Formatter");
 assert.equal(b.history.state.__obsidianStatusManager.depth,1);
});
b.nodes["manager-detail-back"].fire("click");
check("in-page Back never pushes a phantom Home entry",()=>{
 assert.equal(b.screen(),"home");
 assert.equal(b.history.state.__obsidianStatusManager.depth,0);
});
b.menuHome.fire("click");
check("Home menu at root does not create redundant entries",()=>{
 assert.equal(b.screen(),"home");
 assert.equal(b.history.state.__obsidianStatusManager.depth,0);
});
const saved=browser();
saved.select("b4");
const restored=browser(saved.save());
check("reload while in Settings restores exact current view",()=>{
 assert.equal(restored.screen(),"detail");
 assert.equal(restored.nodes["manager-detail-title"].textContent,"GitHub Daily");
 assert.equal(restored.calls.replace,0);
 assert.equal(restored.calls.push,0);
 restored.history.back();
 assert.equal(restored.screen(),"home");
});
check("invalid historical route returns to Home (fail closed)",()=>{
 const invalid=browser({entries:[{__obsidianStatusManager:{
  version:1,view:"detail",id:"UNKNOWN",depth:0}}],current:0});
 assert.equal(invalid.screen(),"home");
 assert.equal(invalid.calls.replace,1);
});
check("native touch listeners are not installed; browser handles edge gesture",()=>{
 assert.doesNotMatch(script,/addEventListener\(["']touch(start|move|end)/);
 assert.match(script,/"popstate"/);
});
console.log("status_manager_history:",count,"checks passed; native Back/Forward, no network");
