from pathlib import Path

path = Path('modules/gestion_kacel_job.py')
source = path.read_text(encoding='utf-8')
dollar = chr(36)

source = source.replace('        let tariffs = [];\\n', '        let tariffs = [];\\n        let tariffCategories = [];\\n        let tariffCategoryDraft = [];\\n', 1)
old = '<input id="kacelJobCustomCategory" placeholder="Catégorie" class="w-36 rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-xs dark:border-white/10 dark:bg-black/20 dark:text-white">'
new = '<select id="kacelJobCustomCategory" aria-label="Catégorie de la prestation" class="w-48 rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-xs dark:border-white/10 dark:bg-black/20 dark:text-white"><option value="">Choisir une catégorie</option></select><button type="button" onclick="kacelJobOpenCategories()" class="rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs font-bold text-slate-700 hover:bg-slate-100 dark:border-white/10 dark:bg-black/20 dark:text-white"><i class="ph-bold ph-folders mr-1"></i>Gérer les catégories</button>'
assert old in source
source = source.replace(old, new, 1)
source = source.replace('placeholder="Nouvelle prestation"', 'placeholder="Nouvelle prestation ou examen"', 1)
source = source.replace('Ajouter</button></div></div><div class="min-h-0 flex-1 overflow-auto p-4">', 'Ajouter prestation / examen</button></div></div><div class="min-h-0 flex-1 overflow-auto p-4">', 1)

old = '<td class="p-2.5"><input data-tariff-prop="categorie" value="' + dollar + '{{esc(item.categorie)}}" class="w-36 rounded-md border border-slate-200 bg-white px-2 py-1.5 text-[11px] dark:border-white/10 dark:bg-black/20 dark:text-white"></td>'
new = '<td class="p-2.5"><select data-tariff-prop="categorie" data-current-category="' + dollar + '{{esc(item.categorie)}}" class="w-44 rounded-md border border-slate-200 bg-white px-2 py-1.5 text-[11px] dark:border-white/10 dark:bg-black/20 dark:text-white"></select></td>'
assert old in source
source = source.replace(old, new, 1)

modal_anchor = '<div id="kacelJobPdfModal"'
category_modal = '<div id="kacelJobCategoriesModal" class="fixed inset-0 z-[2147483646] hidden items-center justify-center bg-black/65 p-4 backdrop-blur-sm"><section class="flex max-h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-2xl dark:border-white/10 dark:bg-[#121c15]"><header class="flex items-center justify-between bg-gradient-to-br from-[#101b13] via-[#183b20] to-[#24653a] px-5 py-4 text-white"><div class="flex items-center gap-3"><span class="grid h-10 w-10 place-items-center rounded-xl bg-lime-300 text-emerald-950"><i class="ph-bold ph-folders text-xl"></i></span><div><h2 class="font-extrabold">Catégories de prestations</h2><p class="text-[10px] text-white/75">Ajoutez, renommez ou supprimez une catégorie.</p></div></div><button type="button" onclick="kacelJobCloseModal(&quot;kacelJobCategoriesModal&quot;)" class="rounded-xl p-2 text-white/75 hover:bg-white/10"><i class="ph-bold ph-x text-xl"></i></button></header><div id="kacelJobCategoriesList" class="min-h-0 flex-1 space-y-2 overflow-y-auto p-5"></div><footer class="flex flex-wrap justify-between gap-2 border-t border-slate-100 bg-slate-50 p-4 dark:border-white/5 dark:bg-black/20"><button type="button" onclick="kacelJobAddCategoryDraft()" class="rounded-xl border border-slate-200 bg-white px-4 py-2.5 text-xs font-bold text-slate-700 dark:border-white/10 dark:bg-black/20 dark:text-white"><i class="ph-bold ph-plus mr-1"></i>Ajouter une catégorie</button><div class="flex gap-2"><button type="button" onclick="kacelJobCloseModal(&quot;kacelJobCategoriesModal&quot;)" class="rounded-xl px-4 py-2.5 text-xs font-semibold text-slate-500">Annuler</button><button type="button" onclick="kacelJobSaveCategories()" class="rounded-xl bg-brand-neon px-5 py-2.5 text-xs font-extrabold text-black"><i class="ph-bold ph-floppy-disk mr-1"></i>Enregistrer</button></div></footer></section></div>'
assert modal_anchor in source and 'kacelJobCategoriesModal' not in source
source = source.replace(modal_anchor, category_modal + modal_anchor, 1)

js_anchor = '        window.kacelJobRenderTariffs = function() {{'
js = '''        window.kacelJobRenderCategorySelect = function() {{
          const select = document.getElementById('kacelJobCustomCategory');
          if (!select) return;
          const current = select.value;
          select.innerHTML = '<option value="">Choisir une catégorie</option>' + tariffCategories.map(item => '<option value="' + esc(item.nom) + '">' + esc(item.nom) + '</option>').join('');
          if (tariffCategories.some(item => item.nom === current)) select.value = current;
          else if (tariffCategories.some(item => item.nom === 'Examens complémentaires')) select.value = 'Examens complémentaires';
          else if (tariffCategories.length) select.value = tariffCategories[0].nom;
        }};
        window.kacelJobRenderCategoryDraft = function() {{
          const list = document.getElementById('kacelJobCategoriesList');
          if (!list) return;
          list.innerHTML = tariffCategoryDraft.map((item, index) => '<div class="flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 p-2.5 dark:border-white/10 dark:bg-black/20"><span class="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-emerald-100 text-emerald-700"><i class="ph-bold ph-folder"></i></span><input data-category-index="' + index + '" value="' + esc(item.nom) + '" maxlength="120" placeholder="Nom de la catégorie" class="min-w-0 flex-1 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs dark:border-white/10 dark:bg-black/20 dark:text-white"><button type="button" onclick="kacelJobRemoveCategoryDraft(' + index + ')" class="rounded-lg p-2 text-rose-500"><i class="ph-bold ph-trash"></i></button></div>').join('') || '<p class="py-8 text-center text-xs text-slate-500">Ajoutez une catégorie pour commencer.</p>';
        }};
        window.kacelJobOpenCategories = function() {{
          tariffCategoryDraft = tariffCategories.map(item => ({{original_nom:item.nom, nom:item.nom}}));
          window.kacelJobRenderCategoryDraft();
          const modal = portalKacelModal('kacelJobCategoriesModal'); modal.classList.remove('hidden'); modal.classList.add('flex');
        }};
        window.kacelJobAddCategoryDraft = function() {{ tariffCategoryDraft.push({{original_nom:'', nom:''}}); window.kacelJobRenderCategoryDraft(); }};
        window.kacelJobRemoveCategoryDraft = index => {{ tariffCategoryDraft.splice(index, 1); window.kacelJobRenderCategoryDraft(); }};
        window.kacelJobSaveCategories = async function() {{
          if (!activeConventionId) return;
          const rows = [...document.querySelectorAll('#kacelJobCategoriesList input[data-category-index]')].map(input => {{ const item = tariffCategoryDraft[Number(input.dataset.categoryIndex)]; return {{original_nom:item?.original_nom || '', nom:input.value.trim()}}; }});
          try {{ const result = await api('/api/kacel-job/conventions/' + activeConventionId + '/categories/save', {{method:'POST', body:JSON.stringify({{categories:rows}})}}); tariffCategories = result.categories || []; window.kacelJobRenderCategorySelect(); window.kacelJobCloseModal('kacelJobCategoriesModal'); window.kacelJobRenderTariffs(); notify('Catégories enregistrées.'); }} catch (error) {{ notify(error.message, true); }}
        }};
'''
assert js_anchor in source
source = source.replace(js_anchor, js.replace('\n', chr(92) + 'n').replace("'", chr(92) + "'") + js_anchor, 1)
source = source.replace("document.getElementById('kacelJobTariffsBody').innerHTML = rendered || emptyRow(6, 'Aucune prestation définie.');", "document.getElementById('kacelJobTariffsBody').innerHTML = rendered || emptyRow(6, 'Aucune prestation définie.');\\n          document.querySelectorAll('#kacelJobTariffsBody select[data-tariff-prop=\\\"categorie\\\"]').forEach(select => {{ const current = select.dataset.currentCategory; select.innerHTML = tariffCategories.map(item => '<option value=\\\"' + esc(item.nom) + '\\\">' + esc(item.nom) + '</option>').join(''); select.value = current; }});",1)

old = 'try {{ const result = await api(`/api/kacel-job/conventions/' + dollar + '{{activeConventionId}}/tariffs`); tariffs = result.tariffs || []; window.kacelJobRenderTariffs(); }}'
new = 'try {{ const [result, categoryResult] = await Promise.all([api(`/api/kacel-job/conventions/' + dollar + '{{activeConventionId}}/tariffs`), api(`/api/kacel-job/conventions/' + dollar + '{{activeConventionId}}/categories`)]); tariffs = result.tariffs || []; tariffCategories = categoryResult.categories || []; window.kacelJobRenderCategorySelect(); window.kacelJobRenderTariffs(); }}'
assert old in source
source = source.replace(old, new, 1)
source = source.replace("document.getElementById(\\'kacelJobCustomCategory\\').value.trim()", "document.getElementById(\\'kacelJobCustomCategory\\').value", 1)
source = source.replace("if (!name || rawAmount === \\'\\')", "if (!category || !name || rawAmount === \\'\\')", 1)
source = source.replace("categorie:category || \\'Prestations complémentaires\\'", "categorie:category", 1)
source = source.replace("[\\'kacelJobPdfModal\\',\\'kacelJobTariffsModal\\'].includes(id)", "[\\'kacelJobPdfModal\\',\\'kacelJobTariffsModal\\',\\'kacelJobCategoriesModal\\'].includes(id)", 1)

path.write_text(source, encoding='utf-8')
