
import json
import logging
from typing import Dict, Any, List

from fastapi import APIRouter, HTTPException
from database import get_db_cursor

logger = logging.getLogger("RoomsModule")

rooms_router = APIRouter(prefix="/api/rooms", tags=["Salles de Soin"])

TYPES_SALLES: List[str] = [
    "Consultation",
    "Salle De Soin",
    "Bloc Opératoire",
    "Salle De Réveil",
    "Salle De Déchocage",
    "Salle De Prélévemnt",
    "Salle radiologique",
]

ETAGES_CLINIQUE: List[str] = [
    "Rez-de-chaussée",
    "1er Étage",
    "2ème Étage",
    "3ème Étage",
    "4ème Étage",
    "Sous-sol -1",
    "Sous-sol -2",
]


# ============================================================================
# LOGIQUE MÉTIER POSTGRESQL
# ============================================================================
class RoomManager:
    """Gestionnaire métier des salles d'examen et blocs opératoires."""

    def get_all(self) -> List[Dict[str, Any]]:
        """Récupère l'ensemble des salles ordonnées par ID."""
        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT id, designation, etage, telephone, types, specialite "
                "FROM rooms ORDER BY id ASC;"
            )
            rows = cursor.fetchall()

        rooms = []
        for r in rows:
            types_data = r["types"]
            if isinstance(types_data, str):
                try:
                    types_list = json.loads(types_data)
                except Exception:
                    types_list = [t.strip() for t in types_data.split(",") if t.strip()]
            else:
                types_list = types_data or []

            rooms.append({
                "id": r["id"],
                "designation": r["designation"],
                "etage": r["etage"] or "-",
                "telephone": r["telephone"] or "-",
                "types": types_list,
                "specialite": r["specialite"] or "Polyvalente / Toutes Spécialités",
            })
        return rooms

    def count(self) -> int:
        """Retourne le nombre total de salles configurées."""
        with get_db_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total FROM rooms;")
            row = cursor.fetchone()
            return row["total"] if row else 0

    def get_specialites(self) -> List[str]:
        """Récupère les spécialités enregistrées pour alimenter la sélection."""
        with get_db_cursor() as cursor:
            cursor.execute("SELECT nom FROM ref_specialites ORDER BY nom ASC;")
            rows = cursor.fetchall()
            return [r["nom"] for r in rows]

    def get_room_types(self) -> List[Dict[str, Any]]:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT id, nom FROM room_types ORDER BY LOWER(nom), nom;")
            return [dict(row) for row in cursor.fetchall()]

    def add_room_type(self, name: str) -> Dict[str, Any]:
        name = " ".join(str(name or "").split())
        if not name:
            return {"success": False, "message": "Saisissez le nom du type de salle."}
        if len(name) > 80:
            return {"success": False, "message": "Le nom du type ne peut pas dépasser 80 caractères."}
        with get_db_cursor() as cursor:
            cursor.execute("""
                INSERT INTO room_types (nom) VALUES (%s)
                ON CONFLICT DO NOTHING
                RETURNING id, nom;
            """, (name,))
            created = cursor.fetchone()
            if not created:
                return {"success": False, "message": "Ce type de salle existe déjà."}
        return {"success": True, "message": "Type de salle ajouté.", "type": dict(created)}

    def delete_room_type(self, type_id: int) -> Dict[str, Any]:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT nom FROM room_types WHERE id = %s;", (type_id,))
            room_type = cursor.fetchone()
            if not room_type:
                return {"success": False, "message": "Type de salle introuvable."}
            cursor.execute("SELECT 1 FROM rooms WHERE types @> %s::jsonb LIMIT 1;", (json.dumps([room_type["nom"]]),))
            if cursor.fetchone():
                return {"success": False, "message": "Ce type est utilisé par au moins une salle. Retirez-le d’abord des salles concernées."}
            cursor.execute("DELETE FROM room_types WHERE id = %s;", (type_id,))
        return {"success": True, "message": "Type de salle supprimé."}

    def save(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Ajoute ou modifie une salle."""
        room_id = data.get("id")
        designation = data.get("designation", "").strip()
        etage = data.get("etage", "").strip()
        telephone = data.get("telephone", "").strip()
        types_list = data.get("types", [])
        specialite = data.get("specialite", "").strip()

        if not designation or not specialite or not types_list:
            return {
                "success": False,
                "message": "La Désignation, la Spécialité et au moins un Type d'espace sont requis.",
            }

        available_types = {item["nom"] for item in self.get_room_types()}
        types_list = list(dict.fromkeys(str(item).strip() for item in types_list if str(item).strip()))
        if not types_list or any(item not in available_types for item in types_list):
            return {"success": False, "message": "Sélectionnez uniquement des types de salle enregistrés."}

        types_json = json.dumps(types_list)

        with get_db_cursor() as cursor:
            if room_id:
                query = """
                    UPDATE rooms 
                    SET designation = %s, etage = %s, telephone = %s, types = %s::jsonb, specialite = %s
                    WHERE id = %s;
                """
                cursor.execute(query, (designation, etage, telephone, types_json, specialite, room_id))
            else:
                query = """
                    INSERT INTO rooms (designation, etage, telephone, types, specialite)
                    VALUES (%s, %s, %s, %s::jsonb, %s);
                """
                cursor.execute(query, (designation, etage, telephone, types_json, specialite))

        return {"success": True, "message": "Salle enregistrée avec succès !"}

    def delete(self, room_id: int) -> Dict[str, Any]:
        """Supprime une salle par son ID."""
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM rooms WHERE id = %s;", (room_id,))
        return {"success": True, "message": "Salle supprimée."}


room_manager = RoomManager()


# ============================================================================
# POINTS D'ACCÈS FASTAPI (ROUTES JSON)
# ============================================================================
@rooms_router.get("")
async def api_get_rooms():
    try:
        return {
            "rooms": room_manager.get_all(),
            "total_count": room_manager.count(),
            "specialites": room_manager.get_specialites(),
            "room_types": room_manager.get_room_types(),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@rooms_router.post("/save")
async def api_save_room(payload: Dict[str, Any]):
    return room_manager.save(payload)


@rooms_router.post("/delete")
async def api_delete_room(payload: Dict[str, Any]):
    return room_manager.delete(payload.get("id"))


@rooms_router.post("/types/save")
async def api_add_room_type(payload: Dict[str, Any]):
    return room_manager.add_room_type(payload.get("nom", ""))


@rooms_router.post("/types/delete")
async def api_delete_room_type(payload: Dict[str, Any]):
    return room_manager.delete_room_type(payload.get("id"))


# ============================================================================
# RENDU DU COMPOSANT HTML ET JAVASCRIPT
# ============================================================================
def render_html() -> str:
    """Génère l'HTML complet de gestion des salles avec Dark Mode et Modale."""
    etages_options = "".join([f'<option value="{e}">{e}</option>' for e in ETAGES_CLINIQUE])

    types_checkboxes = "".join([
        f"""
        <label class="flex items-center gap-2.5 p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 hover:bg-brand-neon/10 cursor-pointer transition">
            <input type="checkbox" value="{t}" class="room-type-check rounded text-brand-neon focus:ring-brand-neon">
            <span class="text-xs font-semibold text-slate-700 dark:text-slate-300">{t}</span>
        </label>
        """ for t in TYPES_SALLES
    ])

    return f"""
    <div id="view-rooms" class="space-y-6">
        
        <!-- BANDEAU D'EN-TÊTE -->
        <div class="banner-glow rounded-3xl p-6 lg:p-8 text-white shadow-xl flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
            <div>
                <h1 class="text-2xl font-extrabold tracking-tight mb-1">Salles de Consultation & Espaces de Soins</h1>
                <p class="text-xs text-slate-300">Répertoire des cabinets de consultation, blocs opératoires, salles de soins et plateaux techniques.</p>
            </div>
            <div class="flex flex-wrap items-center gap-3">
                <div class="bg-white/10 px-4 py-2 rounded-2xl backdrop-blur-sm border border-white/10 text-xs">
                    <span class="text-slate-300">Total Espaces:</span> 
                    <strong id="totalRoomsCount" class="text-brand-neon font-black text-sm ml-1">0</strong>
                </div>
                <button type="button" onclick="openRoomTypesModal()" class="flex items-center gap-2 bg-white/10 hover:bg-white/20 text-white px-4 py-2.5 rounded-2xl font-bold text-xs border border-white/15 transition">
                    <i class="ph-bold ph-tag text-base"></i><span>Types de salle</span>
                </button>
                <button onclick="openRoomModal()" class="flex items-center gap-2 bg-brand-neon hover:bg-brand-neonDark text-black px-5 py-2.5 rounded-2xl font-bold text-xs shadow-md shadow-brand-neon/20 transition">
                    <i class="ph-bold ph-plus-circle text-base"></i>
                    <span>Nouvelle Salle</span>
                </button>
            </div>
        </div>

        <!-- BARRE DE RECHERCHE -->
        <div class="flex items-center bg-white dark:bg-[#121c15] p-3.5 rounded-3xl shadow-sm border border-slate-100 dark:border-white/5 transition-colors">
            <div class="flex items-center gap-2.5 bg-slate-50 dark:bg-black/30 rounded-2xl px-4 py-2 border border-slate-200/80 dark:border-white/10 w-full sm:w-96">
                <i class="ph ph-magnifying-glass text-slate-400 text-base"></i>
                <input type="text" id="searchRoomInput" oninput="filterRooms()" placeholder="Rechercher par salle, étage, spécialité, poste..." class="w-full text-xs outline-none bg-transparent text-slate-700 dark:text-slate-200">
            </div>
        </div>

        <!-- TABLEAU DES SALLES -->
        <div class="bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 overflow-x-auto transition-colors">
            <table class="w-full text-left text-xs">
                <thead>
                    <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-3">
                        <th class="pb-3 pl-2">ID</th>
                        <th class="pb-3">Désignation de la Salle</th>
                        <th class="pb-3">Étage / Emplacement</th>
                        <th class="pb-3">Téléphone / Poste</th>
                        <th class="pb-3">Spécialité Affectée</th>
                        <th class="pb-3">Types / Affectations (Multi)</th>
                        <th class="pb-3 pr-2 text-right">Actions</th>
                    </tr>
                </thead>
                <tbody id="rooms-table-body" class="divide-y divide-slate-100 dark:divide-white/5 text-slate-700 dark:text-slate-300 font-medium"></tbody>
            </table>
        </div>

    </div>

    <!-- ================= MODAL SALLE (AJOUT / MODIFICATION) ================= -->
    <div id="roomModal" class="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center hidden z-50 p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-2xl max-h-[92vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50/50 dark:bg-black/20">
                <div class="flex items-center space-x-2">
                    <div class="w-8 h-8 rounded-xl bg-brand-neon/20 text-brand-neon flex items-center justify-center font-bold text-sm">
                        <i class="ph-bold ph-door"></i>
                    </div>
                    <h3 id="roomModalTitle" class="text-base font-bold text-slate-800 dark:text-white">Ajouter une Salle de Consultation</h3>
                </div>
                <button onclick="closeRoomModal()" class="text-slate-400 hover:text-white p-1 rounded-lg">
                    <i class="ph-bold ph-x text-lg"></i>
                </button>
            </div>

            <form id="roomForm" onsubmit="handleRoomSubmit(event)" class="p-6 overflow-y-auto space-y-4">
                <input type="hidden" id="roomId">

                <div>
                    <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Désignation de la Salle / Espace *</label>
                    <input type="text" id="roomDesignation" required placeholder="Ex: Cabinet 101, Bloc A, Salle de Déchocage..." class="w-full text-xs p-3 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white">
                </div>

                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Étage / Emplacement *</label>
                        <select id="roomEtage" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium">
                            {etages_options}
                        </select>
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Numéro de Téléphone / Poste</label>
                        <input type="text" id="roomTel" placeholder="Ex: Poste 101 / 0550..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                </div>

                <div>
                    <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Spécialité Médicale Affectée *</label>
                    <select id="roomSpecialite" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-semibold"></select>
                </div>

                <div>
                    <div class="flex items-center justify-between mb-2">
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300">Type(s) d'espace (Multi-sélection autorisée) *</label>
                        <span class="text-[10px] text-slate-400">Cochez une ou plusieurs options</span>
                    </div>
                    <div id="roomTypeCheckboxes" class="grid grid-cols-2 gap-2">
                        {types_checkboxes}
                    </div>
                </div>

                <div class="flex items-center justify-end space-x-3 pt-4 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closeRoomModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler</button>
                    <button type="submit" class="px-6 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md shadow-brand-neon/20">Enregistrer la Salle</button>
                </div>
            </form>
        </div>
    </div>

    <div id="roomTypesModal" class="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center hidden z-[60] p-4" onclick="if(event.target===this)closeRoomTypesModal()">
        <section role="dialog" aria-modal="true" aria-labelledby="roomTypesModalTitle" class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-xl max-h-[88vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <header class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex items-center justify-between bg-slate-50/70 dark:bg-black/20">
                <div class="flex items-center gap-3"><div class="w-10 h-10 rounded-2xl bg-brand-neon/20 text-emerald-700 dark:text-brand-neon flex items-center justify-center"><i class="ph-bold ph-tag text-lg"></i></div><div><h2 id="roomTypesModalTitle" class="text-base font-extrabold text-slate-800 dark:text-white">Types de salle</h2><p class="text-[11px] text-slate-500">Ajoutez les types disponibles dans les fiches de salle.</p></div></div>
                <button type="button" onclick="closeRoomTypesModal()" aria-label="Fermer" class="p-2 rounded-xl text-slate-500 hover:bg-slate-200 dark:hover:bg-white/10"><i class="ph-bold ph-x text-lg"></i></button>
            </header>
            <div class="p-5 space-y-4 overflow-y-auto">
                <form onsubmit="handleRoomTypeSubmit(event)" class="flex flex-col sm:flex-row gap-2">
                    <input id="newRoomTypeName" type="text" maxlength="80" required placeholder="Ex. Salle de kinésithérapie" class="flex-1 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-xs font-semibold text-slate-800 outline-none focus:border-emerald-500 dark:border-white/10 dark:bg-black/30 dark:text-white">
                    <button type="submit" class="inline-flex items-center justify-center gap-2 rounded-xl bg-brand-neon px-4 py-2.5 text-xs font-extrabold text-black hover:bg-brand-neonDark"><i class="ph-bold ph-plus"></i>Ajouter le type</button>
                </form>
                <div><h3 class="mb-2 text-[10px] font-extrabold uppercase tracking-wider text-slate-500">Types disponibles</h3><div id="roomTypesList" class="space-y-2"><p class="rounded-xl bg-slate-50 p-3 text-xs text-slate-500 dark:bg-white/5">Chargement…</p></div></div>
                <p class="text-[10px] leading-5 text-slate-500">Un type déjà utilisé par une salle ne peut pas être supprimé. Retirez-le d’abord de cette salle.</p>
            </div>
        </section>
    </div>

    <!-- SCRIPT JAVASCRIPT DU MODULE -->
    <script>
        (function() {{
            let roomsData = [];
            let availableSpecialites = [];
            let roomTypesData = [];

            const escapeRoomHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[character]));

            const typeBadgesColors = {{
                'Consultation': 'bg-sky-50 dark:bg-sky-950/40 text-sky-600 dark:text-sky-300 border-sky-200 dark:border-sky-800/40',
                'Salle De Soin': 'bg-brand-neon/20 text-emerald-800 dark:text-brand-neon border-brand-neon/30',
                'Bloc Opératoire': 'bg-rose-50 dark:bg-rose-950/40 text-rose-600 dark:text-rose-300 border-rose-200 dark:border-rose-800/40',
                'Salle De Réveil': 'bg-amber-50 dark:bg-amber-950/40 text-amber-600 dark:text-amber-300 border-amber-200 dark:border-amber-800/40',
                'Salle De Déchocage': 'bg-purple-50 dark:bg-purple-950/40 text-purple-600 dark:text-purple-300 border-purple-200 dark:border-purple-800/40',
                'Salle De Prélévemnt': 'bg-cyan-50 dark:bg-cyan-950/40 text-cyan-600 dark:text-cyan-300 border-cyan-200 dark:border-cyan-800/40',
                'Salle radiologique': 'bg-indigo-50 dark:bg-indigo-950/40 text-indigo-600 dark:text-indigo-300 border-indigo-200 dark:border-indigo-800/40'
            }};

            window.loadRoomsModule = async function() {{
                try {{
                    const res = await fetch('/api/rooms');
                    const data = await res.json();
                    roomsData = data.rooms || [];
                    availableSpecialites = data.specialites || [];
                    roomTypesData = data.room_types || [];
                    const checkedTypes = [...document.querySelectorAll('.room-type-check:checked')].map(input => input.value);
                    populateRoomTypeCheckboxes(checkedTypes);

                    document.getElementById('totalRoomsCount').innerText = data.total_count || roomsData.length;
                    renderRoomsTable(roomsData);
                    populateSpecialiteSelect();
                    renderRoomTypesList();
                }} catch (e) {{
                    console.error("Erreur de chargement des salles:", e);
                }}
            }};

            window.populateRoomTypeCheckboxes = function(selected = []) {{
                const container = document.getElementById('roomTypeCheckboxes');
                if (!container) return;
                if (!roomTypesData.length) {{
                    container.innerHTML = '<p class="col-span-2 rounded-xl bg-amber-50 p-3 text-xs text-amber-800">Aucun type de salle n’est configuré. Ajoutez-en un avec le bouton « Types de salle ».</p>';
                    return;
                }}
                container.innerHTML = roomTypesData.map(item => `
                    <label class="flex items-center gap-2.5 p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 hover:bg-brand-neon/10 cursor-pointer transition">
                        <input type="checkbox" value="${{escapeRoomHtml(item.nom)}}" class="room-type-check rounded text-brand-neon focus:ring-brand-neon" ${{selected.includes(item.nom) ? 'checked' : ''}}>
                        <span class="text-xs font-semibold text-slate-700 dark:text-slate-300">${{escapeRoomHtml(item.nom)}}</span>
                    </label>`).join('');
            }};

            window.renderRoomTypesList = function() {{
                const container = document.getElementById('roomTypesList');
                if (!container) return;
                container.innerHTML = roomTypesData.length ? roomTypesData.map(item => `
                    <div class="flex items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white px-3 py-2.5 dark:border-white/10 dark:bg-black/20">
                        <span class="flex min-w-0 items-center gap-2 text-xs font-bold text-slate-700 dark:text-slate-200"><i class="ph-bold ph-tag text-emerald-600"></i><span class="truncate">${{escapeRoomHtml(item.nom)}}</span></span>
                        <button type="button" onclick="deleteRoomType(${{Number(item.id)}})" title="Supprimer si le type n’est utilisé par aucune salle" class="shrink-0 rounded-lg p-2 text-rose-600 hover:bg-rose-50 dark:hover:bg-rose-500/10"><i class="ph-bold ph-trash"></i></button>
                    </div>`).join('') : '<p class="rounded-xl bg-slate-50 p-3 text-xs text-slate-500 dark:bg-white/5">Aucun type configuré.</p>';
            }};

            window.openRoomTypesModal = function() {{
                renderRoomTypesList();
                const modal = document.getElementById('roomTypesModal');
                modal.classList.remove('hidden');
                modal.classList.add('flex');
                document.getElementById('newRoomTypeName')?.focus();
            }};

            window.closeRoomTypesModal = function() {{
                const modal = document.getElementById('roomTypesModal');
                modal.classList.add('hidden');
                modal.classList.remove('flex');
            }};

            window.handleRoomTypeSubmit = async function(event) {{
                event.preventDefault();
                const input = document.getElementById('newRoomTypeName');
                const response = await fetch('/api/rooms/types/save', {{
                    method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify({{nom: input.value}})
                }});
                const result = await response.json();
                if (!result.success) {{ alert(result.message || 'Impossible d’ajouter ce type.'); return; }}
                input.value = '';
                await loadRoomsModule();
                input.focus();
            }};

            window.deleteRoomType = async function(id) {{
                const item = roomTypesData.find(type => Number(type.id) === Number(id));
                if (!item || !confirm(`Supprimer le type « ${{item.nom}} » ?`)) return;
                const response = await fetch('/api/rooms/types/delete', {{
                    method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify({{id}})
                }});
                const result = await response.json();
                if (!result.success) {{ alert(result.message || 'Impossible de supprimer ce type.'); return; }}
                await loadRoomsModule();
            }};

            window.populateSpecialiteSelect = function() {{
                const select = document.getElementById('roomSpecialite');
                let options = '<option value="Polyvalente / Toutes Spécialités">Polyvalente / Toutes Spécialités</option>';
                availableSpecialites.forEach(s => {{
                    options += `<option value="${{s}}">${{s}}</option>`;
                }});
                select.innerHTML = options;
            }};

            window.renderRoomsTable = function(rooms) {{
                const tbody = document.getElementById('rooms-table-body');
                tbody.innerHTML = '';

                if (!rooms || rooms.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="7" class="text-center py-6 text-slate-400">Aucune salle enregistrée.</td></tr>';
                    return;
                }}

                rooms.forEach(r => {{
                    const tr = document.createElement('tr');
                    tr.className = "hover:bg-slate-50/80 dark:hover:bg-white/[0.02] transition-colors";

                    const typesBadges = (r.types || []).map(t => 
                        `<span class="px-2 py-0.5 rounded-lg text-[10px] font-bold border ${{typeBadgesColors[t] || 'bg-slate-100 dark:bg-black/30 text-slate-700 dark:text-slate-300 border-slate-200'}}">${{t}}</span>`
                    ).join(' ');

                    tr.innerHTML = `
                        <td class="py-3.5 pl-2 text-slate-400 font-mono text-[11px]">#${{r.id}}</td>
                        <td class="py-3.5">
                            <div class="font-bold text-slate-800 dark:text-slate-100 text-xs">${{r.designation}}</div>
                        </td>
                        <td class="py-3.5 text-slate-600 dark:text-slate-300 font-medium">${{r.etage}}</td>
                        <td class="py-3.5 text-slate-600 dark:text-slate-400 font-mono text-[11px]">${{r.telephone}}</td>
                        <td class="py-3.5">
                            <span class="px-2.5 py-1 rounded-xl text-[10px] font-bold bg-brand-neon/20 text-emerald-800 dark:text-brand-neon border border-brand-neon/30">${{r.specialite}}</span>
                        </td>
                        <td class="py-3.5 flex flex-wrap gap-1 items-center max-w-xs">
                            ${{typesBadges || '-'}}
                        </td>
                        <td class="py-3.5 pr-2 text-right">
                            <div class="inline-flex items-center gap-1.5">
                                <button onclick="editRoom(${{r.id}})" class="p-1.5 text-sky-500 hover:bg-sky-50 dark:hover:bg-sky-500/10 rounded-lg"><i class="ph-bold ph-pencil-simple text-sm"></i></button>
                                <button onclick="deleteRoom(${{r.id}})" class="p-1.5 text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-500/10 rounded-lg"><i class="ph-bold ph-trash text-sm"></i></button>
                            </div>
                        </td>
                    `;
                    tbody.appendChild(tr);
                }});
            }};

            window.filterRooms = function() {{
                const q = (document.getElementById('searchRoomInput').value || '').toLowerCase().trim();
                const filtered = roomsData.filter(r => 
                    r.designation.toLowerCase().includes(q) || 
                    r.etage.toLowerCase().includes(q) || 
                    r.specialite.toLowerCase().includes(q) ||
                    (r.telephone && r.telephone.includes(q)) ||
                    (r.types && r.types.some(t => t.toLowerCase().includes(q)))
                );
                renderRoomsTable(filtered);
            }};

            window.openRoomModal = function() {{
                document.getElementById('roomForm').reset();
                document.getElementById('roomId').value = '';
                document.querySelectorAll('.room-type-check').forEach(cb => cb.checked = false);
                document.getElementById('roomModalTitle').innerText = 'Ajouter une Salle de Consultation';
                document.getElementById('roomModal').classList.remove('hidden');
            }};

            window.closeRoomModal = function() {{
                document.getElementById('roomModal').classList.add('hidden');
            }};

            window.editRoom = function(id) {{
                const r = roomsData.find(x => x.id === id);
                if (!r) return;

                document.getElementById('roomId').value = r.id;
                document.getElementById('roomDesignation').value = r.designation;
                document.getElementById('roomEtage').value = r.etage !== '-' ? r.etage : 'Rez-de-chaussée';
                document.getElementById('roomTel').value = r.telephone !== '-' ? r.telephone : '';
                document.getElementById('roomSpecialite').value = r.specialite;

                document.querySelectorAll('.room-type-check').forEach(cb => {{
                    cb.checked = (r.types || []).includes(cb.value);
                }});

                document.getElementById('roomModalTitle').innerText = 'Modifier : ' + r.designation;
                document.getElementById('roomModal').classList.remove('hidden');
            }};

            window.handleRoomSubmit = async function(e) {{
                e.preventDefault();
                const selectedTypes = [];
                document.querySelectorAll('.room-type-check:checked').forEach(cb => {{
                    selectedTypes.push(cb.value);
                }});

                if (selectedTypes.length === 0) {{
                    alert("Veuillez cocher au moins un Type d'espace.");
                    return;
                }}

                const data = {{
                    id: document.getElementById('roomId').value ? parseInt(document.getElementById('roomId').value) : null,
                    designation: document.getElementById('roomDesignation').value,
                    etage: document.getElementById('roomEtage').value,
                    telephone: document.getElementById('roomTel').value,
                    specialite: document.getElementById('roomSpecialite').value,
                    types: selectedTypes
                }};

                const res = await fetch('/api/rooms/save', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(data)
                }}).then(r => r.json());

                if (res.success) {{
                    closeRoomModal();
                    loadRoomsModule();
                }} else {{
                    alert(res.message);
                }}
            }};

            window.deleteRoom = async function(id) {{
                if (confirm("Voulez-vous supprimer cette salle de consultation ?")) {{
                    await fetch('/api/rooms/delete', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ id }})
                    }});
                    loadRoomsModule();
                }}
            }};

            // Montage initial automatique
            loadRoomsModule();
        }})();
    </script>
    """
