import {test} from 'node:test';
import assert from 'node:assert/strict';
import {layoutColumn} from '../dist/callout-layout.js';
test('les légendes proches restent séparées et à l’intérieur des marges',()=>{
 const items=Array.from({length:30},(_,id)=>({id,targetY:450+id%3,height:id%2?46:31}));
 const result=layoutColumn(items,42,1600);
 assert.equal(result.length,30);assert.ok(result[0].labelY>=42);
 for(let i=1;i<result.length;i++)assert.ok(result[i].labelY>=result[i-1].labelY+result[i-1].height+8-.001);
 assert.ok(result.at(-1).labelY+result.at(-1).height<=1600);
});
test('les légendes sont ordonnées par cible sans changer leurs identités',()=>{
 const result=layoutColumn([{id:'b',targetY:300,height:31},{id:'a',targetY:60,height:31}],42,500);
 assert.deepEqual(result.map(x=>x.id),['a','b']);assert.equal(result[0].targetY,60);
});
