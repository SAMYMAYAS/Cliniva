const assert=require('node:assert/strict'); const dmeResetPermissionAtRender=true;
function element(){return {disabled:false,textContent:'',className:'',attributes:{},classes:new Set(),classList:{toggle(){},remove(){}},setAttribute(k,v){this.attributes[k]=v}};}
const elements={dmeResetConsultationButton:element(),dmeResetConsultationHint:element(),dmeAccessModeBadge:element(),dmeSaveConsultationButton:element()};
const document={getElementById:id=>elements[id],querySelectorAll:()=>[],querySelector:()=>null};
        function getDmeResetPermission(tab) {
            const permission = tab?.data?.dme_can_reset;
            return typeof permission === 'boolean' ? permission : dmeResetPermissionAtRender;
        }
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
                    const enabled = canEdit && permission === true && Boolean(consultationId) && !currentTab?.resetting;
                    const reason = permission === null
                        ? 'Droits non reçus : redémarrez le serveur puis rouvrez le DME.'
                        : permission !== true
                            ? 'Autorisation « Réinitialiser une consultation DME en cours » requise dans le profil.'
                            : !canEdit
                                ? 'Disponible uniquement pour une consultation en cours, en mode exécution.'
                                : !consultationId
                                    ? 'Démarrez la consultation pour activer le reset.'
                                    : currentTab?.resetting ? 'Réinitialisation en cours…' : '';
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


const tab={mode:'execution',consultationId:1,data:{dme:{id:1,verrouille:false}}};
applyDmeAccessMode(tab);assert.equal(elements.dmeResetConsultationButton.disabled,false);
tab.data.dme_can_reset=false;applyDmeAccessMode(tab);assert.equal(elements.dmeResetConsultationButton.disabled,true);assert.match(elements.dmeResetConsultationHint.textContent,/Autorisation/);
tab.data.dme_can_reset=true;tab.mode='lecture';applyDmeAccessMode(tab);assert.equal(elements.dmeResetConsultationButton.disabled,true);assert.match(elements.dmeResetConsultationHint.textContent,/mode exécution/);
tab.mode='execution';tab.consultationId=null;applyDmeAccessMode(tab);assert.equal(elements.dmeResetConsultationButton.disabled,false);
console.log('OK : bouton visible, droits serveur prioritaires, explications et identifiant de secours.');
