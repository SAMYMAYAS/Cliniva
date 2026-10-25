"""Module de gestion des modèles de comptes rendus médicaux (Word .docx & HTML A4).

Gère la bibliothèque typographique, l'ouverture native dans Microsoft Word,
l'éditeur TinyMCE A4 avec macros et la persistance sur PostgreSQL.
Conforme strictement aux normes PEP 8.
"""

from datetime import datetime
import json
import logging
import os
from pathlib import Path
import platform
import shutil
import subprocess
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from database import CR_TEMPLATES_DIR, MODEL_TEMPLATE_DIR, get_db_cursor

logger = logging.getLogger("CrTemplatesModule")

cr_templates_router = APIRouter(prefix="/api/cr-templates", tags=["Modèles CR"])

BASE_CR_TEMPLATE_PATH = os.path.abspath(
    os.path.join(MODEL_TEMPLATE_DIR, "BASE_CR_TYPE.docx")
)

DEFAULT_HTML_REPORT_TEMPLATE = """<p><strong><span style="text-decoration: underline; font-size: 12pt;">INDICATION CLINIQUE :</span></strong></p>
<p style="font-size: 12pt;">Bilan d'exploration et contrôle radiologique.</p>
<p style="font-size: 12pt;">&nbsp;</p>
<p><strong><span style="text-decoration: underline; font-size: 12pt;">PROTOCOLE ET TECHNIQUE :</span></strong></p>
<p style="font-size: 12pt;">Examen réalisé selon le protocole standard d'imagerie médicale.</p>
<p style="font-size: 12pt;">&nbsp;</p>
<p><strong><span style="text-decoration: underline; font-size: 12pt;">RÉSULTATS :</span></strong></p>
<p style="font-size: 12pt;">Absence d'anomalie morphologique ou structurelle décelable.</p>
<p style="font-size: 12pt;">Structures anatomiques d'aspect et de dimensions normaux.</p>
<p style="font-size: 12pt;">&nbsp;</p>
<p><strong><span style="text-decoration: underline; font-size: 12pt; color: #166534;">CONCLUSION :</span></strong></p>
<p style="font-size: 12pt;"><strong>Examen sans anomalie radiologique notable.</strong></p>"""


# ============================================================================
# LOGIQUE MÉTIER & GESTION DES FICHIERS WORD
# ============================================================================
class ReportTemplateManager:
    """Gestionnaire métier des modèles de comptes rendus."""

    @classmethod
    def create_docx_from_base_model(cls, destination_path: str) -> bool:
        """Duplique le fichier modèle de base Word vers la destination cible."""
        try:
            dest_abs = os.path.abspath(destination_path)
            os.makedirs(os.path.dirname(dest_abs), exist_ok=True)

            if os.path.exists(BASE_CR_TEMPLATE_PATH):
                shutil.copy2(BASE_CR_TEMPLATE_PATH, dest_abs)
                return True

            # Si le modèle maître n'existe pas encore, on crée un fichier vide initial
            if not os.path.exists(dest_abs):
                with open(dest_abs, "wb") as f:
                    f.write(b"")
            return True
        except Exception as err:
            logger.error("Erreur duplication DOCX : %s", err)
            return False

    @classmethod
    def open_file_in_word(cls, filepath: str) -> Dict[str, Any]:
        """Ouvre le fichier .docx dans Microsoft Word sous Windows."""
        if not filepath:
            return {"success": False, "message": "Chemin du fichier non spécifié."}

        abs_path = os.path.abspath(os.path.normpath(filepath))

        if not os.path.exists(abs_path):
            cls.create_docx_from_base_model(abs_path)

        if not os.path.exists(abs_path):
            return {"success": False, "message": f"Fichier introuvable : {abs_path}"}

        try:
            if platform.system() == "Windows":
                os.startfile(abs_path)
            elif platform.system() == "Darwin":
                subprocess.call(["open", abs_path])
            else:
                subprocess.call(["xdg-open", abs_path])

            return {
                "success": True,
                "message": "Document ouvert avec succès dans Microsoft Word.",
            }
        except Exception as e:
            return {"success": False, "message": f"Impossible de lancer Word : {str(e)}"}

    def get_all(self) -> List[Dict[str, Any]]:
        """Récupère l'ensemble des modèles enregistrés dans PostgreSQL."""
        with get_db_cursor() as cursor:
            cursor.execute("""
                           SELECT id,
                                  designation,
                                  type_modele,
                                  modalite,
                                  service,
                                  utilisateur,
                                  chemin,
                                  contenu_html,
                                  date_heure_ajout
                           FROM cr_templates
                           ORDER BY id DESC;
                           """)
            rows = cursor.fetchall()

        templates = []
        for r in rows:
            t = dict(r)
            if t["chemin"]:
                t["chemin"] = os.path.abspath(t["chemin"])
            templates.append(t)
        return templates

    def count(self) -> int:
        """Retourne le nombre total de modèles."""
        with get_db_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total FROM cr_templates;")
            row = cursor.fetchone()
            return row["total"] if row else 0

    def get_modalites(self) -> List[str]:
        """Récupère le catalogue des modalités géré dans Gestion des spécialités."""
        with get_db_cursor() as cursor:
            cursor.execute("SELECT nom FROM ref_modalites ORDER BY nom ASC;")
            return [r["nom"] for r in cursor.fetchall()]

    def get_services(self) -> List[str]:
        """Récupère les spécialités disponibles dans le référentiel système."""
        with get_db_cursor() as cursor:
            cursor.execute("SELECT nom FROM ref_specialites ORDER BY nom ASC;")
            return [r["nom"] for r in cursor.fetchall()]

    def save(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Crée ou modifie un modèle de compte rendu."""
        t_id = data.get("id")
        designation = str(data.get("designation") or "").strip()
        type_modele = str(data.get("type_modele") or "Word").strip()
        modalite = str(data.get("modalite") or "IRM").strip()
        service = str(data.get("service") or "").strip()
        utilisateur = str(data.get("utilisateur") or "admin").strip()
        contenu_html = data.get("contenu_html") or DEFAULT_HTML_REPORT_TEMPLATE
        date_heure_ajout = datetime.now().strftime("%Y-%m-%d %H:%M")

        if not designation or not modalite or not service:
            return {
                "success": False,
                "message": "La Désignation, la Modalité et le Service sont obligatoires.",
            }

        safe_name = "".join([c if c.isalnum() else "_" for c in designation]).strip("_")
        abs_cr_dir = os.path.abspath(CR_TEMPLATES_DIR)
        os.makedirs(abs_cr_dir, exist_ok=True)
        file_path = os.path.normpath(os.path.join(abs_cr_dir, f"CR_TYPE_{safe_name}.docx"))

        if type_modele == "Word" and not os.path.exists(file_path):
            self.create_docx_from_base_model(file_path)

        with get_db_cursor() as cursor:
            if t_id:
                query = """
                        UPDATE cr_templates \
                        SET designation  = %s, \
                            type_modele  = %s, \
                            modalite     = %s, \
                            service      = %s, \
                            contenu_html = %s, \
                            chemin       = %s
                        WHERE id = %s; \
                        """
                cursor.execute(query, (
                    designation, type_modele, modalite, service, contenu_html,
                    file_path if type_modele == "Word" else "", t_id
                ))
                saved_id = t_id
            else:
                query = """
                        INSERT INTO cr_templates
                        (designation, type_modele, modalite, service, utilisateur, chemin, contenu_html, \
                         date_heure_ajout)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id; \
                        """
                cursor.execute(query, (
                    designation, type_modele, modalite, service, utilisateur,
                    file_path if type_modele == "Word" else "", contenu_html, date_heure_ajout
                ))
                res = cursor.fetchone()
                saved_id = res["id"] if res else None

        return {
            "success": True,
            "message": "Modèle de compte rendu enregistré avec succès !",
            "open_word": (type_modele == "Word"),
            "template_id": saved_id,
            "designation": designation,
        }

    def delete(self, t_id: int) -> Dict[str, Any]:
        """Supprime un modèle."""
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM cr_templates WHERE id = %s;", (t_id,))
        return {"success": True, "message": "Modèle supprimé avec succès."}


manager = ReportTemplateManager()


# ============================================================================
# POINTS D'ACCÈS FASTAPI (ROUTES JSON & TÉLÉCHARGEMENT)
# ============================================================================
@cr_templates_router.get("")
async def api_get_cr_templates():
    try:
        templates = manager.get_all()
        total = manager.count()
        modalites = manager.get_modalites()
        services = manager.get_services()
        return {
            "templates": templates,
            "total_count": total,
            "modalites": modalites,
            "services": services,
        }
    except Exception as exc:
        logger.error("Erreur GET /api/cr-templates: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@cr_templates_router.post("/save")
async def api_save_cr_template(payload: Dict[str, Any]):
    return manager.save(payload)


@cr_templates_router.post("/delete")
async def api_delete_cr_template(payload: Dict[str, Any]):
    return manager.delete(payload.get("id"))


@cr_templates_router.post("/open-word")
async def api_open_word(payload: Dict[str, Any]):
    t_id = payload.get("id")
    filepath = payload.get("filepath")

    if t_id:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT chemin, designation FROM cr_templates WHERE id = %s;", (t_id,))
            row = cursor.fetchone()
            if row and row["chemin"]:
                filepath = row["chemin"]
            elif row:
                safe_name = "".join([c if c.isalnum() else "_" for c in row["designation"]]).strip("_")
                filepath = os.path.join(CR_TEMPLATES_DIR, f"CR_TYPE_{safe_name}.docx")

    return manager.open_file_in_word(filepath)


@cr_templates_router.get("/download-word/{template_id}")
async def api_download_word(template_id: int):
    with get_db_cursor() as cursor:
        cursor.execute("SELECT chemin, designation FROM cr_templates WHERE id = %s;", (template_id,))
        row = cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Modèle introuvable.")

    filepath = row["chemin"]
    if not filepath or not os.path.exists(filepath):
        safe_name = "".join([c if c.isalnum() else "_" for c in row["designation"]]).strip("_")
        filepath = os.path.join(CR_TEMPLATES_DIR, f"CR_TYPE_{safe_name}.docx")
        manager.create_docx_from_base_model(filepath)

    abs_path = os.path.abspath(filepath)
    filename = os.path.basename(abs_path)
    return FileResponse(abs_path, filename=filename,
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@cr_templates_router.post("/upload-word/{template_id}")
async def api_upload_word(template_id: int, file: UploadFile = File(...)):
    with get_db_cursor() as cursor:
        cursor.execute("SELECT chemin, designation FROM cr_templates WHERE id = %s;", (template_id,))
        row = cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Modèle introuvable.")

    filepath = row["chemin"]
    if not filepath:
        safe_name = "".join([c if c.isalnum() else "_" for c in row["designation"]]).strip("_")
        filepath = os.path.join(CR_TEMPLATES_DIR, f"CR_TYPE_{safe_name}.docx")
        with get_db_cursor() as cursor:
            cursor.execute("UPDATE cr_templates SET chemin = %s WHERE id = %s;", (filepath, template_id))

    abs_path = os.path.abspath(filepath)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)

    contents = await file.read()
    with open(abs_path, "wb") as f:
        f.write(contents)

    return {"success": True, "message": "Fichier Word synchronisé avec succès sur le serveur central !"}


# ============================================================================
# RENDU DU COMPOSANT HTML ET JAVASCRIPT AVEC TINYMCE ET A4
# ============================================================================
def render_html() -> str:
    """Génère l'HTML complet avec Dark Mode, Modales A4 et pagination par 15."""
    return """
    <!-- Librairies pour l'éditeur A4 et l'export PDF -->
    <script src="/static/vendor/tinymce/tinymce.min.js"></script>
    <script src="/static/vendor/js/html2pdf.bundle.min.js"></script>

    <style>
        .a4-page-sheet {
            width: 210mm;
            min-height: 297mm;
            padding: 15mm 20mm;
            margin: auto;
            background: white;
            box-shadow: 0 10px 25px rgba(0, 0, 0, 0.15);
            font-family: 'Times New Roman', Times, serif;
            color: black;
            box-sizing: border-box;
            position: relative;
        }

        .tox-tinymce {
            border-radius: 20px !important;
            border: 1px solid #cbd5e1 !important;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05) !important;
        }

        @media print {
            body * { visibility: hidden; }
            #printableA4Area, #printableA4Area * { visibility: visible; }
            #printableA4Area {
                position: absolute;
                left: 0;
                top: 0;
                width: 100%;
                padding: 10mm 15mm !important;
                box-shadow: none !important;
            }
        }
    </style>

    <div id="view-report-templates" class="space-y-6">

        <!-- BANDEAU D'EN-TÊTE -->
        <div class="banner-glow rounded-3xl p-6 lg:p-8 text-white shadow-xl flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
            <div>
                <h1 class="text-2xl font-extrabold tracking-tight mb-1">Bibliothèque & Modèles de Comptes Rendus</h1>
                <p class="text-xs text-slate-300">Gestion des modèles Word (.docx) avec synchronisation automatique et modèles HTML au format A4 réel.</p>
            </div>

            <div class="flex items-center gap-3">
                <div class="bg-white/10 px-4 py-2 rounded-2xl backdrop-blur-sm border border-white/10 text-xs">
                    <span class="text-slate-300">Total Modèles:</span> 
                    <strong id="totalCrTemplatesCount" class="text-brand-neon font-black text-sm ml-1">0</strong>
                </div>

                <button type="button" onclick="openCrTemplateModal()" class="flex items-center gap-2 bg-brand-neon hover:bg-brand-neonDark text-black px-5 py-2.5 rounded-2xl font-bold text-xs shadow-md shadow-brand-neon/20 transition cursor-pointer">
                    <i class="ph-bold ph-plus-circle text-base"></i>
                    <span>Nouveau Modèle CR</span>
                </button>
            </div>
        </div>

        <!-- BARRE DE RECHERCHE -->
        <div class="flex flex-col lg:flex-row lg:items-center gap-3 bg-white dark:bg-[#121c15] p-3.5 rounded-3xl shadow-sm border border-slate-100 dark:border-white/5 transition-colors">
            <div class="flex items-center gap-2.5 bg-slate-50 dark:bg-black/30 rounded-2xl px-4 py-2 border border-slate-200/80 dark:border-white/10 w-full lg:flex-1">
                <i class="ph ph-magnifying-glass text-slate-400 text-base"></i>
                <input type="text" id="searchCrTemplateInput" oninput="filterCrTemplates()" placeholder="Rechercher par désignation, modalité, service, type (Word/HTML)..." class="w-full text-xs outline-none bg-transparent text-slate-700 dark:text-slate-200">
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-2.5 w-full lg:w-auto lg:min-w-[24rem]">
                <select id="crTemplateFilterModalite" onchange="filterCrTemplates()" aria-label="Choisir une catégorie ou modalité" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-700 dark:text-slate-200">
                    <option value="">Choisir une catégorie / modalité…</option>
                </select>
                <select id="crTemplateFilterService" onchange="filterCrTemplates()" aria-label="Choisir un service rattaché" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-700 dark:text-slate-200">
                    <option value="">Choisir un service…</option>
                </select>
            </div>
        </div>

        <!-- TABLEAU DES MODÈLES CR -->
        <div class="bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 flex flex-col transition-colors">
            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                    <thead>
                        <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-3">
                            <th class="pb-3 font-medium w-16 text-center">Type</th>
                            <th class="pb-3 font-medium">ID</th>
                            <th class="pb-3 font-medium">Désignation du Modèle</th>
                            <th class="pb-3 font-medium">Format</th>
                            <th class="pb-3 font-medium">Modalité</th>
                            <th class="pb-3 font-medium">Service Rattaché</th>
                            <th class="pb-3 font-medium">Auteur & Date</th>
                            <th class="pb-3 font-medium text-right pr-4">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="cr-templates-table-body" class="divide-y divide-slate-100 dark:divide-white/5 text-slate-700 dark:text-slate-300 font-medium"></tbody>
                </table>
            </div>

            <!-- PAGINATION STRICTE PAR 15 LIGNES -->
            <div id="crPaginationWrapper" class="flex flex-col sm:flex-row items-center justify-between gap-3 pt-5 mt-3 border-t border-slate-100 dark:border-white/5 text-xs">
                <span id="crPaginationInfo" class="text-slate-500 dark:text-slate-400 font-medium">Affichage des modèles...</span>
                <div class="flex items-center gap-1.5" id="crPaginationControls"></div>
            </div>
        </div>

    </div>

    <!-- ================= MODALE CRÉATION / ÉDITION MODÈLE CR ================= -->
    <div id="crTemplateModal" class="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center hidden z-50 p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-5xl max-h-[96vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">

            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50/50 dark:bg-black/20">
                <div class="flex items-center space-x-2">
                    <div class="w-8 h-8 rounded-xl bg-brand-neon/20 text-brand-neon flex items-center justify-center font-bold text-sm">
                        <i class="ph-bold ph-file-text"></i>
                    </div>
                    <h3 id="crTemplateModalTitle" class="text-base font-bold text-slate-800 dark:text-white">Ajouter un Modèle de Compte Rendu</h3>
                </div>
                <button type="button" onclick="closeCrTemplateModal()" class="text-slate-400 hover:text-white p-1 rounded-lg">
                    <i class="ph-bold ph-x text-lg"></i>
                </button>
            </div>

            <form id="crTemplateForm" onsubmit="handleCrTemplateSubmit(event)" class="p-6 overflow-y-auto space-y-5">
                <input type="hidden" id="crTemplateId">

                <div class="grid grid-cols-1 md:grid-cols-12 gap-3 items-end">
                    <div class="md:col-span-6">
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Désignation du Modèle CR Type *</label>
                        <input type="text" id="crTemplateDesignation" required placeholder="Ex: CR Type - IRM Cérébrale Normale, Scanner Thoracique..." class="w-full text-xs p-3 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white">
                    </div>
                    <div class="md:col-span-3">
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Modalité *</label>
                        <select id="crTemplateModalite" required class="w-full text-xs p-3 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-medium text-slate-800 dark:text-white"></select>
                    </div>
                    <div class="md:col-span-3">
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Service Rattaché *</label>
                        <select id="crTemplateService" required class="w-full text-xs p-3 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-medium text-slate-800 dark:text-white"></select>
                    </div>
                </div>

                <!-- CHOIX DU FORMAT (WORD OU HTML A4) -->
                <div>
                    <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1.5">Format du Document *</label>
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        <label class="flex items-center space-x-3 p-3.5 bg-slate-50 dark:bg-black/30 rounded-2xl border border-slate-200 dark:border-white/10 cursor-pointer hover:border-brand-neon transition">
                            <input type="radio" name="crTypeRadio" value="Word" checked onchange="toggleCrTypeView()" class="text-brand-neon focus:ring-brand-neon">
                            <div>
                                <span class="text-xs font-bold text-slate-800 dark:text-white block">Modèle Word (.docx)</span>
                                <span class="text-[10px] text-slate-400">Duplication directe et ouverture dans Microsoft Word</span>
                            </div>
                        </label>
                        <label class="flex items-center space-x-3 p-3.5 bg-slate-50 dark:bg-black/30 rounded-2xl border border-slate-200 dark:border-white/10 cursor-pointer hover:border-brand-neon transition">
                            <input type="radio" name="crTypeRadio" value="HTML" onchange="toggleCrTypeView()" class="text-brand-neon focus:ring-brand-neon">
                            <div>
                                <span class="text-xs font-bold text-slate-800 dark:text-white block">Modèle HTML / Feuille A4</span>
                                <span class="text-[10px] text-slate-400">Éditeur TinyMCE 6 Pro avec feuille A4 intégrée & Export PDF</span>
                            </div>
                        </label>
                    </div>
                </div>

                <!-- INFORMATION MODE WORD -->
                <div id="boxWordInfo" class="p-4 rounded-2xl bg-sky-500/10 border border-sky-500/30 text-sky-400 text-xs space-y-1.5">
                    <div class="font-bold flex items-center gap-1.5">
                        <i class="ph-bold ph-microsoft-word-logo text-base"></i>
                        <span>Synchronisation Word Active :</span>
                    </div>
                    <p class="text-[11px] text-sky-300 leading-snug">Le modèle s'ouvrira directement dans <strong>Microsoft Word</strong> sur votre PC. Enregistrez vos modifications normalement dans Word (<kbd class="bg-black/40 px-1.5 py-0.5 rounded border border-white/20 font-mono">Ctrl + S</kbd>).</p>
                </div>

                <!-- ÉDITEUR TINYMCE FEUILLE A4 -->
                <div id="boxHtmlEditor" class="space-y-3 hidden">
                    <div class="flex items-center justify-between">
                        <label class="block text-[11px] font-bold text-slate-700 dark:text-slate-300">Rédacteur de Compte Rendu (Zone A4)</label>
                        <span class="text-[10px] text-brand-neon font-semibold">Format A4 Standard (210mm × 297mm)</span>
                    </div>

                    <!-- MACROS / VARIABLES DYNAMIQUES -->
                    <div class="bg-slate-50 dark:bg-black/30 p-2.5 rounded-2xl border border-slate-200 dark:border-white/10 flex flex-wrap gap-1.5 items-center">
                        <span class="text-[10px] font-bold text-slate-400 uppercase mr-1">Insérer Variable :</span>
                        <button type="button" onclick="insertMacroIntoEditor('${Nom}')" class="px-2.5 py-1 bg-white dark:bg-[#121c15] hover:bg-brand-neon/20 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10 rounded-lg text-[10px] font-mono font-bold transition">${Nom}</button>
                        <button type="button" onclick="insertMacroIntoEditor('${Date_nais}')" class="px-2.5 py-1 bg-white dark:bg-[#121c15] hover:bg-brand-neon/20 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10 rounded-lg text-[10px] font-mono font-bold transition">${Date_nais}</button>
                        <button type="button" onclick="insertMacroIntoEditor('${med}')" class="px-2.5 py-1 bg-white dark:bg-[#121c15] hover:bg-brand-neon/20 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10 rounded-lg text-[10px] font-mono font-bold transition">${med}</button>
                        <button type="button" onclick="insertMacroIntoEditor('${sec}')" class="px-2.5 py-1 bg-white dark:bg-[#121c15] hover:bg-brand-neon/20 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10 rounded-lg text-[10px] font-mono font-bold transition">${sec}</button>
                        <button type="button" onclick="insertMacroIntoEditor('${id_av}')" class="px-2.5 py-1 bg-white dark:bg-[#121c15] hover:bg-brand-neon/20 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10 rounded-lg text-[10px] font-mono font-bold transition">${id_av}</button>
                        <button type="button" onclick="insertMacroIntoEditor('${Date}')" class="px-2.5 py-1 bg-white dark:bg-[#121c15] hover:bg-brand-neon/20 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10 rounded-lg text-[10px] font-mono font-bold transition">${Date}</button>
                        <button type="button" onclick="insertMacroIntoEditor('${Titre}')" class="px-2.5 py-1 bg-white dark:bg-[#121c15] hover:bg-brand-neon/20 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10 rounded-lg text-[10px] font-mono font-bold transition">${Titre}</button>
                        <button type="button" onclick="insertMacroIntoEditor('${cor}')" class="px-2.5 py-1 bg-white dark:bg-[#121c15] hover:bg-brand-neon/20 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10 rounded-lg text-[10px] font-mono font-bold transition">${cor}</button>
                        <button type="button" onclick="insertMacroIntoEditor('${signe}')" class="px-2.5 py-1 bg-white dark:bg-[#121c15] hover:bg-brand-neon/20 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10 rounded-lg text-[10px] font-mono font-bold transition">${signe}</button>
                    </div>

                    <textarea id="crTinyMceEditor"></textarea>
                </div>

                <div class="flex items-center justify-end space-x-3 pt-4 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closeCrTemplateModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler</button>
                    <button type="submit" class="px-6 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md shadow-brand-neon/20">Enregistrer le Modèle</button>
                </div>
            </form>
        </div>
    </div>

    <!-- ================= MODALE APERÇU A4 RÉEL AVEC IMPRESSION & PDF ================= -->
    <div id="htmlPrintPreviewModal" class="fixed inset-0 bg-black/80 backdrop-blur-md flex items-center justify-center hidden z-50 p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-5xl max-h-[96vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">

            <div class="px-6 py-3.5 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50 dark:bg-black/20">
                <div class="flex items-center space-x-2">
                    <span class="w-2.5 h-2.5 rounded-full bg-brand-neon"></span>
                    <h3 id="printPreviewTitle" class="text-sm font-bold text-slate-800 dark:text-white">Aperçu Compte Rendu A4</h3>
                </div>

                <div class="flex items-center space-x-2.5">
                    <button type="button" onclick="printHtmlA4()" class="px-4 py-2 rounded-xl bg-black text-white text-xs font-bold shadow-sm inline-flex items-center gap-1.5 transition hover:opacity-90">
                        <i class="ph-bold ph-printer text-brand-neon"></i>
                        <span>Imprimer en A4</span>
                    </button>
                    <button type="button" onclick="exportHtmlToPdf()" class="px-4 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-bold shadow-sm inline-flex items-center gap-1.5 transition">
                        <i class="ph-bold ph-file-pdf"></i>
                        <span>Exporter en PDF</span>
                    </button>
                    <button type="button" onclick="closeHtmlPrintPreview()" class="text-slate-400 hover:text-white p-1 rounded-lg">
                        <i class="ph-bold ph-x text-lg"></i>
                    </button>
                </div>
            </div>

            <!-- FEUILLE A4 BLANCHE NORME MÉDICALE -->
            <div class="p-8 overflow-y-auto bg-slate-200/70 dark:bg-black/50 flex justify-center">
                <div id="printableA4Area" class="a4-page-sheet flex flex-col justify-between">
                    <div>
                        <!-- En-tête Médical Standard -->
                        <div style="border-bottom: 2px solid #1A367C; padding-bottom: 8px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: flex-end;">
                            <div style="text-align: left;">
                                <div style="font-family: Arial, sans-serif; font-size: 14pt; color: #1A367C; font-weight: bold; line-height: 1.2;">Cliniva Medical Center</div>
                                <div style="font-family: Arial, sans-serif; font-size: 12pt; color: #1A367C; font-weight: normal; line-height: 1.2;">Service d’Imagerie Médicale & Explorations</div>
                            </div>
                            <div style="text-align: right; direction: rtl;">
                                <div style="font-family: Arial, sans-serif; font-size: 15pt; color: #1A367C; font-weight: bold; line-height: 1.2;">مركز التشخيص الطبي</div>
                                <div style="font-family: Arial, sans-serif; font-size: 14pt; color: #1A367C; font-weight: normal; line-height: 1.2;">مصلحة التصوير الطبي</div>
                            </div>
                        </div>

                        <!-- Variables Patients / Médecin -->
                        <div style="display: flex; justify-content: space-between; margin-bottom: 25px; font-size: 11pt; font-weight: bold; line-height: 1.4;">
                            <div>
                                <div>${Nom}</div>
                                <div>${Date_nais}</div>
                                <div>${med}</div>
                                <div>${sec}</div>
                                <div>${id_av}</div>
                            </div>
                            <div style="text-align: right;">
                                <div>${Date}</div>
                            </div>
                        </div>

                        <div id="a4PreviewTitre" style="text-align: center; font-size: 16pt; font-weight: bold; margin: 30px 0 20px 0; color: black;">
                            ${Titre}
                        </div>

                        <div id="a4PreviewBody" style="font-size: 11pt; line-height: 1.5; color: #111827;">
                            ${cor}
                        </div>
                    </div>

                    <div>
                        <div style="text-align: right; font-size: 11pt; font-weight: bold; margin-bottom: 35px;">
                            ${signe}
                        </div>

                        <div style="display: flex; justify-content: space-between; align-items: center; border-top: 1px solid #E2E8F0; padding-top: 10px; margin-top: 15px;">
                            <div style="display: flex; align-items: center; gap: 8px;">
                                <span style="font-family: Arial, sans-serif; font-weight: bold; font-size: 11pt; color: #1A264A;">CLINIVA</span>
                                <span style="font-family: Arial, sans-serif; font-size: 9pt; color: #64748B;">HEALTHCARE OS</span>
                            </div>
                            <div style="font-family: Arial, sans-serif; font-size: 11pt; font-weight: bold; color: #1A264A;">
                                STANDARD MÉDICAL <span style="color: #8be140; margin-left: 6px;">✆ +213 (0) 23 45 67 89</span>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- SCRIPT DU MODULE MODÈLES CR -->
    <script>
        (function() {
            let crTemplatesData = [];
            let filteredCrTemplatesList = [];
            let crModalitesList = [];
            let crServicesList = [];
            let isTinyMceInitialized = false;
            let currentActiveTemplate = null;

            let currentCrPage = 1;
            const CR_PER_PAGE = 15;

            window.loadReportTemplatesModule = async function() {
                try {
                    const res = await fetch('/api/cr-templates');
                    const data = await res.json();
                    crTemplatesData = data.templates || [];
                    crModalitesList = data.modalites || [];
                    crServicesList = data.services || [];

                    document.getElementById('totalCrTemplatesCount').innerText = data.total_count || crTemplatesData.length;
                    populateCrDropdowns();
                    filterCrTemplates();
                } catch (err) {
                    console.error("Erreur chargement modèles CR:", err);
                }
            };

            window.filterCrTemplates = function() {
                const q = (document.getElementById('searchCrTemplateInput')?.value || '').toLowerCase().trim();
                const modalite = document.getElementById('crTemplateFilterModalite')?.value || '';
                const service = document.getElementById('crTemplateFilterService')?.value || '';
                filteredCrTemplatesList = (!modalite && !service) ? [] : crTemplatesData.filter(t => {
                    const matchesText = !q ||
                        (t.designation && t.designation.toLowerCase().includes(q)) ||
                        (t.modalite && t.modalite.toLowerCase().includes(q)) ||
                        (t.service && t.service.toLowerCase().includes(q)) ||
                        (t.type_modele && t.type_modele.toLowerCase().includes(q)) ||
                        (t.utilisateur && t.utilisateur.toLowerCase().includes(q)) ||
                        (t.date_heure_ajout && t.date_heure_ajout.toLowerCase().includes(q)) ||
                        String(t.id).includes(q);
                    return matchesText && (!modalite || t.modalite === modalite) && (!service || t.service === service);
                }).sort((a, b) => String(a.designation || '').localeCompare(String(b.designation || ''), 'fr', { sensitivity: 'base', numeric: true })
                    || String(a.modalite || '').localeCompare(String(b.modalite || ''), 'fr', { sensitivity: 'base' })
                    || String(a.service || '').localeCompare(String(b.service || ''), 'fr', { sensitivity: 'base' }));
                currentCrPage = 1;
                renderCrTemplatesTable();
            };

            window.renderCrTemplatesTable = function() {
                const tbody = document.getElementById('cr-templates-table-body');
                tbody.innerHTML = '';

                const totalItems = filteredCrTemplatesList.length;
                const totalPages = Math.ceil(totalItems / CR_PER_PAGE) || 1;
                const category = document.getElementById('crTemplateFilterModalite')?.value || '';
                const service = document.getElementById('crTemplateFilterService')?.value || '';
                const hasSelection = Boolean(category || service);

                if (currentCrPage > totalPages) currentCrPage = totalPages;
                if (currentCrPage < 1) currentCrPage = 1;

                const startIndex = (currentCrPage - 1) * CR_PER_PAGE;
                const endIndex = startIndex + CR_PER_PAGE;
                const pageItems = filteredCrTemplatesList.slice(startIndex, endIndex);

                if (!hasSelection) {
                    tbody.innerHTML = '<tr><td colspan="8" class="py-14 text-center"><div class="mx-auto flex max-w-lg flex-col items-center gap-3"><span class="flex h-14 w-14 items-center justify-center rounded-2xl bg-emerald-50 text-emerald-600 dark:bg-emerald-950/40 dark:text-emerald-300"><i class="ph-bold ph-funnel text-2xl"></i></span><strong class="text-sm text-slate-700 dark:text-slate-200">Choisissez une catégorie ou un service</strong><span class="text-xs text-slate-500 dark:text-slate-400">Les modèles de comptes rendus correspondants s’afficheront par ordre alphabétique.</span></div></td></tr>';
                } else if (pageItems.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="8" class="text-center py-8 text-slate-400">Aucun modèle de compte rendu ne correspond à cette sélection.</td></tr>';
                } else {
                    pageItems.forEach(t => {
                        const tr = document.createElement('tr');
                        tr.className = "hover:bg-slate-50/80 dark:hover:bg-white/[0.02] transition-colors";

                        // ICÔNES VISUELLES AGRANDIES
                        const typeIconHtml = t.type_modele === 'Word'
                            ? `<div class="flex justify-center items-center" title="Document Word (.docx)">
                                 <div class="w-11 h-11 rounded-2xl bg-sky-50 dark:bg-sky-950/40 border border-sky-200 dark:border-sky-800/40 flex flex-col items-center justify-center shadow-sm">
                                    <i class="ph-bold ph-microsoft-word-logo text-xl text-sky-600 dark:text-sky-300"></i>
                                    <span class="text-[9px] font-black text-white bg-sky-600 px-1 py-0.2 rounded font-sans tracking-tight -mt-0.5">DOC</span>
                                 </div>
                               </div>`
                            : `<div class="flex justify-center items-center" title="Document HTML / A4">
                                 <div class="w-11 h-11 rounded-2xl bg-amber-50 dark:bg-amber-950/40 border border-amber-200 dark:border-amber-800/40 flex flex-col items-center justify-center shadow-sm">
                                    <i class="ph-bold ph-code text-xl text-amber-600 dark:text-amber-300"></i>
                                    <span class="text-[9px] font-black text-white bg-amber-600 px-1 py-0.2 rounded font-sans tracking-tight -mt-0.5">HTML</span>
                                 </div>
                               </div>`;

                        const formatBadge = t.type_modele === 'Word'
                            ? '<span class="px-2.5 py-1 rounded-xl text-[10px] font-bold bg-sky-50 dark:bg-sky-950/40 text-sky-600 dark:text-sky-300 border border-sky-200 dark:border-sky-800/40">Word (.docx)</span>'
                            : '<span class="px-2.5 py-1 rounded-xl text-[10px] font-bold bg-amber-50 dark:bg-amber-950/40 text-amber-600 dark:text-amber-300 border border-amber-200 dark:border-amber-800/40">HTML A4</span>';

                        const actionBtn = t.type_modele === 'Word'
                            ? `<button onclick="openWordTemplateById(${t.id}, '${t.designation.replace(/'/g, "\\'")}')" class="p-1.5 text-emerald-700 dark:text-brand-neon bg-brand-neon/10 hover:bg-brand-neon/20 rounded-xl font-bold text-[11px] inline-flex items-center gap-1"><i class="ph-bold ph-pencil-simple"></i> Ouvrir Word</button><button onclick="uploadWordTemplateById(${t.id})" class="p-1.5 text-sky-700 dark:text-sky-300 bg-sky-500/10 hover:bg-sky-500/20 rounded-xl font-bold text-[11px] inline-flex items-center gap-1"><i class="ph-bold ph-upload-simple"></i> Synchroniser</button>`
                            : `<button onclick="openHtmlPrintPreview(${t.id})" class="p-1.5 text-sky-700 dark:text-sky-300 bg-sky-500/10 hover:bg-sky-500/20 rounded-xl font-bold text-[11px] inline-flex items-center gap-1"><i class="ph-bold ph-printer"></i> Imprimer / PDF</button>`;

                        tr.innerHTML = `
                            <td class="py-3 text-center">${typeIconHtml}</td>
                            <td class="py-3.5 text-slate-400 font-mono text-[11px]">#CR-${String(t.id).padStart(3, '0')}</td>
                            <td class="py-3.5 font-bold text-slate-800 dark:text-slate-100 text-xs">${t.designation}</td>
                            <td class="py-3.5">${formatBadge}</td>
                            <td class="py-3.5"><span class="px-2.5 py-1 rounded-xl text-[10px] font-bold bg-brand-neon/20 text-emerald-800 dark:text-brand-neon border border-brand-neon/30">${t.modalite}</span></td>
                            <td class="py-3.5 text-slate-700 dark:text-slate-300 font-medium">${t.service}</td>
                            <td class="py-3.5 text-slate-500 dark:text-slate-400">
                                <div class="font-bold text-slate-800 dark:text-slate-200 text-[11px]">@${t.utilisateur}</div>
                                <div class="text-[10px] text-slate-400 font-mono">${t.date_heure_ajout}</div>
                            </td>
                            <td class="py-3.5 pr-4 text-right">
                                <div class="inline-flex items-center gap-1.5">
                                    ${actionBtn}
                                    <button onclick="editCrTemplate(${t.id})" class="p-1.5 text-sky-500 hover:bg-sky-50 dark:hover:bg-sky-500/10 rounded-lg"><i class="ph-bold ph-pencil-simple text-sm"></i></button>
                                    <button onclick="deleteCrTemplate(${t.id})" class="p-1.5 text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-500/10 rounded-lg"><i class="ph-bold ph-trash text-sm"></i></button>
                                </div>
                            </td>
                        `;
                        tbody.appendChild(tr);
                    });
                }

                // GESTION DE LA PAGINATION
                const paginationWrapper = document.getElementById('crPaginationWrapper');
                const info = document.getElementById('crPaginationInfo');
                const controls = document.getElementById('crPaginationControls');

                if (hasSelection && totalItems > 0) {
                    paginationWrapper.classList.remove('hidden');
                    info.innerHTML = `Affichage de <strong>${startIndex + 1}</strong> à <strong>${Math.min(endIndex, totalItems)}</strong> sur <strong>${totalItems}</strong> modèles`;

                    if (totalItems > CR_PER_PAGE) {
                        let html = '';
                        const prevDisabled = currentCrPage <= 1 ? 'opacity-40 cursor-not-allowed' : 'hover:bg-slate-100 dark:hover:bg-white/10 cursor-pointer';
                        html += `<button onclick="setCrPage(${currentCrPage - 1})" ${currentCrPage <= 1 ? 'disabled' : ''} class="px-3 py-1 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-semibold ${prevDisabled}">◀ Précédent</button>`;

                        for (let i = 1; i <= totalPages; i++) {
                            if (i === 1 || i === totalPages || (i >= currentCrPage - 1 && i <= currentCrPage + 1)) {
                                const activeClass = i === currentCrPage 
                                    ? 'bg-brand-neon text-black font-bold shadow-sm' 
                                    : 'border border-slate-200 dark:border-white/10 hover:bg-slate-100 dark:hover:bg-white/5 text-slate-700 dark:text-slate-300';
                                html += `<button onclick="setCrPage(${i})" class="w-7 h-7 rounded-xl text-xs flex items-center justify-center transition ${activeClass}">${i}</button>`;
                            } else if (i === currentCrPage - 2 || i === currentCrPage + 2) {
                                html += `<span class="px-1 text-slate-400">...</span>`;
                            }
                        }

                        const nextDisabled = currentCrPage >= totalPages ? 'opacity-40 cursor-not-allowed' : 'hover:bg-slate-100 dark:hover:bg-white/10 cursor-pointer';
                        html += `<button onclick="setCrPage(${currentCrPage + 1})" ${currentCrPage >= totalPages ? 'disabled' : ''} class="px-3 py-1 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-semibold ${nextDisabled}">Suivant ▶</button>`;
                        controls.innerHTML = html;
                    } else {
                        controls.innerHTML = '';
                    }
                } else {
                    paginationWrapper.classList.add('hidden');
                }
            };

            window.setCrPage = function(p) {
                currentCrPage = p;
                renderCrTemplatesTable();
            };

            window.populateCrDropdowns = function() {
                const selectMod = document.getElementById('crTemplateModalite');
                const selectServ = document.getElementById('crTemplateService');
                const filterMod = document.getElementById('crTemplateFilterModalite');
                const filterServ = document.getElementById('crTemplateFilterService');

                if (selectMod) {
                    selectMod.innerHTML = crModalitesList.length
                        ? crModalitesList.map(m => `<option value="${m}">${m}</option>`).join('')
                        : '<option value="">Aucune modalité configurée</option>';
                }
                if (selectServ) {
                    selectServ.innerHTML = crServicesList.length
                        ? crServicesList.map(s => `<option value="${s}">${s}</option>`).join('')
                        : '<option value="">Aucune spécialité configurée</option>';
                }
                if (filterMod) {
                    const previousValue = filterMod.value;
                    filterMod.innerHTML = '<option value="">Choisir une catégorie / modalité…</option>' +
                        crModalitesList.map(m => `<option value="${m}">${m}</option>`).join('');
                    if (crModalitesList.includes(previousValue)) filterMod.value = previousValue;
                }
                if (filterServ) {
                    const previousValue = filterServ.value;
                    filterServ.innerHTML = '<option value="">Choisir un service…</option>' +
                        crServicesList.map(s => `<option value="${s}">${s}</option>`).join('');
                    if (crServicesList.includes(previousValue)) filterServ.value = previousValue;
                }
            };

            window.initTinyMceEditorIfNeeded = function() {
                if (!isTinyMceInitialized && window.tinymce) {
                    tinymce.init({
                        selector: '#crTinyMceEditor',
                        height: 520,
                        menubar: 'file edit view insert format tools table',
                        plugins: 'advlist autolink lists link charmap preview searchreplace visualblocks code fullscreen insertdatetime table code help wordcount',
                        toolbar: 'undo redo | fontfamily fontsize blocks | bold italic underline | forecolor backcolor | alignleft aligncenter alignright alignjustify | bullist numlist | table | removeformat | code fullscreen',
                        font_family_formats: 'Times New Roman=times new roman,times,serif; Arial=arial,helvetica,sans-serif; Calibri=calibri,sans-serif; Segoe UI=segoe ui,sans-serif;',
                        font_size_formats: '9pt 10pt 11pt 12pt 14pt 16pt 18pt 20pt 24pt',
                        content_style: `
                            html { background-color: #f1f5f9; padding: 0; }
                            body {
                                background-color: #ffffff;
                                width: 210mm;
                                min-height: 297mm;
                                padding: 15mm 20mm;
                                margin: 15px auto;
                                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.15);
                                box-sizing: border-box;
                                font-family: "Times New Roman", Times, serif;
                                font-size: 11pt;
                                line-height: 1.5;
                                color: #111827;
                            }
                        `,
                        branding: false,
                        promotion: false,
                        setup: function (editor) {
                            editor.on('init', function () {
                                isTinyMceInitialized = true;
                            });
                        }
                    });
                }
            };

            window.insertMacroIntoEditor = function(macroText) {
                if (window.tinymce && tinymce.get('crTinyMceEditor')) {
                    tinymce.get('crTinyMceEditor').insertContent('<strong>' + macroText + '</strong>');
                }
            };

            window.toggleCrTypeView = function() {
                const selectedType = document.querySelector('input[name="crTypeRadio"]:checked').value;
                const boxWord = document.getElementById('boxWordInfo');
                const boxHtml = document.getElementById('boxHtmlEditor');

                if (selectedType === 'Word') {
                    boxWord.classList.remove('hidden');
                    boxHtml.classList.add('hidden');
                } else {
                    boxWord.classList.add('hidden');
                    boxHtml.classList.remove('hidden');
                    setTimeout(() => {
                        initTinyMceEditorIfNeeded();
                    }, 100);
                }
            };

            window.openCrTemplateModal = function() {
                const form = document.getElementById('crTemplateForm');
                if (form) form.reset();

                document.getElementById('crTemplateId').value = '';
                document.querySelector('input[name="crTypeRadio"][value="Word"]').checked = true;
                toggleCrTypeView();

                populateCrDropdowns();
                document.getElementById('crTemplateModalTitle').innerText = "Ajouter un Modèle de Compte Rendu";

                const defaultHtml = `<p><strong><span style="text-decoration: underline; font-size: 12pt;">INDICATION CLINIQUE :</span></strong></p>
<p style="font-size: 12pt;">Bilan d'exploration et contrôle radiologique.</p>
<p style="font-size: 12pt;">&nbsp;</p>
<p><strong><span style="text-decoration: underline; font-size: 12pt;">PROTOCOLE ET TECHNIQUE :</span></strong></p>
<p style="font-size: 12pt;">Examen réalisé selon le protocole standard d'imagerie médicale.</p>
<p style="font-size: 12pt;">&nbsp;</p>
<p><strong><span style="text-decoration: underline; font-size: 12pt;">RÉSULTATS :</span></strong></p>
<p style="font-size: 12pt;">Absence d'anomalie morphologique ou structurelle décelable.</p>
<p style="font-size: 12pt;">Structures anatomiques d'aspect et de dimensions normaux.</p>
<p style="font-size: 12pt;">&nbsp;</p>
<p><strong><span style="text-decoration: underline; font-size: 12pt; color: #166534;">CONCLUSION :</span></strong></p>
<p style="font-size: 12pt;"><strong>Examen sans anomalie radiologique notable.</strong></p>`;

                if (window.tinymce && tinymce.get('crTinyMceEditor')) {
                    tinymce.get('crTinyMceEditor').setContent(defaultHtml);
                }

                document.getElementById('crTemplateModal').classList.remove('hidden');
            };

            window.closeCrTemplateModal = function() {
                document.getElementById('crTemplateModal').classList.add('hidden');
            };

            window.editCrTemplate = function(id) {
                const t = crTemplatesData.find(x => x.id === id);
                if (!t) return;

                openCrTemplateModal();

                document.getElementById('crTemplateId').value = t.id;
                document.getElementById('crTemplateDesignation').value = t.designation;
                // Conserve une ancienne valeur de modèle si elle a depuis été retirée du référentiel.
                const modaliteSelect = document.getElementById('crTemplateModalite');
                if (t.modalite && !crModalitesList.includes(t.modalite)) {
                    modaliteSelect.add(new Option(`${t.modalite} (ancienne valeur)`, t.modalite), 0);
                }
                document.getElementById('crTemplateModalite').value = t.modalite;
                const serviceSelect = document.getElementById('crTemplateService');
                if (t.service && !crServicesList.includes(t.service)) {
                    serviceSelect.add(new Option(`${t.service} (ancienne valeur)`, t.service), 0);
                }
                document.getElementById('crTemplateService').value = t.service;

                const radio = document.querySelector(`input[name="crTypeRadio"][value="${t.type_modele}"]`);
                if (radio) radio.checked = true;
                toggleCrTypeView();

                if (t.type_modele === 'HTML') {
                    setTimeout(() => {
                        initTinyMceEditorIfNeeded();
                        if (window.tinymce && tinymce.get('crTinyMceEditor')) {
                            tinymce.get('crTinyMceEditor').setContent(t.contenu_html || '');
                        }
                    }, 150);
                }

                document.getElementById('crTemplateModalTitle').innerText = "Modifier : " + t.designation;
            };

            window.handleCrTemplateSubmit = async function(e) {
                e.preventDefault();

                const selectedType = document.querySelector('input[name="crTypeRadio"]:checked').value;
                let htmlContent = '';

                if (selectedType === 'HTML' && window.tinymce && tinymce.get('crTinyMceEditor')) {
                    htmlContent = tinymce.get('crTinyMceEditor').getContent();
                }

                const designation = document.getElementById('crTemplateDesignation').value;

                const data = {
                    id: document.getElementById('crTemplateId').value ? parseInt(document.getElementById('crTemplateId').value) : null,
                    designation: designation,
                    type_modele: selectedType,
                    modalite: document.getElementById('crTemplateModalite').value,
                    service: document.getElementById('crTemplateService').value,
                    utilisateur: 'admin',
                    contenu_html: htmlContent
                };

                const res = await fetch('/api/cr-templates/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                }).then(r => r.json());

                if (res.success) {
                    closeCrTemplateModal();
                    loadReportTemplatesModule();

                    if (selectedType === 'Word' && res.template_id) {
                        openWordTemplateById(res.template_id, designation);
                    }
                } else {
                    alert(res.message);
                }
            };

            window.openHtmlPrintPreview = function(templateId) {
                const t = crTemplatesData.find(x => x.id === templateId);
                if (!t) return;
                currentActiveTemplate = t;

                document.getElementById('printPreviewTitle').innerText = "Aperçu A4 : " + t.designation;
                document.getElementById('a4PreviewTitre').innerText = t.designation.toUpperCase();
                document.getElementById('a4PreviewBody').innerHTML = t.contenu_html || '<p>Corps du compte rendu...</p>';

                document.getElementById('htmlPrintPreviewModal').classList.remove('hidden');
            };

            window.closeHtmlPrintPreview = function() {
                document.getElementById('htmlPrintPreviewModal').classList.add('hidden');
            };

            window.printHtmlA4 = function() {
                window.print();
            };

            window.exportHtmlToPdf = function() {
                const element = document.getElementById('printableA4Area');
                const opt = {
                    margin: 0,
                    filename: (currentActiveTemplate ? currentActiveTemplate.designation : 'Compte_Rendu') + '.pdf',
                    image: { type: 'jpeg', quality: 0.98 },
                    html2canvas: { scale: 2, useCORS: true },
                    jsPDF: { unit: 'mm', format: 'a4', orientation: 'portrait' }
                };

                html2pdf().set(opt).from(element).save();
            };

            window.openWordTemplateById = async function(templateId, designation) {
                try {
                    const response = await fetch('/api/cr-templates/download-word/' + Number(templateId), {credentials: 'same-origin'});
                    if (!response.ok) throw new Error('Le modèle Word ne peut pas être téléchargé depuis le serveur.');
                    const blob = await response.blob();
                    const bridge = window.pywebview?.api;
                    if (bridge?.client_open_word_document) {
                        const dataUrl = await new Promise((resolve, reject) => {
                            const reader = new FileReader();
                            reader.onload = () => resolve(reader.result);
                            reader.onerror = () => reject(new Error('Impossible de lire le modèle Word.'));
                            reader.readAsDataURL(blob);
                        });
                        const safeName = String(designation || 'Modele_compte_rendu').replace(/[\\/:*?"<>|]+/g, '_');
                        const result = await bridge.client_open_word_document(dataUrl, `Modele_CR_${safeName}.docx`);
                        if (!result?.ok) throw new Error(result?.error || 'Word n’a pas pu ouvrir le modèle sur ce poste.');
                        alert(`Le modèle est ouvert dans Word sur ce poste. Après vos modifications et Ctrl+S, cliquez sur « Synchroniser » et choisissez ce fichier :\n${result.path || ''}`);
                        return;
                    }
                    const url = URL.createObjectURL(blob);
                    const link = document.createElement('a');
                    link.href = url;
                    link.download = `Modele_CR_${String(designation || 'Compte_rendu').replace(/[\\/:*?"<>|]+/g, '_')}.docx`;
                    link.click();
                    setTimeout(() => URL.revokeObjectURL(url), 1000);
                } catch (error) {
                    alert(error.message || 'Impossible d’ouvrir le modèle de compte rendu.');
                }
            };

            window.uploadWordTemplateById = function(templateId) {
                const picker = document.createElement('input');
                picker.type = 'file';
                picker.accept = '.docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document';
                picker.onchange = async () => {
                    const file = picker.files?.[0];
                    if (!file) return;
                    const form = new FormData();
                    form.append('file', file, file.name);
                    try {
                        const response = await fetch(`/api/cr-templates/upload-word/${Number(templateId)}`, {
                            method: 'POST', body: form, credentials: 'same-origin'
                        });
                        const result = await response.json().catch(() => ({}));
                        if (!response.ok || !result.success) throw new Error(result.message || result.detail || 'La synchronisation du modèle Word a échoué.');
                        alert('Le modèle Word a été synchronisé avec Cliniva.');
                        loadReportTemplatesModule();
                    } catch (error) {
                        alert(error.message || 'Impossible de synchroniser le modèle Word.');
                    }
                };
                picker.click();
            };

            window.deleteCrTemplate = async function(id) {
                if (confirm("Voulez-vous supprimer ce modèle de compte rendu ?")) {
                    await fetch('/api/cr-templates/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id })
                    });
                    loadReportTemplatesModule();
                }
            };

            loadReportTemplatesModule();
        })();
    </script>
    """
