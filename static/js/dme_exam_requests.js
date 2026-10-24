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
            state.saving=true;applyDmeAccessMode(tab);renderExamRequests(tab);
            try{
                const panels=draft.panels.map(panel=>({...panel,items:panel.items.filter(item=>item.selected).map(({label,details})=>({label,details}))})).filter(panel=>panel.items.length);
                if(!panels.length)throw new Error('Ajoutez un bilan et cochez au moins un paramètre ou examen.');
                const result=await examPost('/save',{...examScope(tab),kind,panels,indication:draft.indication,notes:draft.notes});
                state.drafts[kind]={panels:[],indication:'',notes:''};
                if(window.currentActiveDmeKey===tab.key){await window.loadDmeExamRequests();if(action==='save-print')printDmeExamRequest(result.id,tab);document.getElementById('dmeExamMessage').textContent='Demande enregistrée.';}
            }finally{state.saving=false;if(window.currentActiveDmeKey===tab.key)applyDmeAccessMode(tab);renderExamRequests(tab);}
            return;
        }
        renderExamRequests(tab);
    }catch(error){document.getElementById('dmeExamMessage').textContent=error.message;}
});
