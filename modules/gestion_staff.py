"""Module de gestion du personnel clinique et corps médical (Gestion Du Staff).

Intègre la persistance PostgreSQL, la liaison avec les services cliniques,
le filtrage par rôle/service et la gestion des photos de profil.
Conforme strictement aux normes PEP 8.
"""

import base64
import logging
import os
import time
from typing import Dict, Any, List

from fastapi import APIRouter, HTTPException
from database import AVATARS_DIR, get_db_cursor

logger = logging.getLogger("StaffModule")

staff_router = APIRouter(prefix="/api/staff", tags=["Gestion du Staff"])

# Rôles et fonctions officielles du personnel
TYPES_PERSONNEL: List[str] = [
    "Professeur",
    "Docteur",
    "Médecin",
    "Infirmier",
    "Technicien en radiologie",
    "Sage-femme",
    "Kinésithérapeute",
    "Aide-kiné",
    "Pharmacien",
    "Gestionnaire de stock",
    "Ingénieur",
    "Direction",
    "Maintenancier",
    "Autre",
]
SPECIALITES_MEDICALES: List[str] = [
    "",
    "radiologues",
    "Médecin Radiologue",
    "Médecine Générale",
    "Cardiologie",
    "Chirurgie Générale",
    "Chirurgie Orthopédique & Traumatologie",
    "Pédiatrie",
    "Gynécologie & Obstétrique",
    "Anesthésie & Réanimation",
    "Radiologie & Imagerie Médicale",
    "Ophtalmologie",
    "Dermatologie & Vénérologie",
    "O.R.L (Oto-Rhino-Laryngologie)",
    "Neurologie",
    "Neurochirurgie",
    "Gastro-Entérologie & Hépatologie",
    "Pneumologie",
    "Urologie",
    "Néphrologie",
    "Endocrinologie & Diabétologie",
    "Rhumatologie",
    "Psychiatrie",
    "Oncologie Médicale",
    "Hématologie",
    "Médecine Interne",
    "Médecine d'Urgence",
    "Chirurgie Pédiatrique",
    "Chirurgie Vasculaire",
    "Chirurgie Plastique & Réparatrice",
    "Maladies Infectieuses",
    "Médecine Physique & Réadaptation",
]

AVATAR_PAR_DEFAUT = "/static/images/avatar-placeholder.svg?w=120&auto=format&fit=crop&q=80"


# ============================================================================
# LOGIQUE MÉTIER POSTGRESQL
# ============================================================================
class StaffManager:
    """Gestionnaire métier responsable du personnel et des praticiens."""

    @staticmethod
    def save_avatar_file(base64_data: str) -> str:
        """Sauvegarde une image encodée en base64 dans le dossier avatars."""
        if not base64_data or not base64_data.startswith("data:image"):
            return base64_data

        try:
            header, encoded = base64_data.split(",", 1)
            ext = header.split(";")[0].split("/")[1]
            if ext == "jpeg":
                ext = "jpg"
            filename = f"staff_{int(time.time() * 1000)}.{ext}"
            filepath = os.path.join(AVATARS_DIR, filename)

            with open(filepath, "wb") as f:
                f.write(base64.b64decode(encoded))

            return base64_data
        except Exception as err:
            logger.warning("Erreur lors de l'enregistrement de l'avatar: %s", err)
            return base64_data

    def get_all(self) -> List[Dict[str, Any]]:
        """Récupère la liste de tout le personnel ordonnée par nom."""
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT s.id, s.nom, s.prenom, s.type_personnel, s.service_nom, s.specialite,
                       s.telephone, s.email, s.avatar, s.notes, s.actif,s.linked_user_id,
                       u.prenom AS compte_prenom,u.nom AS compte_nom,u.login AS compte_login
                FROM clinic_staff s LEFT JOIN users u ON u.id=s.linked_user_id
                ORDER BY s.nom ASC, s.prenom ASC;
            """)
            rows = cursor.fetchall()

        staff_list = []
        for r in rows:
            staff_list.append({
                "id": r["id"],
                "nom": r["nom"],
                "prenom": r["prenom"],
                "type_personnel": r["type_personnel"],
                "service_nom": r["service_nom"] or "Non affecté",
                "specialite": r["specialite"] or "-",
                "telephone": r["telephone"] or "-",
                "email": r["email"] or "-",
                "avatar": r["avatar"] or AVATAR_PAR_DEFAUT,
                "notes": r["notes"] or "",
                "actif": r["actif"],
                "linked_user_id": r["linked_user_id"],
                "linked_user_label": (f"{r['compte_prenom']} {r['compte_nom']} · {r['compte_login']}" if r["linked_user_id"] else ""),
            })
        return staff_list

    def count(self) -> int:
        """Retourne le nombre total de membres du personnel."""
        with get_db_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total FROM clinic_staff;")
            row = cursor.fetchone()
            return row["total"] if row else 0

    def get_available_services(self) -> List[str]:
        """Récupère les services enregistrés dans la clinique."""
        with get_db_cursor() as cursor:
            cursor.execute("SELECT nom_service FROM clinic_services ORDER BY nom_service ASC;")
            rows = cursor.fetchall()
            return [r["nom_service"] for r in rows]

    def get_available_specialites(self) -> List[str]:
        """Retourne les spécialités par défaut et celles ajoutées par le staff."""
        with get_db_cursor() as cursor:
            cursor.execute("SELECT nom FROM clinic_staff_specialties ORDER BY nom ASC")
            custom = [row["nom"] for row in cursor.fetchall()]
        return list(dict.fromkeys(SPECIALITES_MEDICALES + custom))

    def get_available_types(self) -> List[str]:
        """Retourne les fonctions par défaut et celles ajoutées par le staff."""
        with get_db_cursor() as cursor:
            cursor.execute("SELECT nom FROM clinic_staff_roles ORDER BY nom ASC")
            custom = [row["nom"] for row in cursor.fetchall()]
        return list(dict.fromkeys(TYPES_PERSONNEL + custom))

    def get_available_accounts(self) -> List[Dict[str, Any]]:
        """Retourne les comptes Cliniva actifs pouvant être associés au staff."""
        with get_db_cursor() as cursor:
            cursor.execute("""SELECT id,prenom,nom,login,role FROM users
                WHERE status='Actif' ORDER BY nom,prenom,login""")
            return [dict(row) for row in cursor.fetchall()]

    def add_option(self, kind: str, value: str) -> Dict[str, Any]:
        """Ajoute une fonction ou une spécialité au catalogue persistant."""
        value = str(value or "").strip()
        if not value or len(value) > 100:
            return {"success": False, "message": "Saisissez un intitulé de 1 à 100 caractères."}
        catalogs = {
            "role": ("clinic_staff_roles", TYPES_PERSONNEL),
            "specialite": ("clinic_staff_specialties", SPECIALITES_MEDICALES),
        }
        if kind not in catalogs:
            return {"success": False, "message": "Type d'option inconnu."}
        table, defaults = catalogs[kind]
        if any(value.casefold() == item.casefold() for item in defaults):
            return {"success": False, "message": "Cette valeur existe déjà dans la liste."}
        with get_db_cursor() as cursor:
            cursor.execute(f"SELECT nom FROM {table} WHERE LOWER(nom)=LOWER(%s) LIMIT 1", (value,))
            if cursor.fetchone():
                return {"success": False, "message": "Cette valeur existe déjà dans la liste."}
            cursor.execute(f"INSERT INTO {table} (nom) VALUES (%s) ON CONFLICT (nom) DO NOTHING", (value,))
        return {"success": True, "value": value, "message": "Option ajoutée à la liste."}

    def save(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Crée ou met à jour un membre du staff."""
        staff_id = data.get("id")
        nom = data.get("nom", "").strip().upper()
        prenom = data.get("prenom", "").strip().title()
        type_personnel = data.get("type_personnel", "").strip()
        service_nom = data.get("service_nom", "").strip()
        specialite = data.get("specialite", "").strip()
        telephone = data.get("telephone", "").strip()
        email = data.get("email", "").strip()
        notes = data.get("notes", "").strip()
        raw_avatar = data.get("avatar", "")
        avatar = self.save_avatar_file(raw_avatar) if raw_avatar else AVATAR_PAR_DEFAUT
        raw_linked_user_id = data.get("linked_user_id")
        try:
            linked_user_id = int(raw_linked_user_id) if raw_linked_user_id not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            return {"success": False, "message": "Le compte Cliniva sélectionné est invalide."}

        if not nom or not prenom or not type_personnel:
            return {
                "success": False,
                "message": "Le Nom, le Prénom et la Fonction (Type) sont obligatoires.",
            }

        with get_db_cursor() as cursor:
            if linked_user_id is not None:
                cursor.execute("SELECT id FROM users WHERE id=%s AND status='Actif'", (linked_user_id,))
                if not cursor.fetchone():
                    return {"success": False, "message": "Le compte Cliniva choisi est introuvable ou inactif."}
            if staff_id:
                query = """
                    UPDATE clinic_staff SET 
                        nom = %s, prenom = %s, type_personnel = %s, service_nom = %s, 
                        specialite = %s, telephone = %s, email = %s, avatar = %s, notes = %s, linked_user_id = %s
                    WHERE id = %s;
                """
                cursor.execute(query, (nom, prenom, type_personnel, service_nom, specialite, telephone, email, avatar, notes, linked_user_id, staff_id))
            else:
                query = """
                    INSERT INTO clinic_staff 
                    (nom, prenom, type_personnel, service_nom, specialite, telephone, email, avatar, notes, linked_user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """
                cursor.execute(query, (nom, prenom, type_personnel, service_nom, specialite, telephone, email, avatar, notes, linked_user_id))

        return {"success": True, "message": "Membre du personnel enregistré avec succès !"}

    def delete(self, staff_id: int) -> Dict[str, Any]:
        """Supprime un membre du personnel."""
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM clinic_staff WHERE id = %s;", (staff_id,))
        return {"success": True, "message": "Collaborateur supprimé."}


staff_manager = StaffManager()


# ============================================================================
# POINTS D'ACCÈS FASTAPI (ROUTES JSON)
# ============================================================================
@staff_router.get("")
async def api_get_staff():
    try:
        return {
            "staff": staff_manager.get_all(),
            "total_count": staff_manager.count(),
            "services": staff_manager.get_available_services(),
            "specialites": staff_manager.get_available_specialites(),
            "types_personnel": staff_manager.get_available_types(),
            "accounts": staff_manager.get_available_accounts(),
        }
    except Exception as exc:
        logger.error("Erreur API Staff: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@staff_router.post("/save")
async def api_save_staff(payload: Dict[str, Any]):
    return staff_manager.save(payload)


@staff_router.post("/options")
async def api_add_staff_option(payload: Dict[str, Any]):
    return staff_manager.add_option(payload.get("kind", ""), payload.get("value", ""))


@staff_router.post("/delete")
async def api_delete_staff(payload: Dict[str, Any]):
    return staff_manager.delete(payload.get("id"))


# ============================================================================
# RENDU DU COMPOSANT HTML ET DU SCRIPT INTERACTIF
# ============================================================================
def render_html() -> str:
    """Génère l'HTML complet avec Dark Mode, Modale et gestion des photos."""
    types_options = "".join([f'<option value="{t}">{t}</option>' for t in TYPES_PERSONNEL])

    return f"""
    <div id="view-staff" class="space-y-6">
        
        <!-- BANDEAU D'EN-TÊTE -->
        <div class="banner-glow rounded-3xl p-6 lg:p-8 text-white shadow-xl flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
            <div>
                <h1 class="text-2xl font-extrabold tracking-tight mb-1">Gestion du Staff & Ressources Humaines</h1>
                <p class="text-xs text-slate-300">Corps médical, soignants, techniciens, administration et services techniques de la clinique.</p>
            </div>
            
            <div class="flex items-center gap-3">
                <div class="bg-white/10 px-4 py-2 rounded-2xl backdrop-blur-sm border border-white/10 text-xs">
                    <span class="text-slate-300">Total Collaborateurs:</span> 
                    <strong id="totalStaffCount" class="text-brand-neon font-black text-sm ml-1">0</strong>
                </div>

                <button onclick="openStaffModal()" class="flex items-center gap-2 bg-brand-neon hover:bg-brand-neonDark text-black px-5 py-2.5 rounded-2xl font-bold text-xs shadow-md shadow-brand-neon/20 transition">
                    <i class="ph-bold ph-user-plus text-base"></i>
                    <span>Nouveau Collaborateur</span>
                </button>
            </div>
        </div>

        <!-- BARRE DE RECHERCHE ET FILTRES DÉROULANTS -->
        <div class="bg-white dark:bg-[#121c15] p-4 rounded-3xl shadow-sm border border-slate-100 dark:border-white/5 space-y-3 transition-colors">
            <div class="flex flex-col md:flex-row items-center justify-between gap-3">
                <div class="flex items-center gap-2.5 bg-slate-50 dark:bg-black/30 rounded-2xl px-4 py-2 border border-slate-200/80 dark:border-white/10 w-full md:w-80">
                    <i class="ph ph-magnifying-glass text-slate-400 text-base"></i>
                    <input type="text" id="searchStaffInput" oninput="filterStaffTable()" placeholder="Rechercher nom, prénom, email, téléphone..." class="w-full text-xs outline-none bg-transparent text-slate-700 dark:text-slate-200">
                </div>

                <div class="grid grid-cols-1 sm:grid-cols-2 gap-2.5 w-full md:w-auto">
                    <select id="filterStaffType" onchange="filterStaffTable()" class="text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-slate-200 font-medium outline-none focus:border-brand-neon">
                        <option value="">👤 Tous les Rôles / Fonctions</option>
                        {types_options}
                    </select>

                    <select id="filterStaffService" onchange="filterStaffTable()" class="text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-slate-200 font-medium outline-none focus:border-brand-neon">
                        <option value="">🏥 Tous les Services</option>
                    </select>
                </div>
            </div>
        </div>

        <!-- TABLEAU DU PERSONNEL -->
        <div class="bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 overflow-x-auto transition-colors">
            <table class="w-full text-left text-xs">
                <thead>
                    <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-3">
                        <th class="pb-3 pl-2">Collaborateur</th>
                        <th class="pb-3">Fonction / Grade</th>
                        <th class="pb-3">Service Rattaché</th>
                        <th class="pb-3">Spécialité</th>
                        <th class="pb-3">Téléphone</th>
                        <th class="pb-3">Courriel</th>
                        <th class="pb-3">Notes</th>
                        <th class="pb-3 pr-2 text-right">Actions</th>
                    </tr>
                </thead>
                <tbody id="staff-table-body" class="divide-y divide-slate-100 dark:divide-white/5 text-slate-700 dark:text-slate-300 font-medium"></tbody>
            </table>
        </div>

    </div>

    <!-- ================= MODALE COLLABORATEUR (AJOUT / MODIFICATION) ================= -->
    <div id="staffModal" class="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center hidden z-50 p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-2xl max-h-[92vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            
            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50/50 dark:bg-black/20">
                <div class="flex items-center space-x-2">
                    <div class="w-8 h-8 rounded-xl bg-brand-neon/20 text-brand-neon flex items-center justify-center font-bold text-sm">
                        <i class="ph-bold ph-identification-badge"></i>
                    </div>
                    <h3 id="staffModalTitle" class="text-base font-bold text-slate-800 dark:text-white">Ajouter un Collaborateur</h3>
                </div>
                <button onclick="closeStaffModal()" class="text-slate-400 hover:text-white p-1 rounded-lg">
                    <i class="ph-bold ph-x text-lg"></i>
                </button>
            </div>

            <form id="staffForm" onsubmit="handleStaffSubmit(event)" class="p-6 overflow-y-auto space-y-4">
                <input type="hidden" id="staffId">
                <input type="hidden" id="staffAvatarData">

                <!-- 1. PHOTO DE PROFIL -->
                <div class="flex items-center space-x-5 bg-brand-neon/10 dark:bg-brand-neon/5 p-4 rounded-2xl border border-brand-neon/20">
                    <div class="relative group shrink-0">
                        <img id="staffAvatarPreview" src="{AVATAR_PAR_DEFAUT}" class="w-20 h-20 rounded-2xl object-cover border-2 border-brand-neon shadow-md">
                        <label for="staffAvatarInput" class="absolute inset-0 bg-black/50 rounded-2xl flex items-center justify-center text-white opacity-0 group-hover:opacity-100 cursor-pointer transition">
                            <i class="ph-bold ph-camera text-2xl"></i>
                        </label>
                    </div>
                    <div>
                        <h4 class="text-xs font-bold text-slate-800 dark:text-white mb-0.5">Photo de Profil</h4>
                        <p class="text-[11px] text-slate-400 mb-2.5">Téléversez un portrait professionnel (JPG, PNG, WEBP).</p>
                        <label for="staffAvatarInput" class="cursor-pointer bg-white dark:bg-[#121c15] text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10 px-3.5 py-1.5 rounded-xl text-xs font-bold shadow-sm inline-flex items-center gap-1.5 hover:bg-slate-50 dark:hover:bg-white/5 transition">
                            <i class="ph-bold ph-upload text-brand-neon"></i>
                            <span>Choisir une photo...</span>
                        </label>
                        <input type="file" id="staffAvatarInput" accept="image/*" class="hidden" onchange="previewStaffAvatar(event)">
                    </div>
                </div>

                <!-- 2. IDENTITÉ (NOM & PRÉNOM) -->
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Nom de famille *</label>
                        <input type="text" id="staffNom" required placeholder="Ex: MANSOURI" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-bold uppercase text-slate-800 dark:text-white">
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Prénom *</label>
                        <input type="text" id="staffPrenom" required placeholder="Ex: Karim" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-medium text-slate-800 dark:text-white">
                    </div>
                </div>

                <!-- 3. RÔLE & SERVICE RATTACHÉ -->
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <div class="mb-1 flex items-center justify-between gap-2"><label for="staffType" class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300">Fonction / Rôle *</label><button type="button" onclick="openStaffOption('role')" class="inline-flex items-center gap-1 rounded-lg border border-brand-neon/30 bg-brand-neon/10 px-2 py-1 text-[10px] font-bold text-emerald-800 transition hover:bg-brand-neon/20 dark:text-brand-neon"><i class="ph-bold ph-plus"></i>Ajouter</button></div>
                        <select id="staffType" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-semibold">
                            <option value="" disabled selected>-- Sélectionnez un rôle --</option>
                            {types_options}
                        </select>
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Service Rattaché *</label>
                        <select id="staffService" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-semibold">
                            <option value="" disabled selected>-- Sélectionnez un service --</option>
                        </select>
                    </div>
                </div>

                <!-- 4. SPÉCIALITÉ MÉDICALE -->
                <div>
                    <div class="mb-1 flex items-center justify-between gap-2"><label for="staffSpecialite" class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300">Spécialité Médicale (Si applicable)</label><button type="button" onclick="openStaffOption('specialite')" class="inline-flex items-center gap-1 rounded-lg border border-brand-neon/30 bg-brand-neon/10 px-2 py-1 text-[10px] font-bold text-emerald-800 transition hover:bg-brand-neon/20 dark:text-brand-neon"><i class="ph-bold ph-plus"></i>Ajouter</button></div>
                    <select id="staffSpecialite" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium">
                        <option value="">Non applicable / Général</option>
                    </select>
                </div>

                <!-- 5. COORDONNÉES DE CONTACT -->
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Numéro de Téléphone</label>
                        <input type="text" id="staffTel" placeholder="0550 11 22 33" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Adresse Courriel</label>
                        <input type="email" id="staffEmail" placeholder="k.mansouri@cliniva.dz" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                </div>

                <div class="rounded-2xl border border-sky-200 bg-sky-50/70 p-4 dark:border-sky-400/20 dark:bg-sky-400/[.05]">
                    <label for="staffLinkedUser" class="block text-[11px] font-bold text-slate-700 dark:text-slate-200">Compte Cliniva associé <span class="font-normal text-slate-500">(facultatif)</span></label>
                    <select id="staffLinkedUser" class="mt-1.5 w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-white dark:bg-black/30 text-slate-800 dark:text-white">
                        <option value="">Aucun · liaison automatique par nom et prénom</option>
                    </select>
                    <p class="mt-2 text-[10px] leading-4 text-slate-500 dark:text-slate-400">Choisissez un compte si son nom de connexion diffère de l’identité du membre du staff. Ce compte recevra ses demandes de validation par e-mail à l’adresse indiquée ci-dessus.</p>
                </div>

                <!-- 6. NOTES ET OBSERVATIONS -->
                <div>
                    <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Notes Internes & Observations</label>
                    <textarea id="staffNotes" rows="2" placeholder="Informations contractuelles, horaires spécifiques, astreintes..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white"></textarea>
                </div>

                <!-- BOUTONS D'ACTION -->
                <div class="flex items-center justify-end space-x-3 pt-4 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closeStaffModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler</button>
                    <button type="submit" class="px-6 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md shadow-brand-neon/20">Enregistrer</button>
                </div>
            </form>
        </div>
    </div>

    <div id="staffOptionModal" class="fixed inset-0 z-[60] hidden items-center justify-center bg-black/60 p-4 backdrop-blur-sm" role="dialog" aria-modal="true" aria-labelledby="staffOptionTitle">
        <form onsubmit="saveStaffOption(event)" class="w-full max-w-md rounded-2xl border border-slate-100 bg-white p-5 shadow-2xl dark:border-white/10 dark:bg-[#121c15]">
            <div class="mb-4 flex items-center justify-between gap-3"><div><p class="text-[10px] font-bold uppercase tracking-widest text-emerald-700 dark:text-brand-neon">Catalogue du staff</p><h3 id="staffOptionTitle" class="mt-1 text-base font-extrabold text-slate-800 dark:text-white">Ajouter une option</h3></div><button type="button" onclick="closeStaffOption()" class="grid h-8 w-8 place-items-center rounded-lg text-slate-500 hover:bg-slate-100 dark:hover:bg-white/10" aria-label="Fermer"><i class="ph-bold ph-x"></i></button></div>
            <input id="staffOptionKind" type="hidden">
            <label class="block text-xs font-semibold text-slate-600 dark:text-slate-300">Intitulé<input id="staffOptionValue" required maxlength="100" class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm text-slate-800 outline-none focus:border-brand-neon dark:border-white/10 dark:bg-black/30 dark:text-white" placeholder="Saisir un intitulé"></label>
            <div class="mt-5 flex justify-end gap-2 border-t border-slate-100 pt-4 dark:border-white/10"><button type="button" onclick="closeStaffOption()" class="rounded-xl border border-slate-200 px-3 py-2 text-xs font-bold text-slate-600 dark:border-white/10 dark:text-slate-300">Annuler</button><button type="submit" class="inline-flex items-center gap-1.5 rounded-xl bg-brand-neon px-4 py-2 text-xs font-bold text-black shadow-sm"><i class="ph-bold ph-plus-circle"></i>Ajouter à la liste</button></div>
        </form>
    </div>

    <!-- SCRIPT JAVASCRIPT DU MODULE STAFF -->
    <script>
        (function() {{
            let staffData = [];
            let availableServices = [];
            let availableSpecialites = [];
            let availableTypes = [];
            let availableAccounts = [];
            const defaultAvatar = "{AVATAR_PAR_DEFAUT}";

            // Couleurs des badges par type de fonction
            const roleBadgeStyles = {{
                'Professeur': 'bg-rose-50 dark:bg-rose-950/40 text-rose-600 dark:text-rose-300 border-rose-200 dark:border-rose-800/40',
                'Docteur': 'bg-sky-50 dark:bg-sky-950/40 text-sky-600 dark:text-sky-300 border-sky-200 dark:border-sky-800/40',
                'Médecin': 'bg-sky-50 dark:bg-sky-950/40 text-sky-600 dark:text-sky-300 border-sky-200 dark:border-sky-800/40',
                'Infirmier': 'bg-emerald-50 dark:bg-emerald-950/40 text-emerald-600 dark:text-emerald-300 border-emerald-200 dark:border-emerald-800/40',
                'Sage-femme': 'bg-purple-50 dark:bg-purple-950/40 text-purple-600 dark:text-purple-300 border-purple-200 dark:border-purple-800/40',
                'Technicien en radiologie': 'bg-indigo-50 dark:bg-indigo-950/40 text-indigo-600 dark:text-indigo-300 border-indigo-200 dark:border-indigo-800/40',
                'Pharmacien': 'bg-brand-neon/20 text-emerald-800 dark:text-brand-neon border-brand-neon/30',
                'Gestionnaire de stock': 'bg-amber-50 dark:bg-amber-950/40 text-amber-600 dark:text-amber-300 border-amber-200 dark:border-amber-800/40',
                'Kinésithérapeute': 'bg-teal-50 dark:bg-teal-950/40 text-teal-600 dark:text-teal-300 border-teal-200 dark:border-teal-800/40',
                'Aide-kiné': 'bg-teal-50 dark:bg-teal-950/40 text-teal-600 dark:text-teal-300 border-teal-200 dark:border-teal-800/40',
                'Direction': 'bg-slate-100 dark:bg-black/40 text-slate-800 dark:text-slate-200 border-slate-300 dark:border-white/10',
                'Ingénieur': 'bg-blue-50 dark:bg-blue-950/40 text-blue-600 dark:text-blue-300 border-blue-200 dark:border-blue-800/40',
                'Maintenancier': 'bg-orange-50 dark:bg-orange-950/40 text-orange-600 dark:text-orange-300 border-orange-200 dark:border-orange-800/40'
            }};

            window.loadStaffModule = async function() {{
                try {{
                    const res = await fetch('/api/staff');
                    const data = await res.json();
                    staffData = data.staff || [];
                    availableServices = data.services || [];
                    availableSpecialites = data.specialites || [];
                    availableTypes = data.types_personnel || [];
                    availableAccounts = data.accounts || [];

                    document.getElementById('totalStaffCount').innerText = data.total_count || staffData.length;
                    renderStaffTable(staffData);
                    populateSelectOptions();
                }} catch (e) {{
                    console.error("Erreur de chargement du staff:", e);
                }}
            }};

            window.populateSelectOptions = function() {{
                const sType = document.getElementById('staffType');
                const fType = document.getElementById('filterStaffType');
                const selectedType = sType.value;
                const selectedFilterType = fType.value;
                sType.innerHTML = '<option value="" disabled>-- Sélectionnez un rôle --</option>';
                fType.innerHTML = '<option value="">👤 Tous les Rôles / Fonctions</option>';
                availableTypes.forEach(type => {{
                    const option = document.createElement('option'); option.value = type; option.textContent = type; sType.appendChild(option);
                    const filterOption = document.createElement('option'); filterOption.value = type; filterOption.textContent = type; fType.appendChild(filterOption);
                }});
                sType.value = selectedType;
                fType.value = selectedFilterType;

                // Sélecteurs de services (dans la modale et dans le filtre de recherche)
                const sService = document.getElementById('staffService');
                const fService = document.getElementById('filterStaffService');
                const selectedService = sService.value;
                const selectedFilterService = fService.value;
                
                let sOptions = '<option value="" disabled selected>-- Sélectionnez un service --</option>';
                let fOptions = '<option value="">🏥 Tous les Services</option>';

                availableServices.forEach(s => {{
                    sOptions += `<option value="${{s}}">${{s}}</option>`;
                    fOptions += `<option value="${{s}}">${{s}}</option>`;
                }});

                sService.innerHTML = sOptions;
                fService.innerHTML = fOptions;
                sService.value = selectedService;
                fService.value = selectedFilterService;

                // Sélecteur de spécialités médicales dans la modale
                const sSpe = document.getElementById('staffSpecialite');
                const selectedSpecialite = sSpe.value;
                let speOptions = '<option value="">Non applicable / Général</option>';
                availableSpecialites.forEach(sp => {{
                    speOptions += `<option value="${{sp}}">${{sp}}</option>`;
                }});
                sSpe.innerHTML = speOptions;
                sSpe.value = selectedSpecialite;

                const linkedUser = document.getElementById('staffLinkedUser');
                const selectedLinkedUser = linkedUser.value;
                linkedUser.innerHTML = '<option value="">Aucun · liaison automatique par nom et prénom</option>' +
                    availableAccounts.map(account => `<option value="${{account.id}}">${{account.prenom}} ${{account.nom}} · ${{account.login}} · ${{account.role}}</option>`).join('');
                linkedUser.value = selectedLinkedUser;
            }};

            window.openStaffOption = function(kind) {{
                document.getElementById('staffOptionKind').value = kind;
                document.getElementById('staffOptionTitle').textContent = kind === 'role' ? 'Ajouter une fonction / un rôle' : 'Ajouter une spécialité médicale';
                document.getElementById('staffOptionValue').value = '';
                const modal = document.getElementById('staffOptionModal');
                modal.classList.remove('hidden'); modal.classList.add('flex');
                document.getElementById('staffOptionValue').focus();
            }};

            window.closeStaffOption = function() {{
                const modal = document.getElementById('staffOptionModal');
                modal.classList.add('hidden'); modal.classList.remove('flex');
            }};

            window.saveStaffOption = async function(event) {{
                event.preventDefault();
                const kind = document.getElementById('staffOptionKind').value;
                const value = document.getElementById('staffOptionValue').value.trim();
                try {{
                    const response = await fetch('/api/staff/options', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{kind,value}})}});
                    const result = await response.json();
                    if (!response.ok || !result.success) {{ alert(result.message || 'Impossible d’ajouter cette option.'); return; }}
                    if (kind === 'role') availableTypes = [...availableTypes, result.value];
                    else availableSpecialites = [...availableSpecialites, result.value];
                    populateSelectOptions();
                    if (kind === 'role') document.getElementById('staffType').value = result.value;
                    else document.getElementById('staffSpecialite').value = result.value;
                    closeStaffOption();
                }} catch (error) {{ alert('Erreur lors de l’ajout de l’option.'); }}
            }};

            window.renderStaffTable = function(items) {{
                const tbody = document.getElementById('staff-table-body');
                tbody.innerHTML = '';

                if (!items || items.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="8" class="text-center py-6 text-slate-400">Aucun collaborateur trouvé.</td></tr>';
                    return;
                }}

                items.forEach(st => {{
                    const tr = document.createElement('tr');
                    tr.className = "hover:bg-slate-50/80 dark:hover:bg-white/[0.02] transition-colors";

                    const badgeStyle = roleBadgeStyles[st.type_personnel] || 'bg-slate-100 dark:bg-black/30 text-slate-700 dark:text-slate-300 border-slate-200 dark:border-white/10';

                    tr.innerHTML = `
                        <td class="py-3.5 pl-2">
                            <div class="flex items-center gap-3">
                                <img src="${{st.avatar}}" class="w-10 h-10 rounded-2xl object-cover border border-slate-200 dark:border-white/10 shadow-sm">
                                <div>
                                    <div class="font-bold text-slate-800 dark:text-slate-100 text-xs">${{st.nom}} ${{st.prenom}}</div>
                                    <div class="text-[10px] text-slate-400 font-mono">ID: #${{st.id}}</div>
                                </div>
                            </div>
                        </td>
                        <td class="py-3.5">
                            <span class="px-2.5 py-1 rounded-xl text-[10px] font-bold border ${{badgeStyle}}">${{st.type_personnel}}</span>
                        </td>
                        <td class="py-3.5">
                            <span class="px-2.5 py-1 rounded-xl text-[10px] font-bold bg-slate-100 dark:bg-black/40 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10">🏥 ${{st.service_nom}}</span>
                        </td>
                        <td class="py-3.5 text-slate-600 dark:text-slate-400">${{st.specialite}}</td>
                        <td class="py-3.5 text-slate-600 dark:text-slate-300 font-medium">${{st.telephone}}</td>
                        <td class="py-3.5 text-slate-600 dark:text-slate-400 font-mono text-[11px]">${{st.email}}</td>
                        <td class="py-3.5 text-slate-400 italic text-[11px] max-w-[140px] truncate" title="${{st.notes}}">
                            ${{st.notes || '-'}}
                        </td>
                        <td class="py-3.5 pr-2 text-right">
                            <div class="inline-flex items-center gap-1.5">
                                <button onclick="editStaff(${{st.id}})" class="p-1.5 text-sky-500 hover:bg-sky-50 dark:hover:bg-sky-500/10 rounded-lg"><i class="ph-bold ph-pencil-simple text-sm"></i></button>
                                <button onclick="deleteStaff(${{st.id}})" class="p-1.5 text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-500/10 rounded-lg"><i class="ph-bold ph-trash text-sm"></i></button>
                            </div>
                        </td>
                    `;
                    tbody.appendChild(tr);
                }});
            }};

            window.filterStaffTable = function() {{
                const q = (document.getElementById('searchStaffInput').value || '').toLowerCase().trim();
                const selType = (document.getElementById('filterStaffType').value || '').trim();
                const selService = (document.getElementById('filterStaffService').value || '').trim();

                const filtered = staffData.filter(st => {{
                    const matchQ = !q || (
                        st.nom.toLowerCase().includes(q) ||
                        st.prenom.toLowerCase().includes(q) ||
                        (st.telephone && st.telephone.includes(q)) ||
                        (st.email && st.email.toLowerCase().includes(q)) ||
                        (st.notes && st.notes.toLowerCase().includes(q))
                    );

                    const matchType = !selType || st.type_personnel === selType;
                    const matchService = !selService || st.service_nom === selService;

                    return matchQ && matchType && matchService;
                }});

                renderStaffTable(filtered);
            }};

            window.previewStaffAvatar = function(event) {{
                const file = event.target.files[0];
                if (file) {{
                    const reader = new FileReader();
                    reader.onload = function(e) {{
                        document.getElementById('staffAvatarPreview').src = e.target.result;
                        document.getElementById('staffAvatarData').value = e.target.result;
                    }};
                    reader.readAsDataURL(file);
                }}
            }};

            window.openStaffModal = function() {{
                document.getElementById('staffForm').reset();
                document.getElementById('staffId').value = '';
                document.getElementById('staffAvatarData').value = '';
                document.getElementById('staffAvatarPreview').src = defaultAvatar;
                document.getElementById('staffModalTitle').innerText = 'Ajouter un Collaborateur';
                document.getElementById('staffModal').classList.remove('hidden');
            }};

            window.closeStaffModal = function() {{
                document.getElementById('staffModal').classList.add('hidden');
            }};

            window.editStaff = function(id) {{
                const st = staffData.find(x => x.id === id);
                if (!st) return;

                document.getElementById('staffId').value = st.id;
                document.getElementById('staffNom').value = st.nom;
                document.getElementById('staffPrenom').value = st.prenom;
                document.getElementById('staffType').value = st.type_personnel;
                document.getElementById('staffService').value = st.service_nom !== 'Non affecté' ? st.service_nom : '';
                document.getElementById('staffSpecialite').value = st.specialite !== '-' ? st.specialite : '';
                document.getElementById('staffTel').value = st.telephone !== '-' ? st.telephone : '';
                document.getElementById('staffEmail').value = st.email !== '-' ? st.email : '';
                document.getElementById('staffLinkedUser').value = st.linked_user_id || '';
                document.getElementById('staffNotes').value = st.notes || '';
                document.getElementById('staffAvatarData').value = st.avatar;
                document.getElementById('staffAvatarPreview').src = st.avatar || defaultAvatar;

                document.getElementById('staffModalTitle').innerText = `Modifier : ${{st.nom}} ${{st.prenom}}`;
                document.getElementById('staffModal').classList.remove('hidden');
            }};

            window.handleStaffSubmit = async function(e) {{
                e.preventDefault();

                const payload = {{
                    id: document.getElementById('staffId').value ? parseInt(document.getElementById('staffId').value) : null,
                    nom: document.getElementById('staffNom').value,
                    prenom: document.getElementById('staffPrenom').value,
                    type_personnel: document.getElementById('staffType').value,
                    service_nom: document.getElementById('staffService').value,
                    specialite: document.getElementById('staffSpecialite').value,
                    telephone: document.getElementById('staffTel').value,
                    email: document.getElementById('staffEmail').value,
                    linked_user_id: document.getElementById('staffLinkedUser').value || null,
                    notes: document.getElementById('staffNotes').value,
                    avatar: document.getElementById('staffAvatarData').value || document.getElementById('staffAvatarPreview').src
                }};

                const res = await fetch('/api/staff/save', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(payload)
                }}).then(r => r.json());

                if (res.success) {{
                    closeStaffModal();
                    loadStaffModule();
                }} else {{
                    alert(res.message);
                }}
            }};

            window.deleteStaff = async function(id) {{
                if (await window.showConfirm("Voulez-vous supprimer ce collaborateur du registre ?")) {{
                    await fetch('/api/staff/delete', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ id }})
                    }});
                    loadStaffModule();
                }}
            }};

            // Montage initial automatique
            loadStaffModule();
        }})();
    </script>
    """
