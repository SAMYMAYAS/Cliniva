import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {screenshotAnnotations as annotations} from '../dist/screenshot-annotations.js';
import {videoAnnotations as legacy} from '../dist/video-annotations.js';
import {validateAnnotations} from '../dist/core.js';
test('les 48 captures couvrent les deux couches des 24 coupes et préservent les identités existantes',()=>{
 assert.equal(annotations.length,728);assert.equal(new Set(annotations.map(a=>a.id)).size,728);
 const sources=new Set(annotations.map(a=>a.source.file));assert.equal(sources.size,48);
 for(let slice=1;slice<=24;slice++){assert.ok(annotations.some(a=>a.slice===slice&&a.category==='Régions'));assert.ok(annotations.some(a=>a.slice===slice&&a.category!=='Régions'));}
 for(const a of legacy)assert.ok(annotations.some(b=>b.id===a.id));
 assert.deepEqual(validateAnnotations(JSON.parse(JSON.stringify({version:1,annotations}))),annotations);
});
test('les points exportés conservent le recalage mesuré et une provenance vérifiable',async()=>{
 const registrations=JSON.parse(await readFile(new URL('../evidence/screenshots-geometry.json',import.meta.url),'utf8'));
 for(const r of registrations.slice(0,24))assert.deepEqual(r.registration,[70,13]);
 for(const a of annotations){assert.equal(a.source.kind,'user-screenshot');assert.match(a.source.frameSha256,/^[a-f0-9]{64}$/);assert.ok(Math.abs(a.x*1920-a.source.pointInFrame[0]-70)<.001);assert.ok(Math.abs(a.y*1080-a.source.pointInFrame[1]-13)<.001);}
});
test('chaque coupe possède une couche vectorielle ; les contours ne sont pas ajoutés aux coupes 23 et 24',async()=>{
 for(let i=1;i<=24;i++){const svg=await readFile(new URL(`../dist/regions/slice-${String(i).padStart(2,'0')}.svg`,import.meta.url),'utf8');assert.match(svg,/viewBox="0 0 1920 1080"/);assert.ok(!svg.includes('<script'));assert.equal(svg.includes('<path'),i<=22);}
 assert.equal(annotations.filter(a=>a.label.includes('…')).length,12);
});
