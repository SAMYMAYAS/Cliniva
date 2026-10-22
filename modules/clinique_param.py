import base64
import json
import logging
import os
import time
from typing import Dict, Any, List

from fastapi import APIRouter, HTTPException
from database import DOCS_DIR, get_db_cursor

logger = logging.getLogger("ClinicParamModule")

JOURS_SEMAINE: List[str] = [
    "Lundi",
    "Mardi",
    "Mercredi",
    "Jeudi",
    "Vendredi",
    "Samedi",
    "Dimanche",
]

clinic_router = APIRouter(prefix="/api/clinic", tags=["Paramétrage Clinique"])


# ============================================================================
# LOGIQUE MÉTIER & PERSISTANCE POSTGRESQL
# ============================================================================
class ClinicManager:
    """Gestionnaire métier de la clinique interagissant avec PostgreSQL."""

    @staticmethod
    def save_base64_file(base64_data: str, prefix: str = "doc") -> str:
        """Sauvegarde les fichiers binaires encodés en base64 sur disque."""
        if not base64_data or not base64_data.startswith("data:image"):
            return base64_data

        try:
            header, encoded = base64_data.split(",", 1)
            ext = header.split(";")[0].split("/")[1]
            if ext == "jpeg":
                ext = "jpg"
            filename = f"{prefix}_{int(time.time() * 1000)}.{ext}"
            filepath = os.path.join(DOCS_DIR, filename)

            with open(filepath, "wb") as file_handler:
                file_handler.write(base64.b64decode(encoded))

            return base64_data
        except Exception as error:
            logger.warning("Échec d'enregistrement du fichier base64 : %s", error)
            return base64_data

    def get_info(self) -> Dict[str, Any]:
        """Récupère les informations légales et horaires de la clinique."""
        with get_db_cursor() as cursor:
            cursor.execute("SELECT * FROM clinic_info WHERE id = 1 LIMIT 1;")
            row = cursor.fetchone()

        if not row:
            return {}

        data = dict(row)
        if isinstance(data.get("horaires_tarification"), str):
            try:
                data["horaires_tarification"] = json.loads(data["horaires_tarification"])
            except Exception:
                data["horaires_tarification"] = {}

        return data

    def save_info(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Sauvegarde les coordonnées et horaires dans PostgreSQL."""
        logo = self.save_base64_file(data.get("logo", ""), "logo")
        entete_pdf = self.save_base64_file(data.get("entete_pdf", ""), "entete")
        pied_page_pdf = self.save_base64_file(data.get("pied_page_pdf", ""), "pied_page")

        horaires = data.get("horaires_tarification", {})
        horaires_json = json.dumps(horaires) if isinstance(horaires, dict) else horaires

        query = """
            UPDATE clinic_info SET 
                nom_raison_sociale = %s,
                telephone = %s,
                email = %s,
                adresse = %s,
                nif = %s,
                nis = %s,
                rc = %s,
                article_imposition = %s,
                entete_ticket = %s,
                logo = %s,
                entete_pdf = %s,
                pied_page_pdf = %s,
                horaires_tarification = %s::jsonb
            WHERE id = 1;
        """
        params = (
            data.get("nom_raison_sociale", ""),
            data.get("telephone", ""),
            data.get("email", ""),
            data.get("adresse", ""),
            data.get("nif", ""),
            data.get("nis", ""),
            data.get("rc", ""),
            data.get("article_imposition", ""),
            data.get("entete_ticket", ""),
            logo,
            entete_pdf,
            pied_page_pdf,
            horaires_json,
        )

        with get_db_cursor() as cursor:
            cursor.execute(query, params)

        return {
            "success": True,
            "message": "Paramètres enregistrés avec succès dans PostgreSQL !",
        }

    def get_services(self) -> List[Dict[str, Any]]:
        """Retourne la liste de l'ensemble des services cliniques."""
        with get_db_cursor() as cursor:
            cursor.execute("SELECT * FROM clinic_services ORDER BY id ASC;")
            rows = cursor.fetchall()
        return [dict(r) for r in rows]

    def save_service(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Ajoute ou modifie un service clinique."""
        service_id = data.get("id")
        nom_service = data.get("nom_service", "").strip()

        if not nom_service:
            return {"success": False, "message": "Le nom du service est obligatoire."}

        with get_db_cursor() as cursor:
            if service_id:
                query = """
                    UPDATE clinic_services SET
                        nom_service = %s,
                        responsable_nom = %s,
                        responsable_tel = %s,
                        entrepot_nom = %s,
                        responsable_pharma_nom = %s,
                        responsable_pharma_tel = %s
                    WHERE id = %s;
                """
                params = (
                    nom_service,
                    data.get("responsable_nom", "").strip(),
                    data.get("responsable_tel", "").strip(),
                    data.get("entrepot_nom", "").strip(),
                    data.get("responsable_pharma_nom", "").strip(),
                    data.get("responsable_pharma_tel", "").strip(),
                    service_id,
                )
            else:
                query = """
                    INSERT INTO clinic_services 
                    (nom_service, responsable_nom, responsable_tel, entrepot_nom, responsable_pharma_nom, responsable_pharma_tel)
                    VALUES (%s, %s, %s, %s, %s, %s);
                """
                params = (
                    nom_service,
                    data.get("responsable_nom", "").strip(),
                    data.get("responsable_tel", "").strip(),
                    data.get("entrepot_nom", "").strip(),
                    data.get("responsable_pharma_nom", "").strip(),
                    data.get("responsable_pharma_tel", "").strip(),
                )
            cursor.execute(query, params)

        return {"success": True, "message": "Service enregistré avec succès !"}

    def delete_service(self, service_id: int) -> Dict[str, Any]:
        """Supprime un service par son identifiant unique."""
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM clinic_services WHERE id = %s;", (service_id,))
        return {"success": True, "message": "Service supprimé."}


clinic_manager = ClinicManager()


# ============================================================================
# POINTS D'ACCÈS FASTAPI (ROUTES API JSON)
# ============================================================================
@clinic_router.get("/info")
async def api_get_clinic_info():
    try:
        return clinic_manager.get_info()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@clinic_router.post("/info")
async def api_save_clinic_info(payload: Dict[str, Any]):
    try:
        return clinic_manager.save_info(payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@clinic_router.get("/services")
async def api_get_clinic_services():
    try:
        return clinic_manager.get_services()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@clinic_router.post("/services/save")
async def api_save_clinic_service(payload: Dict[str, Any]):
    try:
        return clinic_manager.save_service(payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@clinic_router.post("/services/delete")
async def api_delete_clinic_service(payload: Dict[str, Any]):
    try:
        service_id = payload.get("id")
        return clinic_manager.delete_service(service_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ============================================================================
# RENDU DU COMPOSANT HTML AVEC 3 ONGLETS
# ============================================================================
def render_html() -> str:
    """Génère l'HTML complet structuré en 3 onglets indépendants."""
    jours_rows_html = "".join([
        f"""
        <tr class="hover:bg-slate-50/80 dark:hover:bg-white/[0.02] transition-colors border-b border-slate-100 dark:border-white/5" data-jour="{jour}">
            <td class="py-3 font-bold text-slate-800 dark:text-slate-200 text-xs w-32">{jour}</td>
            <td class="py-3">
                <div class="flex items-center space-x-2">
                    <input type="time" id="time_jour_debut_{jour}" value="08:00" class="text-xs p-2 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-semibold text-slate-700 dark:text-white outline-none focus:border-brand-neon">
                    <span class="text-slate-400 text-xs font-semibold">à</span>
                    <input type="time" id="time_jour_fin_{jour}" value="20:00" class="text-xs p-2 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-semibold text-slate-700 dark:text-white outline-none focus:border-brand-neon">
                </div>
            </td>
            <td class="py-3">
                <div class="flex items-center space-x-2">
                    <input type="time" id="time_nuit_debut_{jour}" value="20:00" class="text-xs p-2 rounded-xl border border-purple-200 dark:border-purple-900/40 bg-purple-50/40 dark:bg-purple-950/20 font-semibold text-purple-900 dark:text-purple-300 outline-none focus:border-purple-500">
                    <span class="text-slate-400 text-xs font-semibold">à</span>
                    <input type="time" id="time_nuit_fin_{jour}" value="08:00" class="text-xs p-2 rounded-xl border border-purple-200 dark:border-purple-900/40 bg-purple-50/40 dark:bg-purple-950/20 font-semibold text-purple-900 dark:text-purple-300 outline-none focus:border-purple-500">
                </div>
            </td>
            <td class="py-3 text-center">
                <label class="inline-flex items-center space-x-2 cursor-pointer bg-slate-50 dark:bg-black/20 px-3 py-1.5 rounded-xl border border-slate-200/60 dark:border-white/5">
                    <input type="checkbox" id="check_24h_nuit_{jour}" onchange="toggleJour24hNuit('{jour}')" class="rounded text-purple-600 focus:ring-purple-500">
                    <span class="text-xs text-slate-600 dark:text-slate-400 font-semibold">Garde 24h</span>
                </label>
            </td>
        </tr>
        """ for jour in JOURS_SEMAINE
    ])

    return f"""
    <div id="view-clinic-settings" class="space-y-6">
        
        <!-- BANDEAU D'EN-TÊTE -->
        <div class="banner-glow rounded-3xl p-6 lg:p-8 text-white shadow-xl flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
            <div>
                <h1 class="text-2xl font-extrabold tracking-tight mb-1">Paramétrage Général de la Clinique</h1>
                <p class="text-xs text-slate-300">Gérez l'identité légale, la configuration des services et la politique tarifaire jour/nuit.</p>
            </div>
            <button onclick="saveClinicInfoForm()" class="flex items-center gap-2 bg-brand-neon hover:bg-brand-neonDark text-black px-6 py-3 rounded-2xl font-bold text-xs shadow-md shadow-brand-neon/20 transition-all flex-shrink-0">
                <i class="ph-bold ph-floppy-disk text-base"></i>
                <span>Enregistrer les Données</span>
            </button>
        </div>

        <!-- SÉLECTEUR DES 3 ONGLETS -->
        <div class="flex items-center gap-2 p-1.5 bg-white dark:bg-[#121c15] border border-slate-200/80 dark:border-white/10 rounded-2xl shadow-sm w-fit">
            <button id="tab-btn-info" onclick="switchClinicTab('info')" class="clinic-tab-btn flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-xs bg-brand-neon text-black shadow-md shadow-brand-neon/20 transition-all">
                <i class="ph-bold ph-buildings text-base"></i>
                <span>Informations & Fiscalité</span>
            </button>
            <button id="tab-btn-services" onclick="switchClinicTab('services')" class="clinic-tab-btn flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-xs text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white hover:bg-slate-100 dark:hover:bg-white/5 transition-all">
                <i class="ph-bold ph-first-aid text-base"></i>
                <span>Services de la Clinique</span>
            </button>
            <button id="tab-btn-horaires" onclick="switchClinicTab('horaires')" class="clinic-tab-btn flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-xs text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white hover:bg-slate-100 dark:hover:bg-white/5 transition-all">
                <i class="ph-bold ph-clock text-base"></i>
                <span>Horaires & Tarification</span>
            </button>
        </div>

        <!-- ============================================================= -->
        <!-- ONGLET 1 : INFORMATIONS & FISCALITÉ -->
        <!-- ============================================================= -->
        <div id="tab-content-info" class="clinic-tab-content bg-white dark:bg-[#121c15] rounded-3xl p-6 lg:p-8 shadow-sm border border-slate-100 dark:border-white/5 space-y-6 transition-colors">
            <div class="border-b border-slate-100 dark:border-white/5 pb-4">
                <h3 class="text-base font-bold text-slate-800 dark:text-white flex items-center gap-2">
                    <i class="ph-bold ph-identification-card text-brand-neon"></i>
                    Identité Juridique & Coordonnées Fiscales
                </h3>
                <p class="text-xs text-slate-400 mt-0.5">Informations requises pour la facturation, les entêtes de documents et tickets.</p>
            </div>

            <div class="flex flex-col sm:flex-row items-center gap-5 p-4 rounded-2xl bg-slate-50 dark:bg-black/20 border border-slate-200/60 dark:border-white/5">
                <div class="relative group shrink-0">
                    <img id="clinicLogoPreview" src="/static/images/avatar-placeholder.svg?w=100&auto=format&fit=crop&q=80" class="w-20 h-20 rounded-2xl object-cover border border-slate-200 dark:border-white/10 shadow-sm">
                    <label for="clinicLogoUpload" class="absolute inset-0 bg-black/50 rounded-2xl flex items-center justify-center text-white opacity-0 group-hover:opacity-100 cursor-pointer transition-opacity">
                        <i class="ph-bold ph-camera text-2xl"></i>
                    </label>
                    <input type="file" id="clinicLogoUpload" accept="image/*" class="hidden" onchange="previewClinicLogo(event)">
                </div>
                <div class="flex-1 w-full">
                    <label class="block text-xs font-semibold text-slate-700 dark:text-slate-300 mb-1.5">Nom Officiel / Raison Sociale *</label>
                    <input type="text" id="clinicNom" placeholder="Clinique Médico-Chirurgicale Cliniva" class="w-full text-xs font-bold p-3 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-white dark:bg-black/40 text-slate-800 dark:text-white">
                </div>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                    <label class="block text-xs font-semibold text-slate-700 dark:text-slate-300 mb-1.5">Numéro de Téléphone *</label>
                    <input type="text" id="clinicTel" placeholder="021 50 00 00 / 0550 00 00 00" class="w-full text-xs p-3 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-700 dark:text-slate-300 mb-1.5">Courriel Administratif *</label>
                    <input type="email" id="clinicEmail" placeholder="contact@cliniva.dz" class="w-full text-xs p-3 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                </div>
                <div class="md:col-span-2">
                    <label class="block text-xs font-semibold text-slate-700 dark:text-slate-300 mb-1.5">Adresse Physique Complète *</label>
                    <input type="text" id="clinicAdresse" placeholder="12 Boulevard des Martyrs, Alger, Algérie" class="w-full text-xs p-3 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                </div>
            </div>

            <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
                <div>
                    <label class="block text-[10px] font-bold uppercase text-slate-400 mb-1">NIF</label>
                    <input type="text" id="clinicNif" placeholder="099916000000000" class="w-full text-xs font-mono p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                </div>
                <div>
                    <label class="block text-[10px] font-bold uppercase text-slate-400 mb-1">NIS</label>
                    <input type="text" id="clinicNis" placeholder="000016000000000" class="w-full text-xs font-mono p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                </div>
                <div>
                    <label class="block text-[10px] font-bold uppercase text-slate-400 mb-1">RC</label>
                    <input type="text" id="clinicRc" placeholder="16/00-0000000A16" class="w-full text-xs font-mono p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                </div>
                <div>
                    <label class="block text-[10px] font-bold uppercase text-slate-400 mb-1">Article Imp.</label>
                    <input type="text" id="clinicArtImp" placeholder="16000000000" class="w-full text-xs font-mono p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                </div>
            </div>

            <div>
                <label class="block text-xs font-semibold text-slate-700 dark:text-slate-300 mb-1.5">En-tête Ticket de Caisse</label>
                <input type="text" id="clinicEnteteTicket" placeholder="CLINIVA MEDICAL - SOINS ET CONSULTATIONS" class="w-full text-xs p-3 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
            </div>

            <!-- Uploads Bannières PDF -->
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4 pt-2">
                <div class="p-4 bg-slate-50 dark:bg-black/20 rounded-2xl border border-slate-200/80 dark:border-white/5 flex flex-col justify-between">
                    <div>
                        <span class="block text-xs font-bold text-slate-800 dark:text-slate-200 mb-1">En-tête PDF (Document A4)</span>
                        <p class="text-[11px] text-slate-400 mb-2">Bannière graphique imprimée en haut de chaque ordonnance / facture.</p>
                        <label for="entetePdfUpload" class="cursor-pointer inline-flex items-center gap-2 bg-white dark:bg-[#121c15] border border-slate-200 dark:border-white/10 text-emerald-600 dark:text-brand-neon hover:bg-slate-100 dark:hover:bg-white/5 px-4 py-2 rounded-xl text-xs font-bold shadow-sm transition">
                            <i class="ph-bold ph-upload-simple text-sm"></i>
                            <span>Téléverser En-tête</span>
                        </label>
                        <input type="file" id="entetePdfUpload" accept="image/*" class="hidden" onchange="previewDocImage(event, 'entetePdf')">
                    </div>
                    <span id="entetePdfStatus" class="text-[11px] text-slate-400 italic mt-3">Aucune bannière en-tête configurée.</span>
                </div>

                <div class="p-4 bg-slate-50 dark:bg-black/20 rounded-2xl border border-slate-200/80 dark:border-white/5 flex flex-col justify-between">
                    <div>
                        <span class="block text-xs font-bold text-slate-800 dark:text-slate-200 mb-1">Pied de Page PDF (Document A4)</span>
                        <p class="text-[11px] text-slate-400 mb-2">Bannière imprimée en bas de document avec mentions légales.</p>
                        <label for="piedPdfUpload" class="cursor-pointer inline-flex items-center gap-2 bg-white dark:bg-[#121c15] border border-slate-200 dark:border-white/10 text-emerald-600 dark:text-brand-neon hover:bg-slate-100 dark:hover:bg-white/5 px-4 py-2 rounded-xl text-xs font-bold shadow-sm transition">
                            <i class="ph-bold ph-upload-simple text-sm"></i>
                            <span>Téléverser Pied de Page</span>
                        </label>
                        <input type="file" id="piedPdfUpload" accept="image/*" class="hidden" onchange="previewDocImage(event, 'piedPdf')">
                    </div>
                    <span id="piedPdfStatus" class="text-[11px] text-slate-400 italic mt-3">Aucun pied de page configuré.</span>
                </div>
            </div>
        </div>

        <!-- ============================================================= -->
        <!-- ONGLET 2 : SERVICES DE LA CLINIQUE -->
        <!-- ============================================================= -->
        <div id="tab-content-services" class="clinic-tab-content hidden bg-white dark:bg-[#121c15] rounded-3xl p-6 lg:p-8 shadow-sm border border-slate-100 dark:border-white/5 space-y-6 transition-colors">
            <div class="flex flex-col sm:flex-row sm:items-center justify-between border-b border-slate-100 dark:border-white/5 pb-4 gap-3">
                <div>
                    <h3 class="text-base font-bold text-slate-800 dark:text-white flex items-center gap-2">
                        <i class="ph-bold ph-first-aid text-brand-neon"></i>
                        Organisation & Services Médicaux
                    </h3>
                    <p class="text-xs text-slate-400 mt-0.5">Configuration des pôles, blocs opératoires et entrepôts de la structure.</p>
                </div>
                <button onclick="openServiceModal()" class="flex items-center gap-2 bg-brand-neon text-black px-4 py-2.5 rounded-xl font-bold text-xs shadow-md shadow-brand-neon/20 hover:opacity-90 transition">
                    <i class="ph-bold ph-plus text-base"></i>
                    <span>Ajouter un Nouveau Service</span>
                </button>
            </div>

            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                    <thead>
                        <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-3">
                            <th class="pb-3 font-medium">Nom du Service</th>
                            <th class="pb-3 font-medium">Responsable Médical</th>
                            <th class="pb-3 font-medium">Entrepôt Lié</th>
                            <th class="pb-3 font-medium">Responsable Pharmacie</th>
                            <th class="pb-3 font-medium text-right pr-2">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="services-table-body" class="divide-y divide-slate-100 dark:divide-white/5 text-slate-700 dark:text-slate-300"></tbody>
                </table>
            </div>
        </div>

        <!-- ============================================================= -->
        <!-- ONGLET 3 : HORAIRES & TARIFICATION -->
        <!-- ============================================================= -->
        <div id="tab-content-horaires" class="clinic-tab-content hidden bg-white dark:bg-[#121c15] rounded-3xl p-6 lg:p-8 shadow-sm border border-slate-100 dark:border-white/5 space-y-6 transition-colors">
            <div class="flex flex-col sm:flex-row sm:items-center justify-between border-b border-slate-100 dark:border-white/5 pb-4 gap-3">
                <div>
                    <h3 class="text-base font-bold text-slate-800 dark:text-white flex items-center gap-2">
                        <i class="ph-bold ph-clock text-brand-neon"></i>
                        Périodes de Tarification Hebdomadaire (Jour & Nuit)
                    </h3>
                    <p class="text-xs text-slate-400 mt-0.5">Définit l'application automatique des tarifs de jour ou majorés (nuit et gardes).</p>
                </div>
                <button type="button" onclick="appliquerHorairesATous()" class="flex items-center gap-2 text-xs font-bold text-black bg-brand-neon hover:opacity-90 px-4 py-2.5 rounded-xl shadow-md shadow-brand-neon/20 transition">
                    <i class="ph-bold ph-lightning text-sm"></i>
                    <span>Appliquer Lundi à Tous les Jours</span>
                </button>
            </div>

            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                    <thead>
                        <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-3">
                            <th class="pb-3 font-medium">Jour de la Semaine</th>
                            <th class="pb-3 font-medium">☀️ Plage Tarif Jour</th>
                            <th class="pb-3 font-medium">🌙 Plage Tarif Nuit</th>
                            <th class="pb-3 font-medium text-center">Garde Continue 24h</th>
                        </tr>
                    </thead>
                    <tbody id="horaires-tarifs-body" class="divide-y divide-slate-100 dark:divide-white/5 text-slate-700 dark:text-slate-300">
                        {jours_rows_html}
                    </tbody>
                </table>
            </div>
        </div>

    </div>

    <!-- MODAL SERVICE -->
    <div id="serviceModal" class="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center hidden z-50 p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-md shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50/50 dark:bg-black/20">
                <div class="flex items-center space-x-2">
                    <div class="w-8 h-8 rounded-xl bg-brand-neon/20 text-brand-neon flex items-center justify-center font-bold text-sm">
                        <i class="ph-bold ph-first-aid"></i>
                    </div>
                    <h3 id="serviceModalTitle" class="text-base font-bold text-slate-800 dark:text-white">Ajouter un Service</h3>
                </div>
                <button onclick="closeServiceModal()" class="text-slate-400 hover:text-white p-1 rounded-lg">
                    <i class="ph-bold ph-x text-lg"></i>
                </button>
            </div>
            <form id="serviceForm" onsubmit="handleServiceSubmit(event)" class="p-6 space-y-3.5">
                <input type="hidden" id="serviceId">
                <div>
                    <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Nom du Service *</label>
                    <input type="text" id="serviceNom" required placeholder="Ex: Bloc Opératoire Central..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-bold">
                </div>
                <div class="grid grid-cols-2 gap-2.5">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Responsable Médical</label>
                        <input type="text" id="serviceRespNom" placeholder="Dr. Mansouri" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Tél. Responsable</label>
                        <input type="text" id="serviceRespTel" placeholder="0550 11 22 33" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                </div>
                <div>
                    <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Entrepôt Lié</label>
                    <input type="text" id="serviceEntrepot" placeholder="Ex: Pharmacie Centrale..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                </div>
                <div class="grid grid-cols-2 gap-2.5">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Responsable Pharmacie</label>
                        <input type="text" id="serviceRespPharmaNom" placeholder="Dr. Pharmacien" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Tél. Pharmacie</label>
                        <input type="text" id="serviceRespPharmaTel" placeholder="0550 00 00 00" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                </div>
                <div class="flex items-center justify-end space-x-3 pt-4 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closeServiceModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler</button>
                    <button type="submit" class="px-6 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md shadow-brand-neon/20">Enregistrer</button>
                </div>
            </form>
        </div>
    </div>

    <!-- SCRIPT JS GÉRANT LES ONGLETS ET LA LOGIQUE MÉTIER -->
    <script>
        (function() {{
            let servicesData = [];
            let clinicLogoBase64 = '';
            let entetePdfBase64 = '';
            let piedPdfBase64 = '';
            const LISTE_JOURS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"];

            // 1. SWITCH D'ONGLETS
            window.switchClinicTab = function(tabName) {{
                // Masquer tous les contenus
                document.querySelectorAll('.clinic-tab-content').forEach(el => el.classList.add('hidden'));
                
                // Réinitialiser les boutons d'onglets
                document.querySelectorAll('.clinic-tab-btn').forEach(btn => {{
                    btn.className = "clinic-tab-btn flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-xs text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white hover:bg-slate-100 dark:hover:bg-white/5 transition-all";
                }});

                // Afficher l'onglet sélectionné
                const targetContent = document.getElementById('tab-content-' + tabName);
                if (targetContent) targetContent.classList.remove('hidden');

                // Activer le bouton sélectionné
                const targetBtn = document.getElementById('tab-btn-' + tabName);
                if (targetBtn) {{
                    targetBtn.className = "clinic-tab-btn flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-xs bg-brand-neon text-black shadow-md shadow-brand-neon/20 transition-all";
                }}
            }};

            // 2. CHARGEMENT DES DONNÉES DEPUIS POSTGRESQL
            window.loadClinicSettings = async function() {{
                try {{
                    const res = await fetch('/api/clinic/info');
                    if (!res.ok) return;
                    const info = await res.json();

                    if (info) {{
                        document.getElementById('clinicNom').value = info.nom_raison_sociale || '';
                        document.getElementById('clinicTel').value = info.telephone || '';
                        document.getElementById('clinicEmail').value = info.email || '';
                        document.getElementById('clinicAdresse').value = info.adresse || '';
                        document.getElementById('clinicNif').value = info.nif || '';
                        document.getElementById('clinicNis').value = info.nis || '';
                        document.getElementById('clinicRc').value = info.rc || '';
                        document.getElementById('clinicArtImp').value = info.article_imposition || '';
                        document.getElementById('clinicEnteteTicket').value = info.entete_ticket || '';
                        
                        if (info.logo) {{
                            document.getElementById('clinicLogoPreview').src = info.logo;
                            clinicLogoBase64 = info.logo;
                        }}
                        if (info.entete_pdf) {{
                            document.getElementById('entetePdfStatus').innerText = "✓ Bannière en-tête configurée.";
                            document.getElementById('entetePdfStatus').className = "text-[11px] text-emerald-500 font-bold mt-3";
                            entetePdfBase64 = info.entete_pdf;
                        }}
                        if (info.pied_page_pdf) {{
                            document.getElementById('piedPdfStatus').innerText = "✓ Bannière pied de page configurée.";
                            document.getElementById('piedPdfStatus').className = "text-[11px] text-emerald-500 font-bold mt-3";
                            piedPdfBase64 = info.pied_page_pdf;
                        }}

                        const horaires = info.horaires_tarification || {{}};
                        const joursConfig = horaires.jours || {{}};

                        LISTE_JOURS.forEach(jour => {{
                            const jCfg = joursConfig[jour] || {{ jour_debut: "08:00", jour_fin: "20:00", nuit_24h: false }};
                            const elJDeb = document.getElementById(`time_jour_debut_${{jour}}`);
                            const elJFin = document.getElementById(`time_jour_fin_${{jour}}`);
                            const elNDeb = document.getElementById(`time_nuit_debut_${{jour}}`);
                            const elNFin = document.getElementById(`time_nuit_fin_${{jour}}`);
                            const elCheck24 = document.getElementById(`check_24h_nuit_${{jour}}`);

                            if (elJDeb) elJDeb.value = jCfg.jour_debut || "08:00";
                            if (elJFin) elJFin.value = jCfg.jour_fin || "20:00";
                            if (elNDeb) elNDeb.value = jCfg.jour_fin || "20:00";
                            if (elNFin) elNFin.value = jCfg.jour_debut || "08:00";
                            if (elCheck24) {{
                                elCheck24.checked = Boolean(jCfg.nuit_24h);
                                toggleJour24hNuit(jour);
                            }}
                        }});
                    }}
                    loadClinicServices();
                }} catch (e) {{
                    console.error("Erreur de chargement:", e);
                }}
            }};

            window.toggleJour24hNuit = function(jour) {{
                const is24h = document.getElementById(`check_24h_nuit_${{jour}}`).checked;
                const elJDeb = document.getElementById(`time_jour_debut_${{jour}}`);
                const elJFin = document.getElementById(`time_jour_fin_${{jour}}`);
                
                if (is24h) {{
                    elJDeb.disabled = true;
                    elJFin.disabled = true;
                    elJDeb.classList.add('opacity-40');
                    elJFin.classList.add('opacity-40');
                }} else {{
                    elJDeb.disabled = false;
                    elJFin.disabled = false;
                    elJDeb.classList.remove('opacity-40');
                    elJFin.classList.remove('opacity-40');
                }}
            }};

            window.appliquerHorairesATous = function() {{
                const lundiJDeb = document.getElementById('time_jour_debut_Lundi').value;
                const lundiJFin = document.getElementById('time_jour_fin_Lundi').value;
                const lundiNDeb = document.getElementById('time_nuit_debut_Lundi').value;
                const lundiNFin = document.getElementById('time_nuit_fin_Lundi').value;

                LISTE_JOURS.forEach(jour => {{
                    if (jour !== "Lundi") {{
                        document.getElementById(`time_jour_debut_${{jour}}`).value = lundiJDeb;
                        document.getElementById(`time_jour_fin_${{jour}}`).value = lundiJFin;
                        document.getElementById(`time_nuit_debut_${{jour}}`).value = lundiNDeb;
                        document.getElementById(`time_nuit_fin_${{jour}}`).value = lundiNFin;
                        document.getElementById(`check_24h_nuit_${{jour}}`).checked = false;
                        toggleJour24hNuit(jour);
                    }}
                }});
            }};

            window.collectHorairesTarification = function() {{
                const joursConfig = {{}};
                LISTE_JOURS.forEach(jour => {{
                    joursConfig[jour] = {{
                        jour_debut: document.getElementById(`time_jour_debut_${{jour}}`).value,
                        jour_fin: document.getElementById(`time_jour_fin_${{jour}}`).value,
                        nuit_24h: document.getElementById(`check_24h_nuit_${{jour}}`).checked
                    }};
                }});
                return {{ jours: joursConfig }};
            }};

            window.loadClinicServices = async function() {{
                try {{
                    const res = await fetch('/api/clinic/services');
                    servicesData = await res.json();
                    const tbody = document.getElementById('services-table-body');
                    tbody.innerHTML = '';

                    servicesData.forEach(s => {{
                        const tr = document.createElement('tr');
                        tr.className = "hover:bg-slate-50/80 dark:hover:bg-white/[0.02] transition-colors";
                        tr.innerHTML = `
                            <td class="py-3 font-bold text-slate-800 dark:text-slate-100">${{s.nom_service}}</td>
                            <td class="py-3 text-slate-600 dark:text-slate-400">
                                <div class="font-bold text-slate-800 dark:text-slate-200 text-xs">${{s.responsable_nom || '-'}}</div>
                                <div class="text-[11px] text-slate-400 font-mono">${{s.responsable_tel || ''}}</div>
                            </td>
                            <td class="py-3">
                                <span class="px-2.5 py-1 rounded-xl text-[11px] font-bold bg-brand-neon/20 text-emerald-800 dark:text-brand-neon border border-brand-neon/30">${{s.entrepot_nom || 'Pharmacie Centrale'}}</span>
                            </td>
                            <td class="py-3 text-slate-600 dark:text-slate-400">
                                <div class="font-bold text-slate-800 dark:text-slate-200 text-xs">${{s.responsable_pharma_nom || '-'}}</div>
                                <div class="text-[11px] text-slate-400 font-mono">${{s.responsable_pharma_tel || ''}}</div>
                            </td>
                            <td class="py-3 text-right pr-2">
                                <div class="inline-flex items-center gap-1.5">
                                    <button onclick="editService(${{s.id}})" class="p-1.5 text-sky-500 hover:bg-sky-50 dark:hover:bg-sky-500/10 rounded-lg transition"><i class="ph-bold ph-pencil-simple text-sm"></i></button>
                                    <button onclick="deleteService(${{s.id}})" class="p-1.5 text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-500/10 rounded-lg transition"><i class="ph-bold ph-trash text-sm"></i></button>
                                </div>
                            </td>
                        `;
                        tbody.appendChild(tr);
                    }});
                }} catch (e) {{
                    console.error("Erreur de chargement des services:", e);
                }}
            }};

            window.previewClinicLogo = function(e) {{
                const file = e.target.files[0];
                if (file) {{
                    const reader = new FileReader();
                    reader.onload = function(evt) {{
                        clinicLogoBase64 = evt.target.result;
                        document.getElementById('clinicLogoPreview').src = evt.target.result;
                    }};
                    reader.readAsDataURL(file);
                }}
            }};

            window.previewDocImage = function(e, type) {{
                const file = e.target.files[0];
                if (file) {{
                    const reader = new FileReader();
                    reader.onload = function(evt) {{
                        if (type === 'entetePdf') {{
                            entetePdfBase64 = evt.target.result;
                            document.getElementById('entetePdfStatus').innerText = "✓ Sélectionné: " + file.name;
                            document.getElementById('entetePdfStatus').className = "text-[11px] text-emerald-500 font-bold mt-3";
                        }} else {{
                            piedPdfBase64 = evt.target.result;
                            document.getElementById('piedPdfStatus').innerText = "✓ Sélectionné: " + file.name;
                            document.getElementById('piedPdfStatus').className = "text-[11px] text-emerald-500 font-bold mt-3";
                        }}
                    }};
                    reader.readAsDataURL(file);
                }}
            }};

            window.saveClinicInfoForm = async function() {{
                const payload = {{
                    nom_raison_sociale: document.getElementById('clinicNom').value,
                    telephone: document.getElementById('clinicTel').value,
                    email: document.getElementById('clinicEmail').value,
                    adresse: document.getElementById('clinicAdresse').value,
                    nif: document.getElementById('clinicNif').value,
                    nis: document.getElementById('clinicNis').value,
                    rc: document.getElementById('clinicRc').value,
                    article_imposition: document.getElementById('clinicArtImp').value,
                    entete_ticket: document.getElementById('clinicEnteteTicket').value,
                    logo: clinicLogoBase64,
                    entete_pdf: entetePdfBase64,
                    pied_page_pdf: piedPdfBase64,
                    horaires_tarification: collectHorairesTarification()
                }};

                try {{
                    const res = await fetch('/api/clinic/info', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify(payload)
                    }});
                    const jsonRes = await res.json();
                    alert(jsonRes.message);
                }} catch (err) {{
                    alert("Erreur lors de la sauvegarde : " + err.message);
                }}
            }};

            window.openServiceModal = function() {{
                document.getElementById('serviceForm').reset();
                document.getElementById('serviceId').value = '';
                document.getElementById('serviceModalTitle').innerText = 'Ajouter un Service';
                document.getElementById('serviceModal').classList.remove('hidden');
            }};

            window.closeServiceModal = function() {{
                document.getElementById('serviceModal').classList.add('hidden');
            }};

            window.editService = function(id) {{
                const s = servicesData.find(x => x.id === id);
                if (!s) return;

                document.getElementById('serviceId').value = s.id;
                document.getElementById('serviceNom').value = s.nom_service;
                document.getElementById('serviceRespNom').value = s.responsable_nom || '';
                document.getElementById('serviceRespTel').value = s.responsable_tel || '';
                document.getElementById('serviceEntrepot').value = s.entrepot_nom || '';
                document.getElementById('serviceRespPharmaNom').value = s.responsable_pharma_nom || '';
                document.getElementById('serviceRespPharmaTel').value = s.responsable_pharma_tel || '';

                document.getElementById('serviceModalTitle').innerText = 'Modifier Service : ' + s.nom_service;
                document.getElementById('serviceModal').classList.remove('hidden');
            }};

            window.handleServiceSubmit = async function(e) {{
                e.preventDefault();
                const data = {{
                    id: document.getElementById('serviceId').value ? parseInt(document.getElementById('serviceId').value) : null,
                    nom_service: document.getElementById('serviceNom').value,
                    responsable_nom: document.getElementById('serviceRespNom').value,
                    responsable_tel: document.getElementById('serviceRespTel').value,
                    entrepot_nom: document.getElementById('serviceEntrepot').value,
                    responsable_pharma_nom: document.getElementById('serviceRespPharmaNom').value,
                    responsable_pharma_tel: document.getElementById('serviceRespPharmaTel').value
                }};

                const res = await fetch('/api/clinic/services/save', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(data)
                }});
                const result = await res.json();

                if (result.success) {{
                    closeServiceModal();
                    loadClinicServices();
                }} else {{
                    alert(result.message);
                }}
            }};

            window.deleteService = async function(id) {{
                if (confirm("Voulez-vous supprimer ce service de la base de données ?")) {{
                    await fetch('/api/clinic/services/delete', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ id }})
                    }});
                    loadClinicServices();
                }}
            }};

            // Montage initial
            loadClinicSettings();
        }})();
    </script>
    """