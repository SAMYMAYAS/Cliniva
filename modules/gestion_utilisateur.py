import base64
import hashlib
import json
import logging
import os
import time
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, HTTPException
from database import AVATARS_DIR, get_db_cursor

logger = logging.getLogger("UsersModule")

users_router = APIRouter(prefix="/api/users", tags=["Gestion Utilisateurs"])

AVATAR_DEFAULT = "/static/images/avatar-placeholder.svg?w=120&auto=format&fit=crop&q=80"


# ============================================================================
# LOGIQUE MÉTIER POSTGRESQL
# ============================================================================
class UserManager:
    """Gestionnaire métier des comptes utilisateurs, permissions et périmètres."""

    @staticmethod
    def hash_password(password: str) -> str:
        """Calcule l'empreinte SHA-256 du mot de passe."""
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

    @staticmethod
    def save_avatar_file(base64_data: str) -> str:
        """Sauvegarde l'avatar base64 dans le dossier uploads/avatars."""
        if not base64_data or not base64_data.startswith("data:image"):
            return base64_data

        try:
            header, encoded = base64_data.split(",", 1)
            ext = header.split(";")[0].split("/")[1]
            if ext == "jpeg":
                ext = "jpg"
            filename = f"user_{int(time.time() * 1000)}.{ext}"
            filepath = os.path.join(AVATARS_DIR, filename)

            with open(filepath, "wb") as f:
                f.write(base64.b64decode(encoded))

            return base64_data
        except Exception as err:
            logger.warning("Échec de sauvegarde de l'avatar: %s", err)
            return base64_data

    def get_available_specialites(self) -> List[Dict[str, Any]]:
        """Récupère l'ensemble des spécialités médicales existantes en base."""
        try:
            with get_db_cursor() as cursor:
                cursor.execute("SELECT id, code, nom FROM ref_specialites ORDER BY nom ASC;")
                rows = cursor.fetchall()
                return [dict(r) for r in rows]
        except Exception as err:
            logger.warning("Impossible de lire ref_specialites : %s", err)
            return []

    def get_available_plannings_rdv(self) -> List[Dict[str, Any]]:
        """Renvoie les plannings pouvant être attribués individuellement aux utilisateurs."""
        try:
            with get_db_cursor() as cursor:
                cursor.execute("""
                    SELECT id, specialite, jours_semaine, jour_semaine, heure_debut, heure_fin, actif
                    FROM appointment_specialty_schedules
                    ORDER BY specialite, jour_semaine, heure_debut;
                """)
                return [dict(row) for row in cursor.fetchall()]
        except Exception as err:
            logger.warning("Impossible de lire les plannings de rendez-vous : %s", err)
            return []

    def get_all_users(self) -> List[Dict[str, Any]]:
        """Récupère l'ensemble des utilisateurs enregistrés avec leurs spécialités."""
        with get_db_cursor() as cursor:
            cursor.execute("""
                           SELECT id,
                                  prenom,
                                  nom,
                                  telephone,
                                  role,
                                  login,
                                  status,
                                  avatar,
                                  permissions,
                                  specialites_autorisees,
                                  plannings_rdv_autorises
                           FROM users
                           ORDER BY id DESC;
                           """)
            rows = cursor.fetchall()

        users = []
        for r in rows:
            perms = r["permissions"]
            if isinstance(perms, str):
                try:
                    perms = json.loads(perms)
                except Exception:
                    perms = {}

            specs = r["specialites_autorisees"]
            if isinstance(specs, str):
                try:
                    specs = json.loads(specs)
                except Exception:
                    specs = []

            schedules = r["plannings_rdv_autorises"]
            if isinstance(schedules, str):
                try:
                    schedules = json.loads(schedules)
                except Exception:
                    schedules = []

            users.append({
                "id": r["id"],
                "prenom": r["prenom"],
                "nom": r["nom"],
                "telephone": r["telephone"] or "-",
                "role": r["role"],
                "login": r["login"],
                "status": r["status"] or "Actif",
                "avatar": r["avatar"] or AVATAR_DEFAULT,
                "permissions": perms or {},
                "specialites_autorisees": specs or [],
                "plannings_rdv_autorises": schedules,
            })
        return users

    def count(self) -> int:
        """Retourne le nombre total d'utilisateurs."""
        with get_db_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total FROM users;")
            row = cursor.fetchone()
            return row["total"] if row else 0

    def save_user(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Crée ou met à jour un utilisateur, ses droits et ses spécialités."""
        user_id = data.get("id")
        prenom = str(data.get("prenom") or "").strip().title()
        nom = str(data.get("nom") or "").strip().upper()
        telephone = str(data.get("telephone") or "").strip()
        role = str(data.get("role") or "Médecin").strip()
        login = str(data.get("login") or "").strip().lower()
        password = str(data.get("password") or "").strip()
        status = str(data.get("status") or "Actif").strip()
        perms = data.get("permissions") or {}
        perms_json = json.dumps(perms)

        specs = data.get("specialites_autorisees") or []
        specs_json = json.dumps(specs)
        schedules = data.get("plannings_rdv_autorises") or []
        try:
            schedules = sorted({int(value) for value in schedules})
        except (TypeError, ValueError):
            return {"success": False, "message": "La sélection des plannings de rendez-vous est invalide."}
        if any(value < 1 for value in schedules):
            return {"success": False, "message": "La sélection des plannings de rendez-vous est invalide."}
        with get_db_cursor() as cursor:
            if schedules:
                cursor.execute("SELECT COUNT(*) AS total FROM appointment_specialty_schedules WHERE id=ANY(%s);", (schedules,))
                if int(cursor.fetchone()["total"]) != len(schedules):
                    return {"success": False, "message": "Un planning sélectionné n'existe plus."}
        schedules_json = json.dumps(schedules)

        raw_avatar = data.get("avatar") or ""
        avatar_path = self.save_avatar_file(raw_avatar) if raw_avatar else AVATAR_DEFAULT

        if not prenom or not nom or not login:
            return {"success": False, "message": "Le prénom, le nom et le login sont obligatoires."}

        with get_db_cursor() as cursor:
            try:
                if user_id:
                    if password:
                        pwd_hash = self.hash_password(password)
                        query = """
                                UPDATE users \
                                SET prenom        = %s, \
                                    nom           = %s, \
                                    telephone     = %s, \
                                    role          = %s, \
                                    login         = %s, \
                                    password_hash = %s, \
                                    status        = %s, \
                                    avatar        = %s, \
                                    permissions   = %s::jsonb, specialites_autorisees = %s::jsonb,
                                    plannings_rdv_autorises = %s::jsonb
                                WHERE id = %s; \
                                """
                        cursor.execute(query,
                                       (prenom, nom, telephone, role, login, pwd_hash, status, avatar_path, perms_json,
                                        specs_json, schedules_json, user_id))
                    else:
                        query = """
                                UPDATE users \
                                SET prenom      = %s, \
                                    nom         = %s, \
                                    telephone   = %s, \
                                    role        = %s, \
                                    login       = %s, \
                                    status      = %s, \
                                    avatar      = %s, \
                                    permissions = %s::jsonb, specialites_autorisees = %s::jsonb,
                                    plannings_rdv_autorises = %s::jsonb
                                WHERE id = %s; \
                                """
                        cursor.execute(query, (prenom, nom, telephone, role, login, status, avatar_path, perms_json,
                                               specs_json, schedules_json, user_id))
                else:
                    pwd_hash = self.hash_password(password if password else "123456")
                    query = """
                            INSERT INTO users
                            (prenom, nom, telephone, role, login, password_hash, status, avatar, permissions, \
                             specialites_autorisees, plannings_rdv_autorises)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb); \
                            """
                    cursor.execute(query,
                                   (prenom, nom, telephone, role, login, pwd_hash, status, avatar_path, perms_json,
                                    specs_json, schedules_json))

                return {"success": True, "message": "Utilisateur, droits et spécialités enregistrés avec succès !"}

            except Exception as exc:
                if "unique" in str(exc).lower() or "login" in str(exc).lower():
                    return {"success": False,
                            "message": "Cet identifiant (login) est déjà utilisé par un autre collaborateur."}
                return {"success": False, "message": f"Erreur de sauvegarde : {str(exc)}"}

    def toggle_status(self, user_id: int, current_status: str) -> Dict[str, Any]:
        """Bascule le statut du compte entre Actif et Inactif."""
        new_status = "Inactif" if current_status == "Actif" else "Actif"
        with get_db_cursor() as cursor:
            cursor.execute("UPDATE users SET status = %s WHERE id = %s;", (new_status, user_id))
        return {"success": True, "new_status": new_status}

    def delete_user(self, user_id: int) -> Dict[str, Any]:
        """Supprime un utilisateur."""
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM users WHERE id = %s;", (user_id,))
        return {"success": True, "message": "Compte utilisateur supprimé."}


user_manager = UserManager()


# ============================================================================
# POINTS D'ACCÈS FASTAPI (ROUTES JSON)
# ============================================================================
@users_router.get("")
async def api_get_users():
    try:
        users = user_manager.get_all_users()
        total = user_manager.count()
        specs = user_manager.get_available_specialites()
        schedules = user_manager.get_available_plannings_rdv()
        return {
            "success": True,
            "users": users,
            "total_count": total,
            "available_specialites": specs,
            "available_rdv_schedules": schedules
        }
    except Exception as exc:
        logger.error("Erreur GET /api/users: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@users_router.post("/save")
async def api_save_user(payload: Dict[str, Any]):
    return user_manager.save_user(payload)


@users_router.post("/toggle-status")
async def api_toggle_status(payload: Dict[str, Any]):
    return user_manager.toggle_status(payload.get("id"), payload.get("status"))


@users_router.post("/delete")
async def api_delete_user(payload: Dict[str, Any]):
    return user_manager.delete_user(payload.get("id"))


# ============================================================================
# RENDU DU COMPOSANT HTML ET DU SCRIPT INTERACTIF
# ============================================================================
def _render_html() -> str:
    """Génère l'HTML complet avec la sélection multiple des disciplines et spécialités."""
    return """
    <div id="view-users" class="space-y-6">

        <!-- BANDEAU D'EN-TÊTE -->
        <div class="banner-glow rounded-3xl p-6 lg:p-8 text-white shadow-xl flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
            <div>
                <h1 class="text-2xl font-extrabold tracking-tight mb-1">Gestion des Rôles & Permissions</h1>
                <p class="text-xs text-slate-300">Créez les comptes utilisateurs et configurez les droits d'accès granulaires ainsi que le périmètre des spécialités autorisées.</p>
            </div>

            <div class="flex items-center gap-3">
                <div class="bg-white/10 px-4 py-2 rounded-2xl backdrop-blur-sm border border-white/10 text-xs">
                    <span class="text-slate-300">Total Comptes:</span> 
                    <strong id="totalUsersCount" class="text-brand-neon font-black text-sm ml-1">0</strong>
                </div>

                <button onclick="openUserModal()" class="flex items-center gap-2 bg-brand-neon hover:bg-brand-neonDark text-black px-5 py-2.5 rounded-2xl font-bold text-xs shadow-md shadow-brand-neon/20 transition">
                    <i class="ph-bold ph-user-plus text-base"></i>
                    <span>Ajouter un Utilisateur</span>
                </button>
            </div>
        </div>

        <!-- BARRE DE RECHERCHE -->
        <div class="flex items-center bg-white dark:bg-[#121c15] p-3.5 rounded-3xl shadow-sm border border-slate-100 dark:border-white/5 transition-colors">
            <div class="flex items-center gap-2.5 bg-slate-50 dark:bg-black/30 rounded-2xl px-4 py-2 border border-slate-200/80 dark:border-white/10 w-full md:w-96">
                <i class="ph ph-magnifying-glass text-slate-400 text-base"></i>
                <input type="text" id="searchUserInput" oninput="filterUsers()" placeholder="Rechercher par nom, rôle, login, spécialité..." class="w-full text-xs outline-none bg-transparent text-slate-700 dark:text-slate-200">
            </div>
        </div>

        <!-- TABLEAU DES UTILISATEURS -->
        <div class="bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 flex flex-col transition-colors">
            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                    <thead>
                        <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-3">
                            <th class="pb-3 pl-2">Utilisateur</th>
                            <th class="pb-3">Rôle / Poste</th>
                            <th class="pb-3">Périmètre Médical (Spécialités)</th>
                            <th class="pb-3">Téléphone</th>
                            <th class="pb-3">Identifiant (Login)</th>
                            <th class="pb-3">Statut</th>
                            <th class="pb-3">Droits d'accès</th>
                            <th class="pb-3 pr-2 text-right">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="users-table-body" class="divide-y divide-slate-100 dark:divide-white/5 text-slate-700 dark:text-slate-300 font-medium"></tbody>
                </table>
            </div>

            <!-- PAGINATION STRICTE PAR 15 LIGNES -->
            <div id="userPaginationWrapper" class="flex flex-col sm:flex-row items-center justify-between gap-3 pt-5 mt-3 border-t border-slate-100 dark:border-white/5 text-xs">
                <span id="userPaginationInfo" class="text-slate-500 dark:text-slate-400 font-medium">Affichage des résultats...</span>
                <div class="flex items-center gap-1.5" id="userPaginationControls"></div>
            </div>
        </div>

    </div>

    <!-- ================= MODALE UTILISATEUR ================= -->
    <div id="userModal" class="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center hidden z-50 p-4">
        <div style="max-width:1180px" class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-h-[94vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">

            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50/50 dark:bg-black/20">
                <div class="flex items-center space-x-2">
                    <div class="w-8 h-8 rounded-xl bg-brand-neon/20 text-brand-neon flex items-center justify-center font-bold text-sm">
                        <i class="ph-bold ph-shield-check"></i>
                    </div>
                    <h3 id="modalTitle" class="text-base font-bold text-slate-800 dark:text-white">Ajouter un Collaborateur</h3>
                </div>
                <button type="button" onclick="closeUserModal()" class="text-slate-400 hover:text-white p-1 rounded-lg">
                    <i class="ph-bold ph-x text-lg"></i>
                </button>
            </div>

            <form id="userForm" onsubmit="handleUserFormSubmit(event)" class="p-6 overflow-y-auto space-y-6">
                <input type="hidden" id="userId">
                <input type="hidden" id="avatarData">

                <!-- 1. PHOTO DE PROFIL -->
                <div class="flex items-center space-x-5 bg-brand-neon/10 dark:bg-brand-neon/5 p-4 rounded-2xl border border-brand-neon/20">
                    <div class="relative group shrink-0">
                        <img id="avatarPreview" src="/static/images/avatar-placeholder.svg?w=120&auto=format&fit=crop&q=80" class="w-16 h-16 rounded-2xl object-cover border-2 border-brand-neon shadow-md">
                        <label for="avatarUploadInput" class="absolute inset-0 bg-black/50 rounded-2xl flex items-center justify-center text-white opacity-0 group-hover:opacity-100 cursor-pointer transition">
                            <i class="ph-bold ph-camera text-2xl"></i>
                        </label>
                    </div>
                    <div>
                        <h4 class="text-xs font-bold text-slate-800 dark:text-white mb-0.5">Photo de Profil</h4>
                        <p class="text-[11px] text-slate-400 mb-2">JPG, PNG ou WEBP.</p>
                        <label for="avatarUploadInput" class="cursor-pointer bg-white dark:bg-[#121c15] text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10 px-3.5 py-1.5 rounded-xl text-xs font-bold shadow-sm inline-flex items-center gap-1.5 hover:bg-slate-50 dark:hover:bg-white/5 transition">
                            <i class="ph-bold ph-upload text-brand-neon"></i><span>Choisir une photo...</span>
                        </label>
                        <input type="file" id="avatarUploadInput" accept="image/*" class="hidden" onchange="previewUserAvatar(event)">
                    </div>
                </div>

                <!-- 2. IDENTITÉ (PRÉNOM, NOM & TÉLÉPHONE) -->
                <div>
                    <h4 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-2.5 flex items-center gap-1.5">
                        <i class="ph-bold ph-user"></i> Informations Générales
                    </h4>
                    <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
                        <div>
                            <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Prénom *</label>
                            <input type="text" id="userPrenom" required placeholder="Ex: Karim" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-medium text-slate-800 dark:text-white">
                        </div>
                        <div>
                            <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Nom *</label>
                            <input type="text" id="userNom" required placeholder="Ex: MANSOURI" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-bold uppercase text-slate-800 dark:text-white">
                        </div>
                        <div>
                            <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Téléphone *</label>
                            <input type="text" id="userTelephone" required placeholder="0550 00 00 00" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-medium text-slate-800 dark:text-white">
                        </div>
                    </div>
                </div>

                <!-- 3. RÔLE, LOGIN, MOT DE PASSE & STATUT -->
                <div class="grid grid-cols-1 sm:grid-cols-4 gap-3">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Rôle / Poste *</label>
                        <select id="userRoleSelect" onchange="applyUserRolePreset(this.value)" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-semibold">
                            <option value="Médecin">🩺 Médecin</option>
                            <option value="Réceptionniste">📋 Réceptionniste</option>
                            <option value="Caissier">💳 Caissier</option>
                            <option value="Surveillant Médical">🏥 Surveillant Médical</option>
                            <option value="Direction">👔 Direction</option>
                            <option value="Administrateur">⚙️ Administrateur</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Identifiant (Login) *</label>
                        <input type="text" id="userLogin" required placeholder="dr.mansouri" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white">
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Mot de passe</label>
                        <input type="password" id="userPassword" placeholder="Laisser vide si inchangé" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Statut du Compte</label>
                        <select id="userStatus" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-semibold">
                            <option value="Actif">🟢 Actif</option>
                            <option value="Inactif">🔴 Inactif</option>
                        </select>
                    </div>
                </div>

              

                <!-- 5. MATRICE GRANULAIRE DES 12 MODULES -->
                <div>
                    <div class="flex items-center justify-between mb-2">
                        <h4 class="text-xs font-bold uppercase tracking-wider text-slate-400 flex items-center gap-1.5">
                            <i class="ph-bold ph-lock"></i> Droits d'Accès & Permissions Fonctionnelles
                        </h4>
                        <div class="space-x-2 text-[11px]">
                            <button type="button" onclick="toggleAllUserPerms(true)" class="text-emerald-500 font-bold hover:underline">Tout cocher</button>
                            <span class="text-slate-400">|</span>
                            <button type="button" onclick="toggleAllUserPerms(false)" class="text-slate-400 font-bold hover:underline">Tout décocher</button>
                        </div>
                    </div>

                    <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3 text-xs bg-slate-50 dark:bg-black/30 p-4 rounded-3xl border border-slate-200/80 dark:border-white/10">

                        <!-- 1. Dashboard -->
                        <div class="space-y-1.5 bg-white dark:bg-[#121c15] p-3 rounded-2xl border border-slate-200/60 dark:border-white/10">
                            <div class="font-bold text-slate-800 dark:text-white text-[11px] border-b border-slate-100 dark:border-white/10 pb-1 flex items-center gap-1.5">
                                <i class="ph-bold ph-squares-four text-brand-neon"></i> Dashboard
                            </div>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_dash_view" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Consulter le Dashboard</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_dash_financial" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Voir Recettes & Finances</span></label>
                        </div>

                        <!-- 2. Accueil & Passages -->
                        <div class="space-y-1.5 bg-white dark:bg-[#121c15] p-3 rounded-2xl border border-slate-200/60 dark:border-white/10">
                            <div class="font-bold text-slate-800 dark:text-white text-[11px] border-b border-slate-100 dark:border-white/10 pb-1 flex items-center gap-1.5">
                                <i class="ph-bold ph-door-open text-sky-500"></i> Accueil & Passages
                            </div>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_reception_view" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Voir les Passages</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_reception_create" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Créer Passage / Admission</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_reception_edit" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Modifier / Supprimer Passage</span></label>
                        </div>

                        <!-- 3. Caisse & Encaissement -->
                        <div class="space-y-1.5 bg-white dark:bg-[#121c15] p-3 rounded-2xl border border-slate-200/60 dark:border-white/10">
                            <div class="font-bold text-slate-800 dark:text-white text-[11px] border-b border-slate-100 dark:border-white/10 pb-1 flex items-center gap-1.5">
                                <i class="ph-bold ph-currency-dollar text-amber-500"></i> Caisse & Encaissement
                            </div>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_cash_collect" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Encaisser & Imprimer Ticket</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_cash_collect_rest" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Encaisser les restes dus</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_cash_refund" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Effectuer un remboursement</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_cash_remise" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Accorder des Remises</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_cash_reports" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Voir Journal de Caisse</span></label>
                        </div>

                        <!-- 4. Fiches Patients -->
                        <div class="space-y-1.5 bg-white dark:bg-[#121c15] p-3 rounded-2xl border border-slate-200/60 dark:border-white/10">
                            <div class="font-bold text-slate-800 dark:text-white text-[11px] border-b border-slate-100 dark:border-white/10 pb-1 flex items-center gap-1.5">
                                <i class="ph-bold ph-users text-purple-500"></i> Fiches Patients
                            </div>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_pat_view" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Consulter les Patients</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_pat_create" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Créer un Patient</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_pat_edit" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Modifier Fiche & Scans</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_pat_delete" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span class="text-rose-500 font-semibold">Supprimer Dossier Patient</span></label>
                        </div>

                        <!-- 5. DME (Dossier Médical) -->
                        <div class="space-y-1.5 bg-white dark:bg-[#121c15] p-3 rounded-2xl border border-slate-200/60 dark:border-white/10">
                            <div class="font-bold text-slate-800 dark:text-white text-[11px] border-b border-slate-100 dark:border-white/10 pb-1 flex items-center gap-1.5">
                                <i class="ph-bold ph-stethoscope text-emerald-500"></i> Dossier Médical (DME)
                            </div>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_dme_view" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Ouvrir le DME du Patient</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_dme_consult" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Rédiger Consultations</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_dme_reset" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Réinitialiser une consultation DME en cours</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_dme_prescribe" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Émettre & Imprimer Ordonnances</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_dme_demandes" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Demandes biologiques, radiologiques & PEC / catalogue des bilans</span></label>
                        </div>

                        <!-- 6. Actes Médicaux -->
                        <div class="space-y-1.5 bg-white dark:bg-[#121c15] p-3 rounded-2xl border border-slate-200/60 dark:border-white/10">
                            <div class="font-bold text-slate-800 dark:text-white text-[11px] border-b border-slate-100 dark:border-white/10 pb-1 flex items-center gap-1.5">
                                <i class="ph-bold ph-activity text-cyan-500"></i> Actes Médicaux
                            </div>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_act_view" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Voir Nomenclature Actes</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_act_manage" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Gérer Tarifs Actes & Salles</span></label>
                        </div>

                        <!-- 7. Types de Consultation -->
                        <div class="space-y-1.5 bg-white dark:bg-[#121c15] p-3 rounded-2xl border border-slate-200/60 dark:border-white/10">
                            <div class="font-bold text-slate-800 dark:text-white text-[11px] border-b border-slate-100 dark:border-white/10 pb-1 flex items-center gap-1.5">
                                <i class="ph-bold ph-receipt text-indigo-500"></i> Types de Consultation
                            </div>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_ctype_view" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Voir Types de Consultation</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_ctype_manage" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Gérer Tarifs & Catégories</span></label>
                        </div>

                        <!-- 8. Modèles CR -->
                        <div class="space-y-1.5 bg-white dark:bg-[#121c15] p-3 rounded-2xl border border-slate-200/60 dark:border-white/10">
                            <div class="font-bold text-slate-800 dark:text-white text-[11px] border-b border-slate-100 dark:border-white/10 pb-1 flex items-center gap-1.5">
                                <i class="ph-bold ph-file-text text-orange-500"></i> Modèles Comptes Rendus
                            </div>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_cr_view" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Consulter Bibliothèque CR</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_cr_manage" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Gérer Modèles Word & HTML</span></label>
                        </div>

                        <!-- 9. Pharmacie & Médicaments -->
                        <div class="space-y-1.5 bg-white dark:bg-[#121c15] p-3 rounded-2xl border border-slate-200/60 dark:border-white/10">
                            <div class="font-bold text-slate-800 dark:text-white text-[11px] border-b border-slate-100 dark:border-white/10 pb-1 flex items-center gap-1.5">
                                <i class="ph-bold ph-pill text-pink-500"></i> Médicaments / Pharmacie
                            </div>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_med_view" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Consulter Base Médicaments</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_med_manage" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Gérer Produits & DCI</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_med_import_export" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Import / Export Excel</span></label>
                        </div>

                        <!-- 10. Médecins & Salles -->
                        <div class="space-y-1.5 bg-white dark:bg-[#121c15] p-3 rounded-2xl border border-slate-200/60 dark:border-white/10">
                            <div class="font-bold text-slate-800 dark:text-white text-[11px] border-b border-slate-100 dark:border-white/10 pb-1 flex items-center gap-1.5">
                                <i class="ph-bold ph-door text-teal-500"></i> Médecins & Salles
                            </div>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_doc_manage" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Gérer Praticiens & Spécialités</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_room_manage" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Gérer Salles de Soins</span></label>
                        </div>

                        <!-- 11. Paramètres Clinique -->
                        <div class="space-y-1.5 bg-white dark:bg-[#121c15] p-3 rounded-2xl border border-slate-200/60 dark:border-white/10">
                            <div class="font-bold text-slate-800 dark:text-white text-[11px] border-b border-slate-100 dark:border-white/10 pb-1 flex items-center gap-1.5">
                                <i class="ph-bold ph-buildings text-slate-600 dark:text-slate-300"></i> Paramètres Clinique
                            </div>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_clinic_settings" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Gérer Identité & Horaires</span></label>
                        </div>

                        <!-- 12. Administration Système -->
                        <div class="space-y-1.5 bg-white dark:bg-[#121c15] p-3 rounded-2xl border border-slate-200/60 dark:border-white/10">
                            <div class="font-bold text-slate-800 dark:text-white text-[11px] border-b border-slate-100 dark:border-white/10 pb-1 flex items-center gap-1.5">
                                <i class="ph-bold ph-calendar text-sky-500"></i> Rendez-vous
                            </div>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_rdv_view" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Consulter l'agenda</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_rdv_manage" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Créer, modifier et transférer</span></label>
                        </div>
                        <div class="space-y-1.5 bg-white dark:bg-[#121c15] p-3 rounded-2xl border border-emerald-200/70 dark:border-emerald-400/20">
                            <div class="font-bold text-emerald-800 dark:text-emerald-300 text-[11px] border-b border-emerald-100 dark:border-white/10 pb-1 flex items-center gap-1.5">
                                <i class="ph-bold ph-briefcase text-emerald-600"></i> Kacel Job
                            </div>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_kacel_job_view" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Consulter entreprises et conventions</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_kacel_job_manage_companies" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Gérer les entreprises</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_kacel_job_manage_conventions" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Gérer les conventions</span></label>
                        </div>
                        <!-- 13. Administration Système -->
                        <div class="space-y-1.5 bg-brand-neon/10 dark:bg-brand-neon/5 p-3 rounded-2xl border border-brand-neon/30">
                            <div class="font-bold text-emerald-950 dark:text-brand-neon text-[11px] border-b border-brand-neon/20 pb-1 flex items-center gap-1.5">
                                <i class="ph-bold ph-shield-check text-brand-neon"></i> Administration Système
                            </div>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_admin_users" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Créer / Gérer Utilisateurs</span></label>
                            <label class="flex items-center space-x-2 cursor-pointer"><input type="checkbox" id="perm_admin_perms" class="user-perm-check rounded text-brand-neon focus:ring-brand-neon"><span>Modifier les Permissions</span></label>
                        </div>

                    </div>
                    <br>
                    <br>
                <div class="space-y-2.5 bg-indigo-50/70 dark:bg-indigo-400/5 p-4 rounded-3xl border border-indigo-200/80 dark:border-indigo-300/10">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-indigo-200/70 dark:border-white/10 pb-2.5">
                        <div><h4 class="text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5"><i class="ph-bold ph-calendar-check text-indigo-500 text-base"></i> Plannings de rendez-vous autorisés</h4>
                        <p class="text-[11px] text-slate-500">Cochez les plannings que cet utilisateur peut consulter et gérer. Sans sélection, il n’a accès à aucun planning.</p></div>
                        <div class="flex items-center gap-2 text-[11px]"><button type="button" onclick="toggleAllRdvSchedules(true)" class="text-indigo-600 font-bold hover:underline">Tout cocher</button><span class="text-slate-400">|</span><button type="button" onclick="toggleAllRdvSchedules(false)" class="text-slate-500 font-bold hover:underline">Tout décocher</button></div>
                    </div>
                    <div id="userRdvSchedulesGrid" class="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-48 overflow-y-auto p-1"><span class="text-xs text-slate-400 italic">Chargement des plannings…</span></div>
                </div>
                      <!-- ⭐ 4. PÉRIMÈTRE MÉDICAL : DISCIPLINES & SPÉCIALITÉS AUTORISÉES (MULTI-SÉLECTION) ⭐ -->
                <div class="space-y-2.5 bg-slate-50 dark:bg-black/30 p-4 rounded-3xl border border-slate-200/80 dark:border-white/10">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-slate-200/60 dark:border-white/10 pb-2.5">
                        <div>
                            <h4 class="text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5">
                                <i class="ph-bold ph-stethoscope text-brand-neon text-base"></i>
                                <span>Périmètre Médical : Disciplines & Spécialités Autorisées</span>
                            </h4>
                            <p class="text-[11px] text-slate-400">Cochez les disciplines dont cet utilisateur a le droit de consulter les dossiers, actes et statistiques.</p>
                        </div>
                        <div class="flex items-center gap-2 text-[11px]">
                            <button type="button" onclick="toggleAllSpecialites(true)" class="text-emerald-500 font-bold hover:underline">Tout cocher</button>
                            <span class="text-slate-400">|</span>
                            <button type="button" onclick="toggleAllSpecialites(false)" class="text-slate-400 font-bold hover:underline">Tout décocher</button>
                        </div>
                    </div>

                    <!-- GRILLE DYNAMIQUE DES CASES À COCHER DES SPÉCIALITÉS -->
                    <div id="userSpecialitesGrid" class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-2 max-h-48 overflow-y-auto p-1">
                        <!-- Rempli automatiquement depuis ref_specialites -->
                    </div>
                </div>
                    
                </div>

                <!-- ACTIONS -->
                <div class="flex items-center justify-end space-x-3 pt-4 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closeUserModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler</button>
                    <button type="submit" class="px-6 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md shadow-brand-neon/20">Enregistrer les Droits & Collaborateur</button>
                </div>
            </form>
        </div>
    </div>

    <!-- SCRIPT JAVASCRIPT DU MODULE UTILISATEURS -->
    <script>
        (function() {
            let usersData = [];
            let availableSpecialites = [];
            let availableRdvSchedules = [];
            let filteredUsersList = [];
            let currentUserPage = 1;
            const USERS_PER_PAGE = 15;

            const USER_ROLE_PRESETS = {
                "Médecin": [
                    "dash_view", "reception_view", "pat_view", "pat_create", "pat_edit", 
                    "dme_view", "dme_consult", "dme_prescribe", "dme_demandes",
                    "act_view", "ctype_view", "cr_view", "med_view", "room_manage", "rdv_view", "rdv_manage"
                ],
                "Réceptionniste": [
                    "dash_view", "reception_view", "reception_create", "reception_edit",
                    "cash_collect", "cash_collect_rest", "pat_view", "pat_create", "pat_edit", "act_view", "ctype_view", "med_view", "rdv_view", "rdv_manage"
                ],
                "Caissier": [
                    "dash_view", "reception_view", "cash_collect", "cash_collect_rest", "cash_refund", "cash_remise", "cash_reports",
                    "pat_view", "act_view", "ctype_view"
                ],
                "Surveillant Médical": [
                    "dash_view", "reception_view", "pat_view", "dme_view", "act_view", "med_view", "room_manage", "rdv_view"
                ],
                "Direction": [
                    "dash_view", "dash_financial", "reception_view", "cash_reports", "cash_remise", "cash_refund",
                    "pat_view", "dme_view", "act_view", "ctype_view", "med_view", "doc_manage", "room_manage", "clinic_settings", "admin_users", "rdv_view"
                ],
                "Administrateur": [
                    "dash_view", "dash_financial", "reception_view", "reception_create", "reception_edit",
                    "cash_collect", "cash_collect_rest", "cash_refund", "cash_remise", "cash_reports", "pat_view", "pat_create", "pat_edit", "pat_delete",
                    "dme_view", "dme_consult", "dme_reset", "dme_prescribe", "dme_demandes",
                    "act_view", "act_manage", "ctype_view", "ctype_manage", "cr_view", "cr_manage",
                    "med_view", "med_manage", "med_import_export", "doc_manage", "room_manage", "clinic_settings",
                    "admin_users", "admin_perms", "rdv_view", "rdv_manage"
                ]
            };

            const roleBadges = {
                'Médecin': 'bg-emerald-50 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-300 border-emerald-200 dark:border-emerald-800/40',
                'Réceptionniste': 'bg-sky-50 dark:bg-sky-950/40 text-sky-700 dark:text-sky-300 border-sky-200 dark:border-sky-800/40',
                'Caissier': 'bg-amber-50 dark:bg-amber-950/40 text-amber-700 dark:text-amber-300 border-amber-200 dark:border-amber-800/40',
                'Surveillant Médical': 'bg-purple-50 dark:bg-purple-950/40 text-purple-700 dark:text-purple-300 border-purple-200 dark:border-purple-800/40',
                'Direction': 'bg-slate-800 text-white border-slate-700',
                'Administrateur': 'bg-brand-neon/20 text-emerald-900 dark:text-brand-neon border-brand-neon/30'
            };

            window.loadUsersModule = async function() {
                try {
                    const res = await fetch('/api/users');
                    const data = await res.json();
                    usersData = data.users || [];
                    availableSpecialites = data.available_specialites || [];
                    availableRdvSchedules = data.available_rdv_schedules || [];

                    document.getElementById('totalUsersCount').innerText = data.total_count || usersData.length;
                    renderSpecialitesCheckboxes();
                    renderRdvScheduleCheckboxes();
                    filterUsers();
                } catch(err) {
                    console.error("Erreur chargement utilisateurs:", err);
                }
            };

            // Rendu dynamique des cases à cocher des spécialités
            function renderSpecialitesCheckboxes() {
                const grid = document.getElementById('userSpecialitesGrid');
                if (!grid) return;
                grid.innerHTML = '';

                if (availableSpecialites.length === 0) {
                    grid.innerHTML = '<span class="text-xs text-slate-400 italic col-span-3 py-2">Aucune spécialité trouvée en base. Ajoutez-en dans Gestion Spécialités.</span>';
                    return;
                }

                availableSpecialites.forEach(s => {
                    const label = document.createElement('label');
                    label.className = "flex items-center gap-2.5 p-2 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-[#121c15] hover:bg-slate-50 dark:hover:bg-white/5 cursor-pointer transition";
                    label.innerHTML = `
                        <input type="checkbox" value="${s.nom}" class="user-specialite-check rounded text-brand-neon focus:ring-brand-neon">
                        <div class="truncate">
                            <span class="text-xs font-semibold text-slate-700 dark:text-slate-300 block truncate">${s.nom}</span>
                            ${s.code ? `<span class="text-[10px] text-slate-400 font-mono">${s.code}</span>` : ''}
                        </div>
                    `;
                    grid.appendChild(label);
                });
            }

            function renderRdvScheduleCheckboxes() {
                const grid = document.getElementById('userRdvSchedulesGrid');
                if (!grid) return;
                grid.innerHTML = '';
                if (!availableRdvSchedules.length) {
                    grid.innerHTML = '<span class="text-xs text-slate-400 italic col-span-2 py-2">Aucun planning enregistré. Créez d’abord un planning dans Rendez-vous.</span>';
                    return;
                }
                availableRdvSchedules.forEach(schedule => {
                    const days = (schedule.jours_semaine || [schedule.jour_semaine]).map(Number)
                        .map(day => ['Lun','Mar','Mer','Jeu','Ven','Sam','Dim'][day]).join(', ');
                    const label = document.createElement('label');
                    label.className = 'flex items-start gap-2.5 p-2.5 rounded-xl border border-indigo-100 dark:border-white/10 bg-white dark:bg-[#121c15] hover:bg-indigo-50/60 dark:hover:bg-white/5 cursor-pointer transition';
                    label.innerHTML = `<input type="checkbox" value="${Number(schedule.id)}" class="user-rdv-schedule-check mt-0.5 rounded text-brand-neon focus:ring-brand-neon"><span class="min-w-0"><b class="block truncate text-xs text-slate-700 dark:text-slate-200">${schedule.specialite}</b><small class="block text-[10px] text-slate-500">${days} · ${String(schedule.heure_debut).slice(0,5)}–${String(schedule.heure_fin).slice(0,5)}</small></span>`;
                    grid.appendChild(label);
                });
            }

            window.toggleAllRdvSchedules = function(check) {
                document.querySelectorAll('.user-rdv-schedule-check').forEach(cb => cb.checked = check);
            };

            window.toggleAllSpecialites = function(check) {
                document.querySelectorAll('.user-specialite-check').forEach(cb => cb.checked = check);
            };

            window.filterUsers = function() {
                const q = (document.getElementById('searchUserInput')?.value || '').toLowerCase().trim();
                if (!q) {
                    filteredUsersList = usersData;
                } else {
                    filteredUsersList = usersData.filter(u => 
                        (u.nom && u.nom.toLowerCase().includes(q)) || 
                        (u.prenom && u.prenom.toLowerCase().includes(q)) || 
                        (u.role && u.role.toLowerCase().includes(q)) || 
                        (u.telephone && u.telephone.includes(q)) || 
                        (u.login && u.login.toLowerCase().includes(q)) ||
                        (u.specialites_autorisees && u.specialites_autorisees.some(s => s.toLowerCase().includes(q)))
                    );
                }
                currentUserPage = 1;
                renderUsersTable();
            };

            window.renderUsersTable = function() {
                const tbody = document.getElementById('users-table-body');
                tbody.innerHTML = '';

                const totalItems = filteredUsersList.length;
                const totalPages = Math.ceil(totalItems / USERS_PER_PAGE) || 1;

                if (currentUserPage > totalPages) currentUserPage = totalPages;
                if (currentUserPage < 1) currentUserPage = 1;

                const startIndex = (currentUserPage - 1) * USERS_PER_PAGE;
                const endIndex = startIndex + USERS_PER_PAGE;
                const pageItems = filteredUsersList.slice(startIndex, endIndex);

                if (pageItems.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="8" class="text-center py-8 text-slate-400">Aucun utilisateur trouvé.</td></tr>';
                } else {
                    pageItems.forEach(u => {
                        const tr = document.createElement('tr');
                        tr.className = "hover:bg-slate-50/80 dark:hover:bg-white/[0.02] transition-colors";

                        const activePermsCount = Object.values(u.permissions || {}).filter(Boolean).length;
                        const statusBadge = u.status === 'Actif' 
                            ? `<span class="px-2.5 py-1 rounded-full text-[10px] font-bold bg-emerald-100 dark:bg-emerald-950/60 text-emerald-800 dark:text-emerald-300 border border-emerald-300 dark:border-emerald-800/40 cursor-pointer" title="Cliquer pour désactiver">🟢 Actif</span>`
                            : `<span class="px-2.5 py-1 rounded-full text-[10px] font-bold bg-rose-100 dark:bg-rose-950/60 text-rose-800 dark:text-rose-300 border border-rose-300 dark:border-rose-800/40 cursor-pointer" title="Cliquer pour activer">🔴 Inactif</span>`;

                        // Badge du périmètre médical
                        const specs = u.specialites_autorisees || [];
                        let specsBadge = '';
                        if (availableSpecialites.length > 0 && specs.length >= availableSpecialites.length) {
                            specsBadge = `<span class="px-2.5 py-1 rounded-xl text-[10px] font-bold bg-brand-neon/20 text-emerald-800 dark:text-brand-neon border border-brand-neon/30" title="Accès illimité à toutes les spécialités">🌐 Toutes les Spécialités</span>`;
                        } else if (specs.length === 0) {
                            specsBadge = `<span class="text-slate-400 text-[11px] italic">Aucune spécialité</span>`;
                        } else {
                            const tooltip = specs.join(', ');
                            specsBadge = `<span class="px-2.5 py-1 rounded-xl text-[10px] font-bold bg-sky-50 dark:bg-sky-950/40 text-sky-600 dark:text-sky-300 border border-sky-200 dark:border-sky-800/40 cursor-help" title="${tooltip}">🩺 ${specs.length} autorisée(s)</span>`;
                        }

                        tr.innerHTML = `
                            <td class="py-3.5 pl-2 flex items-center space-x-3">
                                <img src="${u.avatar || '/static/images/avatar-placeholder.svg?w=120&auto=format&fit=crop&q=80'}" class="w-9 h-9 rounded-xl object-cover shadow-sm border border-slate-200 dark:border-white/10">
                                <div>
                                    <div class="font-bold text-slate-800 dark:text-slate-100 text-xs">${u.prenom} ${u.nom}</div>
                                    <div class="text-[10px] text-slate-400 font-mono">ID: #${u.id}</div>
                                </div>
                            </td>
                            <td class="py-3.5"><span class="px-2.5 py-1 rounded-xl text-[10px] font-bold border ${roleBadges[u.role] || 'bg-slate-100'}">${u.role}</span></td>
                            <td class="py-3.5">${specsBadge}</td>
                            <td class="py-3.5 text-slate-600 dark:text-slate-300 font-medium">${u.telephone}</td>
                            <td class="py-3.5 text-slate-700 dark:text-slate-200 font-mono text-[11px] font-bold">${u.login}</td>
                            <td class="py-3.5" onclick="toggleUserStatus(${u.id}, '${u.status}')">${statusBadge}</td>
                            <td class="py-3.5 text-slate-600 dark:text-slate-400 font-semibold text-xs">
                                <span class="bg-slate-100 dark:bg-black/40 px-2 py-0.5 rounded-lg border border-slate-200 dark:border-white/10">${activePermsCount} droits</span>
                            </td>
                            <td class="py-3.5 pr-2 text-right">
                                <div class="inline-flex items-center gap-1.5">
                                    <button type="button" onclick="editUser(${u.id})" title="Modifier permissions & infos" class="p-1.5 text-sky-500 hover:bg-sky-50 dark:hover:bg-sky-500/10 rounded-lg transition"><i class="ph-bold ph-pencil-simple text-sm"></i></button>
                                    <button type="button" onclick="deleteUser(${u.id})" title="Supprimer utilisateur" class="p-1.5 text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-500/10 rounded-lg transition"><i class="ph-bold ph-trash text-sm"></i></button>
                                </div>
                            </td>
                        `;
                        tbody.appendChild(tr);
                    });
                }

                // GESTION DE LA BARRE DE PAGINATION
                const paginationWrapper = document.getElementById('userPaginationWrapper');
                const info = document.getElementById('userPaginationInfo');
                const controls = document.getElementById('userPaginationControls');

                if (totalItems > 0) {
                    paginationWrapper.classList.remove('hidden');
                    info.innerHTML = `Affichage de <strong>${startIndex + 1}</strong> à <strong>${Math.min(endIndex, totalItems)}</strong> sur <strong>${totalItems}</strong> collaborateurs`;

                    if (totalItems > USERS_PER_PAGE) {
                        let html = '';
                        const prevDisabled = currentUserPage <= 1 ? 'opacity-40 cursor-not-allowed' : 'hover:bg-slate-100 dark:hover:bg-white/10 cursor-pointer';
                        html += `<button onclick="setUserPage(${currentUserPage - 1})" ${currentUserPage <= 1 ? 'disabled' : ''} class="px-3 py-1 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-semibold ${prevDisabled}">◀ Précédent</button>`;

                        for (let i = 1; i <= totalPages; i++) {
                            if (i === 1 || i === totalPages || (i >= currentUserPage - 1 && i <= currentUserPage + 1)) {
                                const activeClass = i === currentUserPage 
                                    ? 'bg-brand-neon text-black font-bold shadow-sm' 
                                    : 'border border-slate-200 dark:border-white/10 hover:bg-slate-100 dark:hover:bg-white/5 text-slate-700 dark:text-slate-300';
                                html += `<button onclick="setUserPage(${i})" class="w-7 h-7 rounded-xl text-xs flex items-center justify-center transition ${activeClass}">${i}</button>`;
                            } else if (i === currentUserPage - 2 || i === currentUserPage + 2) {
                                html += `<span class="px-1 text-slate-400">...</span>`;
                            }
                        }

                        const nextDisabled = currentUserPage >= totalPages ? 'opacity-40 cursor-not-allowed' : 'hover:bg-slate-100 dark:hover:bg-white/10 cursor-pointer';
                        html += `<button onclick="setUserPage(${currentUserPage + 1})" ${currentUserPage >= totalPages ? 'disabled' : ''} class="px-3 py-1 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-semibold ${nextDisabled}">Suivant ▶</button>`;
                        controls.innerHTML = html;
                    } else {
                        controls.innerHTML = '';
                    }
                } else {
                    paginationWrapper.classList.add('hidden');
                }
            };

            window.setUserPage = function(p) {
                currentUserPage = p;
                renderUsersTable();
            };

            window.previewUserAvatar = function(event) {
                const file = event.target.files[0];
                if (file) {
                    const reader = new FileReader();
                    reader.onload = function(e) {
                        document.getElementById('avatarPreview').src = e.target.result;
                        document.getElementById('avatarData').value = e.target.result;
                    };
                    reader.readAsDataURL(file);
                }
            };

            window.openUserModal = function() {
                const form = document.getElementById('userForm');
                if (form) form.reset();

                document.getElementById('userId').value = '';
                document.getElementById('avatarData').value = '';
                document.getElementById('avatarPreview').src = '/static/images/avatar-placeholder.svg?w=120&auto=format&fit=crop&q=80';
                document.getElementById('modalTitle').innerText = 'Ajouter un Collaborateur';
                switchUserFormTab('general');

                toggleAllUserPerms(false);
                toggleAllSpecialites(false);
                toggleAllRdvSchedules(false);
                applyUserRolePreset('Médecin');

                document.getElementById('userModal').classList.remove('hidden');
            };

            window.closeUserModal = function() {
                document.getElementById('userModal').classList.add('hidden');
            };

            window.toggleAllUserPerms = function(check) {
                document.querySelectorAll('.user-perm-check').forEach(cb => cb.checked = check);
            };

            window.applyUserRolePreset = function(role) {
                toggleAllUserPerms(false);
                const activeList = USER_ROLE_PRESETS[role] || [];
                activeList.forEach(perm => {
                    const el = document.getElementById('perm_' + perm);
                    if (el) el.checked = true;
                });

                // Si Administrateur ou Direction, on autorise toutes les spécialités par défaut
                if (role === 'Administrateur' || role === 'Direction') {
                    toggleAllSpecialites(true);
                }
            };

            window.editUser = function(userId) {
                const u = usersData.find(x => x.id === userId);
                if (!u) return;

                openUserModal();

                document.getElementById('userId').value = u.id;
                document.getElementById('userPrenom').value = u.prenom || '';
                document.getElementById('userNom').value = u.nom || '';
                document.getElementById('userTelephone').value = u.telephone !== '-' ? u.telephone : '';
                document.getElementById('userRoleSelect').value = u.role || 'Médecin';
                document.getElementById('userLogin').value = u.login || '';
                document.getElementById('userPassword').value = '';
                document.getElementById('avatarData').value = u.avatar || '';
                document.getElementById('avatarPreview').src = u.avatar || '/static/images/avatar-placeholder.svg?w=120&auto=format&fit=crop&q=80';
                document.getElementById('userStatus').value = u.status || 'Actif';
                document.getElementById('modalTitle').innerText = 'Modifier les droits de ' + u.prenom + ' ' + u.nom;

                document.querySelectorAll('.user-dme-display-check').forEach(cb => { cb.checked = u.permissions?.[cb.id.replace('perm_', '')] !== false; });

                // Re-cochage des permissions
                toggleAllUserPerms(false);
                for (const [permKey, val] of Object.entries(u.permissions || {})) {
                    const el = document.getElementById('perm_' + permKey);
                    if (el) el.checked = Boolean(val);
                }
                if (!Object.prototype.hasOwnProperty.call(u.permissions || {}, 'cash_collect_rest')) {
                    const restPermission = document.getElementById('perm_cash_collect_rest');
                    if (restPermission) restPermission.checked = Boolean(u.permissions?.cash_collect);
                }
                if (!Object.prototype.hasOwnProperty.call(u.permissions || {}, 'cash_refund')) {
                    const refundPermission = document.getElementById('perm_cash_refund');
                    if (refundPermission) refundPermission.checked = Boolean(u.permissions?.cash_remise);
                }

                // Re-cochage des spécialités autorisées
                toggleAllSpecialites(false);
                const userSpecs = u.specialites_autorisees || [];
                document.querySelectorAll('.user-specialite-check').forEach(cb => {
                    cb.checked = userSpecs.includes(cb.value);
                });
                const userSchedules = Array.isArray(u.plannings_rdv_autorises)
                    ? u.plannings_rdv_autorises.map(Number)
                    : availableRdvSchedules.map(schedule => Number(schedule.id));
                document.querySelectorAll('.user-rdv-schedule-check').forEach(cb => {
                    cb.checked = userSchedules.includes(Number(cb.value));
                });
            };

            window.handleUserFormSubmit = async function(e) {
                e.preventDefault();

                // Collecte des permissions granulaires
                const perms = {};
                document.querySelectorAll('.user-perm-check, .user-dme-display-check').forEach(cb => {
                    const key = cb.id.replace('perm_', '');
                    perms[key] = cb.checked;
                });

                // Collecte des spécialités autorisées
                const selectedSpecialites = [];
                document.querySelectorAll('.user-specialite-check:checked').forEach(cb => {
                    selectedSpecialites.push(cb.value);
                });
                const selectedRdvSchedules = [...document.querySelectorAll('.user-rdv-schedule-check:checked')]
                    .map(cb => Number(cb.value));

                const userData = {
                    id: document.getElementById('userId').value ? parseInt(document.getElementById('userId').value) : null,
                    prenom: document.getElementById('userPrenom').value.trim(),
                    nom: document.getElementById('userNom').value.trim(),
                    telephone: document.getElementById('userTelephone').value.trim(),
                    role: document.getElementById('userRoleSelect').value,
                    login: document.getElementById('userLogin').value.trim(),
                    password: document.getElementById('userPassword').value.trim(),
                    avatar: document.getElementById('avatarData').value || document.getElementById('avatarPreview').src,
                    status: document.getElementById('userStatus').value,
                    permissions: perms,
                    specialites_autorisees: selectedSpecialites,
                    plannings_rdv_autorises: selectedRdvSchedules
                };

                const res = await fetch('/api/users/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(userData)
                }).then(r => r.json());

                if (res.success) {
                    closeUserModal();
                    await loadUsersModule();
                    alert(res.message);
                } else {
                    alert(res.message);
                }
            };

            window.toggleUserStatus = async function(id, currentStatus) {
                await fetch('/api/users/toggle-status', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id: id, status: currentStatus })
                });
                loadUsersModule();
            };

            window.deleteUser = async function(id) {
                if (confirm("Voulez-vous vraiment supprimer cet utilisateur ?")) {
                    const res = await fetch('/api/users/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id: id })
                    }).then(r => r.json());

                    if (res.success) {
                        loadUsersModule();
                    } else {
                        alert(res.message || "Erreur lors de la suppression.");
                    }
                }
            };

            // Chargement initial automatique
            loadUsersModule();
        })();
    </script>
    """


def render_html() -> str:
    from modules.dme_display import display_settings_html
    html = _render_html()
    content_start = html.index('<!-- 1. PHOTO DE PROFIL -->')
    planning_start = html.index('<div class="space-y-2.5 bg-indigo-50/70')
    medical_start = html.index('<!-- ⭐ 4. PÉRIMÈTRE MÉDICAL')
    actions_start = html.index('<!-- ACTIONS -->')
    general = html[content_start:planning_start].rstrip()
    # Ferme la matrice des autorisations avant les panneaux suivants.
    general = general.replace('<br>', '') + '</div>'
    planning = html[planning_start:medical_start]
    medical = html[medical_start:actions_start].rstrip()
    medical = medical.rsplit('</div>', 1)[0]
    tabs = [
        ('general', 'Autorisations générales et informations', general),
        ('dmi', 'Affichage du DMI par utilisateur', display_settings_html()),
        ('planning', 'Plannings de rendez-vous autorisés', planning),
        ('medical', 'Périmètre médical : disciplines et spécialités autorisées', medical),
    ]
    navigation = '<div role="tablist" aria-label="Paramétrage utilisateur" class="flex flex-wrap gap-2">'
    panels = ''
    for index, (key, label, content) in enumerate(tabs):
        selected = index == 0
        navigation += (
            f'<button type="button" role="tab" id="user-tab-{key}" '
            f'aria-controls="user-panel-{key}" aria-selected="{str(selected).lower()}" '
            f'onclick="switchUserFormTab(\'{key}\')" '
            'class="user-form-tab px-3 py-2.5 rounded-xl text-xs font-bold border border-slate-200 dark:border-white/10">'
            f'{index + 1}. {label}</button>'
        )
        panels += (
            f'<section role="tabpanel" id="user-panel-{key}" aria-labelledby="user-tab-{key}" '
            f'class="user-form-panel space-y-6{" hidden" if not selected else ""}">{content}</section>'
        )
    html = html[:content_start] + navigation + '</div>' + panels + html[actions_start:]
    script = '''
            window.switchUserFormTab = function(key) {
                document.querySelectorAll('.user-form-panel').forEach(panel => {
                    panel.classList.toggle('hidden', panel.id !== 'user-panel-' + key);
                });
                document.querySelectorAll('.user-form-tab').forEach(button => {
                    const selected = button.id === 'user-tab-' + key;
                    button.setAttribute('aria-selected', String(selected));
                    button.classList.toggle('bg-brand-neon', selected);
                    button.classList.toggle('text-black', selected);
                    button.classList.toggle('text-slate-500', !selected);
                });
            };
            document.getElementById('userForm').addEventListener('invalid', event => {
                const panel = event.target.closest('.user-form-panel');
                if (panel) switchUserFormTab(panel.id.replace('user-panel-', ''));
            }, true);
            switchUserFormTab('general');
    '''
    return html.replace('let usersData = [];', script + 'let usersData = [];', 1)
