
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
let percent=100;const calls=[];const events={};
const group={hidden:true,style:{}},label={textContent:''};const saved=new Map([['clinivaInterfaceZoomPercent','150']]);
const window={pywebview:{api:{interface_zoom:async(action,value)=>{calls.push([action,value]);if(action==='set')percent=value;if(action==='in')percent+=25;if(action==='out')percent-=25;if(action==='reset')percent=100;return {ok:true,percent};}}},addEventListener:(name,handler)=>events[name]=handler};
const document={querySelectorAll:s=>s==='[data-interface-zoom-controls]'?[group]:[label],addEventListener:(name,handler)=>events['doc:'+name]=handler};
const context={window,document,localStorage:{getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v)},console,setTimeout,clearTimeout};
vm.runInNewContext(fs.readFileSync('static/js/interface_zoom.js','utf8'),context);
(async()=>{
 await window.clinivaInterfaceZoom.request('status');assert.equal(percent,150);assert.equal(label.textContent,'150 %');assert.equal(group.hidden,false);
 await Promise.all([window.clinivaInterfaceZoom.request('in'),window.clinivaInterfaceZoom.request('out')]);assert.equal(percent,150);
 await window.clinivaInterfaceZoom.request('reset');assert.equal(label.textContent,'100 %');assert.equal(saved.get('clinivaInterfaceZoomPercent'),'100');
 const browserWindow={addEventListener(){}};let forbidden=false;
 vm.runInNewContext(fs.readFileSync('static/js/interface_zoom.js','utf8'),{window:browserWindow,document:{querySelectorAll(){throw Error('Native controls must stay hidden');},addEventListener(){}},localStorage:{getItem(){forbidden=true;}},console,setTimeout,clearTimeout});
 assert.equal(forbidden,false);
 console.log('OK : restauration, affichage, requêtes sérialisées, reset et navigateur externe inchangé.');
})().catch(e=>{console.error(e);process.exitCode=1});
