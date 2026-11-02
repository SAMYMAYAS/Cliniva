
const assert=require('node:assert/strict');
const input={value:'',disabled:false,dispatchEvent(){}};
const zone={dataset:{physioZone:'Face - Genou - Droit'},setAttribute(k,v){this[k]=v;}};
const document={getElementById:()=>input,querySelectorAll:()=>[zone]};
const tab={key:'p1',mode:'edition'};
const window={activeDmeTabs:[tab],currentActiveDmeKey:'p1'};
            function refreshPhysiotherapyMap() {
                const selected = (document.getElementById('physio-zones')?.value || '').split(';').map(v=>v.trim());
                document.querySelectorAll('[data-physio-zone]').forEach(zone=>zone.setAttribute('aria-pressed', String(selected.includes(zone.dataset.physioZone))));
            }
            function togglePhysiotherapyZone(zone) {
                const tab = window.activeDmeTabs.find(t=>t.key===window.currentActiveDmeKey);
                const input = document.getElementById('physio-zones');
                if (!input || input.disabled || tab?.mode === 'lecture') return;
                const selected = input.value.split(';').map(v=>v.trim()).filter(Boolean);
                const value=zone.dataset.physioZone, index=selected.indexOf(value);
                if(index>=0)selected.splice(index,1);else selected.push(value);
                input.value=selected.join('; '); input.dispatchEvent(new Event('input',{bubbles:true}));
                refreshPhysiotherapyMap();
            }

togglePhysiotherapyZone(zone);assert.equal(input.value,'Face - Genou - Droit');assert.equal(zone['aria-pressed'],'true');
togglePhysiotherapyZone(zone);assert.equal(input.value,'');
tab.mode='lecture';togglePhysiotherapyZone(zone);assert.equal(input.value,'');
tab.mode='edition';input.disabled=true;togglePhysiotherapyZone(zone);assert.equal(input.value,'');
input.disabled=false;input.value='Dos - Cou';refreshPhysiotherapyMap();assert.equal(zone['aria-pressed'],'false');
console.log('OK : sélection, désélection, lecture seule, champ désactivé et changement de dossier.');
