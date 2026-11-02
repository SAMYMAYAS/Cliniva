import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {videoAnnotations} from '../dist/video-annotations.js';
import {validateAnnotations} from '../dist/core.js';
test('279 transcriptions, 11 coupes, provenance conservée à l’export/import',()=>{
 assert.equal(videoAnnotations.length,279);
 assert.deepEqual([...new Set(videoAnnotations.map(a=>a.slice))].sort((a,b)=>a-b),[2,3,5,8,13,14,15,18,19,20,24]);
 assert.deepEqual(validateAnnotations(JSON.parse(JSON.stringify({version:1,annotations:videoAnnotations}))),videoAnnotations);
});
test('chaque point est reporté par le recalage mesuré sans extrapolation',async()=>{
 const reg=JSON.parse(await readFile(new URL('../evidence/video/registration.json',import.meta.url),'utf8'));
 for(const a of videoAnnotations){const r=reg[a.source.frame];assert.equal(a.slice,r.slice);assert.ok(Math.abs(a.x*1920-(a.source.pointInFrame[0]+r.dx))<.001);assert.ok(Math.abs(a.y*1080-(a.source.pointInFrame[1]+r.dy))<.001);}
});
test('les champs de provenance invalides sont refusés',()=>assert.throws(()=>validateAnnotations({version:1,annotations:[{...videoAnnotations[0],source:{kind:'unverified'}}]})));
