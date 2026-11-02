import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {COUNT,validateAnnotations} from '../dist/core.js';
test('les 24 PNG restent lisibles à leur résolution source ; comparaison si la source existe',async()=>{for(let i=1;i<=COUNT;i++){const destination=await readFile(new URL(`../dist/assets/slice-${String(i).padStart(2,'0')}.png`,import.meta.url));assert.equal(destination.readUInt32BE(16),1920);assert.equal(destination.readUInt32BE(20),1080);let source;try{source=await readFile(`C:/Users/PROG_ONE/Pictures/Screenshots/Capture d’écran (${i+3}).png`);}catch(e){if(e.code!=='ENOENT')throw e;}if(source)assert.equal(createHash('sha256').update(source).digest('hex'),createHash('sha256').update(destination).digest('hex'));}});
const annotation={id:'a',slice:12,x:.5,y:.4,label:'Repère test',category:'Autre',note:'À valider'};
test('export/import conserve les coordonnées et notes',()=>assert.deepEqual(validateAnnotations({version:1,annotations:[annotation]}),[annotation]));
test('import refuse coordonnées, coupes et identifiants incohérents',()=>{for(const invalid of [{...annotation,slice:25},{...annotation,x:NaN},{...annotation,y:1.2},{...annotation,label:''}])assert.throws(()=>validateAnnotations({version:1,annotations:[invalid]}));assert.throws(()=>validateAnnotations({version:1,annotations:[annotation,annotation]}));});
