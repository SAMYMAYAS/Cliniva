const assert=require('node:assert/strict');
const hidden={rc_douleur:{value:'Chronique'},rc_zones:{value:'Face - Genou - Droit',disabled:false}};
const classes={toggle(){}};
const radio={value:'Chronique',dataset:{rcChoice:'rc_douleur'},parentElement:{classList:classes},addEventListener(type,fn){this.change=fn;}};
const zone={dataset:{rcZone:'Face - Genou - Droit'},setAttribute(k,v){this[k]=v},addEventListener(){}};
const tab={key:'t',mode:'edition'};const window={activeDmeTabs:[tab],currentActiveDmeKey:'t'};
const document={getElementById:id=>hidden[id],querySelectorAll:s=>s==='[data-rc-choice]'?[radio]:[zone]};

            function refreshClassicReeducation() {
                document.querySelectorAll('[data-rc-choice]').forEach(radio=>{
                    radio.checked=radio.value===(document.getElementById(radio.dataset.rcChoice)?.value || '');
                    radio.parentElement.classList.toggle('rc-selected',radio.checked);
                });
                const selected=(document.getElementById('rc_zones')?.value || '').split(';').map(value=>value.trim());
                document.querySelectorAll('[data-rc-zone]').forEach(zone=>zone.setAttribute('aria-pressed',String(selected.includes(zone.dataset.rcZone))));
            }
            document.querySelectorAll('[data-rc-choice]').forEach(radio=>radio.addEventListener('change',()=>{
                const hidden=document.getElementById(radio.dataset.rcChoice);
                if(hidden && radio.checked && !radio.disabled) {
                    hidden.value=radio.value;
                    refreshClassicReeducation();
                }
            }));
            function toggleClassicReeducationZone(zone) {
                const tab=window.activeDmeTabs.find(t=>t.key===window.currentActiveDmeKey);
                const input=document.getElementById('rc_zones');
                if(!input || input.disabled || tab?.mode==='lecture')return;
                const selected=input.value.split(';').map(value=>value.trim()).filter(Boolean);
                const value=zone.dataset.rcZone,index=selected.indexOf(value);
                if(index>=0)selected.splice(index,1);else selected.push(value);
                input.value=selected.join('; ');refreshClassicReeducation();
            }
            document.querySelectorAll('[data-rc-zone]').forEach(zone=>{
                zone.addEventListener('click',()=>toggleClassicReeducationZone(zone));
                zone.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();toggleClassicReeducationZone(zone);}});
            });
refreshClassicReeducation();assert.equal(radio.checked,true);assert.equal(zone['aria-pressed'],'true');
radio.value='Aiguë';radio.checked=true;radio.change();assert.equal(hidden.rc_douleur.value,'Aiguë');
toggleClassicReeducationZone(zone);assert.equal(hidden.rc_zones.value,'');
tab.mode='lecture';toggleClassicReeducationZone(zone);assert.equal(hidden.rc_zones.value,'');
hidden.rc_douleur.value='';refreshClassicReeducation();assert.equal(radio.checked,false);
console.log('OK : rechargement des choix, saisie radio, zones, lecture seule et nouveau dossier vide.');