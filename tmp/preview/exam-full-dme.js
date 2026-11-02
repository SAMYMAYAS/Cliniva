
        (function() {const dmeTabIds = ["consultation", "consommations", "ordonnance", "bilans", "compterendu", "echographie", "historique", "croissance"];const dmeResetPermissionAtRender = true;let dmeDisplayPermissions = {};let dmeVisibleTabs = []; const dmeGroupAnchors = {"observation": "dme_passage_observation", "constantes": "dme_ta", "allergies": "dme_allergies", "traitement": "dme_traitement", "ant_med": "dme_ant_med", "ant_chir": "dme_ant_chir", "habitudes": "dme_habitudes", "motif": "dme_motif", "diagnostic": "dme_diagnostic", "conduite": "dme_conduite", "specialites": "dmeSpecialtyPanel-gastro", "actes": "dmeActsTableBody", "physiotherapie": "dmeSpecialtyPanel-physiotherapie", "reeducation_classique": "dmeSpecialtyPanel-reeducation_classique"};
        function getDmeResetPermission(tab) {
            const permission = tab?.data?.dme_can_reset;
            return typeof permission === 'boolean' ? permission : dmeResetPermissionAtRender;
        }
        function applyDmeDisplayPreferences(permissions) {
            dmeDisplayPermissions = permissions || {};
            dmeVisibleTabs = dmeTabIds.filter(key => dmeDisplayPermissions['dme_display_tab_' + key] !== false);
            const hiddenSelectors = [];
            dmeTabIds.forEach(key => {
                if (!dmeVisibleTabs.includes(key)) hiddenSelectors.push('#subtab-btn-' + key, '#dme-panel-' + key);
            });
            const consultation = document.getElementById('dme-panel-consultation');
            Object.entries(dmeGroupAnchors).forEach(([key, anchorId]) => {
                let group = document.getElementById(anchorId);
                if (!['physiotherapie','reeducation_classique'].includes(key)) {
                    while (group && group.parentElement !== consultation) group = group.parentElement;
                }
                // Les champs communs sont dans une grille : masquer uniquement leur carte.
                if (['allergies','traitement','ant_med','ant_chir','habitudes','motif','diagnostic','conduite'].includes(key)) {
                    group = document.getElementById(anchorId)?.parentElement;
                }
                if (group) {
                    group.dataset.dmeDisplayGroup = key;
                    if (dmeDisplayPermissions['dme_display_group_' + key] === false) {
                        hiddenSelectors.push('[data-dme-display-group="' + key + '"]');
                    }
                }
            });
            let style = document.getElementById('dmeDisplayPreferencesStyle');
            if (!style) {
                style = document.createElement('style'); style.id = 'dmeDisplayPreferencesStyle';
                document.getElementById('dmeSubTabsBar').appendChild(style);
            }
            style.textContent = hiddenSelectors.length ? hiddenSelectors.join(',') + '{display:none!important}' : '';
            let message = document.getElementById('dmeDisplayEmptyMessage');
            if (!message) {
                message = document.createElement('p'); message.id = 'dmeDisplayEmptyMessage';
                message.className = 'p-4 text-sm text-slate-500';
                message.textContent = 'Aucun onglet du dossier médical n’est affiché pour votre profil.';
                document.getElementById('dmeSubTabsBar').after(message);
            }
            message.hidden = dmeVisibleTabs.length > 0;
        }
        applyDmeDisplayPreferences(dmeDisplayPermissions);
    
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
// Ce fichier est exécuté dans la fermeture du DME pour partager son cycle de vie.
const examKindLabels = {biologique:'biologique',radiologique:'radiologique'};
const examEscape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const examTab = () => window.activeDmeTabs?.find(tab => tab.key === window.currentActiveDmeKey);
const examState = tab => tab.examRequestState ||= {kind:'biologique',catalog:[],history:[],canManage:false,loading:false,drafts:{biologique:{panels:[],indication:'',notes:''},radiologique:{panels:[],indication:'',notes:''}}};
const examEditable = tab => Boolean(tab && tab.mode === 'execution' && !tab.data?.dme?.verrouille && !tab.resetting && examState(tab).canManage);
const examScope = tab => ({id_patient:Number(tab.patientId),id_passage:Number(tab.passageId),id_consultation:Number(tab.consultationId || tab.data?.dme?.id)});
async function examFetch(url, options) {
    const response = await fetch('/api/dme/exam-requests'+url, options);
    const result = await response.json().catch(()=>({}));
    if(!response.ok) throw new Error(result.detail || 'Opération impossible.');
    return result;
}
const examPost = (url,body) => examFetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
function captureExamDraft(tab) {
    if(!tab || window.currentActiveDmeKey!==tab.key)return;
    const state=examState(tab), draft=state.drafts[state.kind];
    draft.indication=document.getElementById('dmeExamIndication')?.value || '';
    draft.notes=document.getElementById('dmeExamNotes')?.value || '';
    document.querySelectorAll('[data-exam-pack]').forEach(pack=>{
        const panel=draft.panels[Number(pack.dataset.examPack)];if(!panel)return;
        panel.name=pack.querySelector('[data-exam-pack-name]')?.value || '';
        pack.querySelectorAll('[data-exam-item]').forEach(row=>{
            const item=panel.items[Number(row.dataset.examItem)];if(!item)return;
            item.selected=row.querySelector('[data-exam-selected]').checked;
            item.label=row.querySelector('[data-exam-label]').value;
            item.details=row.querySelector('[data-exam-details]').value;
        });
    });
}
function renderExamRequests(tab) {
    if(!tab || window.currentActiveDmeKey!==tab.key)return;
    const state=examState(tab),draft=state.drafts[state.kind],editable=examEditable(tab);
    document.querySelectorAll('[data-exam-kind]').forEach(button=>{
        const selected=button.dataset.examKind===state.kind;
        button.setAttribute('aria-pressed',String(selected));button.classList.toggle('bg-slate-100',selected);
    });
    document.getElementById('dmeExamPermission').textContent=editable?'Sélectionnez les éléments à demander. Les précisions sont personnalisables pour ce patient.':state.canManage?'Consultation en lecture seule : les demandes enregistrées restent imprimables.':'La création et le catalogue nécessitent l’autorisation « Demandes biologiques, radiologiques & PEC ». ';
    const select=document.getElementById('dmeExamCatalogSelect');
    select.innerHTML='<option value="">Choisir un bilan '+examKindLabels[state.kind]+'</option>'+state.catalog.filter(item=>item.active && item.kind===state.kind).map(item=>`<option value="${item.id}">${examEscape(item.name)}</option>`).join('');
    document.getElementById('dmeExamDraftPacks').innerHTML=draft.panels.length?draft.panels.map((panel,packIndex)=>`
      <article data-exam-pack="${packIndex}" class="rounded-2xl border border-slate-200 dark:border-white/10 p-4 space-y-3">
        <div class="flex gap-2 items-center"><input data-exam-pack-name maxlength="160" value="${examEscape(panel.name)}" aria-label="Nom du bilan demandé" class="flex-1 min-w-0 rounded-xl border border-slate-200 bg-slate-50 dark:bg-black/20 p-2 text-xs font-bold"><button type="button" data-exam-action="remove-pack" data-pack="${packIndex}" class="text-xs font-bold text-rose-600">Retirer le bilan</button></div>
        ${panel.items.map((item,itemIndex)=>`<div data-exam-item="${itemIndex}" class="grid grid-cols-1 md:grid-cols-2 gap-2"><label class="flex items-center gap-2"><input type="checkbox" data-exam-selected ${item.selected?'checked':''} aria-label="Inclure cet élément"><input data-exam-label maxlength="200" value="${examEscape(item.label)}" aria-label="Analyse ou examen" class="w-full rounded-lg border border-slate-200 bg-white dark:bg-black/20 p-2 text-xs"></label><input data-exam-details maxlength="500" value="${examEscape(item.details)}" aria-label="Précisions pour cet élément" placeholder="Précisions pour ce patient (facultatif)" class="rounded-lg border border-slate-200 bg-white dark:bg-black/20 p-2 text-xs"></div>`).join('')}
        <button type="button" data-exam-action="add-item" data-pack="${packIndex}" class="text-xs font-bold text-indigo-600">+ Ajouter un paramètre / examen</button>
      </article>`).join(''):'<p class="rounded-xl bg-slate-50 dark:bg-white/5 p-4 text-xs text-slate-500">Aucun bilan ajouté. Choisissez un bilan du catalogue ou créez un bilan libre.</p>';
    document.getElementById('dmeExamIndication').value=draft.indication;
    document.getElementById('dmeExamNotes').value=draft.notes;
    const rows=state.history.filter(item=>item.kind===state.kind);
    document.getElementById('dmeExamHistory').innerHTML=rows.length?rows.map(item=>{
        const panels=typeof item.panels_json==='string'?JSON.parse(item.panels_json):item.panels_json;
        return `<article class="rounded-2xl border border-slate-200 dark:border-white/10 p-4"><div class="flex flex-wrap items-start justify-between gap-3"><div><b class="text-xs">Demande #${item.id} · ${examEscape(item.created)}</b><p class="text-xs text-slate-500 mt-1">${examEscape(item.prescriber)} · ${examEscape(item.status)}</p></div><div class="flex gap-2"><button type="button" data-exam-action="print" data-id="${item.id}" data-dme-allow-readonly class="rounded-lg bg-slate-900 px-3 py-2 text-xs font-bold text-white">Imprimer / PDF</button>${item.status==='Valide'&&editable?`<button type="button" data-exam-action="cancel" data-id="${item.id}" class="rounded-lg bg-rose-50 px-3 py-2 text-xs font-bold text-rose-600">Annuler</button>`:''}</div></div><div class="mt-3 space-y-2">${panels.map(panel=>`<div><b class="text-xs text-slate-700 dark:text-slate-200">${examEscape(panel.name)}</b><ul class="list-disc pl-5 text-xs text-slate-500">${panel.items.map(entry=>`<li>${examEscape(entry.label)}${entry.details?' — '+examEscape(entry.details):''}</li>`).join('')}</ul></div>`).join('')}</div></article>`;
    }).join(''):'<p class="text-xs text-slate-400">Aucune demande '+examKindLabels[state.kind]+' enregistrée dans cette consultation.</p>';
    document.querySelectorAll('#dme-panel-bilans > section input,#dme-panel-bilans > section textarea,#dme-panel-bilans > section select,#dme-panel-bilans > section button').forEach(control=>{
        const readonly=control.hasAttribute('data-dme-allow-readonly');
        control.disabled=state.saving?true:readonly?false:!editable || state.loading;
    });
}
window.loadDmeExamRequests = async function() {
    const tab=examTab();if(!tab)return;
    const state=examState(tab),kind=state.kind;
    const token=state.loadVersion=(state.loadVersion || 0)+1;
    state.loading=true;
    document.getElementById('dmeExamMessage').textContent='Chargement des bilans…';
    try {
        const scope=examScope(tab);
        const [catalog,history]=await Promise.all([examFetch('/catalog?kind='+kind),examFetch(`/scope/${scope.id_patient}/${scope.id_passage}/${scope.id_consultation}`)]);
        if(window.currentActiveDmeKey!==tab.key || state.kind!==kind || state.loadVersion!==token)return;
        state.catalog=catalog.items;state.history=history.items;state.canManage=history.can_manage;
        document.getElementById('dmeExamMessage').textContent=state.catalog.some(item=>item.active)?'':'Le catalogue est vide. Utilisez « Gérer les bilans du catalogue » pour créer vos bilans.';
    }catch(error){if(window.currentActiveDmeKey===tab.key)document.getElementById('dmeExamMessage').textContent=error.message;}
    finally{if(state.loadVersion===token){state.loading=false;renderExamRequests(tab);}}
};
function printDmeExamRequest(id,tab=examTab()) {
    if(!tab)return;
    const scope=examScope(tab),iframe=document.getElementById('prescriptionIframe'),modal=document.getElementById('prescriptionModal');
    iframe.src=`/api/dme/exam-requests/print/${scope.id_patient}/${scope.id_passage}/${scope.id_consultation}/${id}`;
    document.getElementById('dmePrescriptionModalTitle').textContent='Demande de bilan — aperçu et impression';
    modal.classList.remove('hidden');
}
function renderExamCatalog(tab) {
    const state=examState(tab);
    document.getElementById('dmeExamCatalogTitle').textContent='Catalogue des bilans '+(state.kind==='biologique'?'biologiques':'radiologiques');
    document.getElementById('dmeExamCatalogList').innerHTML=state.catalog.length?state.catalog.map(item=>`<div class="flex justify-between gap-2 rounded-xl bg-slate-50 dark:bg-white/5 p-3"><span class="text-xs font-bold">${examEscape(item.name)} ${item.active?'':'(désactivé)'}</span><div class="flex gap-3"><button type="button" data-exam-action="edit-catalog" data-id="${item.id}" class="text-xs font-bold text-indigo-600">Modifier</button><button type="button" data-exam-action="toggle-catalog" data-id="${item.id}" class="text-xs font-bold text-amber-600">${item.active?'Désactiver':'Réactiver'}</button></div></div>`).join(''):'<p class="text-xs text-slate-500">Créez votre premier bilan ci-dessous.</p>';
}
function resetExamCatalogForm() {
    document.getElementById('dmeExamCatalogForm').reset();document.getElementById('dmeExamCatalogId').value='';
    document.getElementById('dmeExamCatalogMessage').textContent='';
}
document.getElementById('dmeExamCatalogForm')?.addEventListener('submit',async event=>{
    event.preventDefault();const tab=examTab();if(!tab || !examState(tab).canManage)return;
    const state=examState(tab),kind=state.kind,button=event.submitter; if(state.catalogSaving)return;
    state.catalogSaving=true;if(button)button.disabled=true;
    try{
        const id=document.getElementById('dmeExamCatalogId').value;
        const items=document.getElementById('dmeExamCatalogItems').value.split('\n').map(line=>line.trim()).filter(Boolean).map(line=>{const [label,...details]=line.split('|');return {label:label.trim(),details:details.join('|').trim()};});
        await examPost('/catalog/save',{id:id?Number(id):null,kind,name:document.getElementById('dmeExamCatalogName').value,items});
        if(window.currentActiveDmeKey!==tab.key)return;
        await window.loadDmeExamRequests();renderExamCatalog(tab);resetExamCatalogForm();
        document.getElementById('dmeExamCatalogMessage').textContent='Bilan enregistré dans la base de données.';
    }catch(error){document.getElementById('dmeExamCatalogMessage').textContent=error.message;}
    finally{state.catalogSaving=false;if(button)button.disabled=false;}
});
document.getElementById('dme-panel-bilans')?.addEventListener('input',event=>{
    if(event.target.closest('#dmeExamCatalogModal'))return;captureExamDraft(examTab());
});
document.getElementById('dme-panel-bilans')?.addEventListener('change',event=>{
    if(event.target.closest('#dmeExamCatalogModal'))return;captureExamDraft(examTab());
});
document.getElementById('dme-panel-bilans')?.addEventListener('click',async event=>{
    const tab=examTab();if(!tab)return;
    const state=examState(tab),kindButton=event.target.closest('[data-exam-kind]'),button=event.target.closest('[data-exam-action]');
    if(kindButton){captureExamDraft(tab);state.kind=kindButton.dataset.examKind;await window.loadDmeExamRequests();return;}
    if(!button || button.disabled)return;
    const action=button.dataset.examAction,id=Number(button.dataset.id),pack=Number(button.dataset.pack);
    captureExamDraft(tab);const draft=state.drafts[state.kind];
    try{
        if(action==='close-catalog'){const modal=document.getElementById('dmeExamCatalogModal');modal.classList.add('hidden');modal.classList.remove('flex');return;}
        if(action==='print'){printDmeExamRequest(id);return;}
        if(['catalog','new-catalog','edit-catalog','toggle-catalog'].includes(action)){
            if(!state.canManage)throw new Error('Autorisation de gestion des demandes de bilans requise.');
            if(action==='catalog'){renderExamCatalog(tab);resetExamCatalogForm();const modal=document.getElementById('dmeExamCatalogModal');modal.classList.remove('hidden');modal.classList.add('flex');modal.querySelectorAll('input,textarea,button').forEach(control=>control.disabled=false);return;}
            if(action==='new-catalog'){resetExamCatalogForm();return;}
            const item=state.catalog.find(entry=>entry.id===id);if(!item)return;
            if(action==='edit-catalog'){document.getElementById('dmeExamCatalogId').value=item.id;document.getElementById('dmeExamCatalogName').value=item.name;const items=typeof item.items_json==='string'?JSON.parse(item.items_json):item.items_json;document.getElementById('dmeExamCatalogItems').value=items.map(entry=>entry.label+(entry.details?' | '+entry.details:'')).join('\n');return;}
            if(!confirm((item.active?'Désactiver':'Réactiver')+' ce bilan dans le catalogue ?'))return;
            await examPost('/catalog/'+id+'/toggle',{active:!item.active});await window.loadDmeExamRequests();renderExamCatalog(tab);return;
        }
        if(!examEditable(tab))throw new Error('La consultation doit être en cours et vous devez être autorisé à créer des demandes.');
        if(action==='add-pack'){
            const item=state.catalog.find(entry=>entry.id===Number(document.getElementById('dmeExamCatalogSelect').value));
            if(!item || !item.active)throw new Error('Choisissez un bilan du catalogue.');
            const items=typeof item.items_json==='string'?JSON.parse(item.items_json):item.items_json;
            draft.panels.push({catalog_id:item.id,name:item.name,items:items.map(entry=>({...entry,selected:true}))});
        }else if(action==='add-custom'){draft.panels.push({catalog_id:null,name:'Bilan personnalisé',items:[{label:'',details:'',selected:true}]});}
        else if(action==='remove-pack'){draft.panels.splice(pack,1);}
        else if(action==='add-item'){draft.panels[pack]?.items.push({label:'',details:'',selected:true});}
        else if(action==='cancel'){
            if(!confirm('Annuler cette demande de bilan ? Une trace sera conservée.'))return;
            await examPost('/'+id+'/cancel',examScope(tab));await window.loadDmeExamRequests();return;
        }else if(action==='save' || action==='save-print'){
            if(state.saving)return;
            const kind=state.kind;
            state.saving=true;renderExamRequests(tab);applyDmeAccessMode(tab);
            try{
                const panels=draft.panels.map(panel=>({...panel,items:panel.items.filter(item=>item.selected).map(({label,details})=>({label,details}))})).filter(panel=>panel.items.length);
                if(!panels.length)throw new Error('Ajoutez un bilan et cochez au moins un paramètre ou examen.');
                const result=await examPost('/save',{...examScope(tab),kind,panels,indication:draft.indication,notes:draft.notes});
                state.drafts[kind]={panels:[],indication:'',notes:''};
                if(window.currentActiveDmeKey===tab.key){await window.loadDmeExamRequests();if(action==='save-print')printDmeExamRequest(result.id,tab);document.getElementById('dmeExamMessage').textContent='Demande enregistrée.';}
            }finally{state.saving=false;renderExamRequests(tab);if(window.currentActiveDmeKey===tab.key)applyDmeAccessMode(tab);}
            return;
        }
        renderExamRequests(tab);
    }catch(error){document.getElementById('dmeExamMessage').textContent=error.message;}
});

            if (window._clinivaDprintKeyHandler) { document.removeEventListener('keydown', window._clinivaDprintKeyHandler); window._clinivaDprintKeyHandler = null; }
            const staleDmeModals = document.querySelectorAll('body > #livretCreationModal, body > #dmeCrPreviewModal, body > #dmeDicomPrintModal, body > #dmeSpecialtyPickerModal, body > #dprintCarouselModal, body > #dprintManagerModal, body > #dprintManagerPositionMenu, body > #dprintRowActionMenu');
            if (staleDmeModals.length) { staleDmeModals.forEach(modal => modal.remove()); document.body.style.overflow = ''; }
            function portalDmeModal(id) {
                const modal = document.getElementById(id);
                if (modal && modal.parentElement !== document.body) document.body.appendChild(modal);
                return modal;
            }
            let dmeDicomPrintLoaded = false;
            let dmeDicomPrintLoading = null;
            window.openDmeDicomPrint = async function() {
                const dmePrintTab = (window.activeDmeTabs || []).find(tab => tab.key === window.currentActiveDmeKey);
                window._clinivaDmeDicomPrintContext = dmePrintTab ? {
                    key: dmePrintTab.key,
                    patientId: dmePrintTab.patientId,
                    passageId: dmePrintTab.passageId,
                    consultationId: dmePrintTab.consultationId,
                    targetGroupId: pendingEchoUploadTargetGroupId
                } : null;
                const modal = portalDmeModal('dmeDicomPrintModal');
                const mount = document.getElementById('dmeDicomPrintMount');
                if (!modal || !mount) return;
                modal.classList.remove('hidden'); modal.classList.add('flex'); document.body.style.overflow = 'hidden';
                if (dmeDicomPrintLoaded) {
                    if (window.clinivaDprintRefresh) await window.clinivaDprintRefresh();
                    return;
                }
                if (!dmeDicomPrintLoading) {
                    dmeDicomPrintLoading = (async () => {
                        mount.innerHTML = '<div class="p-10 text-center text-sm text-slate-500"><i class="ph-bold ph-spinner animate-spin mr-2"></i>Chargement du module DICOM Print…</div>';
                        const response = await fetch('/api/modules/gestion_dicom_print');
                        if (!response.ok) {
                            const result = await response.json().catch(() => ({}));
                            throw new Error(result.detail || `Impossible de charger DICOM Print (HTTP ${response.status}).`);
                        }
                        mount.innerHTML = await response.text();
                        const dprintHeader = mount.querySelector('header');
                        const dprintActions = mount.querySelector('#dprintOpenConfig')?.parentElement || dprintHeader?.lastElementChild || dprintHeader;
                        if (dprintActions && !dprintActions.querySelector('[data-dme-dprint-return]')) {
                            const returnButton = document.createElement('button');
                            returnButton.type = 'button';
                            returnButton.dataset.dmeDprintReturn = 'true';
                            returnButton.className = 'inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-xl border border-white/25 bg-white/15 px-4 py-2.5 text-sm font-extrabold text-white shadow-sm hover:bg-white/25';
                            returnButton.innerHTML = '<i class="ph-bold ph-x-circle text-base"></i>Fermer';
                            returnButton.setAttribute('aria-label', 'Fermer DICOM Print et revenir à la consultation');
                            returnButton.addEventListener('click', window.closeDmeDicomPrint);
                            dprintActions.appendChild(returnButton);
                        }
                        mount.querySelectorAll('script').forEach(oldScript => {
                            const script = document.createElement('script');
                            Array.from(oldScript.attributes).forEach(attribute => script.setAttribute(attribute.name, attribute.value));
                            script.appendChild(document.createTextNode(oldScript.innerHTML));
                            oldScript.parentNode.replaceChild(script, oldScript);
                        });
                        dmeDicomPrintLoaded = true;
                    })().catch(error => {
                        mount.innerHTML = `<div class="m-5 rounded-2xl border border-rose-200 bg-rose-50 p-5 text-sm text-rose-800">${String(error.message || error).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]))}</div>`;
                        dmeDicomPrintLoading = null;
                    });
                }
                await dmeDicomPrintLoading;
            };
            window.closeDmeDicomPrint = function() {
                try { if (window.clinivaDprintCloseRowActions) window.clinivaDprintCloseRowActions(); } catch (_) {}
                try { if (window.clinivaDprintCloseManager) window.clinivaDprintCloseManager(); } catch (_) {}
                try { if (window.clinivaDprintCloseCarousel) window.clinivaDprintCloseCarousel(); } catch (_) {}
                try { if (window.clinivaDprintCloseConfig) window.clinivaDprintCloseConfig(); } catch (_) {}
                const modal = document.getElementById('dmeDicomPrintModal');
                if (modal) { modal.classList.add('hidden'); modal.classList.remove('flex'); }
                window._clinivaDmeDicomPrintContext = null;
                document.body.style.overflow = '';
            };
            window.clinivaDmeImportDprintGroup = async function(groupId) {
                const context = window._clinivaDmeDicomPrintContext;
                const tab = (window.activeDmeTabs || []).find(item => item.key === context?.key);
                if (!context || !tab || String(tab.patientId) !== String(context.patientId) || String(tab.passageId) !== String(context.passageId)) {
                    alert('La consultation DME cible n’est plus active. Rouvrez DICOM Print depuis la consultation souhaitée.');
                    return;
                }
                const bridge = window.pywebview?.api;
                if (!bridge?.dicom_print_group_images || !bridge?.dicom_print_group_image) {
                    alert('Le service local DICOM Print est indisponible.');
                    return;
                }
                try {
                    const images = await bridge.dicom_print_group_images(groupId);
                    if (!Array.isArray(images) || !images.length) throw new Error('Ce dossier DICOM Print ne contient aucune image.');
                    const files = [];
                    for (let index = 0; index < images.length; index++) {
                        const image = await bridge.dicom_print_group_image(groupId, index);
                        const encoded = String(image?.data_url || '').split(',', 2)[1];
                        if (!encoded) throw new Error(`Impossible de lire l’image ${index + 1}.`);
                        const binary = atob(encoded);
                        const bytes = new Uint8Array(binary.length);
                        for (let offset = 0; offset < binary.length; offset++) bytes[offset] = binary.charCodeAt(offset);
                        const baseName = String(image.name || images[index].name || `DICOM_Print_${index + 1}.png`).replace(/[\/:*?"<>|]/g, '_');
                        files.push(new File([bytes], baseName.toLowerCase().endsWith('.png') ? baseName : `${baseName}.png`, {type: 'image/png'}));
                    }
                    const input = document.getElementById('dmeEchoUploadInput');
                    if (!input) throw new Error('Le formulaire d’importation du dossier échographique est indisponible.');
                    const transfer = new DataTransfer();
                    files.forEach(file => transfer.items.add(file));
                    input.files = transfer.files;
                    window._clinivaDmePrepareDprintAppend?.();
                    window.closeDmeDicomPrint();
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                } catch (error) {
                    alert(error.message || 'Impossible d’importer les images DICOM Print dans le DME.');
                }
            };
            window.activeDmeTabs = window.activeDmeTabs || [];
            window.currentActiveDmeKey = window.currentActiveDmeKey || null;

            let currentDmeActs = [];
            let currentMatchingDmePrestations = [];
            let selectedDmePrestationTemp = null;
            let currentOrdLines = [];
            let allMedicationsList = [];

            // Données carrousel échographie
            let activeCarouselImages = [];
            let currentCarouselIndex = 0;
            let pendingDmeModeResolve = null;
            let pendingEchoUploadFiles = [];
            let pendingEchoUploadPreviewUrls = [];
            let appendNextEchoUpload = false;
            let pendingEchoUploadCanAddDprint = false;
            let pendingEchoUploadTargetGroupId = null;

            window.addDprintImagesToEchoGroup = function(groupId) {
                const currentTab = window.activeDmeTabs.find(tab => tab.key === window.currentActiveDmeKey);
                const group = currentTab ? getEchoImageGroups(currentTab.data.echographies || []).find(item => String(item.id) === String(groupId)) : null;
                if (!currentTab || !group) { alert('Le groupe d’images cible est introuvable.'); return; }
                pendingEchoUploadPreviewUrls.forEach(url => URL.revokeObjectURL(url));
                pendingEchoUploadPreviewUrls = [];
                pendingEchoUploadFiles = [];
                appendNextEchoUpload = false;
                pendingEchoUploadTargetGroupId = String(groupId);
                const categorySelect = document.getElementById('echoUploadConfirmCategory');
                const groupCategory = group.category || 'Général';
                if (![...categorySelect.options].some(option => option.value === groupCategory)) categorySelect.add(new Option(groupCategory, groupCategory));
                categorySelect.value = groupCategory;
                document.getElementById('echoUploadConfirmType').value = group.items[0]?.type_image || 'echographique';
                window.openDmeDicomPrint();
            };

            function askDmeOpenMode(isLocked) {
                const modal = document.getElementById('dmeOpenModeModal');
                const message = document.getElementById('dmeOpenModeMessage');
                const executeButton = document.getElementById('dmeOpenExecuteButton');
                if (!modal) return Promise.resolve('lecture');
                if (isLocked) {
                    if (message) message.textContent = 'Cette consultation est déjà enregistrée et verrouillée. Vous pouvez consulter les informations et ouvrir les documents en lecture seule.';
                    if (executeButton) executeButton.classList.add('hidden');
                } else {
                    if (message) message.textContent = 'Choisissez entre la consultation du dossier en lecture seule ou le démarrage de la consultation médicale.';
                    if (executeButton) executeButton.classList.remove('hidden');
                }
                modal.classList.remove('hidden');
                modal.classList.add('flex');
                return new Promise(resolve => { pendingDmeModeResolve = resolve; });
            }

            window.chooseDmeOpenMode = function(mode) {
                const modal = document.getElementById('dmeOpenModeModal');
                if (modal) { modal.classList.add('hidden'); modal.classList.remove('flex'); }
                if (pendingDmeModeResolve) {
                    pendingDmeModeResolve(mode);
                    pendingDmeModeResolve = null;
                }
            };

            // 1. OUVERTURE D'UN PATIENT DEPUIS PASSAGES
            window.openPatientDme = async function(patientId, passageId) {
                const tabKey = `${patientId}_${passageId}`;
                const loadingNotice = document.getElementById('dmeLoadingNotice');
                if (loadingNotice) loadingNotice.classList.remove('hidden');

                try {
                    if (!Number.isInteger(Number(patientId)) || Number(patientId) <= 0 || !Number.isInteger(Number(passageId)) || Number(passageId) <= 0) {
                        throw new Error(`Identifiants patient/passage invalides (${patientId}/${passageId}).`);
                    }
                    const controller = new AbortController();
                    const requestTimeout = setTimeout(() => controller.abort(), 20000);
                    let res;
                    try {
                        res = await fetch(`/api/dme/record/${Number(patientId)}/${Number(passageId)}`, { signal: controller.signal });
                    } catch (fetchError) {
                        if (fetchError.name === 'AbortError') throw new Error('Le serveur met trop de temps à charger ce dossier. Vérifiez la connexion à la base de données puis réessayez.');
                        throw fetchError;
                    } finally {
                        clearTimeout(requestTimeout);
                    }
                    if (!res.ok) {
                        const errData = await res.json().catch(() => ({}));
                        throw new Error(errData.detail || `Erreur serveur HTTP ${res.status}`);
                    }
                    const data = await res.json();
                    if (!data.patient || !data.passage) throw new Error('Le serveur a renvoyé des données patient ou passage incomplètes.');
                    const isLocked = Boolean(data.dme && data.dme.id && data.dme.verrouille);
                    const openMode = await askDmeOpenMode(isLocked);
                    if (!openMode) {
                        if (loadingNotice) loadingNotice.classList.add('hidden');
                        return;
                    }

                    if (openMode === 'execution' && !isLocked) {
                        const startResponse = await fetch('/api/dme/consultation/start', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ id_patient: patientId, id_passage: passageId })
                        });
                        const startResult = await startResponse.json();
                        if (!startResponse.ok || !startResult.success) {
                            throw new Error(startResult.detail || startResult.message || 'Impossible de démarrer cette consultation.');
                        }
                        data.dme = { ...(data.dme || {}), id: startResult.id_consultation, verrouille: false };
                        data.passage.statut = startResult.statut || 'En cours';
                        data.historique = (data.historique || []).map(item =>
                            Number(item.id_passage) === Number(passageId)
                                ? { ...item, statut: data.passage.statut }
                                : item
                        );
                        if (typeof window.loadReceptionPassagesModule === 'function') window.loadReceptionPassagesModule();
                    }

                    const existingIndex = window.activeDmeTabs.findIndex(t => t.key === tabKey);
                    const tabObj = {
                        key: tabKey,
                        patientId: patientId,
                        passageId: passageId,
                        consultationId: data.dme?.id || null,
                        title: `${data.patient.nom} ${data.patient.prenom}`,
                        data: data,
                    mode: isLocked ? 'lecture' : openMode,
                        currentSubTab: 'consultation'
                    };

                    if (existingIndex >= 0) {
                        window.activeDmeTabs[existingIndex] = tabObj;
                    } else {
                        window.activeDmeTabs.push(tabObj);
                    }

                    window.currentActiveDmeKey = tabKey;
                    renderDmeTabsBar();
                    displayActivePatientDme();
                    if (loadingNotice) loadingNotice.classList.add('hidden');
                } catch (err) {
                    if (loadingNotice) {
                        loadingNotice.classList.remove('hidden');
                        loadingNotice.innerHTML = `<i class="ph-bold ph-warning-circle mr-1 text-rose-500"></i><span class="text-rose-600 dark:text-rose-300">${escapeDmeArchiveHtml(err.message || 'Erreur de chargement.')}</span>`;
                    }
                    console.error("Erreur openPatientDme :", err);
                    window.chooseDmeOpenMode(null);
                    alert("❌ " + err.message);
                }
            };

            // 2. BARRE D'ONGLETS PATIENTS
            function renderDmeTabsBar() {
                const bar = document.getElementById('dmeTabsBar');
                if (!bar) return;
                bar.innerHTML = '';

                if (!window.activeDmeTabs || window.activeDmeTabs.length === 0) {
                    bar.innerHTML = '<span class="text-xs text-slate-400 italic">Aucun onglet ouvert.</span>';
                    return;
                }

                window.activeDmeTabs.forEach(t => {
                    const isActive = t.key === window.currentActiveDmeKey;
                    const btn = document.createElement('div');
                    btn.className = `flex items-center gap-2.5 px-4 py-2.5 rounded-2xl text-xs font-bold transition cursor-pointer flex-shrink-0 ${
                        isActive 
                            ? 'bg-brand-neon text-black shadow-md shadow-brand-neon/20' 
                            : 'bg-white dark:bg-[#121c15] text-slate-600 dark:text-slate-300 border border-slate-200 dark:border-white/10 hover:border-brand-neon'
                    }`;

                    btn.innerHTML = `
                        <i class="ph-bold ph-user text-sm"></i>
                        <span onclick="switchDmeTab('${t.key}')">${t.title} <span class="opacity-70 font-mono text-[10px]">#PASS-${String(t.passageId).padStart(4, '0')}</span></span>
                        <button type="button" onclick="closeDmeTab('${t.key}', event)" class="p-0.5 rounded-full hover:bg-black/20 text-xs">✕</button>
                    `;
                    bar.appendChild(btn);
                });
            }
            window.loadLivretPageGardeOptions = async function() {
            const select = document.getElementById('livretPageGardeSelect');
            const hint = document.getElementById('livretPageGardeHint');
            if (!select) return;
            select.replaceChildren();
            try {
                const response = await fetch('/api/dme/livret/covers');
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Impossible de charger les pages de garde.');
                const covers = data.covers || [];
                covers.forEach(cover => {
                    const option = document.createElement('option');
                    option.value = cover.filename;
                    option.textContent = cover.filename;
                    select.appendChild(option);
                });
                if (!covers.length) {
                    const option = document.createElement('option');
                    option.value = '';
                    option.textContent = 'Aucune page de garde disponible';
                    select.appendChild(option);
                    select.disabled = true;
                    const defaultCheckbox = document.getElementById('livretUseDefaultCover');
                    if (defaultCheckbox) { defaultCheckbox.checked = false; defaultCheckbox.disabled = true; }
                    if (hint) hint.textContent = 'Ajoutez une image PNG ou JPG dans uploads/model_templates/Page_De_Garde.';
                    document.getElementById('btnSubmitLivret').disabled = true;
                    previewLivretPageGarde();
                    return;
                }
                select.disabled = false;
                const defaultCheckbox = document.getElementById('livretUseDefaultCover');
                if (defaultCheckbox) {
                    defaultCheckbox.disabled = false;
                    defaultCheckbox.checked = Boolean(data.user_default);
                }
                document.getElementById('btnSubmitLivret').disabled = false;
                select.value = data.default || covers[0].filename;
                if (hint) hint.textContent = `${covers.length} page(s) de garde disponible(s).`;
                previewLivretPageGarde();
            } catch (error) {
                select.disabled = true;
                const option = document.createElement('option');
                option.value = '';
                option.textContent = 'Chargement impossible';
                select.appendChild(option);
                if (hint) hint.textContent = error.message || 'Impossible de charger les pages de garde.';
                document.getElementById('btnSubmitLivret').disabled = true;
            }
        };

        window.previewLivretPageGarde = function() {
            const select = document.getElementById('livretPageGardeSelect');
            const preview = document.getElementById('livretPageGardePreview');
            const placeholder = document.getElementById('livretPageGardePlaceholder');
            if (!select || !preview) return;
            if (!select.value) {
                preview.classList.add('hidden');
                if (placeholder) placeholder.classList.remove('hidden');
                preview.removeAttribute('src');
                return;
            }
            preview.src = '/api/dme/livret/covers/' + encodeURIComponent(select.value);
            preview.classList.remove('hidden');
            if (placeholder) placeholder.classList.add('hidden');
        };

        window.saveLivretDefaultCoverPreference = async function() {
            const checkbox = document.getElementById('livretUseDefaultCover');
            const select = document.getElementById('livretPageGardeSelect');
            const hint = document.getElementById('livretPageGardeHint');
            if (!checkbox || !checkbox.checked) return true;
            if (!select || !select.value) return false;
            try {
                const response = await fetch('/api/dme/livret/covers/default', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filename: select.value })
                });
                const data = await response.json();
                if (!response.ok || !data.success) throw new Error(data.detail || 'Enregistrement impossible.');
                if (hint) hint.textContent = `Page de garde « ${select.value} » enregistrée par défaut pour votre compte.`;
                return true;
            } catch (error) {
                checkbox.checked = false;
                if (hint) hint.textContent = error.message || 'Impossible d’enregistrer cette page de garde par défaut.';
                return false;
            }
        };

        window.openLivretModal = async function(crId) {
            const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
            if (!currentTab) return;

            const p = currentTab.data.patient || {};
            const pass = currentTab.data.passage || {};
            const cr = (currentTab.data.archived_crs || []).find(c => c.id === crId);
            const echos = currentTab.data.echographies || [];

            document.getElementById('livretCrId').value = crId;
            document.getElementById('livretNomPrenom').value = `${p.nom || ''} ${p.prenom || ''}`.trim().toUpperCase();
            document.getElementById('livretDateExamen').value = pass.date_passage || new Date().toLocaleDateString('fr-FR');
            const prestationSelect = document.getElementById('livretTypeExamen');
            const prestationIdInput = document.getElementById('livretPrestationId');
            const prestationOptions = document.getElementById('livretTypeOptions');
            prestationSelect.value = '';
            prestationIdInput.value = '';
            const normalizePrestation = value => String(value || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/\s+/g, ' ').trim();
            const currentConsultationActs = Array.isArray(currentDmeActs) ? currentDmeActs : [];
            const currentConsultationNames = new Set(currentConsultationActs.map(act => normalizePrestation(act.nom || act.designation || act.name)).filter(Boolean));
            const allowedPrestations = (currentTab.data.consultations || []).filter(item => {
                const text = normalizePrestation(`${item.designation || ''} ${item.categorie || ''} ${item.specialite || ''}`);
                return currentConsultationNames.has(normalizePrestation(item.designation)) && !['contrast', 'gadolinium', 'produit iode', 'sedat', 'anesth', 'soin', 'infirm'].some(term => text.includes(term));
            });
            prestationOptions.innerHTML = allowedPrestations.map(item => `<option value="${item.designation}" data-prestation-id="${item.id}"></option>`).join('');
            if (allowedPrestations.length === 1) {
                prestationSelect.value = allowedPrestations[0].designation;
                prestationIdInput.value = String(allowedPrestations[0].id);
            }
            await loadLivretPageGardeOptions();


            const groupSelect = document.getElementById('livretImageGroup');
            const imageGroups = getEchoImageGroups(echos);
            if (groupSelect) {
                groupSelect.innerHTML = imageGroups.length
                    ? imageGroups.map(group => `<option value="${echoManagerEscape(group.id)}">${echoManagerEscape(group.label)}</option>`).join('')
                    : '<option value="">Aucun groupe disponible</option>';
                groupSelect.value = imageGroups[0]?.id || '';
            }
            window.renderLivretImageChoices();

            const livretModal = portalDmeModal('livretCreationModal');
            livretModal.classList.remove('hidden');
            livretModal.classList.add('flex');
            document.body.style.overflow = 'hidden';
        };
        window.syncLivretPrestationSelection = function() {
            const input = document.getElementById('livretTypeExamen');
            const option = [...(document.getElementById('livretTypeOptions')?.options || [])].find(item => item.value === input?.value);
            if (option) document.getElementById('livretPrestationId').value = option.dataset.prestationId || '';
        };
        window.renderLivretImageChoices = function() {
            const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
            const grid = document.getElementById('livretEchosCheckboxGrid');
            if (!grid || !currentTab) return;
            const allImages = currentTab.data.echographies || [];
            const groupId = String(document.getElementById('livretImageGroup')?.value || '');
            const echos = allImages.filter(ec => String(echoImageGroupId(ec)) === groupId);
            if (echos.length === 0) {
                grid.innerHTML = '<span class="text-xs text-slate-400 italic col-span-full py-8 text-center">Aucune image dans ce groupe pour ce passage.</span>';
            } else {
                grid.innerHTML = echos.map(ec => `
                        <label data-echo-id="${ec.id}" class="group relative flex flex-col gap-3 p-3 rounded-2xl border border-slate-200 dark:border-white/10 bg-white dark:bg-[#121c15] hover:border-brand-neon hover:shadow-md shadow-sm transition">
                            <button type="button" onpointerdown="livretImagePointerDown(event, ${ec.id})" onclick="event.preventDefault(); event.stopPropagation()" class="absolute top-2 right-2 z-10 w-9 h-9 rounded-xl bg-slate-900/80 hover:bg-slate-900 text-white cursor-grab active:cursor-grabbing touch-none select-none shadow-md" title="Maintenir et déplacer pour choisir l’ordre" aria-label="Déplacer cette image dans le livret"><i class="ph-bold ph-dots-six-vertical"></i></button>
                            <div class="w-full h-44 sm:h-52 rounded-xl overflow-hidden bg-slate-100 dark:bg-black/50 border border-slate-200 dark:border-white/10 flex items-center justify-center">
                                <img src="/api/dme/echographies/file/${ec.id}" alt="${ec.nom_fichier || 'Cliché échographique'}" class="max-w-full max-h-full object-contain group-hover:scale-[1.03] transition duration-200">
                            </div>
                            <div class="flex items-center gap-3 min-w-0 px-1 pb-1">
                                <input type="checkbox" value="${ec.id}" checked aria-label="Inclure ce cliché dans le livret" class="livret-echo-check h-6 w-6 shrink-0 rounded-md text-brand-neon focus:ring-brand-neon cursor-pointer">
                                <div class="flex-1 min-w-0">
                                    <span class="text-sm font-bold text-slate-800 dark:text-slate-100 block truncate" title="${ec.nom_fichier}">${ec.nom_fichier || 'Image'} · ${(ec.type_image || 'echographique') === 'radiologique' ? 'Radiologique' : (ec.type_image || '') === 'planche_trois' ? 'Planche A3' : 'Échographique'}</span>
                                    <span class="text-[11px] text-slate-500 dark:text-slate-400 block mt-1 font-mono">${ec.date_heure_str || ''}</span>
                                    <span class="mt-2 inline-block px-2 py-1 rounded-lg text-[10px] font-bold bg-brand-neon/20 text-emerald-800 dark:text-brand-neon">À inclure au livret</span>
                                </div>
                            </div>
                        </label>
                    `).join('');
            }
        };
        window.closeLivretModal = function() {
            const modal = document.getElementById('livretCreationModal');
            if (modal) { modal.classList.add('hidden'); modal.classList.remove('flex'); }
            document.body.style.overflow = '';
        };

        let livretPointerDragId = null;
        window.livretImagePointerDown = function(event, imageId) {
            event.preventDefault();
            event.stopPropagation();
            livretPointerDragId = String(imageId);
            const card = document.querySelector(`#livretEchosCheckboxGrid [data-echo-id="${imageId}"]`);
            if (card) card.classList.add('ring-2', 'ring-brand-neon', 'opacity-60');
            document.addEventListener('pointermove', window.livretImagePointerMove);
            document.addEventListener('pointerup', window.livretImagePointerUp, { once: true });
            document.addEventListener('pointercancel', window.livretImagePointerUp, { once: true });
        };
        window.livretImagePointerMove = function(event) {
            if (!livretPointerDragId) return;
            event.preventDefault();
            const grid = document.getElementById('livretEchosCheckboxGrid');
            const source = [...grid.querySelectorAll('[data-echo-id]')].find(card => card.dataset.echoId === livretPointerDragId);
            const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-echo-id]');
            if (!source || !target || source === target || !grid.contains(target)) return;
            const rect = target.getBoundingClientRect();
            const sameRow = Math.abs(rect.top - source.getBoundingClientRect().top) < rect.height / 2;
            const after = sameRow ? event.clientX > rect.left + rect.width / 2 : event.clientY > rect.top + rect.height / 2;
            grid.insertBefore(source, after ? target.nextElementSibling : target);
        };
        window.livretImagePointerUp = function() {
            document.removeEventListener('pointermove', window.livretImagePointerMove);
            document.removeEventListener('pointerup', window.livretImagePointerUp);
            document.removeEventListener('pointercancel', window.livretImagePointerUp);
            document.querySelectorAll('#livretEchosCheckboxGrid [data-echo-id]').forEach(card => card.classList.remove('ring-2', 'ring-brand-neon', 'opacity-60'));
            livretPointerDragId = null;
        };

        window.toggleAllLivretEchos = function(check) {
            document.querySelectorAll('.livret-echo-check').forEach(cb => cb.checked = check);
        };

        window.handleConfirmLivret = async function(e) {
            e.preventDefault();
            const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
            if (!currentTab) return;
            if (!await window.saveLivretDefaultCoverPreference()) {
                alert('Impossible d’enregistrer la page de garde par défaut pour votre compte. Vérifiez votre session puis réessayez.');
                return;
            }

            const selectedEchoIds = [];
            document.querySelectorAll('#livretEchosCheckboxGrid [data-echo-id]').forEach(card => {
                const checkbox = card.querySelector('.livret-echo-check');
                if (checkbox?.checked) selectedEchoIds.push(parseInt(checkbox.value));
            });

            const btn = document.getElementById('btnSubmitLivret');
            const originalHtml = btn ? btn.innerHTML : '';
            if (btn) {
                btn.disabled = true;
                btn.innerHTML = '<i class="ph-bold ph-spinner animate-spin"></i> Génération du livret...';
            }

            const payload = {
                id_patient: currentTab.patientId,
                id_passage: currentTab.passageId,
                cr_id: parseInt(document.getElementById('livretCrId').value),
                nom_prenom: document.getElementById('livretNomPrenom').value,
                date_examen: document.getElementById('livretDateExamen').value,
                prestation_id: parseInt(document.getElementById('livretPrestationId').value || '0'),
                type_examen: document.getElementById('livretTypeExamen').value,
                page_de_garde: document.getElementById('livretPageGardeSelect').value,
                groupe_id: document.getElementById('livretImageGroup').value,
                selected_echo_ids: selectedEchoIds
            };

            try {
                const res = await fetch('/api/dme/livret/create', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                }).then(r => r.json());

                if (res.success && res.draft_token) {
                    closeLivretModal();
                    openLivretDraftPreview(res.draft_token, document.getElementById('livretTypeExamen').value || 'Livret Médical');
                } else {
                    alert(res.message || "Erreur lors de la création du livret.");
                }
            } catch (err) {
                alert("Erreur lors de la génération du livret.");
            } finally {
                if (btn) {
                    btn.disabled = false;
                    btn.innerHTML = originalHtml;
                }
            }
        };

            window.switchDmeTab = function(key) {
                window.currentActiveDmeKey = key;
                renderDmeTabsBar();
                displayActivePatientDme();
            };

            window.closeDmeTab = function(key, e) {
                e.stopPropagation();
                window.activeDmeTabs = window.activeDmeTabs.filter(t => t.key !== key);
                if (window.currentActiveDmeKey === key) {
                    window.currentActiveDmeKey = window.activeDmeTabs.length > 0 ? window.activeDmeTabs[window.activeDmeTabs.length - 1].key : null;
                }
                renderDmeTabsBar();
                displayActivePatientDme();
            };

            function applyDmeAccessMode(currentTab) {
                const saved = Boolean(currentTab?.data?.dme?.verrouille);
                const canEdit = currentTab?.mode === 'execution' && !saved;
                const badge = document.getElementById('dmeAccessModeBadge');
                const saveButton = document.getElementById('dmeSaveConsultationButton');

                if (badge) {
                    badge.textContent = canEdit ? 'Consultation en cours · modifiable' : (saved ? 'Consultation enregistrée · lecture seule' : 'Lecture seule');
                    badge.className = canEdit
                        ? 'px-3 py-2 rounded-xl text-[10px] font-bold bg-brand-neon/20 text-emerald-800 dark:text-brand-neon'
                        : 'px-3 py-2 rounded-xl text-[10px] font-bold bg-slate-100 text-slate-600 dark:bg-white/5 dark:text-slate-300';
                }
                if (saveButton) saveButton.classList.toggle('hidden', !canEdit);
                const resetButton = document.getElementById('dmeResetConsultationButton');
                if (resetButton) {
                    const permission = getDmeResetPermission(currentTab);
                    const consultationId = currentTab?.consultationId || currentTab?.data?.dme?.id;
                    const enabled = canEdit && permission === true && Boolean(consultationId) && !currentTab?.resetting && !currentTab?.examRequestState?.saving;
                    const reason = permission === null
                        ? 'Droits non reçus : redémarrez le serveur puis rouvrez le DME.'
                        : permission !== true
                            ? 'Autorisation « Réinitialiser une consultation DME en cours » requise dans le profil.'
                            : !canEdit
                                ? 'Disponible uniquement pour une consultation en cours, en mode exécution.'
                                : !consultationId
                                    ? 'Démarrez la consultation pour activer le reset.'
                                    : currentTab?.resetting ? 'Réinitialisation en cours…' : currentTab?.examRequestState?.saving ? 'Enregistrement de la demande en cours…' : '';
                    resetButton.classList.remove('hidden');
                    resetButton.disabled = !enabled;
                    resetButton.title = reason || 'Remettre toute la consultation en cours à zéro';
                    resetButton.setAttribute('aria-describedby', 'dmeResetConsultationHint');
                    const hint = document.getElementById('dmeResetConsultationHint');
                    if (hint) { hint.textContent = reason; hint.classList.toggle('hidden', !reason); }
                }

                const viewOnlyActions = /viewArchivedConsultation|openWordPdfPreview|openCrPreviewModal|openPrescriptionModal|startEchoCarousel|openEchoCarouselAtIndex|openDmeAdmissionDocument|openDmeDemandeExamen|previewDmeReeducationPlan/;
                document.querySelectorAll('.dme-subtab-panel').forEach(panel => {
                    panel.querySelectorAll('input, textarea, select, button').forEach(control => {
                        if (canEdit) {
                            control.disabled = false;
                            control.classList.remove('opacity-50', 'cursor-not-allowed');
                            return;
                        }
                        const inlineAction = control.getAttribute('onclick') || '';
                        const isReadAction = (control.tagName === 'BUTTON' && viewOnlyActions.test(inlineAction))
                            || control.hasAttribute('data-dme-allow-readonly');
                        control.disabled = !isReadAction;
                        control.classList.toggle('opacity-50', !isReadAction);
                        control.classList.toggle('cursor-not-allowed', !isReadAction);
                    });
                });

                const uploadLabel = document.querySelector('label[for="dmeEchoUploadInput"]');
                if (uploadLabel) uploadLabel.classList.toggle('hidden', !canEdit);
            }

            let dmeAdmissionViewerItems = [];
            let dmeAdmissionViewerIndex = 0;
            let dmeAdmissionViewerZoom = 1;
            window.openDmeAdmissionViewer = function(items, index) {
                dmeAdmissionViewerItems = items || [];
                if (!dmeAdmissionViewerItems.length) return;
                dmeAdmissionViewerIndex = Math.max(0, Math.min(Number(index) || 0, dmeAdmissionViewerItems.length - 1));
                dmeAdmissionViewerZoom = 1;
                renderDmeAdmissionViewer();
                const modal = document.getElementById('dmeAdmissionViewerModal');
                modal.classList.remove('hidden'); modal.classList.add('flex');
            };
            window.renderDmeAdmissionViewer = function() {
                const item = dmeAdmissionViewerItems[dmeAdmissionViewerIndex];
                if (!item) return;
                document.getElementById('dmeAdmissionViewerTitle').textContent = item.title || 'Document patient';
                document.getElementById('dmeAdmissionViewerCounter').textContent = `${dmeAdmissionViewerIndex + 1} / ${dmeAdmissionViewerItems.length}`;
                document.getElementById('dmeAdmissionZoomLabel').textContent = `${Math.round(dmeAdmissionViewerZoom * 100)}%`;
                const frame = document.getElementById('dmeAdmissionViewerFrame');
                frame.src = item.url;
                frame.style.zoom = String(dmeAdmissionViewerZoom);
                frame.style.width = `${100 / dmeAdmissionViewerZoom}%`;
                frame.style.height = `${100 / dmeAdmissionViewerZoom}%`;
            };
            window.navigateDmeAdmissionViewer = function(delta) {
                const next = dmeAdmissionViewerIndex + Number(delta);
                if (next < 0 || next >= dmeAdmissionViewerItems.length) return;
                dmeAdmissionViewerIndex = next; dmeAdmissionViewerZoom = 1; renderDmeAdmissionViewer();
            };
            window.changeDmeAdmissionZoom = function(delta) {
                dmeAdmissionViewerZoom = Math.max(0.5, Math.min(2.5, dmeAdmissionViewerZoom + Number(delta)));
                renderDmeAdmissionViewer();
            };
            window.resetDmeAdmissionZoom = function() { dmeAdmissionViewerZoom = 1; renderDmeAdmissionViewer(); };
            window.closeDmeAdmissionViewer = function() {
                const modal = document.getElementById('dmeAdmissionViewerModal');
                modal.classList.add('hidden'); modal.classList.remove('flex');
                document.getElementById('dmeAdmissionViewerFrame').src = 'about:blank';
                dmeAdmissionViewerItems = [];
            };
            window.openDmeAdmissionDocument = function(patientId, passageId, documentIndex) {
                const items = [{title: 'Demande d’examen', url: `/api/reception/demande-examen/${Number(passageId)}`}];
                const docs = window.activeDmeTabs?.find(t => t.key === window.currentActiveDmeKey)?.data?.passage?.documents_scannes || [];
                docs.forEach((doc, index) => items.push({title: doc.type || `Document scanné ${index + 1}`, url: `/api/dme/admission-document/${Number(patientId)}/${Number(passageId)}/${index}`}));
                window.openDmeAdmissionViewer(items, Number(documentIndex) + 1);
            };
            window.openDmeDemandeExamen = function(passageId) {
                const tab = window.activeDmeTabs?.find(t => t.key === window.currentActiveDmeKey);
                const items = [{title: 'Demande d’examen', url: `/api/reception/demande-examen/${Number(passageId)}`}];
                (tab?.data?.passage?.documents_scannes || []).forEach((doc, index) => items.push({title: doc.type || `Document scanné ${index + 1}`, url: `/api/dme/admission-document/${Number(tab.patientId)}/${Number(passageId)}/${index}`}));
                window.openDmeAdmissionViewer(items, 0);
            };

            let dmeConsumptionOptions = null;
            const escapeDmeConsumption = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
            const dmeConsumptionDate = value => {
                if (!value) return '—';
                const date = new Date(value);
                return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('fr-FR');
            };
            window.loadDmeConsumptions = async function() {
                const tab = window.activeDmeTabs.find(item => item.key === window.currentActiveDmeKey);
                if (!tab) return;
                const consultationId = Number(tab.consultationId || tab.data?.dme?.id || 0);
                const rows = document.getElementById('dmeConsumptionRows');
                const form = document.getElementById('dmeConsumptionForm');
                try {
                    if (!dmeConsumptionOptions) {
                        const optionsResponse = await fetch('/api/dme/consumptions/options');
                        const optionsData = await optionsResponse.json();
                        if (!optionsResponse.ok) throw new Error(optionsData.detail || 'Chargement des salles et services impossible.');
                        dmeConsumptionOptions = optionsData;
                        const fillSelect = (id, options, firstLabel, getValue, getLabel) => {
                            const select = document.getElementById(id);
                            select.innerHTML = '<option value="">'+escapeDmeConsumption(firstLabel)+'</option>' + options.map(item => '<option value="'+escapeDmeConsumption(getValue(item))+'">'+escapeDmeConsumption(getLabel(item))+'</option>').join('');
                        };
                        fillSelect('dmeConsumptionService', optionsData.services || [], 'Choisir un service', value => value, value => value);
                        fillSelect('dmeConsumptionRoom', optionsData.rooms || [], 'Sans salle dédiée', item => item.id, item => item.designation);
                        const userName = optionsData.current_user?.name || 'Utilisateur connecté';
                        document.getElementById('dmeConsumptionUser').value = userName;
                        document.getElementById('dmeConsumptionUserBadge').textContent = 'Enregistré par · ' + userName;
                    }
                    const defaultService = tab.data?.passage?.type_passage || '';
                    if (defaultService && [...document.getElementById('dmeConsumptionService').options].some(option => option.value === defaultService)) {
                        document.getElementById('dmeConsumptionService').value = defaultService;
                    }
                    const response = consultationId
                        ? await fetch(`/api/dme/consumptions/${Number(tab.patientId)}/${Number(tab.passageId)}/${consultationId}`)
                        : null;
                    const data = response ? await response.json() : {consumptions: []};
                    if (response && !response.ok) throw new Error(data.detail || 'Chargement des consommations impossible.');
                    const items = data.consumptions || [];
                    rows.innerHTML = items.map(item => '<tr><td class="p-2.5">'+escapeDmeConsumption(dmeConsumptionDate(item.consomme_le))+'</td><td class="p-2.5"><strong>'+escapeDmeConsumption(item.nom_produit)+'</strong><div class="text-[10px] text-slate-400">'+escapeDmeConsumption(item.type_produit)+'</div></td><td class="p-2.5 font-bold">'+escapeDmeConsumption(item.quantite)+' '+escapeDmeConsumption(item.unite)+'</td><td class="p-2.5">'+escapeDmeConsumption(item.service)+'</td><td class="p-2.5">'+escapeDmeConsumption(item.salle || '—')+'</td><td class="p-2.5">'+escapeDmeConsumption(item.utilisateur)+'</td><td class="p-2.5">'+escapeDmeConsumption(item.lot || '—')+'</td></tr>').join('') || '<tr><td colspan="7" class="p-5 text-center text-slate-400">Aucune consommation enregistrée pour cette consultation.</td></tr>';
                    if (form) form.onsubmit = async event => {
                        event.preventDefault();
                        const activeTab = window.activeDmeTabs.find(item => item.key === window.currentActiveDmeKey);
                        if (!activeTab) return;
                        const saveButton = document.getElementById('dmeConsumptionSave');
                        const message = document.getElementById('dmeConsumptionMessage');
                        saveButton.disabled = true;
                        try {
                            const payload = {
                                id_patient: activeTab.patientId, id_passage: activeTab.passageId,
                                id_consultation: activeTab.consultationId || activeTab.data?.dme?.id,
                                type_produit: document.getElementById('dmeConsumptionType').value,
                                nom_produit: document.getElementById('dmeConsumptionProduct').value.trim(),
                                quantite: document.getElementById('dmeConsumptionQty').value,
                                unite: document.getElementById('dmeConsumptionUnit').value.trim(),
                                service: document.getElementById('dmeConsumptionService').value,
                                salle_id: document.getElementById('dmeConsumptionRoom').value || null,
                                lot: document.getElementById('dmeConsumptionLot').value,
                                notes: document.getElementById('dmeConsumptionNotes').value
                            };
                            const saveResponse = await fetch('/api/dme/consumptions/save', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
                            const saveData = await saveResponse.json();
                            if (!saveResponse.ok || !saveData.success) throw new Error(saveData.detail || 'Enregistrement impossible.');
                            form.reset();
                            document.getElementById('dmeConsumptionUser').value = dmeConsumptionOptions?.current_user?.name || '';
                            if (defaultService && [...document.getElementById('dmeConsumptionService').options].some(option => option.value === defaultService)) document.getElementById('dmeConsumptionService').value = defaultService;
                            message.textContent = 'Consommation enregistrée.';
                            message.className = 'mt-3 rounded-xl bg-emerald-50 p-3 text-xs font-semibold text-emerald-700';
                            await window.loadDmeConsumptions();
                        } catch (error) {
                            message.textContent = error.message || 'Enregistrement impossible.';
                            message.className = 'mt-3 rounded-xl bg-rose-50 p-3 text-xs font-semibold text-rose-700';
                        } finally { saveButton.disabled = false; }
                    };
                } catch (error) {
                    rows.innerHTML = '<tr><td colspan="7" class="p-5 text-center text-rose-500">'+escapeDmeConsumption(error.message)+'</td></tr>';
                }
            };
            // 3. NAVIGATION ENTRE LES SOUS-ONGLETS
            window.switchDmeSubTab = function(subTabId) {
                const permittedTabs = dmeVisibleTabs.filter(id => id !== 'croissance' || refreshDmeGrowthAvailability(window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey)));
                if (!permittedTabs.includes(subTabId)) subTabId = permittedTabs[0] || null;
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (subTabId === 'croissance' && currentTab && !refreshDmeGrowthAvailability(currentTab)) {
                    window.switchDmeSubTab('consultation');
                    return;
                }
                if (currentTab) currentTab.currentSubTab = subTabId;

                document.querySelectorAll('.dme-subtab-panel').forEach(p => p.classList.add('hidden'));

                document.querySelectorAll('.dme-subtab-btn').forEach(btn => {
                    btn.className = "dme-subtab-btn flex items-center gap-2 px-4 py-2.5 rounded-2xl text-xs font-bold transition cursor-pointer bg-white dark:bg-[#121c15] text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white border border-slate-200 dark:border-white/10";
                });

                const targetPanel = document.getElementById(`dme-panel-${subTabId}`);
                if (targetPanel) targetPanel.classList.remove('hidden');
                document.getElementById('dme-panel-croissance')?.classList.toggle('dme-growth-active', subTabId === 'croissance');

                const targetBtn = document.getElementById(`subtab-btn-${subTabId}`);
                if (targetBtn) {
                    targetBtn.className = "dme-subtab-btn flex items-center gap-2 px-4 py-2.5 rounded-2xl text-xs font-bold transition cursor-pointer bg-brand-neon text-black shadow-md shadow-brand-neon/20";
                }
                refreshDmeGrowthAvailability(currentTab);

                if (subTabId === 'ordonnance' && currentTab) {
                    loadMedsForPrescription();
                    renderActiveOrdLines();
                    loadPatientPrescriptionsHistory(currentTab.patientId, currentTab.passageId, currentTab.consultationId || currentTab.data?.dme?.id || 0);
                } else if (subTabId === 'bilans' && currentTab) {
                    window.loadDmeExamRequests();
                } else if (subTabId === 'consommations' && currentTab) {
                    loadDmeConsumptions();
                } else if (subTabId === 'echographie' && currentTab) {
                    renderEchoGallery();
                } else if (subTabId === 'croissance' && currentTab) {
                    window.renderDmeGrowthChart();
                }
                if (currentTab) applyDmeAccessMode(currentTab);
            };

            let dmeGrowthReferencePromise = null;
            function growthAgeMonths(birthIso, visitIso) {
                if (!birthIso || !visitIso) return null;
                const normalize = value => {
                    const raw = String(value).trim();
                    const iso = raw.match(/^(\d{4})-(\d{2})-(\d{2})/);
                    if (iso) return `${iso[1]}-${iso[2]}-${iso[3]}T12:00:00`;
                    const fr = raw.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
                    return fr ? `${fr[3]}-${fr[2]}-${fr[1]}T12:00:00` : raw;
                };
                const birth = new Date(normalize(birthIso)), visit = new Date(normalize(visitIso));
                if (Number.isNaN(birth.getTime()) || Number.isNaN(visit.getTime()) || visit < birth) return null;
                return (visit - birth) / (1000 * 60 * 60 * 24 * 30.4375);
            }
            function refreshDmeGrowthAvailability(tab) {
                const activeTab = tab || window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const patient = activeTab?.data?.patient || {};
                const dob = patient.date_naissance_iso || patient.date_naissance;
                const age = dob ? growthAgeMonths(dob, activeTab?.data?.passage?.date_passage_iso || new Date().toISOString()) : null;
                // En cas de date absente ou non reconnue, laisser l’onglet accessible au lieu de le masquer.
                const pediatricOrUnknown = age === null || age <= 228;
                document.getElementById('subtab-btn-croissance')?.classList.toggle('hidden', !pediatricOrUnknown);
                return pediatricOrUnknown;
            }
            function dmeGrowthSex(patient) {
                const sex = String(patient?.genre || '').trim().toLowerCase();
                if (['f', 'femme', 'female', 'féminin', 'feminin'].includes(sex)) return 'F';
                if (['m', 'homme', 'masculin', 'male'].includes(sex)) return 'M';
                return null;
            }
            window.renderDmeGrowthChart = async function() {
                const tab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!tab?.data) return;
                const p = tab.data.patient || {}, passage = tab.data.passage || {}, c = tab.data.dme?.constantes || {};
                const birthIso = p.date_naissance_iso || p.date_naissance;
                const currentAge = growthAgeMonths(birthIso, passage.date_passage_iso || new Date().toISOString());
                if (currentAge === null) {
                    const notice = document.getElementById('dmeGrowthNotice');
                    if (notice) { notice.textContent = 'La date de naissance ou la date du passage ne permet pas de calculer l’âge. Vérifiez la fiche patient.'; notice.classList.remove('hidden'); }
                    return;
                }
                if (currentAge > 228) return;

                const ageYears = Math.floor(currentAge / 12), ageMonths = Math.floor(currentAge % 12);
                const meta = document.getElementById('dmeGrowthPatientMeta');
                if (meta) meta.textContent = `${p.genre || 'Sexe non renseigné'} · ${ageYears} an(s) ${ageMonths} mois · références OMS par âge et sexe`;
                const sex = dmeGrowthSex(p), indicatorKey = document.getElementById('dmeGrowthIndicator')?.value || 'height_age';
                const chart = document.getElementById('dmeGrowthChart'), notice = document.getElementById('dmeGrowthNotice');
                if (!chart) return;
                if (!dmeGrowthReferencePromise) dmeGrowthReferencePromise = fetch('/api/dme/growth-reference').then(r => { if (!r.ok) throw new Error('Références OMS indisponibles'); return r.json(); });
                let reference;
                try { reference = await dmeGrowthReferencePromise; }
                catch (e) { if (notice) { notice.textContent = 'Les références OMS ne sont pas disponibles. Vérifiez que resources/who_growth/growth_reference.json est présent.'; notice.classList.remove('hidden'); } return; }
                if (tab.key !== window.currentActiveDmeKey) return;
                const indicator = reference.indicators?.[indicatorKey];
                if (!sex || !indicator?.[sex]?.length) {
                    if (notice) { notice.textContent = !sex ? 'Renseignez le sexe dans la fiche patient pour afficher la référence OMS correspondante.' : 'Aucune référence OMS disponible pour cet indicateur.'; notice.classList.remove('hidden'); }
                    chart.innerHTML = ''; return;
                }
                const minAge = Number(indicator.min_month || 0), maxAge = Number(indicator.max_month || 228);
                if (notice) {
                    const outside = currentAge < minAge || currentAge > maxAge;
                    notice.textContent = outside ? `La référence « ${indicator.label} » s’applique de ${Math.floor(minAge / 12)} à ${Math.floor(maxAge / 12)} ans. L’âge actuel est hors de cette plage.` : '';
                    notice.classList.toggle('hidden', !outside);
                }
                const valueKey = {height_age:'taille', weight_age:'poids', bmi_age:'imc', head_age:'perimetre_cranien', arm_age:'perimetre_brachial'}[indicatorKey];
                const history = (tab.data.historique || []).map(item => {
                    const values = item.constantes || {};
                    let value = Number(values[valueKey]);
                    if (indicatorKey === 'bmi_age' && !value && Number(values.poids) > 0 && Number(values.taille) > 0) value = Number(values.poids) / ((Number(values.taille) / 100) ** 2);
                    return {x:growthAgeMonths(birthIso, item.date_passage_iso), y:value, label:item.date_passage || ''};
                }).filter(point => Number.isFinite(point.x) && Number.isFinite(point.y) && point.y > 0 && point.x >= minAge && point.x <= maxAge);
                if (!tab.data.dme?.id || tab.data.dme?.verrouille === false) {
                    const inputValue = Number(document.getElementById(({height_age:'dme_growth_height',weight_age:'dme_growth_weight',bmi_age:'dme_imc',head_age:'dme_growth_head',arm_age:'dme_growth_arm'})[indicatorKey])?.value || 0);
                    let currentValue = inputValue;
                    if (indicatorKey === 'bmi_age' && !currentValue) {
                        const w = Number(document.getElementById('dme_growth_weight')?.value || 0), h = Number(document.getElementById('dme_growth_height')?.value || 0);
                        if (w > 0 && h > 0) currentValue = w / ((h / 100) ** 2);
                    }
                    if (currentAge >= minAge && currentAge <= maxAge && currentValue > 0) history.push({x:currentAge,y:currentValue,label:'Consultation en cours'});
                }
                const rows = indicator[sex].filter(row => row[0] >= minAge && row[0] <= maxAge);
                const measureValues = history.map(point => point.y);
                const curveValues = rows.flatMap(row => row.slice(1));
                const yMin = Math.min(...curveValues, ...measureValues), yMax = Math.max(...curveValues, ...measureValues);
                const xMin = minAge, xMax = maxAge, W = 900, H = 330, L = 58, R = 18, T = 18, B = 42;
                const sx = x => L + ((x-xMin)/(xMax-xMin || 1))*(W-L-R);
                const sy = y => H-B-((y-yMin)/(yMax-yMin || 1))*(H-T-B);
                const colors = ['#dc2626','#f97316','#f59e0b','#059669','#84cc16','#0ea5e9','#2563eb'];
                let svg = `<svg viewBox="0 0 ${W} ${H}" class="w-full min-w-[680px]" role="img" aria-label="${indicator.label}, courbes OMS et mesures du patient">`;
                for (let i=0;i<=4;i++) { const yy=T+i*(H-T-B)/4, value=yMax-i*(yMax-yMin)/4; svg+=`<line x1="${L}" y1="${yy}" x2="${W-R}" y2="${yy}" stroke="#cbd5e1" stroke-dasharray="3 5"/><text x="${L-8}" y="${yy+4}" text-anchor="end" font-size="10" fill="#64748b">${value.toFixed(1)}</text>`; }
                for (let i=0;i<=6;i++) { const x=xMin+i*(xMax-xMin)/6, xx=sx(x), years=(x/12).toFixed(0); svg+=`<line x1="${xx}" y1="${T}" x2="${xx}" y2="${H-B}" stroke="#e2e8f0"/><text x="${xx}" y="${H-14}" text-anchor="middle" font-size="10" fill="#64748b">${years} an${years==='1'?'':'s'}</text>`; }
                for(let z=0;z<7;z++){const points=rows.map(row=>`${sx(row[0]).toFixed(1)},${sy(row[z+1]).toFixed(1)}`).join(' ');svg+=`<polyline fill="none" stroke="${colors[z]}" stroke-width="${z===3?2.5:1.5}" opacity="${z===3?1:.78}" points="${points}"/>`;}
                if(history.length){ const points=history.sort((a,b)=>a.x-b.x).map(point=>`${sx(point.x).toFixed(1)},${sy(point.y).toFixed(1)}`).join(' '); if(history.length>1) svg+=`<polyline fill="none" stroke="#111827" stroke-width="2.5" points="${points}"/>`; history.forEach(point=>{svg+=`<circle cx="${sx(point.x)}" cy="${sy(point.y)}" r="5" fill="#111827"><title>${point.label}: ${point.y.toFixed(1)} ${indicator.unit}</title></circle>`;}); }
                svg+=`<text x="${(L+W-R)/2}" y="${H-1}" text-anchor="middle" font-size="11" fill="#475569">Âge</text><text transform="translate(14 ${(T+H-B)/2}) rotate(-90)" text-anchor="middle" font-size="11" fill="#475569">${indicator.label} (${indicator.unit})</text></svg>`;
                chart.innerHTML=svg;
                const legend=document.getElementById('dmeGrowthLegend');
                if(legend) legend.innerHTML=reference.z_levels.map((z,i)=>`<span class="inline-flex items-center gap-1"><i class="w-3 h-0.5" style="background:${colors[i]}"></i>${z==='0'?'Médiane':`${z} ET`}</span>`).join('')+'<span class="inline-flex items-center gap-1"><i class="w-3 h-0.5 bg-slate-900"></i>Mesures du patient</span>';
            };
            window.openDmeGrowthPrintView = function() {
                const tab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const chart = document.getElementById('dmeGrowthChart')?.querySelector('svg');
                if (!tab?.data || !chart) { alert('La courbe de croissance n’est pas encore prête à être imprimée.'); return; }
                const patient = tab.data.patient || {};
                const indicator = document.getElementById('dmeGrowthIndicator')?.selectedOptions?.[0]?.textContent || 'Courbe de croissance';
                const modal = document.getElementById('dmeGrowthPrintModal');
                if (!modal) return;
                document.getElementById('dmeGrowthPrintTitle').textContent = indicator;
                document.getElementById('dmeGrowthPrintPatient').textContent = `Patient : ${patient.nom || ''} ${patient.prenom || ''}`.trim();
                document.getElementById('dmeGrowthPrintBirth').textContent = `Date de naissance : ${patient.date_naissance || 'Non renseignée'} · Sexe : ${patient.genre || 'Non renseigné'}`;
                document.getElementById('dmeGrowthPrintDate').textContent = `Généré le ${new Date().toLocaleDateString('fr-FR')}`;
                document.getElementById('dmeGrowthPrintGraph').innerHTML = chart.outerHTML;
                document.getElementById('dmeGrowthPrintLegend').innerHTML = document.getElementById('dmeGrowthLegend')?.innerHTML || '';
                modal.classList.remove('hidden');
                modal.classList.add('flex');
            };
            window.closeDmeGrowthPrintView = function() {
                const modal = document.getElementById('dmeGrowthPrintModal');
                if (modal) { modal.classList.add('hidden'); modal.classList.remove('flex'); }
            };
            window.printDmeGrowthPdf = function() {
                const printTitle = document.getElementById('dmeGrowthPrintTitle')?.textContent || 'Courbe de croissance';
                const previousTitle = document.title;
                document.title = `${printTitle} - Cliniva`;
                window.onafterprint = function() { document.title = previousTitle; window.onafterprint = null; };
                window.print();
            };

            const DME_SPECIALTY_CHOICES = [
                {key:'reeducation_classique', label:'Rééducation classique', match:/reeducation classique/},
                {key:'physiotherapie', label:'Bilan de rééducation / physiothérapie', match:/physiotherap|bilan kine/},
                {key:'gastro', label:'Gastro-entérologie', match:/gastro|hepat|digest/},
                {key:'anesthesie', label:'Anesthésie', match:/anesth|reanim/},
                {key:'dermatologie', label:'Dermatologie', match:/dermat/},
                {key:'gynecologie', label:'Gynécologie', match:/gynec|obstet/},
                {key:'general', label:'Médecine générale', match:/medecine generale|generaliste/},
                {key:'medecine_physique', label:'Médecine physique et réadaptation', match:/medecine physique|readaptation|reeducation fonctionnelle/},
                {key:'medecine_sport', label:'Médecine du sport', match:/medecine du sport|medecine sportive|traumatologie du sport/}
            ];
            const DME_SPECIALTY_FIELD_LABELS = {"date_bilan":"Date du bilan","type_bilan":"\u00c9tape du bilan","evaluateur":"Professionnel \u00e9valuateur","indication":"Motif, diagnostic m\u00e9dical et prescription","antecedents":"Ant\u00e9c\u00e9dents, interventions et examens utiles","precautions":"Pr\u00e9cautions, contre-indications et points \u00e0 v\u00e9rifier","zones":"Zones concern\u00e9es (sch\u00e9ma ou saisie libre)","cote":"C\u00f4t\u00e9 du patient","localisation_details":"Localisation pr\u00e9cise, irradiation et contexte","douleur_presence":"Douleur pr\u00e9sente","douleur_type":"Caract\u00e9ristiques / m\u00e9canisme \u00e9voqu\u00e9 par le professionnel","douleur_duree":"\u00c9volution de la douleur","douleur_debut":"Date de d\u00e9but ou dur\u00e9e rapport\u00e9e","douleur_repos":"Douleur au repos (EN 0 \u00e0 10)","douleur_effort":"Douleur au mouvement (EN 0 \u00e0 10)","douleur_nuit":"Douleur nocturne (EN 0 \u00e0 10)","douleur_details":"Facteurs aggravants / soulageants et retentissement","inflammation":"Inflammation","oedeme":"\u0152d\u00e8me","dystrophie":"Troubles trophiques / dystrophie","sensibilite":"Perte de sensibilit\u00e9","amyotrophie":"Amyotrophie","contracture":"Contracture","instabilite":"Instabilit\u00e9 articulaire","mobilite":"Restriction de mobilit\u00e9","motricite":"Perte de motricit\u00e9 : origine \u00e9voqu\u00e9e","proprioception":"D\u00e9ficit de proprioception","deficits_details":"Pr\u00e9cisions par zone, c\u00f4t\u00e9, tests et constatations","amplitudes":"Mobilit\u00e9 : articulation, mouvement, actif/passif, degr\u00e9s, c\u00f4t\u00e9","force":"Force : muscle / mouvement, c\u00f4t\u00e9, m\u00e9thode et r\u00e9sultat","mesures_neuro":"Sensibilit\u00e9, proprioception, \u0153d\u00e8me : m\u00e9thode et r\u00e9sultat","activites":"Activit\u00e9s limit\u00e9es et priorit\u00e9s du patient","marche":"Marche, \u00e9quilibre, transferts et aides techniques","autonomie":"Autonomie, travail, sport et sommeil","tests":"Tests / \u00e9chelles : nom, date, r\u00e9sultat et unit\u00e9","synthese":"Synth\u00e8se clinique du professionnel","objectifs":"Objectifs mesurables et \u00e9ch\u00e9ance","programme":"Techniques, exercices, \u00e9ducation et consignes \u00e0 domicile","rythme":"Rythme, dur\u00e9e et nombre de s\u00e9ances pr\u00e9vus","reevaluation":"Date de r\u00e9\u00e9valuation pr\u00e9vue","evolution":"\u00c9volution : douleur, mobilit\u00e9, force et activit\u00e9s","adaptation":"Tol\u00e9rance, \u00e9v\u00e9nements et adaptations du programme","validation":"Nom et visa / signature du professionnel","rc_douleur":"Douleur","rc_inflammation":"Inflammation","rc_oedeme":"\u0152d\u00e8me-dystrophie","rc_sensibilite":"Perte de sensibilit\u00e9","rc_amyotrophie":"Amyotrophie","rc_contracture":"Contracture","rc_instabilite":"Instabilit\u00e9 articulaire","rc_mobilite":"Restriction de mobilit\u00e9","rc_motricite":"Perte de motricit\u00e9","rc_proprioception":"D\u00e9ficit de proprioception","rc_zones":"Localisation anatomique","rc_traitement":"Traitement en cours","rc_atcd":"Ant\u00e9c\u00e9dents (ATCD)","rc_bilan_cutaneo_trophique":"Bilan cutan\u00e9o-trophique","rc_bilan_osteo_articulaire":"Bilan ost\u00e9o-articulaire","rc_bilan_neuromusculaire":"Bilan neuromusculaire","rc_bilan_neurologique_specifique":"Bilan neurologique sp\u00e9cifique","rc_bilan_vesico_sphincterien":"Bilan v\u00e9sico-sphinct\u00e9rien","rc_bilan_cardio_respiratoire":"Bilan cardio-respiratoire","rc_capacite_vitale":"Capacit\u00e9 vitale","rc_tolerance_effort":"Tol\u00e9rance \u00e0 l\u2019effort",
                symptomes_digestifs:'Symptômes digestifs, début, durée et évolution', transit_selles:'Transit et selles', symptomes_associes:'Symptômes associés et contexte', signes_alerte_digestifs:'Signes d’alerte recherchés', explorations_digestives:'Antécédents et explorations digestives', examen_abdominal:'Examen abdominal', plan_digestif:'Évaluation et plan digestif',
                intervention_prevue:'Intervention prévue, date et indication', antecedents_anesthesiques:'Antécédents anesthésiques', voies_aeriennes:'Voies aériennes et dentition', evaluation_cardiorespiratoire:'Évaluation cardio-respiratoire', capacite_fonctionnelle:'Capacité fonctionnelle et éléments de risque', classe_asa:'Classe ASA', examens_preoperatoires:'Examens préopératoires et consultations', consentement_anesthesique:'Consentement et information', plan_anesthesique:'Plan anesthésique et consignes',
                lesion_localisation:'Localisation de la lésion', lesion_evolution:'Apparition et évolution', lesion_description:'Description clinique', lesion_symptomes:'Symptômes et facteurs contextuels', examen_cutane:'Étendue de l’examen cutané', dermoscopie_histologie:'Antécédents, dermoscopie et histologie', photos_cliniques:'Photos cliniques / dermoscopie', plan_dermatologie:'Évaluation et suivi',
                dernieres_regles:'Dernières règles', cycles_menstruels:'Cycles et symptômes menstruels', antecedents_grossesse:'Antécédents de grossesse et d’accouchement', contraception:'Contraception et contexte hormonal', depistage_col:'Dépistage du col et antécédents gynécologiques', symptomes_gynecologiques:'Symptômes gynécologiques et ménopause', examen_gynecologique:'Examen gynécologique', plan_gynecologique:'Évaluation, plan et suivi',
                antecedents_familiaux:'Antécédents familiaux', revue_systemes:'Revue des symptômes par appareil', autonomie_fonctionnelle:'Autonomie et capacité fonctionnelle', prevention_depistage:'Prévention et dépistages', vaccinations:'Vaccinations', suivi_orientation:'Suivi et orientation',
                motif_reeducation:'Motif de rééducation et objectifs du patient', histoire_fonctionnelle:'Histoire fonctionnelle et évolution', douleur_reeducation:'Douleur', limitations_activites:'Limitations dans les activités quotidiennes', autonomie_deplacements:'Autonomie, transferts et déplacements', examen_articulaire_moteur:'Examen articulaire et moteur', examen_neurologique_fonctionnel:'Examen neurologique fonctionnel', equilibre_marche:'Équilibre et marche', aides_techniques:'Aides techniques, orthèses et appareillages', evaluations_fonctionnelles:'Échelles et mesures fonctionnelles', objectifs_reeducation:'Objectifs de réadaptation', plans_reeducation_json:'Plan de rééducation', type_reeducation:'Type de rééducation', seances_par_semaine:'Séances par semaine', nombre_total_seances:'Nombre total de séances', duree_seance_minutes:'Durée d’une séance (min)', kine_staff_id:'Kinésithérapeute responsable', kine_assigne_label:'Kinésithérapeute responsable', programme_reeducation:'Programme, précautions et consignes', suivi_reeducation:'Suivi et évolution',
                sport_pratique:'Sport pratiqué et rôle / poste', niveau_pratique_charge:'Niveau et charge d’entraînement', contexte_blessure_sport:'Contexte de la blessure ou du symptôme', antecedents_sportifs:'Antécédents sportifs et blessures', douleur_sport:'Douleur et symptômes à l’effort', examen_musculosquelettique_sport:'Examen musculosquelettique', tests_fonctionnels_sport:'Tests fonctionnels / de performance réalisés', aptitude_restrictions_sport:'Aptitude, restrictions et consignes sportives', plan_reprise_sport:'Plan de prise en charge et reprise', suivi_sport:'Suivi sportif'
            };


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
            document.querySelectorAll('[data-physio-zone]').forEach(zone=>{
                zone.addEventListener('click',()=>togglePhysiotherapyZone(zone));
                zone.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();togglePhysiotherapyZone(zone);}});
            });
            document.getElementById('physio-zones')?.addEventListener('input',refreshPhysiotherapyMap);
            window.printPhysiotherapyBilan = async function() {
                const tab=window.activeDmeTabs.find(t=>t.key===window.currentActiveDmeKey);
                if(!tab)return;
                const values={};
                document.querySelectorAll('[data-dme-specialty="physiotherapie"][data-dme-specialty-field]').forEach(field=>values[field.dataset.dmeSpecialtyField]=field.value);
                try {
                    const response=await fetch('/api/dme/rehabilitation/bilan-pdf',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({patient_id:tab.patientId,passage_id:tab.passageId,values})});
                    if(!response.ok){const error=await response.json().catch(()=>({}));throw new Error(error.detail||'Génération PDF impossible');}
                    const blob=await response.blob();
                    if(!blob.type.includes('pdf'))throw new Error('Réponse PDF invalide');
                    if(activeRehabPlanPdfUrl)URL.revokeObjectURL(activeRehabPlanPdfUrl);
                    activeRehabPlanPdfUrl=URL.createObjectURL(blob);
                    activeLivretDraftToken=null;setDmeCrPreviewMode('archived');
                    document.getElementById('dmeCrModalTitle').innerText='Bilan de rééducation / physiothérapie';
                    const modal=portalDmeModal('dmeCrPreviewModal');
                    document.getElementById('dmeCrIframe').src=activeRehabPlanPdfUrl;
                    modal.classList.remove('hidden');modal.classList.add('flex');document.body.style.overflow='hidden';
                }catch(error){alert(error.message);}
            };

            let dmeRehabilitationTherapistsCache = null;
            let dmeReeducationTypesCache = null;
            const escapeRehabHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
            function getDmeReeducationRows(tab = window.activeDmeTabs?.find(item => item.key === window.currentActiveDmeKey)) {
                const hidden = document.querySelector('[data-dme-specialty-field="plans_reeducation_json"]');
                try {
                    const parsed = JSON.parse(hidden?.value || '[]');
                    if (Array.isArray(parsed)) return parsed;
                } catch (_) {}
                const saved = tab?.data?.dme?.donnees_specialisees?.sections?.medecine_physique || {};
                if (saved.type_reeducation) return [{
                    type: saved.type_reeducation, seances_par_semaine: saved.seances_par_semaine || '',
                    nombre_total_seances: saved.nombre_total_seances || '', duree_seance_minutes: saved.duree_seance_minutes || '',
                    kine_id: saved.kine_staff_id || 'any', kine: saved.kine_assigne_label || "N’importe quelle kiné",
                }];
                return [];
            }
            window.syncDmeReeducationPlan = function() {
                const hidden = document.querySelector('[data-dme-specialty-field="plans_reeducation_json"]');
                const body = document.getElementById('dmeReeducationPlanRows');
                if (!hidden || !body) return;
                const rows = [...body.querySelectorAll('tr[data-plan-row]')].map(row => {
                    const field = name => row.querySelector(`[data-plan-field="${name}"]`);
                    const therapist = field('kine_id');
                    return {
                        type: field('type')?.value || '',
                        seances_par_semaine: field('seances_par_semaine')?.value || '',
                        nombre_total_seances: field('nombre_total_seances')?.value || '',
                        duree_seance_minutes: field('duree_seance_minutes')?.value || '',
                        kine_id: therapist?.value || 'any',
                        kine: therapist?.selectedOptions?.[0]?.dataset?.displayName || "N’importe quelle kiné",
                    };
                });
                hidden.value = JSON.stringify(rows);
            };
            window.renderDmeReeducationPlanRows = function(rows = getDmeReeducationRows()) {
                const body = document.getElementById('dmeReeducationPlanRows');
                if (!body) return;
                const typeOptions = dmeReeducationTypesCache || [];
                const therapists = dmeRehabilitationTherapistsCache || [];
                if (!rows.length) {
                    body.innerHTML = `<tr><td colspan="6" class="p-4 text-center text-slate-400">Aucune ligne. Cliquez sur « Ajouter une ligne » pour commencer le plan.</td></tr>`;
                    window.syncDmeReeducationPlan();
                    return;
                }
                body.innerHTML = rows.map((item, index) => {
                    const typeOptionsHtml = `<option value="">Choisir une rééducation</option>${typeOptions.map(type => `<option value="${escapeRehabHtml(type)}" ${String(item.type || '') === type ? 'selected' : ''}>${escapeRehabHtml(type)}</option>`).join('')}`;
                    const savedKineId = String(item.kine_id || 'any');
                    const savedKine = item.kine || "N’importe quelle kiné";
                    const therapistOptions = [`<option value="any" data-display-name="N’importe quelle kiné" ${savedKineId === 'any' ? 'selected' : ''}>N’importe quelle kiné</option>`, ...therapists.map(person => {
                        const name = `${person.nom || ''} ${person.prenom || ''}`.trim() || `Kiné #${person.id}`;
                        const extra = person.service_nom ? ` · ${person.service_nom}` : '';
                        return `<option value="${escapeRehabHtml(person.id)}" data-display-name="${escapeRehabHtml(name)}" ${String(person.id) === savedKineId ? 'selected' : ''}>${escapeRehabHtml(name + extra)}</option>`;
                    })];
                    if (savedKineId !== 'any' && !therapists.some(person => String(person.id) === savedKineId)) {
                        therapistOptions.push(`<option value="${escapeRehabHtml(savedKineId)}" data-display-name="${escapeRehabHtml(savedKine)}" selected>${escapeRehabHtml(savedKine)} · affectation enregistrée</option>`);
                    }
                    return `<tr data-plan-row="${index}" class="align-top">
                        <td class="min-w-56 p-2"><select data-plan-field="type" onchange="window.syncDmeReeducationPlan()" class="w-full rounded-lg border border-slate-200 bg-slate-50 px-2 py-2 text-[10px] font-semibold text-slate-700 dark:border-white/10 dark:bg-black/30 dark:text-white">${typeOptionsHtml}</select></td>
                        <td class="w-32 p-2"><input data-plan-field="seances_par_semaine" type="number" min="1" max="99" step="1" value="${escapeRehabHtml(item.seances_par_semaine || '')}" placeholder="Ex. 3" oninput="window.syncDmeReeducationPlan()" class="w-full rounded-lg border border-slate-200 bg-slate-50 px-2 py-2 text-[10px] dark:border-white/10 dark:bg-black/30 dark:text-white"></td>
                        <td class="w-28 p-2"><input data-plan-field="nombre_total_seances" type="number" min="1" max="999" step="1" value="${escapeRehabHtml(item.nombre_total_seances || '')}" placeholder="Ex. 12" oninput="window.syncDmeReeducationPlan()" class="w-full rounded-lg border border-slate-200 bg-slate-50 px-2 py-2 text-[10px] dark:border-white/10 dark:bg-black/30 dark:text-white"></td>
                        <td class="w-28 p-2"><input data-plan-field="duree_seance_minutes" type="number" min="1" max="600" step="5" value="${escapeRehabHtml(item.duree_seance_minutes || '')}" placeholder="Ex. 45" oninput="window.syncDmeReeducationPlan()" class="w-full rounded-lg border border-slate-200 bg-slate-50 px-2 py-2 text-[10px] dark:border-white/10 dark:bg-black/30 dark:text-white"></td>
                        <td class="min-w-52 p-2"><select data-plan-field="kine_id" onchange="window.syncDmeReeducationPlan()" class="w-full rounded-lg border border-slate-200 bg-slate-50 px-2 py-2 text-[10px] font-semibold text-slate-700 dark:border-white/10 dark:bg-black/30 dark:text-white">${therapistOptions.join('')}</select></td>
                        <td class="p-2 text-center"><button type="button" onclick="window.removeDmeReeducationPlanRow(${index})" aria-label="Supprimer cette ligne" class="rounded-lg p-2 text-rose-600 hover:bg-rose-50 dark:hover:bg-rose-500/10"><i class="ph-bold ph-trash"></i></button></td>
                    </tr>`;
                }).join('');
                window.syncDmeReeducationPlan();
            };
            window.addDmeReeducationPlanRow = function() {
                const rows = getDmeReeducationRows();
                rows.push({type:'', seances_par_semaine:'', nombre_total_seances:'', duree_seance_minutes:'', kine_id:'any', kine:"N’importe quelle kiné"});
                window.renderDmeReeducationPlanRows(rows);
            };
            window.removeDmeReeducationPlanRow = function(index) {
                const rows = getDmeReeducationRows();
                rows.splice(index, 1);
                window.renderDmeReeducationPlanRows(rows);
            };
            window.loadDmeRehabilitationTherapists = async function(tab) {
                const body = document.getElementById('dmeReeducationPlanRows');
                if (!body) return;
                try {
                    const [therapistResponse, typeResponse] = await Promise.all([
                        fetch('/api/dme/rehabilitation/kinesitherapeutes', {cache:'no-store'}),
                        fetch('/api/dme/rehabilitation/types', {cache:'no-store'})
                    ]);
                    const [therapistResult, typeResult] = await Promise.all([
                        therapistResponse.json(), typeResponse.json()
                    ]);
                    if (!therapistResponse.ok || !therapistResult.success) throw new Error(therapistResult.detail || 'Liste des kinésithérapeutes indisponible.');
                    if (!typeResponse.ok || !typeResult.success) throw new Error(typeResult.detail || 'Types de rééducation indisponibles.');
                    dmeRehabilitationTherapistsCache = Array.isArray(therapistResult.therapists) ? therapistResult.therapists : [];
                    dmeReeducationTypesCache = Array.isArray(typeResult.types) ? typeResult.types : [];
                    const hint = document.getElementById('dmeRehabilitationTherapistHint');
                    if (hint) hint.textContent = dmeReeducationTypesCache.length
                        ? `${dmeRehabilitationTherapistsCache.length} kiné(s) actif(s) · choix repérés dans les catégories, types et spécialités liés à la rééducation.`
                        : 'Aucun type de rééducation n’a été trouvé dans les catégories, types ou spécialités du catalogue.';
                    window.renderDmeReeducationPlanRows(getDmeReeducationRows(tab));
                } catch (error) {
                    body.innerHTML = `<tr><td colspan="6" class="p-4 text-center text-rose-600">${escapeRehabHtml(error.message || 'Impossible de charger le plan de rééducation.')}</td></tr>`;
                }
            };
            let activeRehabPlanPdfUrl = null;
            window.previewDmeReeducationPlan = async function() {
                const tab = window.activeDmeTabs?.find(item => item.key === window.currentActiveDmeKey);
                if (!tab?.patientId || !tab?.passageId) { alert('Aucun patient actif dans le DME.'); return; }
                window.syncDmeReeducationPlan();
                const plans = getDmeReeducationRows(tab).filter(row => String(row.type || '').trim());
                if (!plans.length) { alert('Ajoutez au moins une ligne et sélectionnez un type de rééducation.'); return; }
                const incomplete = plans.some(row => !Number(row.seances_par_semaine) || !Number(row.nombre_total_seances));
                if (incomplete) { alert('Renseignez les séances par semaine et le nombre total de séances pour chaque ligne.'); return; }
                try {
                    const section = tab.data?.dme?.donnees_specialisees?.sections?.medecine_physique || {};
                    const objectifs = document.querySelector('[data-dme-specialty-field="objectifs_reeducation"]')?.value || section.objectifs_reeducation || '';
                    const consignes = document.querySelector('[data-dme-specialty-field="programme_reeducation"]')?.value || section.programme_reeducation || '';
                    const response = await fetch('/api/dme/rehabilitation/plan-pdf', {
                        method:'POST', headers:{'Content-Type':'application/json'},
                        body:JSON.stringify({patient_id:tab.patientId, passage_id:tab.passageId, plans, objectifs, consignes})
                    });
                    if (!response.ok) {
                        let detail = 'Impossible de générer le PDF du plan de rééducation.';
                        try { const error = await response.json(); detail = error.detail || detail; } catch (_) {}
                        throw new Error(detail);
                    }
                    const blob = await response.blob();
                    if (!blob.size || !blob.type.toLowerCase().includes('pdf')) throw new Error('Le serveur n’a pas renvoyé un PDF valide.');
                    if (activeRehabPlanPdfUrl) URL.revokeObjectURL(activeRehabPlanPdfUrl);
                    activeRehabPlanPdfUrl = URL.createObjectURL(blob);
                    activeLivretDraftToken = null;
                    setDmeCrPreviewMode('archived');
                    const title = document.getElementById('dmeCrModalTitle');
                    if (title) title.innerText = 'Aperçu du plan de rééducation';
                    const modal = portalDmeModal('dmeCrPreviewModal');
                    const iframe = document.getElementById('dmeCrIframe');
                    if (iframe) iframe.src = activeRehabPlanPdfUrl;
                    if (modal) { modal.classList.remove('hidden'); modal.classList.add('flex'); document.body.style.overflow = 'hidden'; }
                } catch (error) {
                    alert(error.message || 'Erreur lors de la génération du plan PDF.');
                }
            };

            window.openDmeSpecialtyPicker = function() {
                const modal = portalDmeModal('dmeSpecialtyPickerModal');
                if (modal) { modal.classList.remove('hidden'); modal.classList.add('flex'); document.body.style.overflow = 'hidden'; }
            };
            window.closeDmeSpecialtyPicker = function() {
                const modal = document.getElementById('dmeSpecialtyPickerModal');
                if (modal) { modal.classList.add('hidden'); modal.classList.remove('flex'); }
                if (!document.querySelector('#dmeDicomPrintModal:not(.hidden), #echoManagerModal:not(.hidden), #echoCarouselModal:not(.hidden)')) document.body.style.overflow = '';
            };
            window.updateDmeSpecialtySection = function(specialtyKey) {
                const panel = document.querySelector(`[data-dme-specialty-panel="${specialtyKey}"]`);
                if (panel) { panel.classList.remove('hidden'); panel.style.display = 'grid'; }
            };
            window.addDmeSpecialtySection = async function() {
                const specialtyKey = document.getElementById('dmeSpecialtyAddSelect')?.value || '';
                const currentTab = window.activeDmeTabs.find(tab => tab.key === window.currentActiveDmeKey);
                if (!currentTab || currentTab.mode === 'lecture') {
                    alert('Cette consultation est en lecture seule. Les champs spécialisés ne peuvent pas être ajoutés.');
                    return;
                }
                window.updateDmeSpecialtySection(specialtyKey);
                window.closeDmeSpecialtyPicker();
                document.querySelector(`[data-dme-specialty-panel="${specialtyKey}"]`)?.scrollIntoView({behavior:'smooth', block:'center'});
                const visibleSections = [...document.querySelectorAll('[data-dme-specialty-panel]')]
                    .filter(panel => panel.style.display === 'grid')
                    .map(panel => panel.dataset.dmeSpecialtyPanel);
                try {
                    const response = await fetch('/api/dme/save', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            id_patient: currentTab.patientId,
                            id_passage: currentTab.passageId,
                            sections: visibleSections,
                            sections_only: true
                        })
                    });
                    const result = await response.json().catch(() => ({}));
                    if (!response.ok || !result.success) throw new Error(result.detail || result.message || 'Impossible de mémoriser les sections spécialisées.');
                    currentTab.data.dme = currentTab.data.dme || {};
                    currentTab.data.dme.donnees_specialisees = result.donnees_specialisees;
                } catch (error) {
                    alert(error.message || 'Erreur lors de l’enregistrement des sections spécialisées.');
                }
            };

            function initializeDmeSpecialtyFields(tab) {
                if (!tab) return;
                const saved = tab.data?.dme?.donnees_specialisees || {};
                const savedSections = saved.sections && typeof saved.sections === 'object' ? saved.sections : {};
                document.querySelectorAll('[data-dme-specialty-panel]').forEach(panel => { panel.classList.add('hidden'); panel.style.display = 'none'; });
                document.querySelectorAll('[data-dme-specialty-field]').forEach(field => {
                    field.value = savedSections[field.dataset.dmeSpecialty]?.[field.dataset.dmeSpecialtyField] || '';
                });
                const savedKeys = Object.keys(savedSections);
                if (saved.specialite && DME_SPECIALTY_CHOICES.some(choice => choice.key === saved.specialite)) savedKeys.push(saved.specialite);
                [...new Set(savedKeys)].forEach(window.updateDmeSpecialtySection);
                refreshPhysiotherapyMap();
                refreshClassicReeducation();
            }

            function collectDmeSpecialtyFields() {
                const sections = {};
                document.querySelectorAll('[data-dme-specialty-field]').forEach(field => {
                    if (field.closest('[data-dme-specialty-panel]')?.style.display !== 'grid') return;
                    const specialty = field.dataset.dmeSpecialty;
                    sections[specialty] ||= {};
                    sections[specialty][field.dataset.dmeSpecialtyField] = field.value || '';
                });
                const visible = [...document.querySelectorAll('[data-dme-specialty-panel]')].filter(panel => panel.style.display === 'grid').map(panel => panel.dataset.dmeSpecialtyPanel);
                return {specialite: visible[0] || '', sections};
            }

            // 4. INJECTION DANS LE DOM
            function displayActivePatientDme() {
                const container = document.getElementById('dmePatientContainer');
                const placeholder = document.getElementById('dmeEmptyPlaceholder');

                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab || !currentTab.data) {
                    if (container) container.classList.add('hidden');
                    if (placeholder) placeholder.classList.remove('hidden');
                    return;
                }

                if (placeholder) placeholder.classList.add('hidden');
                if (container) container.classList.remove('hidden');

                const p = currentTab.data.patient || {};
                const pass = currentTab.data.passage || {};
                const dme = currentTab.data.dme || {};
                const c = dme.constantes || {};
                initializeDmeSpecialtyFields(currentTab);
                window.loadDmeRehabilitationTherapists(currentTab);
                const histo = currentTab.data.historique || [];
                const crTemplates = currentTab.data.cr_templates || [];
                const radiologues = currentTab.data.radiologues || [];
                const archivedCrs = currentTab.data.archived_crs || [];

                // En-tête patient
                document.getElementById('dme_hdr_avatar').src = p.photo_face || p.avatar || '/static/images/avatar-placeholder.svg?w=120';
                document.getElementById('dme_hdr_fullname').innerText = `${p.nom || 'Patient'} ${p.prenom || ''}`;
                document.getElementById('dme_hdr_blood').innerText = p.groupe_sanguin || 'O+';
                document.getElementById('dme_hdr_dossier').innerText = `#DOS-${String(p.id || 0).padStart(4, '0')}`;
                document.getElementById('dme_hdr_dob').innerText = p.date_naissance || '-';
                document.getElementById('dme_hdr_tel').innerText = p.telephone_1 || '-';
                document.getElementById('dme_hdr_location').innerText = `${p.commune || ''} (${p.wilaya || ''})`;
                document.getElementById('dme_hdr_medecin').innerText = pass.medecin_nom ? `${pass.medecin_grade || 'Dr.'} ${pass.medecin_nom}` : 'Aucun';
                renderDmePassagePrestations(Array.isArray(pass.actes) ? pass.actes : [], pass.type_passage);

                const questionnaire = pass.questionnaire || {};
                const observation = questionnaire.observation_medicale || questionnaire.observation || '';
                document.getElementById('dme_passage_observation').innerText = observation || 'Aucune observation médicale saisie pour ce passage.';
                const clinicalLabels = {
                    hospitalise: 'Hospitalisé', dossier_interne: 'Dossier interne', dossier_externe: 'Dossier externe',
                    urgence_vitale: 'Urgence vitale', grossesse: 'Grossesse', allergie: 'Allergie', diabete: 'Diabète',
                    hta: 'HTA', asthme: 'Asthme', insuffisance_renale: 'Insuffisance rénale', pace_maker: 'Pacemaker',
                    valve_cardiaque: 'Valve cardiaque', prothese_metallique: 'Prothèse métallique'
                };
                document.getElementById('dme_passage_clinical_info').innerHTML = Object.entries(clinicalLabels)
                    .filter(([key]) => questionnaire[key] === true || String(questionnaire[key]).trim().toUpperCase() === 'OUI')
                    .map(([, label]) => `<span class="px-2.5 py-1 rounded-lg bg-white dark:bg-black/30 border border-emerald-200 dark:border-emerald-900/40 text-slate-700 dark:text-slate-200"><b>${label} :</b> Oui</span>`).join('') || '<span class="text-[11px] text-slate-500">Aucun renseignement clinique coché « Oui ».</span>';

                const admissionDocs = pass.documents_scannes || [];
                const admissionDocsBox = document.getElementById('dmeAdmissionDocs');
                if (admissionDocsBox) {
                    const demandeButton = `<button type="button" onclick="openDmeDemandeExamen(${Number(currentTab.passageId)})" class="px-3 py-2 rounded-xl bg-slate-900 dark:bg-brand-neon text-white dark:text-black text-[10px] font-bold inline-flex items-center gap-1.5"><i class="ph-bold ph-file-pdf"></i>Demande d’examen · PDF / imprimer</button>`;
                    const docButtons = admissionDocs.map((doc, index) => `<button type="button" onclick="openDmeAdmissionDocument(${Number(currentTab.patientId)}, ${Number(currentTab.passageId)}, ${index})" class="px-3 py-2 rounded-xl bg-white dark:bg-black/20 border border-slate-200 dark:border-white/10 text-slate-700 dark:text-slate-200 text-[10px] font-bold inline-flex items-center gap-1.5"><i class="ph-bold ph-file-pdf text-rose-500"></i>${escapeDmeArchiveHtml(doc.type || `Document scanné ${index + 1}`)}${doc.heure ? ` · ${escapeDmeArchiveHtml(doc.heure)}` : ''}</button>`).join('');
                    admissionDocsBox.innerHTML = `<div class="flex flex-wrap items-center gap-2"><span class="text-[10px] font-extrabold uppercase tracking-wide text-slate-500 mr-1">Documents d’admission :</span>${demandeButton}${docButtons || '<span class="text-[10px] text-slate-400 italic">Aucun document scanné pour ce passage.</span>'}</div>`;
                }

                // Constantes
                document.getElementById('dme_ta').value = c.ta || '';
                document.getElementById('dme_pouls').value = c.pouls || '';
                document.getElementById('dme_temp').value = c.temp || '';
                document.getElementById('dme_poids').value = c.poids || '';
                document.getElementById('dme_taille').value = c.taille || '';
                document.getElementById('dme_imc').value = c.imc || '';
                document.getElementById('dme_glycemie').value = c.glycemie || '';
                document.getElementById('dme_growth_weight').value = c.poids || '';
                document.getElementById('dme_growth_height').value = c.taille || '';
                document.getElementById('dme_growth_head').value = c.perimetre_cranien || '';
                document.getElementById('dme_growth_arm').value = c.perimetre_brachial || '';

                // Les 8 champs ordonnés
                document.getElementById('dme_allergies').value = dme.allergies_intolerances || '';
                document.getElementById('dme_traitement').value = dme.traitement_en_cours || '';
                document.getElementById('dme_ant_med').value = dme.antecedents_medicaux || '';
                document.getElementById('dme_ant_chir').value = dme.antecedents_chirurgicaux || '';
                document.getElementById('dme_habitudes').value = dme.notes_habitudes || '';
                document.getElementById('dme_motif').value = dme.motif || '';
                document.getElementById('dme_diagnostic').value = dme.diagnostic || '';
                document.getElementById('dme_conduite').value = dme.conduite_a_tenir || '';

                // Actes
                currentDmeActs = Array.isArray(pass.actes) ? [...pass.actes] : [];
                renderDmeActsTable();
                autoDetectDmeTarifPeriod();
                calculateImc();
                window.renderDmeGrowthChart();

                // Modèles CR
                const tplSelect = document.getElementById('crTemplateSelect');
                if (tplSelect) {
                    tplSelect.value = '';
                }
                const tplSearch = document.getElementById('crTemplateSearchInput');
                if (tplSearch) tplSearch.value = '';
                const tplResults = document.getElementById('crTemplateSearchResults');
                if (tplResults) { tplResults.innerHTML = ''; tplResults.classList.add('hidden'); }

                // Prescripteur
                const prescInput = document.getElementById('crMedPrescripteurSelect');
                if (prescInput) {
                    prescInput.value = pass.medecin_nom ? `${pass.medecin_grade || 'Dr.'} ${pass.medecin_nom} ${pass.medecin_prenom || ''}`.trim() : 'Dr. Prescripteur';
                }

                // Signataires
                const sigSelect = document.getElementById('crMedSignataireSelect');
                if (sigSelect) {
                    sigSelect.innerHTML = radiologues.length > 0 
                        ? radiologues.map(r => `<option value="Dr. ${r.nom} ${r.prenom} (${r.specialite || 'Radiologie'})">Dr. ${r.nom} ${r.prenom} (${r.specialite || 'Radiologie'})</option>`).join('')
                        : '<option value="Dr. Praticien (Radiologie)">Dr. Praticien (Radiologie)</option>';
                }

                // Tableaux
                renderDmeArchivedCrTable(archivedCrs);
                renderEchoGallery();

                // Historique
                const histoBody = document.getElementById('dmeHistoTableBody');
                if (histoBody) {
                    histoBody.innerHTML = histo.length > 0 ? histo.map(h => `
                        <tr class="border-b border-slate-100 dark:border-white/5 hover:bg-slate-50 dark:hover:bg-white/[0.02]">
                            <td class="py-2.5 font-mono text-[11px]">#PASS-${String(h.id_passage).padStart(4, '0')}</td>
                            <td class="py-2.5">${h.date_passage} (${h.heure_passage})</td>
                            <td class="py-2.5 font-bold">${h.type_passage}</td>
                            <td class="py-2.5 italic">${h.diagnostic || 'Non renseigné'}</td>
                            <td class="py-2.5 font-semibold">${h.praticien || 'Dr'}</td>
                            <td class="py-2.5"><span class="px-2 py-0.5 rounded-lg text-[10px] font-bold bg-brand-neon/20 text-brand-neon">${h.statut}</span></td>
                            <td class="py-2.5 text-right"><button type="button" onclick="viewArchivedConsultation(${h.id_passage})" class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-slate-900 dark:bg-brand-neon text-white dark:text-black text-[10px] font-bold hover:opacity-90 transition"><i class="ph-bold ph-eye"></i><span>Voir l'archive</span></button></td>
                        </tr>
                    `).join('') : '<tr><td colspan="7" class="py-6 text-center text-slate-400 italic">Aucun passage antérieur.</td></tr>';
                }

                applyDmeDisplayPreferences(currentTab.data?.dme_display_permissions || dmeDisplayPermissions);
                switchDmeSubTab(currentTab.currentSubTab || 'consultation');
            }

            function escapeDmeArchiveHtml(value) {
                return String(value ?? '')
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;')
                    .replace(/'/g, '&#39;');
            }

            window.closeDmeArchiveDetailModal = function() {
                const modal = document.getElementById('dmeArchiveDetailModal');
                if (modal) { modal.classList.add('hidden'); modal.classList.remove('flex'); }
            };

            window.openArchivedDocumentPreview = function(recordId, format) {
                if (format === 'html') openCrPreviewModal(recordId);
                else openWordPdfPreview(recordId, 'Document archivé');
            };

            window.viewArchivedConsultation = async function(passageId) {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const modal = document.getElementById('dmeArchiveDetailModal');
                const body = document.getElementById('dmeArchiveModalBody');
                const title = document.getElementById('dmeArchiveModalTitle');
                const subtitle = document.getElementById('dmeArchiveModalSubtitle');
                if (!currentTab || !modal || !body) return;

                modal.classList.remove('hidden');
                modal.classList.add('flex');
                if (title) title.textContent = `Archive de consultation · #PASS-${String(passageId).padStart(4, '0')}`;
                if (subtitle) subtitle.textContent = 'Chargement des informations cliniques et documents associés...';
                body.innerHTML = `<div class="py-16 text-center text-xs text-slate-400"><i class="ph-bold ph-spinner animate-spin text-xl"></i><p class="mt-2">Chargement de l'archive...</p></div>`;

                try {
                    const response = await fetch(`/api/dme/archive/${currentTab.patientId}/${passageId}`);
                    const result = await response.json();
                    if (!response.ok) throw new Error(result.detail || 'Archive introuvable.');

                    const c = result.consultation || {};
                    const constants = c.constantes || {};
                    const safe = escapeDmeArchiveHtml;
                    const displayValue = value => value === null || value === undefined || String(value).trim() === '' ? '<span class="italic text-slate-400">Non renseigné</span>' : safe(value);
                    const field = (label, value) => `<div class="rounded-xl border border-slate-100 dark:border-white/5 bg-slate-50/70 dark:bg-black/15 p-3"><div class="text-[9px] font-bold uppercase tracking-wide text-slate-400">${safe(label)}</div><div class="mt-1 text-xs leading-5 text-slate-700 dark:text-slate-200 whitespace-pre-wrap break-words">${displayValue(value)}</div></div>`;
                    const section = (titleText, icon, content) => `<section class="space-y-3"><h4 class="flex items-center gap-2 text-xs font-extrabold text-slate-800 dark:text-white"><i class="ph-bold ${icon} text-brand-neon text-base"></i>${safe(titleText)}</h4>${content}</section>`;

                    const constantFields = [
                        ['Tension artérielle', constants.ta], ['Pouls (BPM)', constants.pouls],
                        ['Température (°C)', constants.temp], ['Poids (kg)', constants.poids],
                        ['Taille (cm)', constants.taille], ['IMC (kg/m²)', constants.imc], ['Glycémie (g/L)', constants.glycemie]
                    ].map(([labelText, value]) => field(labelText, value)).join('');
                    const clinicalFields = [
                        ['Observation médicale', c.questionnaire?.observation_medicale || c.questionnaire?.observation],
                        ['Allergies & intolérances', c.allergies_intolerances], ['Traitement en cours', c.traitement_en_cours],
                        ['Antécédents médicaux', c.antecedents_medicaux], ['Antécédents chirurgicaux', c.antecedents_chirurgicaux],
                        ['Notes & habitudes de vie', c.notes_habitudes], ['Motif de consultation', c.motif],
                        ['Diagnostic & hypothèse', c.diagnostic], ['Conduite à tenir', c.conduite_a_tenir]
                    ].map(([labelText, value]) => field(labelText, value)).join('');
                    const specialtyData = c.donnees_specialisees || {};
                    const specialtySections = specialtyData.sections && typeof specialtyData.sections === 'object' ? specialtyData.sections : {};
                    const specialtyFields = Object.entries(specialtySections).map(([sectionKey, values]) => {
                        const sectionChoice = DME_SPECIALTY_CHOICES.find(item => item.key === sectionKey);
                        const fields = Object.entries(values || {}).filter(([key, value]) => DME_SPECIALTY_FIELD_LABELS[key] && String(value || '').trim()).map(([key, value]) => {
                            if (key === 'plans_reeducation_json') {
                                try {
                                    const rows = JSON.parse(value);
                                    if (Array.isArray(rows) && rows.length) {
                                        const tableRows = rows.map(plan => `<tr class="border-t border-slate-200 dark:border-white/10"><td class="p-2">${safe(plan.type || '—')}</td><td class="p-2">${safe(plan.seances_par_semaine || '—')}</td><td class="p-2">${safe(plan.nombre_total_seances || '—')}</td><td class="p-2">${safe(plan.duree_seance_minutes || '—')}</td><td class="p-2">${safe(plan.kine || "N’importe quelle kiné")}</td></tr>`).join('');
                                        return `<div class="col-span-full overflow-x-auto rounded-xl border border-slate-200 dark:border-white/10"><div class="p-3 text-[9px] font-bold uppercase tracking-wide text-slate-400">${safe(DME_SPECIALTY_FIELD_LABELS[key])}</div><table class="w-full min-w-[680px] text-left text-[10px]"><thead class="bg-slate-100 dark:bg-white/5"><tr><th class="p-2">Type</th><th class="p-2">Séances / semaine</th><th class="p-2">Total</th><th class="p-2">Durée (min)</th><th class="p-2">Kinésithérapeute</th></tr></thead><tbody>${tableRows}</tbody></table></div>`;
                                    }
                                } catch (_) {}
                                return '';
                            }
                            return field(DME_SPECIALTY_FIELD_LABELS[key], value);
                        }).join('');
                        return fields && sectionChoice ? section(`Section ${sectionChoice.label}`, 'ph-first-aid-kit', `<div class="grid grid-cols-1 gap-2 sm:grid-cols-2">${fields}</div>`) : '';
                    }).join('');
                    const intakeLabels = {
                        hospitalise: 'Hospitalisé', dossier_interne: 'Dossier interne', dossier_externe: 'Dossier externe',
                        urgence_vitale: 'Urgence vitale', grossesse: 'Grossesse', allergie: 'Allergie', diabete: 'Diabète',
                        hta: 'HTA', asthme: 'Asthme', insuffisance_renale: 'Insuffisance rénale', pace_maker: 'Pacemaker',
                        valve_cardiaque: 'Valve cardiaque', prothese_metallique: 'Prothèse métallique'
                    };
                    const intakeFields = Object.entries(intakeLabels)
                        .map(([key, label]) => [label, c.questionnaire?.[key]])
                        .filter(([, value]) => value !== undefined && value !== null && value !== '')
                        .map(([label, value]) => field(label, value)).join('');

                    const acts = Array.isArray(c.actes) ? c.actes : [];
                    const amounts = [
                        ['Total brut', c.montant_total], ['Remise', c.remise],
                        ['Versement', c.versement], ['Reste', c.reste]
                    ].map(([labelText, value]) => field(labelText, value === null || value === undefined ? '' : `${Number(value).toLocaleString('fr-FR')} DA`)).join('');
                    const actsHtml = acts.length
                        ? `<div class="overflow-x-auto rounded-xl border border-slate-100 dark:border-white/5"><table class="w-full text-left text-[11px]"><thead class="bg-slate-50 dark:bg-white/5 text-slate-400"><tr><th class="p-2.5">Acte / prestation</th><th class="p-2.5">Période</th><th class="p-2.5 text-center">Qté</th><th class="p-2.5 text-right">Sous-total</th></tr></thead><tbody>${acts.map(act => `<tr class="border-t border-slate-100 dark:border-white/5"><td class="p-2.5 font-semibold">${safe(act.nom || 'Acte')}</td><td class="p-2.5">${safe(act.periode || '-')}</td><td class="p-2.5 text-center">${safe(act.quantite ?? '-')}</td><td class="p-2.5 text-right font-mono">${Number(act.sous_total || 0).toLocaleString('fr-FR')} DA</td></tr>`).join('')}</tbody></table></div>`
                        : '<p class="text-[11px] italic text-slate-400">Aucun acte rattaché à ce passage.</p>';

                    const prescriptions = Array.isArray(result.prescriptions) ? result.prescriptions : [];
                    const prescriptionsHtml = prescriptions.length
                        ? prescriptions.map(ord => `<article class="rounded-2xl border border-slate-200 dark:border-white/10 p-4"><div class="flex flex-wrap items-center justify-between gap-2"><div><div class="text-xs font-bold text-slate-800 dark:text-white">Ordonnance #ORD-${String(ord.id).padStart(4, '0')}</div><div class="mt-1 text-[10px] text-slate-500">${safe(ord.date_creation_str || '')} · ${safe(ord.medecin_nom || 'Praticien')} (${safe(ord.medecin_specialite || 'Médecine')}) · ${safe(ord.statut || 'Valide')}</div></div><button type="button" onclick="openPrescriptionModal(${Number(ord.id)}, ${Number(currentTab.patientId)}, ${Number(c.id_passage)}, ${Number(c.id_consultation || 0)})" class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-slate-900 dark:bg-brand-neon text-white dark:text-black text-[10px] font-bold"><i class="ph-bold ph-file-pdf"></i>Ouvrir le PDF</button></div><div class="mt-3 space-y-1.5">${(ord.lignes || []).map(line => `<div class="text-[11px] text-slate-600 dark:text-slate-300"><strong>${safe(line.medicament || 'Médicament')}</strong> · ${safe(line.posologie || 'Posologie non renseignée')} · ${safe(line.duree || '-')} · Qté ${safe(line.quantite ?? '-')}</div>`).join('') || '<span class="text-[11px] text-slate-400">Aucun médicament détaillé.</span>'}</div></article>`).join('')
                        : '<p class="text-[11px] italic text-slate-400">Aucune ordonnance liée à ce passage.</p>';

                    const documents = Array.isArray(result.documents) ? result.documents : [];
                    const documentsHtml = documents.length
                        ? documents.map(doc => {
                            const format = doc.type_modele === 'HTML' ? 'html' : 'pdf';
                            const titleText = doc.nom_fichier || doc.titre || 'Document archivé';
                            const labelText = doc.type_modele === 'Livret' ? 'Livret PDF' : `Compte rendu${doc.type_modele ? ` · ${doc.type_modele}` : ''}`;
                            return `<article class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 p-4 rounded-2xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/10"><div class="min-w-0"><div class="text-xs font-bold text-slate-800 dark:text-white break-words">${safe(titleText)}</div><div class="mt-1 text-[10px] text-slate-500">${safe(labelText)} · ${safe(doc.date_heure_str || '')} · ${safe(doc.medecin_signataire || 'Signataire non renseigné')}</div></div><button type="button" onclick="openArchivedDocumentPreview(${Number(doc.id)}, '${format}')" class="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-brand-neon text-black text-[10px] font-bold"><i class="ph-bold ph-file-pdf"></i>Ouvrir le document</button></article>`;
                        }).join('')
                        : '<p class="text-[11px] italic text-slate-400">Aucun compte rendu ou livret archivé pour ce passage.</p>';

                    if (title) title.textContent = `${currentTab.data.patient.nom || ''} ${currentTab.data.patient.prenom || ''}`.trim() || 'Archive de consultation';
                    if (subtitle) subtitle.textContent = `#PASS-${String(c.id_passage).padStart(4, '0')} · ${c.date_passage || '-'} à ${c.heure_passage || '-'} · ${c.type_passage || 'Consultation'} · ${c.statut || ''}`;
                    body.innerHTML = `
                        ${!c.id_consultation ? '<div class="p-3 rounded-xl border border-amber-200 bg-amber-50 dark:bg-amber-950/20 dark:border-amber-900/50 text-[11px] text-amber-800 dark:text-amber-200">Aucune fiche clinique DME n’a été enregistrée pour ce passage. Les archives et ordonnances disponibles restent consultables ci-dessous.</div>' : ''}
                        <div class="flex flex-wrap items-center justify-between gap-3 p-4 rounded-2xl bg-brand-neon/10 border border-brand-neon/20">
                            <div><div class="text-[10px] font-bold uppercase tracking-wider text-emerald-800 dark:text-brand-neon">Praticien</div><div class="mt-1 text-xs font-bold text-slate-800 dark:text-white">${displayValue(c.praticien)}</div></div>
                            <div class="px-3 py-1.5 rounded-full bg-white dark:bg-black/20 border border-slate-200 dark:border-white/10 text-[10px] font-bold text-slate-600 dark:text-slate-300">${safe(c.statut || 'Archive')}</div>
                        </div>
                        ${section('Constantes vitales', 'ph-heartbeat', `<div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2">${constantFields}</div>`)}
                        ${section('Dossier clinique', 'ph-notepad', `<div class="grid grid-cols-1 md:grid-cols-2 gap-2">${clinicalFields}</div>`)}
                        ${specialty ? section(`Consultation spécialisée · ${specialty.label}`, 'ph-first-aid-kit', `<div class="grid grid-cols-1 md:grid-cols-2 gap-2">${specialtyFields || field('Section spécialisée', 'Aucune information renseignée.')}</div>`) : ''}
                        ${section('Renseignements cliniques à l’admission', 'ph-clipboard-text', `<div class="grid grid-cols-1 sm:grid-cols-2 gap-2.5">${intakeFields || field('Questionnaire', 'Aucun renseignement disponible.')}</div>`)}
                        ${section('Actes & règlement du passage', 'ph-first-aid', `<div class="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-3">${amounts}</div>${actsHtml}`)}
                        ${section(`Ordonnances (${prescriptions.length})`, 'ph-pill', `<div class="space-y-3">${prescriptionsHtml}</div>`)}
                        ${section(`Comptes rendus & livrets (${documents.length})`, 'ph-files', `<div class="space-y-3">${documentsHtml}</div>`)}
                    `;
                } catch (error) {
                    if (subtitle) subtitle.textContent = 'Impossible de charger cette archive.';
                    body.innerHTML = `<div class="p-6 rounded-2xl bg-rose-50 dark:bg-rose-950/20 text-sm text-rose-700 dark:text-rose-300">${escapeDmeArchiveHtml(error.message || 'Erreur de chargement.')}</div>`;
                }
            };

            // =================================================================
            // 5. TABLEAU CR & NOUVEAU BOUTON « VISION PDF »
            // =================================================================
            function renderDmeArchivedCrTable(archivedCrs) {
                const tbody = document.getElementById('dmeArchivedCrTableBody');
                if (!tbody) return;

                if (!archivedCrs || archivedCrs.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" class="py-8 text-center text-slate-400 italic font-medium">Aucun compte rendu ou livret généré pour ce patient lors de ce passage.</td></tr>';
                    return;
                }

                tbody.innerHTML = archivedCrs.map(cr => {
                    const isLivret = cr.type_modele === 'Livret';
                    const isWord = cr.type_modele === 'Word';
                    const prestationLabel = isLivret ? String(cr.titre || '').replace(/^Livret\s*-\s*/i, '').trim() : '';

                    let badge = '';
                    let actionBtns = '';

                    if (isLivret) {
                        badge = '<span class="px-2.5 py-0.5 rounded-lg text-[10px] font-extrabold bg-brand-neon/20 text-emerald-800 dark:text-brand-neon border border-brand-neon/30">Livret PDF</span>';
                        actionBtns = `
                            <button type="button" onclick="openWordPdfPreview(${cr.id}, '${cr.nom_fichier || 'Livret'}')" class="px-3 py-1.5 rounded-xl bg-brand-neon hover:bg-brand-neonDark text-black font-extrabold text-[11px] inline-flex items-center gap-1 cursor-pointer shadow-sm">
                                <i class="ph-bold ph-book-open text-sm"></i>
                                <span>Voir Livret PDF</span>
                            </button>
                        `;
                    } else if (isWord) {
                        badge = '<span class="px-2.5 py-0.5 rounded-lg text-[10px] font-bold bg-sky-50 dark:bg-sky-950/40 text-sky-600 border border-sky-200">Word</span>';
                        actionBtns = `
                            <button type="button" onclick="openLivretModal(${cr.id})" class="px-2.5 py-1.5 rounded-xl bg-brand-neon hover:bg-brand-neonDark text-black font-extrabold text-[11px] inline-flex items-center gap-1 cursor-pointer shadow-sm" title="Créer un livret médical multipage">
                                <i class="ph-bold ph-book-open text-sm"></i>
                                <span>Création Livret</span>
                            </button>
                            <button type="button" onclick="openWordPdfPreview(${cr.id}, '${cr.nom_fichier || 'Compte_Rendu'}')" class="px-2.5 py-1.5 rounded-xl bg-[#0c1a10] hover:bg-[#1a3822] text-[#9fe870] font-bold text-[11px] inline-flex items-center gap-1 cursor-pointer border border-[#9fe870]/30">
                                <i class="ph-bold ph-file-pdf text-sm"></i>
                                <span>Vision Pdf</span>
                            </button>
                            <button type="button" onclick="openWordCrDocument(${cr.id})" class="px-2.5 py-1.5 rounded-xl bg-sky-500 hover:bg-sky-600 text-white font-bold text-[11px] inline-flex items-center gap-1 cursor-pointer">
                                <i class="ph-bold ph-microsoft-word-logo text-sm"></i>
                                <span>Ouvrir Word</span>
                            </button>
                        `;
                    } else {
                        badge = '<span class="px-2.5 py-0.5 rounded-lg text-[10px] font-bold bg-amber-50 dark:bg-amber-950/40 text-amber-600 border border-amber-200">HTML</span>';
                        actionBtns = `
                            <button type="button" onclick="openLivretModal(${cr.id})" class="px-2.5 py-1.5 rounded-xl bg-brand-neon hover:bg-brand-neonDark text-black font-extrabold text-[11px] inline-flex items-center gap-1 cursor-pointer shadow-sm">
                                <i class="ph-bold ph-book-open text-sm"></i>
                                <span>Création Livret</span>
                            </button>
                            <button type="button" onclick="openCrPreviewModal(${cr.id})" class="px-2.5 py-1.5 rounded-xl bg-slate-100 dark:bg-white/5 hover:bg-slate-200 text-slate-700 dark:text-slate-200 font-bold text-[11px] inline-flex items-center gap-1 cursor-pointer">
                                <i class="ph-bold ph-printer text-sm"></i>
                                <span>Aperçu A4</span>
                            </button>
                        `;
                    }

                    return `
                        <tr class="h-10 hover:bg-slate-50 dark:hover:bg-white/[0.02]">
                            <td class="py-1 pl-3 font-mono text-slate-400 whitespace-nowrap">${cr.date_heure_str || ''}</td>
                            <td class="py-0 font-bold text-slate-800 dark:text-slate-100 font-mono text-[11px]">
                                <div class="h-10 flex flex-col justify-center overflow-hidden pr-2 leading-none">
                                    <span class="block truncate leading-4" title="${cr.nom_fichier || cr.titre || ''}">${cr.nom_fichier || cr.titre || ''}</span>
                                    ${prestationLabel ? `<span class="block truncate text-[10px] font-semibold text-indigo-600 dark:text-indigo-300 leading-3" title="Prestation : ${prestationLabel}">Prestation : ${prestationLabel}</span>` : ''}
                                </div>
                            </td>
                            <td class="py-1 text-emerald-600 dark:text-brand-neon font-semibold">${cr.medecin_signataire || ''}</td>
                            <td class="py-1 text-slate-500">@${cr.proprietaire || 'admin'}</td>
                            <td class="py-1">${badge}</td>
                            <td class="py-1 text-right pr-4">
                                <div class="inline-flex items-center gap-1.5">
                                    ${actionBtns}
                                    <button type="button" onclick="deleteArchivedCr(${cr.id})" class="p-1.5 text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-500/10 rounded-lg transition cursor-pointer" title="Supprimer">
                                        <i class="ph-bold ph-trash text-sm"></i>
                                    </button>
                                </div>
                            </td>
                        </tr>
                    `;
                }).join('');
            }

            // Ouvre le PDF du Word dans le visionneur intégré
            let activeLivretDraftToken = null;
            function setDmeCrPreviewMode(mode) {
                const printButton = document.getElementById('btnPrintCrIframe');
                const validateButton = document.getElementById('btnValidateLivretDraft');
                if (printButton) printButton.classList.toggle('hidden', mode === 'draft');
                if (validateButton) validateButton.classList.toggle('hidden', mode !== 'draft');
            }
            window.openLivretDraftPreview = function(draftToken, nomFichier = 'Livret Médical') {
                if (!draftToken) { alert('Le brouillon du livret est introuvable.'); return; }
                const modal = portalDmeModal('dmeCrPreviewModal');
                const iframe = document.getElementById('dmeCrIframe');
                const titleEl = document.getElementById('dmeCrModalTitle');
                activeLivretDraftToken = draftToken;
                setDmeCrPreviewMode('draft');
                if (titleEl) titleEl.innerText = 'Aperçu du livret — ' + (nomFichier || 'Livret Médical');
                if (modal) { modal.classList.remove('hidden'); modal.classList.add('flex'); document.body.style.overflow = 'hidden'; }
                if (iframe) iframe.src = '/api/dme/livret/preview/' + encodeURIComponent(draftToken) + '?t=' + Date.now();
            };
            window.validateLivretDraft = async function() {
                const token = activeLivretDraftToken;
                const button = document.getElementById('btnValidateLivretDraft');
                if (!token || !button) return;
                const original = button.innerHTML;
                button.disabled = true;
                button.innerHTML = '<i class="ph-bold ph-spinner animate-spin"></i><span>Validation...</span>';
                try {
                    const response = await fetch('/api/dme/livret/validate', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({draft_token: token})
                    });
                    const result = await response.json();
                    if (!response.ok || !result.success) throw new Error(result.detail || result.message || 'Impossible de valider le livret.');
                    activeLivretDraftToken = null;
                    setDmeCrPreviewMode('archived');
                    const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                    if (currentTab) {
                        const refreshed = await fetch(`/api/dme/record/${currentTab.patientId}/${currentTab.passageId}`).then(r => r.json());
                        currentTab.data.archived_crs = refreshed.archived_crs || [];
                        renderDmeArchivedCrTable(currentTab.data.archived_crs);
                    }
                } catch (error) {
                    alert(error.message || 'Erreur lors de la validation du livret.');
                } finally {
                    button.disabled = false;
                    button.innerHTML = original;
                }
            };
            window.openWordPdfPreview = function(recordId, nomFichier) {
                const modal = portalDmeModal('dmeCrPreviewModal');
                const iframe = document.getElementById('dmeCrIframe');
                const titleEl = document.getElementById('dmeCrModalTitle');
                activeLivretDraftToken = null;
                setDmeCrPreviewMode('archived');

                if (titleEl) titleEl.innerText = "Vision Pdf : " + (nomFichier || "Compte Rendu");

                // 1. Écran de chargement esthétique immédiat pendant la conversion Word
                iframe.srcdoc = `
                    <!DOCTYPE html>
                    <html>
                    <body style="margin:0; background:#0c1a10; color:#8be140; font-family:sans-serif; display:flex; flex-direction:column; align-items:center; justify-content:center; height:100vh;">
                        <div style="font-size:32px; margin-bottom:12px; animation: spin 1s infinite linear;">⚙️</div>
                        <div style="font-size:14px; font-weight:bold;">Conversion du document Word en PDF en cours...</div>
                        <div style="font-size:11px; color:#94a3b8; margin-top:6px;">Veuillez patienter un instant.</div>
                    </body>
                    </html>
                `;
                if (modal) { modal.classList.remove('hidden'); modal.classList.add('flex'); document.body.style.overflow = 'hidden'; }

                // 2. Chargement du PDF dans l'iframe dès qu'il est prêt
                setTimeout(() => {
                    iframe.removeAttribute('srcdoc');
                    iframe.src = '/api/dme/cr/view-pdf/' + recordId + '?t=' + Date.now();
                }, 200);
            };

            // =================================================================
            // 6. DOSSIER ÉCHOGRAPHIE (PNG) & CARROUSEL INTERACTIF
            // =================================================================
            function echoImageTypeLabel(type) {
                return type === 'radiologique' ? 'Images de livret radiologique' : type === 'planche_trois' ? 'Images Planche A3' : 'Images échographiques';
            }
            function echoImageGroupId(image) {
                return image.groupe_id || `legacy_${image.type_image || 'echographique'}_${image.categorie || 'Général'}`;
            }
            function getEchoImageGroups(echos) {
                const groups = new Map();
                (echos || []).forEach(image => {
                    const imageType = image.type_image || 'echographique';
                    const category = image.categorie || 'Général';
                    const groupId = echoImageGroupId(image);
                    if (!groups.has(groupId)) groups.set(groupId, {id: groupId, category, items: []});
                    groups.get(groupId).items.push(image);
                });
                return [...groups.values()].map(group => {
                    group.items.sort((a, b) => (Number(a.ordre_affichage || 0) - Number(b.ordre_affichage || 0)) || ((Date.parse(a.created_at || '') || 0) - (Date.parse(b.created_at || '') || 0)) || (Number(a.id) - Number(b.id)));
                    group.types = [...new Set(group.items.map(image => image.type_image || 'echographique'))];
                    group.label = `${group.types.map(echoImageTypeLabel).join(' + ')} — ${group.category}`;
                    group.addedAt = Math.min(...group.items.map(image => Date.parse(image.created_at || '') || Number(image.id || 0)));
                    return group;
                }).sort((a, b) => a.addedAt - b.addedAt);
            }
            function renderEchoGallery() {
                const grid = document.getElementById('dmeEchoGalleryGrid');
                if (!grid) return;

                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const echos = (currentTab && currentTab.data && currentTab.data.echographies) ? currentTab.data.echographies : [];

                activeCarouselImages = echos;

                if (echos.length === 0) {
                    grid.innerHTML = `
                        <div class="col-span-full py-12 text-center text-slate-400">
                            <i class="ph-bold ph-image text-3xl opacity-30 mx-auto mb-2"></i>
                            <p class="text-xs">Aucun cliché échographique importé pour cette consultation.</p>
                            <p class="text-[11px] opacity-70 mt-0.5">Cliquez sur « Importer Clichés PNG » ci-dessus.</p>
                        </div>
                    `;
                    return;
                }

                const orderedGroups = getEchoImageGroups(echos);
                activeCarouselImages = orderedGroups.flatMap(group => group.items);
                let idx = 0;
                grid.innerHTML = orderedGroups.map(({id: groupId, label: typeLabel, category, items}) => {
                    const categoryArg = encodeURIComponent(category);
                    const groupIdArg = echoManagerEscape(groupId);
                    const cards = items.map(ec => {
                        const imageIndex = idx++;
                        return `
                    <div class="group relative bg-slate-50 dark:bg-black/30 rounded-2xl p-2 border border-slate-200 dark:border-white/10 shadow-xs flex flex-col justify-between overflow-hidden">
                        <div class="relative w-full aspect-video rounded-xl overflow-hidden bg-black flex items-center justify-center cursor-pointer" onclick="openEchoCarouselAtIndex(${imageIndex})">
                            <img src="/api/dme/echographies/file/${ec.id}" class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300">
                            <div class="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 flex items-center justify-center gap-2 transition-opacity">
                                <span class="p-2 rounded-xl bg-white/20 text-white text-sm"><i class="ph-bold ph-arrows-out-simple"></i></span>
                            </div>
                        </div>

                        <div class="pt-2 px-1 flex items-center justify-between text-[11px]">
                            <span class="font-bold text-slate-700 dark:text-slate-300 truncate max-w-[150px]" title="${ec.nom_fichier}">${ec.nom_fichier || 'Image'}</span>
                            <button type="button" onclick="deleteEchographie(${ec.id})" class="text-rose-500 hover:text-rose-700 p-1 rounded-lg" title="Supprimer">
                                <i class="ph-bold ph-trash"></i>
                            </button>
                        </div>
                    </div>
                `;
                    }).join('');
                    const managerButton = `<button type="button" onclick="openEchoManagerModal('${groupIdArg}')" class="shrink-0 inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50 px-2.5 py-1.5 text-[10px] font-bold normal-case text-indigo-700 hover:bg-indigo-600 hover:text-white dark:border-indigo-400/20 dark:bg-indigo-950/30 dark:text-indigo-200"><i class="ph-bold ph-arrows-out-simple"></i>Gérer ce groupe</button>`;
                    const addImagesButton = `<button type="button" onclick="addDprintImagesToEchoGroup('${groupIdArg}')" class="shrink-0 inline-flex items-center gap-1.5 rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-1.5 text-[10px] font-bold normal-case text-emerald-700 hover:bg-emerald-600 hover:text-white dark:border-emerald-400/20 dark:bg-emerald-950/30 dark:text-emerald-200"><i class="ph-bold ph-plus-circle"></i>Ajouter des images</button>`;
                    const deleteGroupButton = `<button type="button" onclick="deleteEchoImageGroup('${groupIdArg}', decodeURIComponent('${categoryArg}'))" class="shrink-0 inline-flex items-center gap-1.5 rounded-lg border border-rose-200 bg-rose-50 px-2.5 py-1.5 text-[10px] font-bold normal-case text-rose-700 hover:bg-rose-600 hover:text-white dark:border-rose-400/20 dark:bg-rose-950/30 dark:text-rose-200"><i class="ph-bold ph-trash"></i>Supprimer le groupe</button>`;
                    return `<section class="col-span-full"><div class="flex flex-wrap items-center gap-2 mb-3 mt-2">${managerButton}${addImagesButton}${deleteGroupButton}<h4 class="text-xs font-extrabold text-slate-700 dark:text-slate-200 uppercase tracking-wide">${typeLabel} <span class="text-slate-400 normal-case">— ${category}</span></h4></div><div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-4">${cards}</div></section>`;
                }).join('');
            }

            let echoManagerSourceGroups = [];
            let echoManagerDragId = null;
            const echoManagerEscape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
            let echoManagerOriginalParent = null;
            let echoManagerOriginalNextSibling = null;
            let echoManagerOriginalStyle = null;
            window.openEchoManagerModal = function(groupId = '') {
                const modal = document.getElementById('echoManagerModal');
                if (!modal) return;
                if (modal.parentElement !== document.body) {
                    echoManagerOriginalParent = modal.parentElement;
                    echoManagerOriginalNextSibling = modal.nextSibling;
                    echoManagerOriginalStyle = modal.getAttribute('style');
                    document.body.appendChild(modal);
                }
                modal.style.position = 'fixed';
                modal.style.inset = '0';
                modal.style.width = '100vw';
                modal.style.height = '100vh';
                modal.style.zIndex = '2147483647';
                modal.classList.remove('hidden');
                window.renderEchoManager();
                const groupSelect = document.getElementById('echoManagerGroup');
                if ([...groupSelect.options].some(option => option.value === groupId)) {
                    groupSelect.value = groupId;
                    window.renderEchoManager();
                }
            };
            window.closeEchoManagerModal = function() {
                const modal = document.getElementById('echoManagerModal');
                if (!modal) return;
                modal.classList.add('hidden');
                if (echoManagerOriginalParent && echoManagerOriginalParent.isConnected && modal.parentElement === document.body) {
                    echoManagerOriginalParent.insertBefore(modal, echoManagerOriginalNextSibling?.parentNode === echoManagerOriginalParent ? echoManagerOriginalNextSibling : null);
                }
                if (echoManagerOriginalStyle === null) modal.removeAttribute('style');
                else if (echoManagerOriginalStyle !== null) modal.setAttribute('style', echoManagerOriginalStyle);
                echoManagerOriginalParent = null;
                echoManagerOriginalNextSibling = null;
                echoManagerOriginalStyle = null;
            };
            window.renderEchoManager = function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab) return;
                const echos = currentTab.data?.echographies || [];
                const groups = getEchoImageGroups(echos);
                const groupSelect = document.getElementById('echoManagerGroup');
                const currentGroupId = groupSelect.value;
                groupSelect.innerHTML = groups.map(group => `<option value="${echoManagerEscape(group.id)}">${echoManagerEscape(group.label)}</option>`).join('');
                const selectedGroup = groups.find(group => group.id === currentGroupId) || groups[0];
                if (selectedGroup) groupSelect.value = selectedGroup.id;
                const groupId = selectedGroup?.id || '';
                const category = selectedGroup?.category || '';
                const images = selectedGroup?.items || [];
                const grid = document.getElementById('echoManagerGrid');
                if (!images.length) {
                    grid.innerHTML = '<div class="col-span-full py-16 text-center text-slate-400"><i class="ph-bold ph-images text-4xl"></i><p class="mt-2 text-sm">Aucune image dans ce groupe.</p><p class="text-xs mt-1">Utilisez « Importer d’un autre groupe » pour y ajouter des images.</p></div>';
                } else {
                    grid.innerHTML = images.map((ec, index) => `
                        <article data-echo-id="${ec.id}" class="relative flex flex-col rounded-2xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/25 overflow-hidden shadow-sm transition">
                            <div class="relative w-full min-h-44 max-h-[48vh] bg-black/90 flex items-center justify-center overflow-hidden p-2">
                                <img src="/api/dme/echographies/file/${ec.id}" class="max-w-full max-h-[46vh] w-auto h-auto object-contain" alt="${echoManagerEscape(ec.nom_fichier)}">
                                <button type="button" data-echo-position="${index + 1}" onclick="openEchoManagerPositionMenu(event, ${ec.id})" class="absolute top-2 left-2 z-20 min-w-8 h-8 px-2 rounded-lg bg-black/75 hover:bg-indigo-600 text-white text-[10px] font-extrabold shadow-lg" title="Choisir la position de cette image">${index + 1}</button>
                                <button type="button" onpointerdown="echoManagerPointerDown(event, ${ec.id})" onclick="event.preventDefault();event.stopPropagation()" class="absolute top-2 right-2 z-20 w-9 h-9 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white touch-none select-none cursor-grab active:cursor-grabbing shadow-lg" title="Maintenir et glisser pour changer l’ordre"><i class="ph-bold ph-dots-six-vertical"></i></button>
                            </div>
                            <div class="min-w-0 px-2 pt-2"><div class="flex items-center justify-between gap-1"><p class="truncate text-[11px] font-bold text-slate-800 dark:text-white" title="${echoManagerEscape(ec.nom_fichier || 'Image')}">${echoManagerEscape(ec.nom_fichier || 'Image')}</p><span class="shrink-0 rounded-md bg-slate-200 px-1.5 py-0.5 text-[8px] font-bold text-slate-600 dark:bg-white/10 dark:text-slate-300">${(ec.type_image || 'echographique') === 'radiologique' ? 'Radiologique' : (ec.type_image || '') === 'planche_trois' ? 'Planche A3' : 'Échographique'}</span></div><p class="truncate text-[9px] text-slate-500" title="${echoManagerEscape(ec.date_heure_str || '')}">${echoManagerEscape(ec.date_heure_str || '')}</p></div>
                            <div class="flex items-center justify-between gap-1 p-2">
                                <span class="px-1 text-[9px] font-semibold text-slate-400">Position</span>
                                <div class="flex items-center gap-0.5">
                                    <button type="button" onclick="moveEchoManagerCard(${ec.id}, 'first')" class="w-7 h-7 rounded-md hover:bg-indigo-100 text-indigo-700" title="Déplacer en premier"><i class="ph-bold ph-caret-double-up"></i></button>
                                    <button type="button" onclick="moveEchoManagerCard(${ec.id}, 'up')" class="w-7 h-7 rounded-md hover:bg-indigo-100 text-indigo-700" title="Monter"><i class="ph-bold ph-caret-up"></i></button>
                                    <button type="button" onclick="moveEchoManagerCard(${ec.id}, 'down')" class="w-7 h-7 rounded-md hover:bg-indigo-100 text-indigo-700" title="Descendre"><i class="ph-bold ph-caret-down"></i></button>
                                    <button type="button" onclick="moveEchoManagerCard(${ec.id}, 'last')" class="w-7 h-7 rounded-md hover:bg-indigo-100 text-indigo-700" title="Déplacer en dernier"><i class="ph-bold ph-caret-double-down"></i></button>
                                </div>
                                <button type="button" onclick="deleteEchographie(${ec.id})" class="w-7 h-7 rounded-md text-rose-500 hover:bg-rose-50" title="Supprimer"><i class="ph-bold ph-trash"></i></button>
                            </div>
                        </article>`).join('');
                }
                const positionMenu = document.getElementById('echoManagerPositionMenu');
                if (positionMenu) positionMenu.classList.add('hidden');
                echoManagerSourceGroups = groups.filter(group => group.id !== groupId);
                const sourceSelect = document.getElementById('echoManagerSourceGroup');
                sourceSelect.innerHTML = echoManagerSourceGroups.map((group, i) => `<option value="${i}">${echoManagerEscape(group.label)}</option>`).join('');
                if (!echoManagerSourceGroups.length) sourceSelect.innerHTML = '<option value="">Aucun autre groupe disponible</option>';
                window.renderEchoManagerSources();
            };
            window.toggleEchoImportPanel = function() {
                const panel = document.getElementById('echoManagerImportPanel');
                panel.classList.toggle('hidden');
                window.renderEchoManagerSources();
            };
            window.renderEchoManagerSources = function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const grid = document.getElementById('echoManagerSourceGrid');
                if (!grid || !currentTab) return;
                const group = echoManagerSourceGroups[Number(document.getElementById('echoManagerSourceGroup').value)];
                const targetGroupId = document.getElementById('echoManagerGroup').value;
                if (!group) { grid.innerHTML = '<p class="text-xs text-slate-500">Aucun autre groupe à importer.</p>'; return; }
                const targetPaths = new Set((currentTab.data.echographies || []).filter(ec => echoImageGroupId(ec) === targetGroupId).map(ec => ec.chemin_image));
                const images = group.items.filter(ec => !targetPaths.has(ec.chemin_image));
                grid.innerHTML = images.length ? images.map(ec => `<label class="flex items-center gap-2 p-2 rounded-xl border border-indigo-100 dark:border-white/10 bg-white dark:bg-black/20 cursor-pointer"><input type="checkbox" class="echo-manager-import-check h-4 w-4" value="${ec.id}"><img src="/api/dme/echographies/file/${ec.id}" class="w-14 h-12 object-contain bg-black rounded-lg"><span class="min-w-0 text-[10px] font-semibold truncate">${echoManagerEscape(ec.nom_fichier)}</span></label>`).join('') : '<p class="text-xs text-slate-500">Aucune nouvelle image disponible dans ce groupe.</p>';
                document.getElementById('echoManagerImportHint').textContent = `${images.length} image(s) disponible(s)`;
            };
            window.importSelectedEchoManagerImages = async function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const imageIds = [...document.querySelectorAll('.echo-manager-import-check:checked')].map(input => Number(input.value));
                if (!currentTab || !imageIds.length) { alert('Sélectionnez au moins une image à importer.'); return; }
                const response = await fetch('/api/dme/echographies/import', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id_patient:currentTab.patientId, id_passage:currentTab.passageId, image_ids:imageIds, groupe_id:document.getElementById('echoManagerGroup').value})}).then(r => r.json());
                if (!response.success) { alert(response.detail || response.message || 'Impossible d’importer ces images.'); return; }
                await refreshEchoManagerData(currentTab);
                alert(response.message || 'Images importées.');
            };
            window.saveEchoManagerOrder = async function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab) return;
                const imageIds = [...document.querySelectorAll('#echoManagerGrid [data-echo-id]')].map(card => Number(card.dataset.echoId));
                const response = await fetch('/api/dme/echographies/reorder', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id_patient:currentTab.patientId, id_passage:currentTab.passageId, groupe_id:document.getElementById('echoManagerGroup').value, image_ids:imageIds})}).then(r => r.json());
                if (!response.success) { alert(response.detail || response.message || 'Impossible d’enregistrer cet ordre.'); return; }
                await refreshEchoManagerData(currentTab);
                alert(response.message || 'Ordre enregistré.');
            };
            async function refreshEchoManagerData(currentTab) {
                const ref = await fetch(`/api/dme/record/${currentTab.patientId}/${currentTab.passageId}`).then(r => r.json());
                currentTab.data.echographies = ref.echographies || [];
                renderEchoGallery();
                window.renderEchoManager();
            }
            window.echoManagerPointerDown = function(event, imageId) {
                event.preventDefault(); event.stopPropagation(); echoManagerDragId = String(imageId);
                document.addEventListener('pointermove', window.echoManagerPointerMove);
                document.addEventListener('pointerup', window.echoManagerPointerUp, {once:true});
                document.addEventListener('pointercancel', window.echoManagerPointerUp, {once:true});
            };
            window.echoManagerPointerMove = function(event) {
                if (!echoManagerDragId) return;
                event.preventDefault();
                const grid = document.getElementById('echoManagerGrid');
                const gridRect = grid.getBoundingClientRect();
                const edgeSize = 72;
                if (event.clientY < gridRect.top + edgeSize) grid.scrollTop -= 28;
                else if (event.clientY > gridRect.bottom - edgeSize) grid.scrollTop += 28;
                const cards = [...grid.querySelectorAll('[data-echo-id]')];
                const source = cards.find(card => card.dataset.echoId === echoManagerDragId);
                const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('#echoManagerGrid [data-echo-id]');
                if (!source || !target || source === target) return;
                const rect = target.getBoundingClientRect();
                const sourceRect = source.getBoundingClientRect();
                const sameRow = Math.abs(rect.top - sourceRect.top) < rect.height / 2;
                const after = sameRow ? event.clientX > rect.left + rect.width / 2 : event.clientY > rect.top + rect.height / 2;
                grid.insertBefore(source, after ? target.nextElementSibling : target);
            };
            window.moveEchoManagerCard = function(imageId, direction) {
                const grid = document.getElementById('echoManagerGrid');
                const cards = [...grid.querySelectorAll('[data-echo-id]')];
                const card = cards.find(item => item.dataset.echoId === String(imageId));
                if (!card || cards.length < 2) return;
                const index = cards.indexOf(card);
                if (direction === 'first') grid.insertBefore(card, cards[0]);
                else if (direction === 'last') grid.appendChild(card);
                else if (direction === 'up' && index > 0) grid.insertBefore(card, cards[index - 1]);
                else if (direction === 'down' && index < cards.length - 1) grid.insertBefore(cards[index + 1], card);
                window.refreshEchoManagerPositions();
            };
            window.refreshEchoManagerPositions = function() {
                document.querySelectorAll('#echoManagerGrid [data-echo-id]').forEach((card, index) => {
                    const badge = card.querySelector('[data-echo-position]');
                    if (badge) { badge.dataset.echoPosition = String(index + 1); badge.textContent = String(index + 1); }
                });
            };
            window.openEchoManagerPositionMenu = function(event, imageId) {
                event.preventDefault(); event.stopPropagation();
                const menu = document.getElementById('echoManagerPositionMenu');
                const cards = [...document.querySelectorAll('#echoManagerGrid [data-echo-id]')];
                if (!menu || !cards.length) return;
                menu.innerHTML = `<div class="sticky top-0 px-2 py-1.5 text-[10px] font-bold uppercase tracking-wide text-slate-500 bg-white dark:bg-slate-900">Déplacer vers</div>` + cards.map((card, index) => {
                    const selected = card.dataset.echoId === String(imageId);
                    return `<button type="button" onclick="setEchoManagerPosition(${Number(imageId)}, ${index + 1})" class="block w-full rounded-lg px-2 py-1.5 text-left text-xs font-semibold ${selected ? 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/50 dark:text-indigo-200' : 'text-slate-700 hover:bg-indigo-50 dark:text-slate-200 dark:hover:bg-white/10'}">Image ${index + 1}${selected ? ' · actuelle' : ''}</button>`;
                }).join('');
                menu.classList.remove('hidden');
                const rect = event.currentTarget.getBoundingClientRect();
                const width = 144, height = Math.min(256, cards.length * 36 + 40);
                menu.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`;
                menu.style.top = `${Math.max(8, Math.min(rect.bottom + 4, window.innerHeight - height - 8))}px`;
            };
            window.setEchoManagerPosition = function(imageId, position) {
                const grid = document.getElementById('echoManagerGrid');
                const cards = [...grid.querySelectorAll('[data-echo-id]')];
                const card = cards.find(item => item.dataset.echoId === String(imageId));
                const targetIndex = Math.max(0, Math.min(cards.length - 1, Number(position) - 1));
                if (card && cards.length) {
                    const withoutCard = cards.filter(item => item !== card);
                    const reference = withoutCard[targetIndex] || null;
                    grid.insertBefore(card, reference);
                    window.refreshEchoManagerPositions();
                }
                document.getElementById('echoManagerPositionMenu')?.classList.add('hidden');
            };
            document.addEventListener('click', function(event) {
                const menu = document.getElementById('echoManagerPositionMenu');
                if (menu && !menu.classList.contains('hidden') && !menu.contains(event.target) && !event.target.closest('[data-echo-position]')) menu.classList.add('hidden');
            });
            window.echoManagerPointerUp = function() {
                document.removeEventListener('pointermove', window.echoManagerPointerMove);
                document.removeEventListener('pointerup', window.echoManagerPointerUp);
                document.removeEventListener('pointercancel', window.echoManagerPointerUp);
                echoManagerDragId = null;
                window.refreshEchoManagerPositions();
            };

            window.handleEchoFilesUpload = async function(event) {
                const incomingFiles = Array.from(event.target.files || []);
                if (appendNextEchoUpload) pendingEchoUploadFiles = [...pendingEchoUploadFiles, ...incomingFiles];
                else {
                    pendingEchoUploadFiles = incomingFiles;
                    pendingEchoUploadCanAddDprint = false;
                }
                appendNextEchoUpload = false;
                if (!incomingFiles.length && !pendingEchoUploadFiles.length) return;
                pendingEchoUploadPreviewUrls.forEach(url => URL.revokeObjectURL(url));
                pendingEchoUploadPreviewUrls = pendingEchoUploadFiles.map(file => URL.createObjectURL(file));
                const list = document.getElementById('echoUploadConfirmFiles');
                list.innerHTML = pendingEchoUploadFiles.map((file, index) => `<label class="group relative flex cursor-pointer flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm transition hover:border-emerald-400 hover:shadow-md dark:border-white/10 dark:bg-[#121c15]"><span class="relative grid h-36 place-items-center overflow-hidden bg-slate-900 sm:h-40"><img src="${pendingEchoUploadPreviewUrls[index]}" alt="Aperçu de ${echoManagerEscape(file.name)}" loading="lazy" class="h-full w-full object-contain"><span class="absolute left-2 top-2"><input type="checkbox" data-echo-upload-index="${index}" checked class="h-5 w-5 cursor-pointer rounded accent-emerald-600" aria-label="Importer ${echoManagerEscape(file.name)}"></span></span><span class="min-w-0 p-2.5"><span class="block truncate text-[10px] font-bold text-slate-700 dark:text-slate-200" title="${echoManagerEscape(file.name)}">${echoManagerEscape(file.name)}</span><span class="mt-1 block text-[10px] text-slate-400">${(file.size / 1024).toFixed(0)} Ko</span></span></label>`).join('');
                list.onchange = window.updateEchoUploadSelection;
                window.updateEchoUploadSelection();
                document.getElementById('btnAddMoreDprintImages')?.classList.toggle('hidden', !pendingEchoUploadCanAddDprint);
                document.getElementById('echoUploadConfirmModal').classList.remove('hidden');
                document.getElementById('echoUploadConfirmModal').classList.add('flex');
                event.target.value = '';
            };

            window.updateEchoUploadSelection = function() {
                const list = document.getElementById('echoUploadConfirmFiles');
                if (!list) return;
                const selected = list.querySelectorAll('[data-echo-upload-index]:checked').length;
                const total = pendingEchoUploadFiles.length;
                const count = document.getElementById('echoUploadConfirmCount');
                if (count) count.textContent = `${selected} image(s) sélectionnée(s) sur ${total}. Cochez les images à importer et vérifiez le type et la catégorie.`;
                const button = document.getElementById('btnConfirmEchoUpload');
                if (button) button.disabled = selected === 0;
            };

            window.setEchoUploadSelection = function(checked) {
                document.querySelectorAll('#echoUploadConfirmFiles [data-echo-upload-index]').forEach(input => { input.checked = Boolean(checked); });
                window.updateEchoUploadSelection();
            };

            window._clinivaDmePrepareDprintAppend = function() {
                appendNextEchoUpload = true;
                pendingEchoUploadCanAddDprint = true;
            };

            window.addMoreDprintImagesToEchoUpload = function() {
                const selectedIndexes = [...document.querySelectorAll('#echoUploadConfirmFiles [data-echo-upload-index]:checked')].map(input => Number(input.dataset.echoUploadIndex));
                pendingEchoUploadFiles = selectedIndexes.map(index => pendingEchoUploadFiles[index]).filter(Boolean);
                pendingEchoUploadPreviewUrls.forEach(url => URL.revokeObjectURL(url));
                pendingEchoUploadPreviewUrls = [];
                appendNextEchoUpload = true;
                pendingEchoUploadCanAddDprint = true;
                const modal = document.getElementById('echoUploadConfirmModal');
                modal.classList.add('hidden');
                modal.classList.remove('flex');
                window.openDmeDicomPrint();
            };

            window.cancelEchoUploadConfirmation = function() {
                pendingEchoUploadFiles = [];
                pendingEchoUploadPreviewUrls.forEach(url => URL.revokeObjectURL(url));
                pendingEchoUploadPreviewUrls = [];
                appendNextEchoUpload = false;
                pendingEchoUploadCanAddDprint = false;
                pendingEchoUploadTargetGroupId = null;
                document.getElementById('btnAddMoreDprintImages')?.classList.add('hidden');
                const list = document.getElementById('echoUploadConfirmFiles');
                if (list) list.innerHTML = '';
                const modal = document.getElementById('echoUploadConfirmModal');
                modal.classList.add('hidden');
                modal.classList.remove('flex');
            };

            window.confirmEchoFilesUpload = async function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const selectedIndexes = [...document.querySelectorAll('#echoUploadConfirmFiles [data-echo-upload-index]:checked')].map(input => Number(input.dataset.echoUploadIndex));
                const selectedFiles = selectedIndexes.map(index => pendingEchoUploadFiles[index]).filter(Boolean);
                if (!currentTab || !selectedFiles.length) return;
                const button = document.getElementById('btnConfirmEchoUpload');
                const originalText = button.innerHTML;
                button.disabled = true;
                button.innerHTML = '<i class="ph-bold ph-spinner animate-spin mr-1"></i>Importation…';
                const formData = new FormData();
                formData.append('type_image', document.getElementById('echoUploadConfirmType').value);
                formData.append('categorie', document.getElementById('echoUploadConfirmCategory').value);
                if (pendingEchoUploadTargetGroupId) formData.append('groupe_id', pendingEchoUploadTargetGroupId);
                selectedFiles.forEach(file => formData.append('files', file));
                try {
                    const url = `/api/dme/echographies/upload?id_patient=${currentTab.patientId}&id_passage=${currentTab.passageId}`;
                    const response = await fetch(url, {method: 'POST', body: formData});
                    const result = await response.json();
                    if (!response.ok || !result.success) throw new Error(result.detail || result.message || 'Importation impossible.');
                    pendingEchoUploadFiles = [];
                    pendingEchoUploadTargetGroupId = null;
                    window.cancelEchoUploadConfirmation();
                    const ref = await fetch(`/api/dme/record/${currentTab.patientId}/${currentTab.passageId}`).then(r => r.json());
                    currentTab.data.echographies = ref.echographies || [];
                    renderEchoGallery();
                    if (!document.getElementById('echoManagerModal')?.classList.contains('hidden')) window.renderEchoManager();
                    alert('✅ ' + result.message);
                } catch (error) {
                    alert(error.message || 'Erreur lors de l’importation des images.');
                } finally {
                    button.disabled = false;
                    button.innerHTML = originalText;
                }
            };

            window.deleteEchographie = async function(echoId) {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!confirm("Voulez-vous supprimer ce cliché échographique ?")) return;

                try {
                    const res = await fetch('/api/dme/echographies/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id: echoId })
                    }).then(r => r.json());

                    if (res.success && currentTab) {
                        const ref = await fetch(`/api/dme/record/${currentTab.patientId}/${currentTab.passageId}`).then(r => r.json());
                        currentTab.data.echographies = ref.echographies || [];
                        renderEchoGallery();
                        if (!document.getElementById('echoManagerModal')?.classList.contains('hidden')) window.renderEchoManager();
                    }
                } catch(e) {
                    alert("Erreur de suppression.");
                }
            };

            window.deleteEchoImageGroup = async function(groupId, category) {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab || currentTab.mode !== 'execution' || currentTab.data?.dme?.verrouille) {
                    alert('La suppression est disponible uniquement dans une consultation ouverte en modification.');
                    return;
                }
                const groupImages = (currentTab.data?.echographies || []).filter(image =>
                    echoImageGroupId(image) === groupId
                );
                if (!groupImages.length) return;
                if (!confirm(`Supprimer le groupe « ${category} » et ses ${groupImages.length} image(s) de ce passage, quel que soit leur type ? Cette action est définitive.`)) return;

                try {
                    const response = await fetch('/api/dme/echographies/delete-group', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({id_patient: currentTab.patientId, id_passage: currentTab.passageId, groupe_id: groupId})
                    });
                    const result = await response.json();
                    if (!response.ok || !result.success) throw new Error(result.detail || result.message || 'La suppression du groupe a échoué.');
                    const refreshed = await fetch(`/api/dme/record/${currentTab.patientId}/${currentTab.passageId}`).then(r => r.json());
                    currentTab.data.echographies = refreshed.echographies || [];
                    renderEchoGallery();
                    if (!document.getElementById('echoManagerModal')?.classList.contains('hidden')) window.renderEchoManager();
                } catch (error) {
                    alert(error.message || 'Impossible de supprimer ce groupe.');
                }
            };

            // CARROUSEL INTERACTIF PLEIN ÉCRAN
            window.startEchoCarousel = function() {
                if (activeCarouselImages.length === 0) {
                    alert("Aucun cliché disponible pour le carrousel.");
                    return;
                }
                openEchoCarouselAtIndex(0);
            };

            window.openEchoCarouselAtIndex = function(idx) {
                if (!activeCarouselImages || activeCarouselImages.length === 0) return;
                currentCarouselIndex = (idx >= 0 && idx < activeCarouselImages.length) ? idx : 0;
                updateCarouselDisplay();

                const modal = document.getElementById('echoCarouselModal');
                if (modal) modal.classList.remove('hidden');

                // Écouteur clavier flèches gauche / droite
                window.onkeydown = function(e) {
                    if (e.key === "ArrowLeft") prevCarouselImage();
                    if (e.key === "ArrowRight") nextCarouselImage();
                    if (e.key === "Escape") closeEchoCarousel();
                };
            };

            window.printEchoCarouselImageA3 = async function() {
                const item = activeCarouselImages[currentCarouselIndex];
                const button = document.getElementById('carouselPrintA3Button');
                const status = document.getElementById('carouselPrintStatus');
                if (!item || !button || !status) return;
                button.disabled = true;
                status.className = 'w-full rounded-xl bg-white/10 px-3 py-2 text-center text-xs font-semibold text-white';
                status.textContent = 'Préparation de l’image et envoi à l’imprimante A3 du serveur…';
                try {
                    const imageResponse = await fetch(`/api/dme/echographies/file/${item.id}`);
                    if (!imageResponse.ok) throw new Error('Impossible de charger cette image pour l’impression.');
                    const imageBlob = await imageResponse.blob();
                    const imageData = await new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onload = () => resolve(reader.result);
                        reader.onerror = () => reject(new Error('Lecture de l’image impossible.'));
                        reader.readAsDataURL(imageBlob);
                    });
                    const response = await fetch('/api/serveur-impression/dicom-print/a3', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({image_base64: imageData, file_name: item.nom_fichier || 'Image_DME'})
                    });
                    const result = await response.json().catch(() => ({}));
                    if (!response.ok || !result.success) throw new Error(result.detail || 'Échec de l’impression A3.');
                    if (result.download_url) {
                        const link = document.createElement('a'); link.href = result.download_url; link.download = ''; document.body.appendChild(link); link.click(); link.remove();
                    }
                    status.textContent = result.message || `Image envoyée à ${result.printer || 'l’imprimante A3 du serveur'}.`;
                    status.className = 'w-full rounded-xl bg-emerald-600/90 px-3 py-2 text-center text-xs font-semibold text-white';
                } catch (error) {
                    status.textContent = error.message || 'Impossible d’imprimer cette image en A3.';
                    status.className = 'w-full rounded-xl bg-rose-600/90 px-3 py-2 text-center text-xs font-semibold text-white';
                } finally {
                    status.classList.remove('hidden');
                    button.disabled = false;
                }
            };

            function updateCarouselDisplay() {
                const item = activeCarouselImages[currentCarouselIndex];
                if (!item) return;

                document.getElementById('carouselMainImg').src = `/api/dme/echographies/file/${item.id}`;
                document.getElementById('carouselTitle').innerText = `${item.nom_fichier || 'Cliché'} (${item.date_heure_str || ''})`;
                document.getElementById('carouselCounter').innerText = `${currentCarouselIndex + 1} / ${activeCarouselImages.length}`;

                // Rendu du ruban miniature
                const ribbon = document.getElementById('carouselThumbnailsRibbon');
                if (ribbon) {
                    ribbon.innerHTML = activeCarouselImages.map((ec, i) => `
                        <img src="/api/dme/echographies/file/${ec.id}" 
                             onclick="openEchoCarouselAtIndex(${i})" 
                             class="h-12 w-16 object-cover rounded-lg cursor-pointer border-2 transition ${i === currentCarouselIndex ? 'border-brand-neon scale-105' : 'border-transparent opacity-60 hover:opacity-100'}">
                    `).join('');
                }
            }

            window.prevCarouselImage = function() {
                if (activeCarouselImages.length === 0) return;
                currentCarouselIndex = (currentCarouselIndex - 1 + activeCarouselImages.length) % activeCarouselImages.length;
                updateCarouselDisplay();
            };

            window.nextCarouselImage = function() {
                if (activeCarouselImages.length === 0) return;
                currentCarouselIndex = (currentCarouselIndex + 1) % activeCarouselImages.length;
                updateCarouselDisplay();
            };

            window.closeEchoCarousel = function() {
                const modal = document.getElementById('echoCarouselModal');
                if (modal) modal.classList.add('hidden');
                window.onkeydown = null;
            };

            // =================================================================
            // 7. FONCTIONS GÉNÉRALES DU DME
            // =================================================================
            window.calculateImc = function() {
                const poids = parseFloat(document.getElementById('dme_poids')?.value || 0);
                const taille = parseFloat(document.getElementById('dme_taille')?.value || 0);
                const imcDisplay = document.getElementById('dme_imc');

                if (poids > 0 && taille > 0) {
                    const tailleM = taille / 100;
                    const imc = (poids / (tailleM * tailleM)).toFixed(1);
                    if (imcDisplay) imcDisplay.value = imc;
                } else if (imcDisplay) {
                    imcDisplay.value = '';
                }
                const growthWeight = document.getElementById('dme_growth_weight');
                const growthHeight = document.getElementById('dme_growth_height');
                if (growthWeight && document.activeElement !== growthWeight) growthWeight.value = document.getElementById('dme_poids')?.value || '';
                if (growthHeight && document.activeElement !== growthHeight) growthHeight.value = document.getElementById('dme_taille')?.value || '';
                if (typeof window.renderDmeGrowthChart === 'function') window.renderDmeGrowthChart();
            };

            window.searchDmePrestationsLive = function() {
                const q = (document.getElementById('dmePrestationSearchInput')?.value || '').toLowerCase().trim();
                const dropdown = document.getElementById('dmePrestationSearchResultsTable');
                if (!dropdown) return;

                if (!q) {
                    dropdown.classList.add('hidden');
                    return;
                }

                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const consultations = (currentTab && currentTab.data && currentTab.data.consultations) ? currentTab.data.consultations : [];

                currentMatchingDmePrestations = consultations.filter(c => 
                    (c.designation && c.designation.toLowerCase().includes(q)) || 
                    (c.categorie && c.categorie.toLowerCase().includes(q)) ||
                    (c.specialite && c.specialite.toLowerCase().includes(q))
                ).map(c => ({
                    nom: c.designation,
                    categorie: c.categorie,
                    prix_jour: parseFloat(c.tarif || 0),
                    prix_nuit: parseFloat(c.tarif_nuit || c.tarif || 0)
                }));

                if (currentMatchingDmePrestations.length === 0) {
                    dropdown.innerHTML = '<div class="p-3 text-xs text-slate-400 italic text-center">Aucune prestation trouvée.</div>';
                } else {
                    dropdown.innerHTML = currentMatchingDmePrestations.map((r, i) => `
                        <div onclick="onSelectDmePrestationByIndex(${i})" class="p-2 hover:bg-brand-neon/10 flex items-center justify-between cursor-pointer text-xs">
                            <span class="font-bold text-slate-800 dark:text-slate-100">${r.nom}</span>
                            <div class="space-x-2 font-mono">
                                <span class="text-emerald-600 dark:text-brand-neon font-bold">☀️ ${r.prix_jour} DA</span>
                                <span class="text-purple-600 dark:text-purple-400 font-bold">🌙 ${r.prix_nuit} DA</span>
                            </div>
                        </div>
                    `).join('');
                }
                dropdown.classList.remove('hidden');
            };

            window.onSelectDmePrestationByIndex = function(index) {
                selectedDmePrestationTemp = currentMatchingDmePrestations[index];
                if (!selectedDmePrestationTemp) return;

                document.getElementById('dmePrestationSearchInput').value = selectedDmePrestationTemp.nom;
                document.getElementById('dmePrestationSearchResultsTable').classList.add('hidden');
            };

            window.autoDetectDmeTarifPeriod = function() {
                const now = new Date();
                const h = now.getHours();
                const isNight = (h >= 20 || h < 8);

                const selectPeriode = document.getElementById('dmeSelectAddPeriode');
                if (selectPeriode) selectPeriode.value = isNight ? 'Garde' : 'Jour';

                const badge = document.getElementById('dmeAutoTarifBadge');
                if (badge) {
                    badge.innerText = isNight ? "🌙 Période Garde/Nuit active" : "☀️ Période Jour active";
                    badge.className = isNight 
                        ? "text-[10px] font-bold px-2.5 py-0.5 rounded-full bg-purple-100 dark:bg-purple-950/40 text-purple-800 dark:text-purple-300" 
                        : "text-[10px] font-bold px-2.5 py-0.5 rounded-full bg-amber-100 dark:bg-amber-950/40 text-amber-800 dark:text-amber-300";
                }
            };

            window.addSelectedDmePrestationToTable = function() {
                if (!selectedDmePrestationTemp) {
                    alert("Veuillez d'abord rechercher et sélectionner une prestation.");
                    return;
                }

                const periode = document.getElementById('dmeSelectAddPeriode').value;
                const quantite = parseInt(document.getElementById('dmeInputAddQuantite').value) || 1;
                const tarif = (periode === 'Jour') ? selectedDmePrestationTemp.prix_jour : selectedDmePrestationTemp.prix_nuit;

                currentDmeActs.push({
                    nom: selectedDmePrestationTemp.nom,
                    periode: periode,
                    quantite: quantite,
                    tarif: tarif,
                    sous_total: tarif * quantite
                });

                renderDmeActsTable();
                document.getElementById('dmePrestationSearchInput').value = '';
                selectedDmePrestationTemp = null;
            };

            window.removeDmeActFromTable = function(index) {
                currentDmeActs.splice(index, 1);
                renderDmeActsTable();
            };

            function renderDmePassagePrestations(acts, fallbackType) {
                const container = document.getElementById('dme_hdr_prestations');
                if (!container) return;
                container.replaceChildren();
                const names = (Array.isArray(acts) ? acts : [])
                    .filter(act => act && typeof act === 'object')
                    .map(act => ({
                        name: String(act.nom || act.designation || act.name || '').trim(),
                        quantity: Number(act.quantite || act.quantity || 1)
                    }))
                    .filter(act => act.name);

                if (!names.length && fallbackType) names.push({ name: String(fallbackType).trim(), quantity: 1 });
                if (!names.length) {
                    const empty = document.createElement('span');
                    empty.className = 'text-[11px] text-slate-400 italic';
                    empty.textContent = 'Aucune prestation renseignée';
                    container.appendChild(empty);
                    return;
                }

                names.forEach(act => {
                    const badge = document.createElement('span');
                    badge.className = 'px-2.5 py-1 rounded-lg bg-brand-neon/15 border border-brand-neon/30 text-[11px] font-bold text-emerald-800 dark:text-brand-neon';
                    badge.textContent = `${act.name}${act.quantity > 1 ? ` × ${act.quantity}` : ''}`;
                    container.appendChild(badge);
                });
            }

            window.renderDmeActsTable = function() {
                const tbody = document.getElementById('dmeActsTableBody');
                if (!tbody) return;
                renderDmePassagePrestations(currentDmeActs);
                tbody.innerHTML = '';

                let grandTotal = 0;
                currentDmeActs.forEach((act, idx) => {
                    grandTotal += act.sous_total;
                    const tr = document.createElement('tr');
                    tr.className = "hover:bg-slate-50 dark:hover:bg-white/[0.02]";
                    tr.innerHTML = `
                        <td class="p-2.5 font-bold text-slate-800 dark:text-slate-100">${act.nom}</td>
                        <td class="p-2.5"><span class="px-2 py-0.5 rounded text-[10px] font-bold ${act.periode === 'Jour' ? 'bg-amber-100 text-amber-800' : 'bg-purple-100 text-purple-800'}">${act.periode}</span></td>
                        <td class="p-2.5 text-center font-bold">${act.quantite}</td>
                        <td class="p-2.5 font-mono">${Number(act.tarif).toLocaleString()} DA</td>
                        <td class="p-2.5 font-black text-emerald-600 dark:text-brand-neon font-mono">${Number(act.sous_total).toLocaleString()} DA</td>
                        <td class="p-2.5 text-right"><button type="button" onclick="removeDmeActFromTable(${idx})" class="text-rose-500 hover:text-rose-700 font-bold p-1">✕</button></td>
                    `;
                    tbody.appendChild(tr);
                });

                if (currentDmeActs.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" class="p-3 text-center text-slate-400 italic">Aucune prestation en cours de consultation.</td></tr>';
                }

                const totalDisplay = document.getElementById('dmeTotalActsDisplay');
                if (totalDisplay) totalDisplay.innerText = `${Number(grandTotal).toLocaleString()} DA`;
            };

            async function loadMedsForPrescription() {
                if (allMedicationsList.length === 0) {
                    try {
                        const res = await fetch('/api/medications');
                        if (!res.ok) throw new Error(`Erreur HTTP ${res.status}`);
                        const data = await res.json();
                        allMedicationsList = data.medications || [];
                    } catch(e) {
                        console.error("Erreur chargement médicaments :", e);
                    }
                }
            }

            window.openDmeMedicationModal = function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab || currentTab.mode !== 'execution' || currentTab.data?.dme?.verrouille) {
                    alert("L'ajout d'un médicament est disponible pendant l'exécution d'une consultation.");
                    return;
                }
                const form = document.getElementById('dmeMedicationForm');
                const error = document.getElementById('dmeMedicationError');
                const modal = document.getElementById('dmeAddMedicationModal');
                if (form) form.reset();
                if (error) { error.textContent = ''; error.classList.add('hidden'); }
                if (modal) { modal.classList.remove('hidden'); modal.classList.add('flex'); }
                document.getElementById('dmeMedicationName')?.focus();
            };

            window.closeDmeMedicationModal = function() {
                const modal = document.getElementById('dmeAddMedicationModal');
                if (modal) { modal.classList.add('hidden'); modal.classList.remove('flex'); }
            };

            window.saveDmeMedication = async function(event) {
                event.preventDefault();
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab || currentTab.mode !== 'execution' || currentTab.data?.dme?.verrouille) {
                    alert("La consultation est en lecture seule : aucun médicament ne peut être ajouté depuis ce dossier.");
                    closeDmeMedicationModal();
                    return;
                }

                const payload = {
                    id: null,
                    nom_commercial: document.getElementById('dmeMedicationName')?.value.trim() || '',
                    dci: document.getElementById('dmeMedicationDci')?.value.trim() || '',
                    forme: document.getElementById('dmeMedicationForm')?.value.trim() || '',
                    dosage: document.getElementById('dmeMedicationDosage')?.value.trim() || ''
                };
                const submitButton = document.getElementById('dmeMedicationSubmitButton');
                const errorBox = document.getElementById('dmeMedicationError');
                if (submitButton) submitButton.disabled = true;
                if (errorBox) { errorBox.textContent = ''; errorBox.classList.add('hidden'); }

                try {
                    const response = await fetch('/api/medications/save', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                    const result = await response.json();
                    if (!response.ok || !result.success) throw new Error(result.message || result.detail || 'Impossible d’enregistrer le médicament.');

                    let savedMedication = payload;
                    try {
                        const lookupResponse = await fetch(`/api/medications?q=${encodeURIComponent(payload.nom_commercial)}`);
                        if (lookupResponse.ok) {
                            const lookupData = await lookupResponse.json();
                            savedMedication = (lookupData.medications || []).find(medication =>
                                (medication.nom_commercial || '').trim().toLocaleUpperCase('fr') === payload.nom_commercial.toLocaleUpperCase('fr') &&
                                (medication.dci || '').trim().toLocaleLowerCase('fr') === payload.dci.toLocaleLowerCase('fr') &&
                                (medication.forme || '').trim().toLocaleLowerCase('fr') === payload.forme.toLocaleLowerCase('fr') &&
                                (medication.dosage || '').trim().toLocaleLowerCase('fr') === payload.dosage.toLocaleLowerCase('fr')
                            ) || payload;
                        }
                    } catch (lookupError) {
                        console.warn('Médicament enregistré; utilisation des données saisies pour la prescription :', lookupError);
                    }
                    const sameMedication = medication =>
                        (medication.nom_commercial || '').trim().toLocaleUpperCase('fr') === payload.nom_commercial.toLocaleUpperCase('fr') &&
                        (medication.dci || '').trim().toLocaleLowerCase('fr') === payload.dci.toLocaleLowerCase('fr') &&
                        (medication.forme || '').trim().toLocaleLowerCase('fr') === payload.forme.toLocaleLowerCase('fr') &&
                        (medication.dosage || '').trim().toLocaleLowerCase('fr') === payload.dosage.toLocaleLowerCase('fr');
                    const existingIndex = allMedicationsList.findIndex(sameMedication);
                    if (existingIndex >= 0) allMedicationsList[existingIndex] = savedMedication;
                    else allMedicationsList.push(savedMedication);

                    closeDmeMedicationModal();
                    window.selectOrdMedication(savedMedication.nom_commercial, savedMedication.dosage || '', savedMedication.forme || '');
                    if (typeof window.showToast === 'function') window.showToast('Médicament ajouté et sélectionné pour l’ordonnance.', 'success');
                } catch (error) {
                    if (errorBox) {
                        errorBox.textContent = error.message || 'Erreur lors de l’enregistrement du médicament.';
                        errorBox.classList.remove('hidden');
                    } else {
                        alert(error.message || 'Erreur lors de l’enregistrement du médicament.');
                    }
                } finally {
                    if (submitButton) submitButton.disabled = false;
                }
            };

            window.searchOrdMedicationsLive = function() {
                const q = (document.getElementById('ordMedSearchInput')?.value || '').toLowerCase().trim();
                const dropdown = document.getElementById('ordMedSearchResults');
                if (!dropdown) return;

                if (!q) {
                    dropdown.classList.add('hidden');
                    return;
                }

                const matches = allMedicationsList.filter(m => 
                    (m.nom_commercial && m.nom_commercial.toLowerCase().includes(q)) ||
                    (m.dci && m.dci.toLowerCase().includes(q))
                ).slice(0, 10);

                if (matches.length === 0) {
                    dropdown.innerHTML = '<div class="p-2.5 text-xs text-slate-400 italic">Aucun médicament trouvé.</div>';
                } else {
                    dropdown.innerHTML = matches.map(m => `
                        <div onclick="selectOrdMedication('${m.nom_commercial.replace(/'/g, "\'")}', '${(m.dosage || '').replace(/'/g, "\'")}', '${(m.forme || '').replace(/'/g, "\'")}')" class="p-2.5 hover:bg-brand-neon/10 cursor-pointer text-xs flex justify-between items-center">
                            <span class="font-bold text-slate-800 dark:text-slate-100">${m.nom_commercial} <span class="text-slate-400 font-normal">(${m.forme || ''} ${m.dosage || ''})</span></span>
                            <span class="text-[10px] text-emerald-600 font-mono">${m.dci}</span>
                        </div>
                    `).join('');
                }
                dropdown.classList.remove('hidden');
            };

            window.selectOrdMedication = function(nom, dosage, forme) {
                const label = `${nom} ${dosage} ${forme}`.trim();
                document.getElementById('ordMedSearchInput').value = label;
                document.getElementById('ordMedSearchResults').classList.add('hidden');
                document.getElementById('ordPosologieInput').focus();
            };

            window.addMedicationToPrescription = function() {
                const med = document.getElementById('ordMedSearchInput')?.value.trim();
                const poso = document.getElementById('ordPosologieInput')?.value.trim();
                const duree = document.getElementById('ordDureeInput')?.value.trim();
                const qte = parseInt(document.getElementById('ordQteInput')?.value) || 1;

                if (!med) {
                    alert("Veuillez choisir un médicament.");
                    return;
                }

                currentOrdLines.push({
                    medicament: med,
                    posologie: poso || "Selon prescription",
                    duree: duree || "-",
                    quantite: qte
                });

                document.getElementById('ordMedSearchInput').value = '';
                document.getElementById('ordPosologieInput').value = '';
                document.getElementById('ordDureeInput').value = '';
                document.getElementById('ordQteInput').value = '1';

                renderActiveOrdLines();
            };

            window.removeOrdLine = function(idx) {
                currentOrdLines.splice(idx, 1);
                renderActiveOrdLines();
            };

            function renderActiveOrdLines() {
                const tbody = document.getElementById('ordActiveLinesTableBody');
                if (!tbody) return;
                tbody.innerHTML = '';

                if (currentOrdLines.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="5" class="py-6 text-center text-slate-400 italic">Aucun médicament ajouté à cette ordonnance.</td></tr>';
                    return;
                }

                currentOrdLines.forEach((l, idx) => {
                    const tr = document.createElement('tr');
                    tr.className = "hover:bg-slate-50 dark:hover:bg-white/[0.02]";
                    tr.innerHTML = `
                        <td class="py-2.5 font-bold text-slate-800 dark:text-slate-100">${l.medicament}</td>
                        <td class="py-2.5 text-slate-600 dark:text-slate-300">${l.posologie}</td>
                        <td class="py-2.5 text-slate-600 dark:text-slate-300">${l.duree}</td>
                        <td class="py-2.5 text-center font-black text-emerald-600 dark:text-brand-neon font-mono">${l.quantite}</td>
                        <td class="py-2.5 text-right pr-2">
                            <button type="button" onclick="removeOrdLine(${idx})" class="text-rose-500 hover:text-rose-700 font-bold p-1">✕</button>
                        </td>
                    `;
                    tbody.appendChild(tr);
                });
            }

            window.saveActivePrescriptionOnly = async function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab || currentOrdLines.length === 0) {
                    alert("Veuillez ajouter au moins un médicament dans l'ordonnance.");
                    return;
                }

                const payload = {
                    id_patient: currentTab.patientId,
                    id_passage: currentTab.passageId,
                    id_consultation: currentTab.consultationId || currentTab.data?.dme?.id,
                    lignes: currentOrdLines
                };

                try {
                    const res = await fetch('/api/dme/prescriptions/save', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    }).then(r => r.json());

                    if (res.success && res.ord_id) {
                        alert("✅ " + res.message);
                        currentOrdLines = [];
                        renderActiveOrdLines();
                        await loadPatientPrescriptionsHistory(currentTab.patientId, currentTab.passageId, currentTab.consultationId || currentTab.data?.dme?.id || 0);
                    } else {
                        alert(res.message);
                    }
                } catch(e) {
                    alert("Erreur lors de l'enregistrement de l'ordonnance.");
                }
            };

            window.saveAndPrintActivePrescription = async function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab || currentOrdLines.length === 0) {
                    alert("Veuillez ajouter au moins un médicament avant de générer l'ordonnance.");
                    return;
                }

                const payload = {
                    id_patient: currentTab.patientId,
                    id_passage: currentTab.passageId,
                    id_consultation: currentTab.consultationId || currentTab.data?.dme?.id,
                    lignes: currentOrdLines
                };

                try {
                    const res = await fetch('/api/dme/prescriptions/save', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    }).then(r => r.json());

                    if (res.success && res.ord_id) {
                        currentOrdLines = [];
                        renderActiveOrdLines();
                        await loadPatientPrescriptionsHistory(currentTab.patientId, currentTab.passageId, currentTab.consultationId || currentTab.data?.dme?.id || 0);
                        openPrescriptionModal(res.ord_id, currentTab.patientId, currentTab.passageId, currentTab.consultationId || currentTab.data?.dme?.id || 0);
                    } else {
                        alert(res.message);
                    }
                } catch(e) {
                    alert("Erreur lors de la création de l'ordonnance.");
                }
            };

            window.openPrescriptionModal = function(ordId, patientId = null, passageId = null, consultationId = null) {
                document.getElementById('dmePrescriptionModalTitle').textContent = 'Ordonnance Médicale Officielle (Fond KACEL ORD.png)';
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const scopedPatientId = patientId ?? currentTab?.patientId;
                const scopedPassageId = passageId ?? currentTab?.passageId;
                const scopedConsultationId = consultationId ?? currentTab?.consultationId ?? currentTab?.data?.dme?.id ?? 0;
                if (!scopedPatientId || !scopedPassageId) {
                    alert('Impossible d’ouvrir cette ordonnance sans son patient et son passage.');
                    return;
                }
                const modal = document.getElementById('prescriptionModal');
                const iframe = document.getElementById('prescriptionIframe');
                if (iframe) iframe.src = `/api/dme/prescriptions/print/${scopedPatientId}/${scopedPassageId}/${Number(scopedConsultationId) || 0}/${ordId}`;
                if (modal) modal.classList.remove('hidden');
            };

            window.closePrescriptionModal = function() {
                const modal = document.getElementById('prescriptionModal');
                const iframe = document.getElementById('prescriptionIframe');
                if (iframe) iframe.src = 'about:blank';
                if (modal) modal.classList.add('hidden');
            };

            window.printPrescriptionIframe = function() {
                const iframe = document.getElementById('prescriptionIframe');
                if (iframe && iframe.contentWindow) {
                    iframe.contentWindow.focus();
                    iframe.contentWindow.print();
                }
            };

            async function loadPatientPrescriptionsHistory(patientId, passageId, consultationId) {
                const container = document.getElementById('ordHistoryCardsList');
                if (!container) return;

                try {
                    const res = await fetch(`/api/dme/prescriptions/${patientId}/${passageId}/${Number(consultationId) || 0}`).then(r => r.json());
                    const ords = res.prescriptions || [];

                    if (ords.length === 0) {
                        container.innerHTML = '<div class="text-xs text-slate-400 italic p-3">Aucune ordonnance antérieure pour ce patient.</div>';
                        return;
                    }

                    container.innerHTML = ords.map(o => {
                        const isSupprimee = o.statut === 'Supprimée';
                        const badgeStatut = isSupprimee
                            ? `<span class="px-2 py-0.5 rounded text-[10px] font-extrabold bg-rose-50 text-rose-600 border border-rose-200">SUPPRIMÉE</span> <span class="text-[10px] text-slate-400">(@${o.supprime_par || 'admin'} le ${o.date_suppression_str || ''})</span>`
                            : `<span class="px-2 py-0.5 rounded text-[10px] font-extrabold bg-emerald-50 text-emerald-800 border border-emerald-200">VALIDE</span>`;

                        const resumeMedicaments = (o.lignes || []).map(l => `<strong>${l.medicament}</strong> (${l.quantite} boîte)`).join(' • ');

                        return `
                        <div class="p-3.5 rounded-2xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                            <div class="space-y-1">
                                <div class="flex items-center gap-2 text-xs font-bold text-slate-800 dark:text-white">
                                    <span>#ORD-${String(o.id).padStart(4, '0')} du ${o.date_creation_str} — ${o.medecin_nom} (${o.medecin_specialite})</span>
                                    ${badgeStatut}
                                </div>
                                <div class="text-xs text-slate-600 dark:text-slate-300 leading-snug">
                                    ${resumeMedicaments || 'Aucun détail'}
                                </div>
                            </div>

                            <div class="flex items-center gap-2 flex-shrink-0">
                                <button type="button" onclick="openPrescriptionModal(${o.id}, ${patientId}, ${passageId}, ${Number(consultationId) || 0})" class="px-3.5 py-1.5 rounded-xl bg-[#0c1a10] hover:bg-[#1a3822] text-[#9fe870] font-bold text-xs inline-flex items-center gap-1.5 transition cursor-pointer">
                                    <i class="ph-bold ph-printer"></i>
                                    <span>Visualiser / Imprimer</span>
                                </button>
                                ${!isSupprimee ? `
                                <button type="button" onclick="deletePrescription(${o.id}, ${patientId}, ${passageId}, ${Number(consultationId) || 0})" class="p-2 text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-500/10 rounded-xl transition cursor-pointer" title="Annuler cette ordonnance">
                                    <i class="ph-bold ph-trash text-sm"></i>
                                </button>
                                ` : ''}
                            </div>
                        </div>
                        `;
                    }).join('');
                    const activeTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                    if (activeTab) applyDmeAccessMode(activeTab);
                } catch(e) {
                    console.error("Erreur historique ordonnances :", e);
                }
            }

            window.deletePrescription = async function(ordId, patientId, passageId, consultationId) {
                if (confirm("Voulez-vous marquer cette ordonnance comme SUPPRIMÉE ? Un filigrane d'annulation sera apposé.")) {
                    const response = await fetch('/api/dme/prescriptions/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id: ordId, id_patient: patientId, id_passage: passageId, id_consultation: Number(consultationId) || null })
                    });
                    const res = await response.json();

                    if (res.success) {
                        loadPatientPrescriptionsHistory(patientId, passageId, consultationId);
                    } else {
                        alert(res.message);
                    }
                }
            };

            // MODÈLES CR
            function normalizeDmeModalite(value) {
                return String(value || '').trim().normalize('NFD').replace(/[̀-ͯ]/g, '').toLocaleLowerCase();
            }

            window.searchDmeCrTemplatesLive = function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const input = document.getElementById('crTemplateSearchInput');
                const results = document.getElementById('crTemplateSearchResults');
                if (!currentTab || !input || !results) return;

                const modalities = new Set((currentDmeActs || currentTab.data.passage?.actes || [])
                    .map(act => normalizeDmeModalite(act?.modalite))
                    .filter(Boolean));
                const selectedTemplate = (currentTab.data.cr_templates || []).find(item => Number(item.id) === Number(document.getElementById('crTemplateSelect')?.value));
                const selectedLabel = selectedTemplate ? `${selectedTemplate.designation} · ${selectedTemplate.modalite}` : '';
                const query = input.value.trim() === selectedLabel ? '' : input.value.trim().toLocaleLowerCase();
                const matchingTemplates = (currentTab.data.cr_templates || []).filter(template => {
                    const sameModalite = modalities.has(normalizeDmeModalite(template.modalite));
                    const matchesName = !query || String(template.designation || '').toLocaleLowerCase().includes(query);
                    return sameModalite && matchesName;
                });

                if (!modalities.size) {
                    results.innerHTML = '<div class="p-3 text-xs text-amber-700 dark:text-amber-300">Aucune modalité n’est renseignée sur les prestations de ce passage.</div>';
                } else if (!matchingTemplates.length) {
                    results.innerHTML = '<div class="p-3 text-xs text-slate-500">Aucun modèle ne correspond à cette recherche pour la modalité de la prestation.</div>';
                } else {
                    results.innerHTML = `
                        <table class="w-full text-left text-xs">
                            <thead class="sticky top-0 bg-slate-50 dark:bg-black/40 text-[10px] uppercase tracking-wide text-slate-500">
                                <tr><th class="px-3 py-2">Compte rendu</th><th class="px-3 py-2">Modalité</th><th class="px-3 py-2">Format</th></tr>
                            </thead>
                            <tbody class="divide-y divide-slate-100 dark:divide-white/5">
                                ${matchingTemplates.map(template => `
                                    <tr class="hover:bg-brand-neon/10 cursor-pointer" onclick="selectDmeCrTemplate(${Number(template.id)})">
                                        <td class="px-3 py-2.5 font-bold text-slate-800 dark:text-slate-100">${escapeDmeArchiveHtml(template.designation || 'Modèle sans nom')}</td>
                                        <td class="px-3 py-2.5 text-slate-600 dark:text-slate-300">${escapeDmeArchiveHtml(template.modalite || '')}</td>
                                        <td class="px-3 py-2.5 text-slate-500">${escapeDmeArchiveHtml(template.type_modele || '')}</td>
                                    </tr>
                                `).join('')}
                            </tbody>
                        </table>`;
                }
                results.classList.remove('hidden');
            };

            window.handleDmeCrTemplateSearchInput = function() {
                const selected = document.getElementById('crTemplateSelect');
                if (selected) selected.value = '';
                window.onCrTemplateChange();
                window.searchDmeCrTemplatesLive();
            };

            window.selectDmeCrTemplate = function(templateId) {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const template = (currentTab?.data.cr_templates || []).find(item => Number(item.id) === Number(templateId));
                if (!template) return;
                const select = document.getElementById('crTemplateSelect');
                const input = document.getElementById('crTemplateSearchInput');
                const results = document.getElementById('crTemplateSearchResults');
                if (select) select.value = String(template.id);
                if (input) input.value = `${template.designation} · ${template.modalite}`;
                if (results) results.classList.add('hidden');
                window.onCrTemplateChange();
            };

            window.onCrTemplateChange = function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const select = document.getElementById('crTemplateSelect');
                const btnText = document.getElementById('crGenerateBtnText');
                if (!select || !btnText) return;

                const selectedTemplate = (currentTab?.data.cr_templates || []).find(item => Number(item.id) === Number(select.value));
                const format = selectedTemplate?.type_modele || 'Word';

                btnText.innerText = format === 'Word' ? "Générer Word" : "Générer HTML A4";
            };

            window.generateCrFromDme = async function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab) return;

                const templateSelect = document.getElementById('crTemplateSelect');
                const templateId = templateSelect ? parseInt(templateSelect.value) : null;
                const medPrescripteur = document.getElementById('crMedPrescripteurSelect')?.value || '';
                const medSignataire = document.getElementById('crMedSignataireSelect')?.value || '';

                if (!templateId) {
                    alert("Veuillez sélectionner un modèle de compte rendu.");
                    return;
                }

                const btn = document.getElementById('btnGenerateCr');
                const originalHtml = btn ? btn.innerHTML : '';
                if (btn) {
                    btn.disabled = true;
                    btn.innerHTML = '<i class="ph-bold ph-spinner animate-spin text-sm"></i> Génération...';
                }

                try {
                    const payload = {
                        id_patient: currentTab.patientId,
                        id_passage: currentTab.passageId,
                        template_id: templateId,
                        medecin_prescripteur: medPrescripteur,
                        medecin_signataire: medSignataire
                    };

                    const res = await fetch('/api/dme/cr/generate', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    }).then(r => r.json());

                    if (res.success) {
                        const ref = await fetch(`/api/dme/record/${currentTab.patientId}/${currentTab.passageId}`).then(r => r.json());
                        currentTab.data.archived_crs = ref.archived_crs || [];
                        renderDmeArchivedCrTable(currentTab.data.archived_crs);

                        if (res.type_modele === 'HTML' && res.record_id) {
                            openCrPreviewModal(res.record_id);
                        } else if (res.type_modele === 'Word') {
                            alert(`✅ Compte rendu Word généré : ${res.nom_fichier}\nOuvert automatiquement dans Microsoft Word.`);
                        }
                    } else {
                        alert(res.message);
                    }
                } catch(err) {
                    alert("Erreur lors de la génération du compte rendu : " + err.message);
                } finally {
                    if (btn) {
                        btn.disabled = false;
                        btn.innerHTML = originalHtml;
                    }
                }
            };

            window.openWordCrDocument = async function(recordId) {
                try {
                    const res = await fetch('/api/dme/cr/open-word', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id: recordId })
                    }).then(r => r.json());

                    if (!res.success) {
                        alert("❌ " + res.message);
                    }
                } catch(e) {
                    alert("Erreur lors de l'ouverture du document Word.");
                }
            };

            window.openCrPreviewModal = function(recordId) {
                const modal = portalDmeModal('dmeCrPreviewModal');
                const iframe = document.getElementById('dmeCrIframe');
                const titleEl = document.getElementById('dmeCrModalTitle');
                activeLivretDraftToken = null;
                setDmeCrPreviewMode('archived');
                if (titleEl) titleEl.innerText = "Aperçu Compte Rendu Médical A4";
                if (iframe) iframe.src = '/api/dme/cr/print/' + recordId;
                if (modal) { modal.classList.remove('hidden'); modal.classList.add('flex'); document.body.style.overflow = 'hidden'; }
            };

            window.closeCrPreviewModal = function() {
                const modal = document.getElementById('dmeCrPreviewModal');
                const iframe = document.getElementById('dmeCrIframe');
                const draftToken = activeLivretDraftToken;
                activeLivretDraftToken = null;
                if (iframe) iframe.src = 'about:blank';
                if (activeRehabPlanPdfUrl) {
                    const previousUrl = activeRehabPlanPdfUrl;
                    activeRehabPlanPdfUrl = null;
                    setTimeout(() => URL.revokeObjectURL(previousUrl), 1000);
                }
                if (modal) { modal.classList.add('hidden'); modal.classList.remove('flex'); }
                document.body.style.overflow = '';
                setDmeCrPreviewMode('archived');
                if (draftToken) {
                    fetch('/api/dme/livret/discard', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({draft_token:draftToken})}).catch(() => {});
                }
            };

            window.printCrIframe = function() {
                const iframe = document.getElementById('dmeCrIframe');
                if (iframe && iframe.contentWindow) {
                    iframe.contentWindow.focus();
                    iframe.contentWindow.print();
                }
            };

            window.deleteArchivedCr = async function(recordId) {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!confirm("Voulez-vous supprimer ce compte rendu archivé ?")) return;

                try {
                    const res = await fetch('/api/dme/cr/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id: recordId })
                    }).then(r => r.json());

                    if (res.success && currentTab) {
                        const ref = await fetch(`/api/dme/record/${currentTab.patientId}/${currentTab.passageId}`).then(r => r.json());
                        currentTab.data.archived_crs = ref.archived_crs || [];
                        renderDmeArchivedCrTable(currentTab.data.archived_crs);
                    }
                } catch(e) {
                    alert("Erreur lors de la suppression.");
                }
            };

            // ENREGISTREMENT GLOBAL DE LA CONSULTATION
            window.resetActiveDmeConsultation = async function() {
                const tab = window.activeDmeTabs.find(item => item.key === window.currentActiveDmeKey);
                if (!tab || tab.mode !== 'execution' || tab.data?.dme?.verrouille || getDmeResetPermission(tab) !== true || !(tab.consultationId || tab.data?.dme?.id)) return;
                if (tab.resetting) return;
                if (tab.examRequestState?.saving) return;
                if (!confirm('Remettre TOUTE cette consultation en cours à zéro, comme au démarrage ?\n\nLes champs cliniques, fiches spécialisées, ordonnances (y compris celle en préparation), produits consommés, comptes rendus, livrets et images ajoutés pendant cette consultation seront retirés.\n\nL’identité du patient, les anciens dossiers, les documents d’accueil et la caisse seront conservés.')) return;
                const button = document.getElementById('dmeResetConsultationButton');
                const saveButton = document.getElementById('dmeSaveConsultationButton');
                const controls = [...document.querySelectorAll('.dme-subtab-panel input, .dme-subtab-panel textarea, .dme-subtab-panel select, .dme-subtab-panel button')].map(control => [control, control.disabled]);
                controls.forEach(([control]) => control.disabled = true);
                let completed = false;
                tab.resetting = true;
                if (button) button.disabled = true;
                if (saveButton) saveButton.disabled = true;
                try {
                    const response = await fetch('/api/dme/consultation/reset', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({id_patient: Number(tab.patientId), id_passage: Number(tab.passageId), id_consultation: Number(tab.consultationId || tab.data?.dme?.id)})
                    });
                    const result = await response.json();
                    if (!response.ok || !result.success) throw new Error(result.detail || result.message || 'Réinitialisation impossible.');
                    completed = true;
                    delete tab.examRequestState;
                    tab.data = result.data;
                    tab.consultationId = result.data.dme?.id || tab.consultationId;
                    tab.mode = result.data.dme?.verrouille ? 'lecture' : 'execution';
                    tab.currentSubTab = 'consultation';
                    if (window.currentActiveDmeKey === tab.key) {
                        document.querySelectorAll('.dme-subtab-panel input, .dme-subtab-panel textarea, .dme-subtab-panel select').forEach(field => {
                            if (field.tagName === 'SELECT') field.selectedIndex = 0;
                            else if (field.type === 'checkbox' || field.type === 'radio') field.checked = field.defaultChecked;
                            else field.value = field.defaultValue || '';
                        });
                        currentOrdLines = [];
                        activeLivretDraftToken = null;
                        activeCarouselImages = [];
                        currentCarouselIndex = 0;
                        pendingEchoUploadPreviewUrls.forEach(url => URL.revokeObjectURL(url));
                        pendingEchoUploadPreviewUrls = [];
                        pendingEchoUploadFiles = [];
                        pendingEchoUploadTargetGroupId = null;
                        pendingEchoUploadCanAddDprint = false;
                        appendNextEchoUpload = false;
                        if (activeRehabPlanPdfUrl) URL.revokeObjectURL(activeRehabPlanPdfUrl);
                        activeRehabPlanPdfUrl = null;
                        document.getElementById('ordMedSearchResults')?.classList.add('hidden');
                        renderActiveOrdLines();
                        displayActivePatientDme();
                        window.renderDmeReeducationPlanRows([]);
                        loadPatientPrescriptionsHistory(tab.patientId, tab.passageId, tab.consultationId);
                        loadDmeConsumptions();
                        applyDmeAccessMode(tab);
                    }
                    alert(result.message);
                } catch (error) {
                    alert(error.message || 'Erreur lors de la réinitialisation.');
                } finally {
                    tab.resetting = false;
                    if (!completed && window.currentActiveDmeKey === tab.key) controls.forEach(([control, disabled]) => control.disabled = disabled);
                    if (window.currentActiveDmeKey === tab.key) {
                        if (saveButton) saveButton.disabled = false;
                        applyDmeAccessMode(tab);
                    }
                }
            };

            window.saveActiveDmeConsultation = async function() {
                if (window.activeDmeTabs.find(item => item.key === window.currentActiveDmeKey)?.resetting) return;
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab) return;
                if (currentTab.mode !== 'execution' || currentTab.data?.dme?.verrouille) {
                    alert("Cette consultation est en lecture seule.");
                    return;
                }

                // Le tableau de rééducation stocke ses lignes dans un champ JSON caché.
                // Synchroniser avant la collecte pour ne pas perdre la dernière saisie.
                window.syncDmeReeducationPlan();
                const payload = {
                    id_patient: currentTab.patientId,
                    id_passage: currentTab.passageId,
                    id_consultation: currentTab.consultationId || currentTab.data?.dme?.id,
                    constantes: {
                        ta: document.getElementById('dme_ta')?.value || '',
                        pouls: document.getElementById('dme_pouls')?.value || '',
                        temp: document.getElementById('dme_temp')?.value || '',
                        poids: document.getElementById('dme_poids')?.value || '',
                        taille: document.getElementById('dme_taille')?.value || '',
                        imc: document.getElementById('dme_imc')?.value || '',
                        glycemie: document.getElementById('dme_glycemie')?.value || '',
                        perimetre_cranien: document.getElementById('dme_growth_head')?.value || '',
                        perimetre_brachial: document.getElementById('dme_growth_arm')?.value || '',
                    },
                    allergies_intolerances: document.getElementById('dme_allergies')?.value || '',
                    traitement_en_cours: document.getElementById('dme_traitement')?.value || '',
                    antecedents_medicaux: document.getElementById('dme_ant_med')?.value || '',
                    antecedents_chirurgicaux: document.getElementById('dme_ant_chir')?.value || '',
                    notes_habitudes: document.getElementById('dme_habitudes')?.value || '',
                    motif: document.getElementById('dme_motif')?.value || '',
                    diagnostic: document.getElementById('dme_diagnostic')?.value || '',
                    conduite_a_tenir: document.getElementById('dme_conduite')?.value || '',
                    donnees_specialisees: collectDmeSpecialtyFields(),
                    actes: currentDmeActs
                };

                try {
                    const res = await fetch('/api/dme/save', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    }).then(r => r.json());

                    if (res.success) {
                        alert("✅ " + res.message);
                        currentTab.data.dme = {
                            ...currentTab.data.dme,
                            ...payload,
                            id: res.id_consultation || currentTab.consultationId || currentTab.data.dme?.id,
                            constantes: payload.constantes,
                            praticien: res.praticien || currentTab.data.dme?.praticien,
                            verrouille: true
                        };
                        currentTab.consultationId = currentTab.data.dme.id;
                        currentTab.data.passage.actes = currentDmeActs;
                        currentTab.data.passage.statut = res.statut || 'Terminé';
                        currentTab.data.historique = (currentTab.data.historique || []).map(item =>
                            Number(item.id_passage) === Number(currentTab.passageId)
                                ? { ...item, statut: currentTab.data.passage.statut, diagnostic: payload.diagnostic, praticien: currentTab.data.dme.praticien, constantes: payload.constantes }
                                : item
                        );
                        currentTab.mode = 'lecture';
                        displayActivePatientDme();
                        if (typeof window.loadReceptionPassagesModule === 'function') window.loadReceptionPassagesModule();
                    } else {
                        alert("❌ " + res.message);
                    }
                } catch (err) {
                    alert("Erreur de sauvegarde du DME.");
                }
            };

            renderDmeTabsBar();
            displayActivePatientDme();
        })();
    