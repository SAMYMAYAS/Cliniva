
const assert=require('node:assert/strict');
let permitted=true, confirmed=true, calls=0, success=true, switched=false;
const tab={key:'p1',mode:'execution',patientId:11,passageId:22,consultationId:33,data:{dme:{id:33,verrouille:false},dme_can_reset:true}};
const controls=[{tagName:'INPUT',type:'text',value:'old',defaultValue:'',disabled:false},{tagName:'INPUT',type:'number',value:'9',defaultValue:'1',disabled:false}];
const button={disabled:false};const document={getElementById:()=>button,querySelectorAll:()=>controls};
const window={activeDmeTabs:[tab],currentActiveDmeKey:'p1',renderDmeReeducationPlanRows(){}};
let currentOrdLines=[{nom:'ancienne ordonnance'}],activeLivretDraftToken='draft',activeCarouselImages=[1],currentCarouselIndex=2;
let pendingEchoUploadPreviewUrls=[],pendingEchoUploadFiles=[1],pendingEchoUploadTargetGroupId='g',pendingEchoUploadCanAddDprint=true,appendNextEchoUpload=true,activeRehabPlanPdfUrl=null;
function renderActiveOrdLines(){} function displayActivePatientDme(){} function loadPatientPrescriptionsHistory(){} function loadDmeConsumptions(){} function applyDmeAccessMode(){}
const confirm=()=>confirmed,alert=()=>{};
const fetch=async(url,options)=>{calls++;assert.equal(url,'/api/dme/consultation/reset');assert.deepEqual(JSON.parse(options.body),{id_patient:11,id_passage:22,id_consultation:33});if(switched)window.currentActiveDmeKey='p2';return {ok:success,json:async()=>success?{success:true,data:{dme:{id:33,verrouille:false},dme_can_reset:true},message:'OK'}:{detail:'Interdit'}};};
button.classList={add(){}};
            window.resetActiveDmeConsultation = async function() {
                const tab = window.activeDmeTabs.find(item => item.key === window.currentActiveDmeKey);
                if (!tab || tab.mode !== 'execution' || tab.data?.dme?.verrouille || !tab.data?.dme_can_reset || !tab.consultationId) return;
                if (tab.resetting) return;
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
                        body: JSON.stringify({id_patient: Number(tab.patientId), id_passage: Number(tab.passageId), id_consultation: Number(tab.consultationId)})
                    });
                    const result = await response.json();
                    if (!response.ok || !result.success) throw new Error(result.detail || result.message || 'Réinitialisation impossible.');
                    completed = true;
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
                    if (button) button.disabled = false;
                    if (saveButton) saveButton.disabled = false;
                }
            };


(async()=>{
 tab.data.dme_can_reset=false;await window.resetActiveDmeConsultation();assert.equal(calls,0);
 tab.data.dme_can_reset=true;confirmed=false;await window.resetActiveDmeConsultation();assert.equal(calls,0);
 confirmed=true;success=false;await window.resetActiveDmeConsultation();assert.equal(controls[0].value,'old');assert.equal(currentOrdLines.length,1);assert.equal(button.disabled,false);assert.equal(controls[0].disabled,false);
 success=true;await window.resetActiveDmeConsultation();assert.equal(controls[0].value,'');assert.equal(controls[1].value,'1');assert.deepEqual(currentOrdLines,[]);assert.equal(activeLivretDraftToken,null);assert.deepEqual(pendingEchoUploadFiles,[]);assert.equal(tab.currentSubTab,'consultation');assert.equal(tab.mode,'execution');
 controls[0].value='autre patient';switched=true;await window.resetActiveDmeConsultation();assert.equal(controls[0].value,'autre patient');
 console.log('OK : refus, annulation, erreur sans perte, reset complet, valeurs initiales et changement de patient.');
})().catch(error=>{console.error(error);process.exitCode=1;});
