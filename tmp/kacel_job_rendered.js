
      (() => {
        const CAN_MANAGE_COMPANIES = true;
        const CAN_MANAGE_CONVENTIONS = true;
        document.querySelectorAll('body > #kacelJobTariffsModal, body > #kacelJobPdfModal').forEach(modal => modal.remove());
        const portalKacelModal = id => { const modal = document.getElementById(id); if (modal && modal.parentElement !== document.body) document.body.appendChild(modal); return modal; };
        let companies = [];
        let conventions = [];
        let employees = [];
        let tariffs = [];
        let tariffCategories = [];
        let tariffCategoryDraft = [];
        let activeCompanyId = null;
        let activeConventionId = null;
        let activePdfUrl = null;
        const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
        const api = async (url, options = {}) => {
          const response = await fetch(url, { credentials: 'same-origin', headers: {'Content-Type':'application/json', ...(options.headers || {})}, ...options });
          const body = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(body.detail || body.message || 'La requête a échoué.');
          return body;
        };
        const notify = (message, error = false) => { if (typeof showToast === 'function') showToast(message, error ? 'error' : 'success'); else alert(message); };
        const companyName = id => companies.find(item => Number(item.id) === Number(id))?.designation || 'Entreprise';
        const emptyRow = (colspan, message) => `<tr><td colspan="${colspan}" class="p-8 text-center text-xs text-slate-400"><i class="ph ph-magnifying-glass mb-2 block text-2xl"></i>${esc(message)}</td></tr>`;
        window.kacelJobRefresh = async function() {
          try {
            const [companyData, conventionData] = await Promise.all([api('/api/kacel-job/companies'), api('/api/kacel-job/conventions')]);
            companies = companyData.companies || [];
            conventions = conventionData.conventions || [];
            document.getElementById('kacelJobCompanyKpi').textContent = companies.length;
            document.getElementById('kacelJobConventionKpi').textContent = conventions.length;
            document.getElementById('kacelJobActiveKpi').textContent = conventions.filter(item => item.statut === 'Active').length;
            window.kacelJobRenderCompanies(); window.kacelJobRenderConventions(); window.kacelJobPopulateCompanySelects();
          } catch (error) { notify(error.message, true); }
        };
        window.kacelJobPopulateCompanySelects = function() {
          const options = companies.map(item => `<option value="${Number(item.id)}">${esc(item.designation)}</option>`).join('');
          const select = document.getElementById('kacelJobConventionCompany');
          const current = select.value;
          select.innerHTML = `<option value="">Choisir une entreprise</option>${options}`;
          if (current) select.value = current;
          const filter = document.getElementById('kacelJobConventionCompanyFilter');
          const filterCurrent = filter.value;
          filter.innerHTML = `<option value="">Toutes les entreprises</option>${options}`;
          if (filterCurrent) filter.value = filterCurrent;
        };
        window.kacelJobRenderCompanies = function() {
          const q = (document.getElementById('kacelJobCompanySearch')?.value || '').trim().toLowerCase();
          const rows = companies.filter(item => [item.designation,item.adresse,item.email,item.telephone,item.contact,item.secteur_activite].some(value => String(value || '').toLowerCase().includes(q)));
          document.getElementById('kacelJobCompaniesBody').innerHTML = rows.length ? rows.map(item => `<tr class="transition hover:bg-emerald-50/60 dark:hover:bg-white/[.03]"><td class="p-3"><div class="flex items-center gap-2"><span class="rounded-md bg-emerald-50 px-1.5 py-1 font-mono text-[9px] font-bold text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300">#${Number(item.id)}</span><span class="font-bold text-slate-800 dark:text-white">${esc(item.designation)}</span></div><div class="mt-1 text-[10px] text-slate-400">${esc(item.email || 'Pas d’e-mail')}</div></td><td class="max-w-[230px] p-3 text-slate-600 dark:text-slate-300">${esc(item.adresse || '—')}</td><td class="p-3"><div class="font-semibold text-slate-700 dark:text-slate-200">${esc(item.contact || '—')}</div><div class="mt-1 text-[10px] text-slate-400">${esc(item.telephone || '—')}</div></td><td class="p-3 text-slate-600 dark:text-slate-300">${esc(item.secteur_activite || '—')}</td><td class="p-3 text-center"><button type="button" onclick="kacelJobShowEmployees(${Number(item.id)})" class="rounded-full bg-violet-50 px-2.5 py-1 text-[10px] font-bold text-violet-700 hover:bg-violet-100 dark:bg-violet-500/10 dark:text-violet-300">${Number(item.nombre_employes || 0)}</button></td><td class="p-3 text-center"><span class="rounded-full bg-sky-50 px-2.5 py-1 text-[10px] font-bold text-sky-700 dark:bg-sky-500/10 dark:text-sky-300">${Number(item.nombre_conventions || 0)}</span></td><td class="p-3"><span class="rounded-full px-2.5 py-1 text-[10px] font-bold ${item.actif ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300' : 'bg-slate-100 text-slate-500'}">${item.actif ? 'Active' : 'Inactive'}</span></td><td class="p-3 text-right whitespace-nowrap"><button type="button" onclick="kacelJobShowEmployees(${Number(item.id)})" title="Gérer les employés" class="rounded-lg p-2 text-violet-600 hover:bg-violet-50"><i class="ph-bold ph-users-three"></i></button><button type="button" onclick="kacelJobShowCompanyConventions(${Number(item.id)})" title="Voir les conventions" class="rounded-lg p-2 text-sky-600 hover:bg-sky-50"><i class="ph-bold ph-files"></i></button>${CAN_MANAGE_COMPANIES ? `<button type="button" onclick="kacelJobEditCompany(${Number(item.id)})" title="Modifier" class="rounded-lg p-2 text-emerald-600 hover:bg-emerald-50"><i class="ph-bold ph-pencil-simple"></i></button><button type="button" onclick="kacelJobDeleteCompany(${Number(item.id)})" title="Supprimer" class="rounded-lg p-2 text-rose-500 hover:bg-rose-50"><i class="ph-bold ph-trash"></i></button>` : ''}</td></tr>`).join('') : emptyRow(8, 'Aucune entreprise trouvée.');
        };
        window.kacelJobRenderConventions = function() {
          const companyId = document.getElementById('kacelJobConventionCompanyFilter')?.value || '';
          const q = (document.getElementById('kacelJobConventionSearch')?.value || '').trim().toLowerCase();
          const rows = conventions.filter(item => (!companyId || Number(item.entreprise_id) === Number(companyId)) && [item.reference,item.designation,item.entreprise,item.statut,item.conditions].some(value => String(value || '').toLowerCase().includes(q)));
          document.getElementById('kacelJobConventionsBody').innerHTML = rows.length ? rows.map(item => `<tr class="transition hover:bg-sky-50/60 dark:hover:bg-white/[.03]"><td class="p-3"><div class="font-bold text-slate-800 dark:text-white">${esc(item.reference || 'Sans référence')}</div><div class="mt-1 text-[10px] text-slate-500">${esc(item.designation)}</div></td><td class="p-3 font-semibold text-slate-700 dark:text-slate-200">${esc(item.entreprise)}</td><td class="p-3 text-slate-600 dark:text-slate-300">${esc(item.date_debut || '—')} <span class="text-slate-400">→</span> ${esc(item.date_fin || '—')}</td><td class="p-3"><span class="rounded-full px-2.5 py-1 text-[10px] font-bold ${item.statut === 'Active' ? 'bg-emerald-50 text-emerald-700' : item.statut === 'Suspendue' ? 'bg-amber-50 text-amber-700' : 'bg-slate-100 text-slate-600'}">${esc(item.statut)}</span></td><td class="max-w-[250px] p-3 text-slate-500">${esc(item.conditions || item.observation || '—')}</td><td class="p-3 text-right whitespace-nowrap"><button type="button" onclick="kacelJobDownloadWord(${Number(item.id)})" title="Télécharger le document Word" class="rounded-lg p-2 text-blue-600 hover:bg-blue-50"><i class="ph-bold ph-file-doc"></i></button><button type="button" onclick="kacelJobDownloadWord(${Number(item.id)})" title="Télécharger le document Word" class="rounded-lg p-2 text-blue-600 hover:bg-blue-50"><i class="ph-bold ph-file-doc"></i></button><button type="button" onclick="kacelJobOpenPdf(${Number(item.id)})" title="Consulter ou imprimer le PDF" class="rounded-lg p-2 text-violet-600 hover:bg-violet-50"><i class="ph-bold ph-file-pdf"></i></button>${CAN_MANAGE_CONVENTIONS ? `<button type="button" onclick="kacelJobOpenTariffs(${Number(item.id)})" title="Prestations et tarifs" class="rounded-lg p-2 text-sky-600 hover:bg-sky-50"><i class="ph-bold ph-currency-circle-dollar"></i></button><button type="button" onclick="kacelJobEditConvention(${Number(item.id)})" title="Modifier" class="rounded-lg p-2 text-emerald-600 hover:bg-emerald-50"><i class="ph-bold ph-pencil-simple"></i></button><button type="button" onclick="kacelJobDeleteConvention(${Number(item.id)})" title="Supprimer" class="rounded-lg p-2 text-rose-500 hover:bg-rose-50"><i class="ph-bold ph-trash"></i></button>` : ''}</td></tr>`).join('') : emptyRow(6, 'Aucune convention trouvée.');
        };
        window.kacelJobRenderCategorySelect = function() {
          const select = document.getElementById('kacelJobCustomCategory');
          if (!select) return;
          const current = select.value;
          select.innerHTML = '<option value="">Choisir une catégorie</option>' + tariffCategories.map(item => '<option value="' + esc(item.nom) + '">' + esc(item.nom) + '</option>').join('');
          if (tariffCategories.some(item => item.nom === current)) select.value = current;
          else if (tariffCategories.some(item => item.nom === 'Examens complémentaires')) select.value = 'Examens complémentaires';
          else if (tariffCategories.length) select.value = tariffCategories[0].nom;
        };
        window.kacelJobRenderCategoryDraft = function() {
          const list = document.getElementById('kacelJobCategoriesList');
          if (!list) return;
          list.innerHTML = tariffCategoryDraft.map((item, index) => '<div class="flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 p-2.5 dark:border-white/10 dark:bg-black/20"><span class="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-emerald-100 text-emerald-700"><i class="ph-bold ph-folder"></i></span><input data-category-index="' + index + '" value="' + esc(item.nom) + '" maxlength="120" placeholder="Nom de la catégorie" class="min-w-0 flex-1 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs dark:border-white/10 dark:bg-black/20 dark:text-white"><button type="button" onclick="kacelJobRemoveCategoryDraft(' + index + ')" class="rounded-lg p-2 text-rose-500"><i class="ph-bold ph-trash"></i></button></div>').join('') || '<p class="py-8 text-center text-xs text-slate-500">Ajoutez une catégorie pour commencer.</p>';
        };
        window.kacelJobOpenCategories = function() {
          tariffCategoryDraft = tariffCategories.map(item => ({original_nom:item.nom, nom:item.nom}));
          window.kacelJobRenderCategoryDraft();
          const modal = portalKacelModal('kacelJobCategoriesModal'); modal.classList.remove('hidden'); modal.classList.add('flex');
        };
        window.kacelJobAddCategoryDraft = function() { tariffCategoryDraft.push({original_nom:'', nom:''}); window.kacelJobRenderCategoryDraft(); };
        window.kacelJobRemoveCategoryDraft = index => { tariffCategoryDraft.splice(index, 1); window.kacelJobRenderCategoryDraft(); };
        window.kacelJobSaveCategories = async function() {
          if (!activeConventionId) return;
          const rows = [...document.querySelectorAll('#kacelJobCategoriesList input[data-category-index]')].map(input => { const item = tariffCategoryDraft[Number(input.dataset.categoryIndex)]; return {original_nom:item?.original_nom || '', nom:input.value.trim()}; });
          try { const result = await api('/api/kacel-job/conventions/' + activeConventionId + '/categories/save', {method:'POST', body:JSON.stringify({categories:rows})}); const renamed = new Map(rows.filter(row => row.original_nom && row.original_nom !== row.nom).map(row => [row.original_nom, row.nom])); tariffs = tariffs.map(item => ({...item, categorie:renamed.get(item.categorie) || item.categorie})); tariffCategories = result.categories || []; window.kacelJobRenderCategorySelect(); window.kacelJobCloseModal('kacelJobCategoriesModal'); window.kacelJobRenderTariffs(); notify('Catégories enregistrées.'); } catch (error) { notify(error.message, true); }
        };
        window.kacelJobRenderTariffs = function() {
          const categoryOrder = ['Visite périodique','Visites médicales','Examens complémentaires','Vaccination','Visites des lieux de travail','Frais de déplacement','Expertise et CPHS','Sensibilisation','Formation','Risques et mesures','Remises sur prestations des centres'];
          const orderedTariffs = [...tariffs].sort((a,b) => { const ai = categoryOrder.indexOf(a.categorie), bi = categoryOrder.indexOf(b.categorie); return (ai < 0 ? 999 : ai) - (bi < 0 ? 999 : bi) || Number(a.ordre || 0) - Number(b.ordre || 0); });
          const input = (key, label, value, width = 'w-24') => `<label class="inline-flex items-center gap-1 text-[10px] text-slate-500">${label}<input data-tariff-key="${key}" type="number" min="0" step="0.01" value="${value ?? ''}" class="${width} rounded-md border border-slate-200 bg-white px-2 py-1.5 text-xs text-slate-800 dark:border-white/10 dark:bg-black/20 dark:text-white"></label>`;
          let currentCategory = null;
          const rendered = orderedTariffs.map(item => {
            const config = item.configuration || {};
            let controls = '';
            if (item.mode_tarif === 'discount') controls = input('percent', '%', config.percent);
            else if (item.mode_tarif === 'headcount_tiers') controls = `<div class="flex flex-wrap gap-2">${input('under_10','< 10',config.under_10)}${input('from_10_to_25','10–25',config.from_10_to_25)}${input('over_25','> 25',config.over_25)}</div><input data-tariff-key="unit" value="${esc(config.unit || 'DA HT / jour')}" class="mt-1 w-40 rounded-md border border-slate-200 bg-white px-2 py-1.5 text-[10px] dark:border-white/10 dark:bg-black/20 dark:text-white">`;
            else if (item.mode_tarif === 'company_size_tiers') controls = `<div class="flex flex-wrap gap-2">${input('small','Petite',config.small)}${input('medium','Moyenne',config.medium)}${input('large','Grande',config.large)}</div><input data-tariff-key="unit" value="${esc(config.unit || 'DA HT')}" class="mt-1 w-40 rounded-md border border-slate-200 bg-white px-2 py-1.5 text-[10px] dark:border-white/10 dark:bg-black/20 dark:text-white">`;
            else if (item.mode_tarif === 'custom_unit') controls = `<div class="flex flex-wrap gap-2">${input('office','Bureau',config.office)}${input('workstation','Poste',config.workstation)}</div><input data-tariff-key="unit" value="${esc(config.unit || 'DA HT')}" class="mt-1 w-40 rounded-md border border-slate-200 bg-white px-2 py-1.5 text-[10px] dark:border-white/10 dark:bg-black/20 dark:text-white">`;
            else controls = `<div class="flex flex-wrap items-center gap-2">${input('amount','Montant',config.amount,'w-32')}<input data-tariff-key="unit" value="${esc(config.unit || 'DA HT')}" class="w-36 rounded-md border border-slate-200 bg-white px-2 py-1.5 text-[10px] dark:border-white/10 dark:bg-black/20 dark:text-white"></div>`;
            const category = item.categorie || 'Autres prestations';
            const groupHeader = category !== currentCategory ? `<tr class="bg-emerald-50 dark:bg-emerald-950/40"><td colspan="6" class="border-y border-emerald-100 px-3 py-2 text-[10px] font-extrabold uppercase tracking-wider text-emerald-800 dark:border-emerald-900 dark:text-emerald-300"><i class="ph-bold ph-folder-open mr-1.5"></i>${esc(category)}</td></tr>` : '';
            currentCategory = category;
            return `${groupHeader}<tr data-tariff-code="${esc(item.code)}" class="align-top hover:bg-emerald-50/40 dark:hover:bg-white/[.025]"><td class="p-2.5"><select data-tariff-prop="categorie" data-current-category="${esc(item.categorie)}" class="w-44 rounded-md border border-slate-200 bg-white px-2 py-1.5 text-[11px] dark:border-white/10 dark:bg-black/20 dark:text-white"></select></td><td class="min-w-[250px] p-2.5"><input data-tariff-prop="designation" value="${esc(item.designation)}" class="w-full min-w-[220px] rounded-md border border-slate-200 bg-white px-2 py-1.5 text-[11px] font-semibold dark:border-white/10 dark:bg-black/20 dark:text-white"><p class="mt-1 text-[9px] text-slate-400">${esc(item.description || '')}</p></td><td class="min-w-[310px] p-2.5">${controls}</td><td class="p-2.5"><input data-tariff-prop="description" value="${esc(item.description || '')}" placeholder="Conditions ou inclusions" class="w-56 rounded-md border border-slate-200 bg-white px-2 py-1.5 text-[10px] dark:border-white/10 dark:bg-black/20 dark:text-white"></td><td class="p-2.5 text-center"><input data-tariff-active type="checkbox" ${config.active !== false ? 'checked' : ''} aria-label="Inclure dans la convention" class="h-4 w-4 rounded border-slate-300 text-emerald-600"></td><td class="p-2.5 text-right">${item.personnalisee ? `<button type="button" onclick="kacelJobRemoveCustomTariff('${esc(item.code)}')" title="Supprimer cette prestation" class="rounded-lg p-2 text-rose-500 hover:bg-rose-50"><i class="ph-bold ph-trash"></i></button>` : '<span class="text-[9px] text-slate-400">Modèle</span>'}</td></tr>`;
          }).join('');
          document.getElementById('kacelJobTariffsBody').innerHTML = rendered || emptyRow(6, 'Aucune prestation définie.');
          document.querySelectorAll('#kacelJobTariffsBody select[data-tariff-prop="categorie"]').forEach(select => { const current = select.dataset.currentCategory || ''; select.innerHTML = tariffCategories.map(item => '<option value="' + esc(item.nom) + '">' + esc(item.nom) + '</option>').join(''); select.value = current; });
        };
        window.kacelJobSetTab = function(tab) {
          const companiesTab = tab === 'companies';
          document.getElementById('kacelJobCompaniesPanel').classList.toggle('hidden', !companiesTab);
          document.getElementById('kacelJobConventionsPanel').classList.toggle('hidden', companiesTab);
          document.getElementById('kacelJobTabCompanies').className = `flex items-center gap-2 rounded-xl px-4 py-2.5 text-xs font-bold transition ${companiesTab ? 'bg-brand-neon text-black shadow-md shadow-brand-neon/20' : 'text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-white/5'}`;
          document.getElementById('kacelJobTabConventions').className = `flex items-center gap-2 rounded-xl px-4 py-2.5 text-xs font-bold transition ${!companiesTab ? 'bg-brand-neon text-black shadow-md shadow-brand-neon/20' : 'text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-white/5'}`;
        };
        window.kacelJobShowCompanyConventions = function(id) { window.kacelJobSetTab('conventions'); document.getElementById('kacelJobConventionCompanyFilter').value = String(id); window.kacelJobRenderConventions(); };
        window.kacelJobOpenCompany = function(item = null) {
          document.getElementById('kacelJobCompanyModalTitle').textContent = item ? 'Modifier l’entreprise' : 'Nouvelle entreprise';
          document.getElementById('kacelJobCompanyId').value = item?.id || '';
          document.getElementById('kacelJobCompanyDesignation').value = item?.designation || '';
          document.getElementById('kacelJobCompanyAddress').value = item?.adresse || '';
          document.getElementById('kacelJobCompanyEmail').value = item?.email || '';
          document.getElementById('kacelJobCompanyPhone').value = item?.telephone || '';
          document.getElementById('kacelJobCompanyContact').value = item?.contact || '';
          document.getElementById('kacelJobCompanyRepresentative').value = item?.representant || '';
          document.getElementById('kacelJobCompanyRc').value = item?.rc || '';
          document.getElementById('kacelJobCompanyNif').value = item?.nif || '';
          document.getElementById('kacelJobCompanyArt').value = item?.art || '';
          document.getElementById('kacelJobCompanySector').value = item?.secteur_activite || '';
          document.getElementById('kacelJobCompanyObservation').value = item?.observation || '';
          document.getElementById('kacelJobCompanyActive').checked = item?.actif !== false;
          const modal = document.getElementById('kacelJobCompanyModal'); modal.classList.remove('hidden'); modal.classList.add('flex');
        };
        window.kacelJobEditCompany = id => window.kacelJobOpenCompany(companies.find(item => Number(item.id) === Number(id)));
        window.kacelJobSaveCompany = async function(event) {
          event.preventDefault();
          const payload = {id: document.getElementById('kacelJobCompanyId').value || null, designation: document.getElementById('kacelJobCompanyDesignation').value, adresse: document.getElementById('kacelJobCompanyAddress').value, email: document.getElementById('kacelJobCompanyEmail').value, telephone: document.getElementById('kacelJobCompanyPhone').value, contact: document.getElementById('kacelJobCompanyContact').value, representant: document.getElementById('kacelJobCompanyRepresentative').value, rc: document.getElementById('kacelJobCompanyRc').value, nif: document.getElementById('kacelJobCompanyNif').value, art: document.getElementById('kacelJobCompanyArt').value, secteur_activite: document.getElementById('kacelJobCompanySector').value, observation: document.getElementById('kacelJobCompanyObservation').value, actif: document.getElementById('kacelJobCompanyActive').checked};
          try { const result = await api('/api/kacel-job/companies/save', {method:'POST', body:JSON.stringify(payload)}); window.kacelJobCloseModal('kacelJobCompanyModal'); notify('Entreprise enregistrée.'); await window.kacelJobRefresh(); await window.kacelJobShowEmployees(result.company.id); } catch (error) { notify(error.message, true); }
        };
        window.kacelJobDeleteCompany = async id => {
          const item = companies.find(row => Number(row.id) === Number(id));
          if (!item || !confirm(`Supprimer « ${item.designation} », ses conventions et ses employés ?`)) return;
          try { await api('/api/kacel-job/companies/delete', {method:'POST', body:JSON.stringify({id})}); notify('Entreprise supprimée.'); await window.kacelJobRefresh(); } catch (error) { notify(error.message, true); }
        };
        const dateFr = value => { if (!value) return '—'; const [year, month, day] = String(value).slice(0,10).split('-'); return day && month && year ? `${day}/${month}/${year}` : '—'; };
        window.kacelJobShowEmployees = async function(companyId) {
          const company = companies.find(item => Number(item.id) === Number(companyId));
          if (!company) return;
          activeCompanyId = Number(companyId);
          document.getElementById('kacelJobEmployeesTitle').textContent = company.designation;
          document.getElementById('kacelJobEmployeeSearch').value = '';
          const modal = document.getElementById('kacelJobEmployeesModal'); modal.classList.remove('hidden'); modal.classList.add('flex');
          await window.kacelJobLoadEmployees();
        };
        window.kacelJobLoadEmployees = async function() {
          if (!activeCompanyId) return;
          try { const result = await api(`/api/kacel-job/employees?entreprise_id=${activeCompanyId}`); employees = result.employees || []; window.kacelJobRenderEmployees(); }
          catch (error) { notify(error.message, true); }
        };
        window.kacelJobRenderEmployees = function() {
          const query = (document.getElementById('kacelJobEmployeeSearch')?.value || '').trim().toLowerCase();
          const filtered = employees.filter(item => [item.nom, item.prenom, item.poste].some(value => String(value || '').toLowerCase().includes(query)));
          document.getElementById('kacelJobEmployeesCount').textContent = `${filtered.length} employé(s) affiché(s) · ${employees.length} au total`;
          document.getElementById('kacelJobEmployeesBody').innerHTML = filtered.length ? filtered.map(item => `<tr class="transition hover:bg-emerald-50/60 dark:hover:bg-white/[.03]"><td class="px-4 py-3 font-mono text-xs font-bold text-slate-400">#${Number(item.id)}</td><td class="px-4 py-3 font-bold text-slate-800 dark:text-white">${esc(item.nom)}</td><td class="px-4 py-3 font-semibold text-slate-700 dark:text-slate-200">${esc(item.prenom)}</td><td class="px-4 py-3 text-slate-600 dark:text-slate-300">${dateFr(item.date_naissance)}</td><td class="px-4 py-3 text-slate-600 dark:text-slate-300">${dateFr(item.date_embauche)}</td><td class="px-4 py-3 text-slate-600 dark:text-slate-300">${esc(item.poste || '—')}</td><td class="px-4 py-3 text-right">${CAN_MANAGE_COMPANIES ? `<button type="button" onclick="kacelJobEditEmployee(${Number(item.id)})" title="Modifier" class="rounded-lg p-2 text-emerald-600 hover:bg-emerald-50"><i class="ph-bold ph-pencil-simple"></i></button>` : ''}</td></tr>`).join('') : emptyRow(7, query ? 'Aucun employé ne correspond à cette recherche.' : 'Aucun employé enregistré pour cette entreprise.');
        };
        window.kacelJobOpenEmployee = function(item = null) {
          if (!activeCompanyId) return;
          document.getElementById('kacelJobEmployeeFormTitle').textContent = item ? 'Modifier un employé' : 'Ajouter un employé';
          document.getElementById('kacelJobEmployeeId').value = item?.id || '';
          document.getElementById('kacelJobEmployeeLastName').value = item?.nom || '';
          document.getElementById('kacelJobEmployeeFirstName').value = item?.prenom || '';
          document.getElementById('kacelJobEmployeeBirth').value = String(item?.date_naissance || '').slice(0,10);
          document.getElementById('kacelJobEmployeeHire').value = String(item?.date_embauche || '').slice(0,10);
          document.getElementById('kacelJobEmployeePosition').value = item?.poste || '';
          const modal = document.getElementById('kacelJobEmployeeFormModal'); modal.classList.remove('hidden'); modal.classList.add('flex');
        };
        window.kacelJobEditEmployee = id => window.kacelJobOpenEmployee(employees.find(item => Number(item.id) === Number(id)));
        window.kacelJobSaveEmployee = async function(event) {
          event.preventDefault();
          const payload = {id:document.getElementById('kacelJobEmployeeId').value || null, entreprise_id:activeCompanyId, nom:document.getElementById('kacelJobEmployeeLastName').value, prenom:document.getElementById('kacelJobEmployeeFirstName').value, date_naissance:document.getElementById('kacelJobEmployeeBirth').value, date_embauche:document.getElementById('kacelJobEmployeeHire').value, poste:document.getElementById('kacelJobEmployeePosition').value};
          try { await api('/api/kacel-job/employees/save', {method:'POST', body:JSON.stringify(payload)}); window.kacelJobCloseModal('kacelJobEmployeeFormModal'); notify('Employé enregistré.'); await window.kacelJobLoadEmployees(); await window.kacelJobRefresh(); } catch (error) { notify(error.message, true); }
        };
        window.kacelJobHandleEmployeeImport = async function(event) {
          const file = event.target.files?.[0];
          if (!file || !activeCompanyId) return;
          const formData = new FormData(); formData.append('entreprise_id', String(activeCompanyId)); formData.append('file', file);
          try {
            const response = await fetch('/api/kacel-job/employees/import', {method:'POST', credentials:'same-origin', body:formData});
            const result = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(result.detail || 'Échec de l’import Excel.');
            notify(`${result.imported} employé(s) importé(s).`); await window.kacelJobLoadEmployees(); await window.kacelJobRefresh();
          } catch (error) { notify(error.message, true); } finally { event.target.value = ''; }
        };
        window.kacelJobOpenConvention = function(item = null) {
          if (!companies.length) { notify('Crée d’abord une entreprise pour pouvoir lui rattacher une convention.', true); return; }
          window.kacelJobPopulateCompanySelects();
          const referenceInput = document.getElementById('kacelJobConventionReference');
          referenceInput.readOnly = true;
          referenceInput.placeholder = 'Attribué automatiquement à l’enregistrement';
          referenceInput.parentElement.querySelector('span').textContent = 'N° convention (automatique)';
          document.getElementById('kacelJobConventionModalTitle').textContent = item ? 'Modifier la convention' : 'Nouvelle convention';
          document.getElementById('kacelJobConventionId').value = item?.id || '';
          document.getElementById('kacelJobConventionCompany').value = item?.entreprise_id || companies[0].id;
          document.getElementById('kacelJobConventionReference').value = item?.reference || '';
          document.getElementById('kacelJobConventionDesignation').value = item?.designation || '';
          document.getElementById('kacelJobConventionStart').value = String(item?.date_debut || '').slice(0,10);
          document.getElementById('kacelJobConventionEnd').value = String(item?.date_fin || '').slice(0,10);
          document.getElementById('kacelJobConventionStatus').value = item?.statut || 'Brouillon';
          document.getElementById('kacelJobConventionTerms').value = item?.conditions || '';
          document.getElementById('kacelJobConventionObservation').value = item?.observation || '';
          document.getElementById('kacelJobConventionWorkforce').value = item?.effectif_contractuel ?? '';
          document.getElementById('kacelJobConventionSize').value = item?.taille_entreprise || '';
          const modal = document.getElementById('kacelJobConventionModal'); modal.classList.remove('hidden'); modal.classList.add('flex');
        };
        window.kacelJobEditConvention = id => window.kacelJobOpenConvention(conventions.find(item => Number(item.id) === Number(id)));
        window.kacelJobSaveConvention = async function(event) {
          event.preventDefault();
          const payload = {id: document.getElementById('kacelJobConventionId').value || null, entreprise_id: document.getElementById('kacelJobConventionCompany').value, reference: document.getElementById('kacelJobConventionReference').value, designation: document.getElementById('kacelJobConventionDesignation').value, date_debut: document.getElementById('kacelJobConventionStart').value, date_fin: document.getElementById('kacelJobConventionEnd').value, statut: document.getElementById('kacelJobConventionStatus').value, effectif_contractuel: document.getElementById('kacelJobConventionWorkforce').value || 0, taille_entreprise: document.getElementById('kacelJobConventionSize').value, conditions: document.getElementById('kacelJobConventionTerms').value, observation: document.getElementById('kacelJobConventionObservation').value};
          try { const result = await api('/api/kacel-job/conventions/save', {method:'POST', body:JSON.stringify(payload)}); window.kacelJobCloseModal('kacelJobConventionModal'); notify('Convention enregistrée.'); await window.kacelJobRefresh(); await window.kacelJobOpenTariffs(result.convention.id); } catch (error) { notify(error.message, true); }
        };
        window.kacelJobOpenTariffs = async function(id) {
          const item = conventions.find(row => Number(row.id) === Number(id));
          activeConventionId = Number(id);
          document.getElementById('kacelJobTariffsTitle').textContent = `${item?.reference || 'Convention'} · ${item?.entreprise || ''}`;
          const modal = portalKacelModal('kacelJobTariffsModal'); modal.classList.remove('hidden'); modal.classList.add('flex'); document.body.style.overflow = 'hidden';
          try { const [result, categoryResult] = await Promise.all([api(`/api/kacel-job/conventions/${activeConventionId}/tariffs`), api(`/api/kacel-job/conventions/${activeConventionId}/categories`)]); tariffs = result.tariffs || []; tariffCategories = categoryResult.categories || []; window.kacelJobRenderCategorySelect(); window.kacelJobRenderTariffs(); }
          catch (error) { notify(error.message, true); }
        };
        window.kacelJobAddCustomTariff = function() {
          const category = document.getElementById('kacelJobCustomCategory').value;
          const name = document.getElementById('kacelJobCustomName').value.trim();
          const rawAmount = document.getElementById('kacelJobCustomAmount').value;
          const unit = document.getElementById('kacelJobCustomUnit').value.trim() || 'DA HT';
          if (!category || !name || rawAmount === '') { notify('Renseignez le nom de la prestation et son tarif.', true); return; }
          tariffs.push({code:`custom_${Date.now()}`, categorie:category, designation:name, mode_tarif:'fixed', configuration:{amount:Number(rawAmount),unit,active:true}, description:'', ordre:1000 + tariffs.length, personnalisee:true});
          document.getElementById('kacelJobCustomName').value = ''; document.getElementById('kacelJobCustomAmount').value = '';
          window.kacelJobRenderTariffs();
        };
        window.kacelJobRemoveCustomTariff = code => { tariffs = tariffs.filter(item => item.code !== code); window.kacelJobRenderTariffs(); };
        window.kacelJobSaveTariffs = async function() {
          if (!activeConventionId) return;
          const rows = [...document.querySelectorAll('#kacelJobTariffsBody tr[data-tariff-code]')].map(row => {
            const item = tariffs.find(tariff => tariff.code === row.dataset.tariffCode);
            if (!item) return null;
            const updated = {...item, configuration:{...(item.configuration || {})}};
            row.querySelectorAll('[data-tariff-key]').forEach(field => { updated.configuration[field.dataset.tariffKey] = field.value === '' ? null : field.type === 'number' ? Number(field.value) : field.value; });
            row.querySelectorAll('[data-tariff-prop]').forEach(field => { updated[field.dataset.tariffProp] = field.value; });
            updated.configuration.active = row.querySelector('[data-tariff-active]')?.checked !== false;
            return updated;
          }).filter(Boolean);
          try { await api(`/api/kacel-job/conventions/${activeConventionId}/tariffs/save`, {method:'POST', body:JSON.stringify({tariffs:rows})}); tariffs = rows; notify('Prestations et tarifs enregistrés pour cette convention.'); await window.kacelJobRefresh(); }
          catch (error) { notify(error.message, true); }
        };
        window.kacelJobOpenPdf = function(id) {
          activeConventionId = Number(id);
          activePdfUrl = `/api/kacel-job/conventions/${Number(id)}/pdf`;
          const item = conventions.find(row => Number(row.id) === Number(id));
          document.getElementById('kacelJobPdfTitle').textContent = `Convention PDF · ${item?.reference || ''} · ${item?.entreprise || ''}`;
          document.getElementById('kacelJobPdfFrame').src = `${activePdfUrl}#view=FitH`;
          const modal = portalKacelModal('kacelJobPdfModal'); modal.classList.remove('hidden'); modal.classList.add('flex'); document.body.style.overflow = 'hidden';
        };
        window.kacelJobDownloadWord = async function(id) {
          try {
            const response = await fetch(`/api/kacel-job/conventions/${Number(id)}/docx`, {credentials:'same-origin'});
            if (!response.ok) { const result = await response.json().catch(() => ({})); throw new Error(result.detail || 'Le document Word n’a pas pu être généré.'); }
            const blob = await response.blob();
            const item = conventions.find(row => Number(row.id) === Number(id));
            const reference = String(item?.reference || id).replace(/[^a-z0-9_-]/gi, '_');
            const filename = `Convention_${reference}.docx`;
            if (window.pywebview?.api?.kacel_job_save_and_open_docx) {
              const bytes = new Uint8Array(await blob.arrayBuffer()); let binary = '';
              for (let offset = 0; offset < bytes.length; offset += 0x8000) binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
              const encoded = btoa(binary);
              const result = await window.pywebview.api.kacel_job_save_and_open_docx(`data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64,${encoded}`, filename);
              if (!result?.ok) throw new Error(result?.error || 'Enregistrement du document Word impossible.');
              if (!result.cancelled) notify('Document Word sauvegardé et ouvert.');
              return;
            }
            const url = URL.createObjectURL(blob); const anchor = document.createElement('a');
            anchor.href = url; anchor.download = filename; anchor.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
          } catch (error) { notify(error.message, true); }
        };
        window.kacelJobDownloadPdf = async function() {
          if (!activePdfUrl) return;
          try { const response = await fetch(activePdfUrl, {credentials:'same-origin'}); if (!response.ok) throw new Error('Le PDF n’a pas pu être téléchargé.'); const blob = await response.blob(); const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href = url; anchor.download = `Convention_${activeConventionId || ''}.pdf`; anchor.click(); URL.revokeObjectURL(url); }
          catch (error) { notify(error.message, true); }
        };
        window.kacelJobPrintPdf = function() { try { document.getElementById('kacelJobPdfFrame').contentWindow.print(); } catch (_) { window.open(activePdfUrl, '_blank'); } };
        window.kacelJobClosePdf = function() { document.getElementById('kacelJobPdfFrame').src = 'about:blank'; window.kacelJobCloseModal('kacelJobPdfModal'); activePdfUrl = null; document.body.style.overflow = ''; };
        window.kacelJobDeleteConvention = async id => { if (!confirm('Supprimer cette convention ?')) return; try { await api('/api/kacel-job/conventions/delete', {method:'POST', body:JSON.stringify({id})}); notify('Convention supprimée.'); await window.kacelJobRefresh(); } catch (error) { notify(error.message, true); } };
        window.kacelJobCloseModal = id => { const modal = document.getElementById(id); modal.classList.add('hidden'); modal.classList.remove('flex'); if (['kacelJobPdfModal','kacelJobTariffsModal','kacelJobCategoriesModal'].includes(id)) document.body.style.overflow = ''; };
        window.kacelJobRefresh();
      })();
      