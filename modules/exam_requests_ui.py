"""Interface des demandes de bilans, insérée dans le DME."""
from pathlib import Path
import sys


def render_exam_requests_panel():
    return '''
    <div id="dme-panel-bilans" class="dme-subtab-panel hidden space-y-5">
      <section class="bg-white dark:bg-[#121c15] rounded-3xl border border-slate-200 dark:border-white/10 p-5 space-y-4">
        <div class="flex flex-wrap justify-between items-center gap-3">
          <div><h3 class="font-extrabold text-sm text-slate-800 dark:text-white">Demandes de bilans</h3><p class="text-xs text-slate-500 mt-1">Choisir les bilans et adapter les analyses ou examens pour cette consultation.</p></div>
          <button type="button" data-exam-action="catalog" data-dme-allow-readonly class="rounded-xl bg-indigo-600 px-4 py-2.5 text-xs font-bold text-white">Gérer les bilans du catalogue</button>
        </div>
        <div class="flex flex-wrap gap-2">
          <button type="button" data-exam-kind="biologique" data-dme-allow-readonly class="rounded-xl border border-emerald-200 px-4 py-2 text-xs font-bold text-emerald-700">Biologie</button>
          <button type="button" data-exam-kind="radiologique" data-dme-allow-readonly class="rounded-xl border border-indigo-200 px-4 py-2 text-xs font-bold text-indigo-700">Radiologie</button>
        </div>
        <p id="dmeExamPermission" class="text-xs text-slate-500"></p>
        <div class="flex flex-wrap items-end gap-2">
          <label class="flex-1 min-w-48 text-xs font-bold text-slate-600 dark:text-slate-300">Bilan du catalogue<select id="dmeExamCatalogSelect" class="mt-1 w-full rounded-xl border border-slate-200 p-2.5 text-xs bg-slate-50 dark:bg-black/20 dark:border-white/10"><option value="">Chargement…</option></select></label>
          <button type="button" data-exam-action="add-pack" class="rounded-xl bg-emerald-600 px-4 py-2.5 text-xs font-bold text-white">Ajouter ce bilan</button>
          <button type="button" data-exam-action="add-custom" class="rounded-xl border border-slate-200 px-4 py-2.5 text-xs font-bold text-slate-600 dark:text-slate-300">Ajouter un bilan libre</button>
        </div>
        <div id="dmeExamDraftPacks" class="space-y-3"></div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
          <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Renseignements cliniques / indication<textarea id="dmeExamIndication" rows="3" maxlength="4000" class="mt-1 w-full rounded-xl border border-slate-200 p-3 text-xs bg-slate-50 dark:bg-black/20 dark:border-white/10"></textarea></label>
          <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Consignes / précisions<textarea id="dmeExamNotes" rows="3" maxlength="4000" class="mt-1 w-full rounded-xl border border-slate-200 p-3 text-xs bg-slate-50 dark:bg-black/20 dark:border-white/10"></textarea></label>
        </div>
        <p id="dmeExamMessage" role="status" class="text-xs font-semibold text-slate-600 dark:text-slate-300"></p>
        <div class="flex flex-wrap gap-2">
          <button type="button" data-exam-action="save" class="rounded-xl bg-brand-neon px-4 py-2.5 text-xs font-extrabold text-black">Enregistrer la demande</button>
          <button type="button" data-exam-action="save-print" class="rounded-xl bg-slate-900 px-4 py-2.5 text-xs font-extrabold text-white">Enregistrer et imprimer / PDF</button>
        </div>
      </section>
      <section class="bg-white dark:bg-[#121c15] rounded-3xl border border-slate-200 dark:border-white/10 p-5">
        <h4 class="font-extrabold text-xs text-slate-700 dark:text-white mb-3">Demandes enregistrées dans cette consultation</h4>
        <div id="dmeExamHistory" class="space-y-3"><p class="text-xs text-slate-400">Chargement…</p></div>
      </section>
      <div id="dmeExamCatalogModal" class="hidden fixed inset-0 z-[210] bg-black/60 backdrop-blur-sm items-center justify-center p-4">
        <div class="bg-white dark:bg-[#121c15] border border-slate-200 dark:border-white/10 rounded-3xl w-full max-w-3xl max-h-[90vh] overflow-y-auto p-6 space-y-4">
          <div class="flex justify-between items-center gap-3"><h3 id="dmeExamCatalogTitle" class="font-extrabold text-sm text-slate-800 dark:text-white">Catalogue des bilans</h3><button type="button" data-exam-action="close-catalog" data-dme-allow-readonly class="rounded-xl p-2 text-slate-500" aria-label="Fermer le catalogue">✕</button></div>
          <div id="dmeExamCatalogList" class="max-h-48 overflow-y-auto space-y-2"></div>
          <form id="dmeExamCatalogForm" class="space-y-3">
            <input type="hidden" id="dmeExamCatalogId">
            <label class="block text-xs font-bold text-slate-600 dark:text-slate-300">Nom du bilan<input id="dmeExamCatalogName" required maxlength="160" class="mt-1 w-full rounded-xl border border-slate-200 p-2.5 text-xs bg-slate-50 dark:bg-black/20 dark:border-white/10" placeholder="Nom du bilan personnalisé"></label>
            <label class="block text-xs font-bold text-slate-600 dark:text-slate-300">Paramètres à analyser / radios ou examens du bilan<textarea id="dmeExamCatalogItems" required rows="7" class="mt-1 w-full rounded-xl border border-slate-200 p-3 text-xs bg-slate-50 dark:bg-black/20 dark:border-white/10" placeholder="Un paramètre ou examen par ligne&#10;Nom | précision facultative"></textarea></label>
            <p class="text-[11px] text-slate-500">Une ligne par analyse ou examen. Ajouter « | » pour préciser la technique, la localisation ou une consigne. Les modifications du catalogue ne changent pas les demandes déjà enregistrées.</p>
            <p id="dmeExamCatalogMessage" role="status" class="text-xs font-semibold text-slate-600"></p>
            <div class="flex gap-2"><button type="submit" class="rounded-xl bg-indigo-600 px-4 py-2.5 text-xs font-bold text-white">Enregistrer dans le catalogue</button><button type="button" data-exam-action="new-catalog" data-dme-allow-readonly class="rounded-xl border border-slate-200 px-4 py-2.5 text-xs font-bold text-slate-600">Nouveau bilan</button></div>
          </form>
        </div>
      </div>
    </div>'''


def exam_requests_script():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    return (root / 'static' / 'js' / 'dme_exam_requests.js').read_text(encoding='utf-8')
