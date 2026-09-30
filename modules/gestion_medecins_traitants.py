import json
import logging
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, HTTPException
from database import get_db_cursor
from modules.gestion_patients import ALGERIA_WILAYAS

logger = logging.getLogger("TreatingDoctorsModule")

treating_doctors_router = APIRouter(
    prefix="/api/treating-doctors",
    tags=["Médecins Traitants"]
)


# ============================================================================
# LOGIQUE MÉTIER POSTGRESQL
# ============================================================================
class TreatingDoctorManager:
    """Gestionnaire métier des médecins correspondants."""

    def get_all(self) -> List[Dict[str, Any]]:
        """Récupère l'ensemble des médecins traitants ordonnés alphabétiquement."""
        with get_db_cursor() as cursor:
            cursor.execute("""
                           SELECT id,
                                  grade,
                                  nom,
                                  prenom,
                                  telephone,
                                  email,
                                  adresse,
                                  wilaya,
                                  commune
                           FROM treating_doctors
                           ORDER BY nom ASC, prenom ASC, id DESC;
                           """)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_one(self, doctor_id: int) -> Optional[Dict[str, Any]]:
        """Récupère un médecin traitant par son identifiant unique."""
        with get_db_cursor() as cursor:
            cursor.execute("""
                           SELECT id,
                                  grade,
                                  nom,
                                  prenom,
                                  telephone,
                                  email,
                                  adresse,
                                  wilaya,
                                  commune
                           FROM treating_doctors
                           WHERE id = %s;
                           """, (doctor_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def save(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Crée ou met à jour la fiche d'un médecin traitant."""
        doc_id = data.get("id")
        grade = str(data.get("grade") or "Dr").strip()
        grade = grade if grade in ("Dr", "Pr") else "Dr"
        nom = str(data.get("nom") or "").strip().upper()
        prenom = str(data.get("prenom") or "").strip().title()
        telephone = str(data.get("telephone") or "").strip()
        email = str(data.get("email") or "").strip()
        adresse = str(data.get("adresse") or "").strip()
        wilaya = str(data.get("wilaya") or "").strip()
        commune = str(data.get("commune") or "").strip()

        if not nom or not prenom:
            return {"success": False, "message": "Le Nom et le Prénom sont obligatoires."}

        with get_db_cursor() as cursor:
            if doc_id:
                query = """
                        UPDATE treating_doctors
                        SET grade     = %s, \
                            nom       = %s, \
                            prenom    = %s, \
                            telephone = %s,
                            email     = %s, \
                            adresse   = %s, \
                            wilaya    = %s, \
                            commune   = %s
                        WHERE id = %s; \
                        """
                cursor.execute(query, (grade, nom, prenom, telephone, email, adresse, wilaya, commune, doc_id))
                saved_id = doc_id
                message = "Médecin traitant modifié avec succès."
            else:
                query = """
                        INSERT INTO treating_doctors
                            (grade, nom, prenom, telephone, email, adresse, wilaya, commune)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id; \
                        """
                cursor.execute(query, (grade, nom, prenom, telephone, email, adresse, wilaya, commune))
                res = cursor.fetchone()
                saved_id = res["id"] if res else None
                message = "Médecin traitant enregistré avec succès."

        return {
            "success": True,
            "message": message,
            "doctor": self.get_one(saved_id) if saved_id else None
        }

    def delete(self, doctor_id: int) -> Dict[str, Any]:
        """Supprime un médecin traitant."""
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM treating_doctors WHERE id = %s;", (doctor_id,))
        return {"success": True, "message": "Médecin traitant supprimé avec succès."}


manager = TreatingDoctorManager()


# ============================================================================
# POINTS D'ACCÈS FASTAPI (ROUTES JSON)
# ============================================================================
@treating_doctors_router.get("")
async def api_get_treating_doctors():
    try:
        doctors = manager.get_all()
        return {
            "success": True,
            "doctors": doctors,
            "total_count": len(doctors),
            "wilayas_data": ALGERIA_WILAYAS
        }
    except Exception as exc:
        logger.error("Erreur GET /api/treating-doctors : %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@treating_doctors_router.get("/{doctor_id}")
async def api_get_treating_doctor(doctor_id: int):
    doctor = manager.get_one(doctor_id)
    if not doctor:
        raise HTTPException(status_code=404, detail="Médecin traitant introuvable.")
    return {"success": True, "doctor": doctor}


@treating_doctors_router.post("/save")
async def api_save_treating_doctor(payload: Dict[str, Any]):
    return manager.save(payload)


@treating_doctors_router.post("/delete")
async def api_delete_treating_doctor(payload: Dict[str, Any]):
    doc_id = payload.get("id")
    if not doc_id:
        raise HTTPException(status_code=400, detail="Identifiant du médecin manquant.")
    return manager.delete(int(doc_id))


# ============================================================================
# RENDU DU COMPOSANT HTML ET DU SCRIPT INTERACTIF
# ============================================================================
def render_html() -> str:
    """Génère l'HTML complet avec Dark Mode, Modale Wilaya/Commune et Pagination stricte de 15."""
    wilayas_options = "".join([f'<option value="{w}">{w}</option>' for w in sorted(ALGERIA_WILAYAS.keys())])
    wilayas_json = json.dumps(ALGERIA_WILAYAS)

    return f"""
    <div id="view-treating-doctors" class="space-y-6">

        <!-- BANDEAU D'EN-TÊTE -->
        <div class="banner-glow rounded-3xl p-6 lg:p-8 text-white shadow-xl flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
            <div>
                <h1 class="text-2xl font-extrabold tracking-tight mb-1">Médecins Traitants & Correspondants</h1>
                <p class="text-xs text-slate-300">Répertoire des médecins traitants externes, confrères adresseurs et correspondants des patients.</p>
            </div>

            <div class="flex items-center gap-3">
                <div class="bg-white/10 px-4 py-2 rounded-2xl backdrop-blur-sm border border-white/10 text-xs">
                    <span class="text-slate-300">Total Référencés:</span> 
                    <strong id="treatingDoctorsCount" class="text-brand-neon font-black text-sm ml-1">0</strong>
                </div>

                <button onclick="openTreatingDoctorModal()" class="flex items-center gap-2 bg-brand-neon hover:bg-brand-neonDark text-black px-5 py-2.5 rounded-2xl font-bold text-xs shadow-md shadow-brand-neon/20 transition">
                    <i class="ph-bold ph-user-plus text-base"></i>
                    <span>Nouveau Médecin Traitant</span>
                </button>
            </div>
        </div>

        <!-- BARRE DE RECHERCHE -->
        <div class="flex items-center bg-white dark:bg-[#121c15] p-3.5 rounded-3xl shadow-sm border border-slate-100 dark:border-white/5 transition-colors">
            <div class="flex items-center gap-2.5 bg-slate-50 dark:bg-black/30 rounded-2xl px-4 py-2 border border-slate-200/80 dark:border-white/10 w-full md:w-96">
                <i class="ph ph-magnifying-glass text-slate-400 text-base"></i>
                <input type="text" id="treatingDoctorSearch" oninput="filterTreatingDoctors()" placeholder="Rechercher par nom, téléphone, wilaya, commune..." class="w-full text-xs outline-none bg-transparent text-slate-700 dark:text-slate-200">
            </div>
            <button onclick="loadTreatingDoctorsModule()" class="ml-2 p-2 rounded-xl border border-slate-200 dark:border-white/10 text-slate-500 hover:text-brand-neon transition" title="Actualiser">
                <i class="ph-bold ph-arrows-clockwise text-base"></i>
            </button>
        </div>

        <!-- TABLEAU DES MÉDECINS TRAITANTS -->
        <div class="bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 flex flex-col transition-colors">
            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                    <thead>
                        <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-3">
                            <th class="pb-3 pl-2">Médecin Traitant</th>
                            <th class="pb-3">Téléphone</th>
                            <th class="pb-3">Courriel</th>
                            <th class="pb-3">Adresse / Cabinet</th>
                            <th class="pb-3">Wilaya</th>
                            <th class="pb-3">Commune</th>
                            <th class="pb-3 pr-2 text-right">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="treatingDoctorsTableBody" class="divide-y divide-slate-100 dark:divide-white/5 text-slate-700 dark:text-slate-300 font-medium"></tbody>
                </table>
            </div>

            <!-- PAGINATION STRICTE PAR 15 LIGNES -->
            <div id="treatingPaginationWrapper" class="flex flex-col sm:flex-row items-center justify-between gap-3 pt-5 mt-3 border-t border-slate-100 dark:border-white/5 text-xs">
                <span id="treatingPaginationInfo" class="text-slate-500 dark:text-slate-400 font-medium">Affichage des résultats...</span>
                <div class="flex items-center gap-1.5" id="treatingPaginationControls"></div>
            </div>
        </div>

    </div>

    <!-- ================= MODALE MÉDECIN TRAITANT ================= -->
    <div id="treatingDoctorModal" class="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center hidden z-50 p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-2xl max-h-[92vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50/50 dark:bg-black/20">
                <div class="flex items-center space-x-2">
                    <div class="w-8 h-8 rounded-xl bg-brand-neon/20 text-brand-neon flex items-center justify-center font-bold text-sm">
                        <i class="ph-bold ph-stethoscope"></i>
                    </div>
                    <h3 id="treatingDoctorModalTitle" class="text-base font-bold text-slate-800 dark:text-white">Nouveau Médecin Traitant</h3>
                </div>
                <button type="button" onclick="closeTreatingDoctorModal()" class="text-slate-400 hover:text-white p-1 rounded-lg">
                    <i class="ph-bold ph-x text-lg"></i>
                </button>
            </div>

            <form id="treatingDoctorForm" onsubmit="saveTreatingDoctor(event)" class="p-6 overflow-y-auto space-y-4">
                <input type="hidden" id="treatingDoctorId">

                <div class="grid grid-cols-1 sm:grid-cols-4 gap-3">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Grade *</label>
                        <select id="treatingDoctorGrade" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white">
                            <option value="Dr">Dr</option>
                            <option value="Pr">Pr</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Nom *</label>
                        <input id="treatingDoctorNom" required placeholder="Ex: BENMANSOUR" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-bold uppercase text-slate-800 dark:text-white">
                    </div>
                    <div class="sm:col-span-2">
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Prénom *</label>
                        <input id="treatingDoctorPrenom" required placeholder="Ex: Amine" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-medium text-slate-800 dark:text-white">
                    </div>
                </div>

                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Téléphone Direct</label>
                        <input id="treatingDoctorTelephone" placeholder="0550 12 34 56" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Adresse Courriel</label>
                        <input type="email" id="treatingDoctorEmail" placeholder="dr.benmansour@cabinet.dz" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                </div>

                <div>
                    <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Adresse du Cabinet / Établissement</label>
                    <textarea id="treatingDoctorAdresse" rows="2" placeholder="Ex: 14 Rue Didouche Mourad, Cabinet Médical..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white"></textarea>
                </div>

                <!-- SÉLECTION DYNAMIQUE WILAYA ET COMMUNE (IDENTIQUE AU MODULE PATIENT) -->
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Wilaya (58 Wilayas) *</label>
                        <select id="treatingDoctorWilaya" onchange="handleTreatingWilayaChange()" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-bold">
                            {wilayas_options}
                        </select>
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Commune *</label>
                        <select id="treatingDoctorCommune" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium">
                        </select>
                    </div>
                </div>

                <div class="flex items-center justify-end space-x-3 pt-4 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closeTreatingDoctorModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler</button>
                    <button type="submit" class="px-6 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md shadow-brand-neon/20">Enregistrer</button>
                </div>
            </form>
        </div>
    </div>

    <!-- SCRIPT JAVASCRIPT DU MODULE MÉDECINS TRAITANTS -->
    <script>
        (function() {{
            const ALGERIA_WILAYAS_DATA = {wilayas_json};
            let allTreatingList = [];
            let filteredTreatingList = [];
            let currentTreatingPage = 1;
            const TREATING_PER_PAGE = 15;

            window.loadTreatingDoctorsModule = async function() {{
                try {{
                    const res = await fetch('/api/treating-doctors');
                    const data = await res.json();
                    allTreatingList = data.doctors || [];
                    document.getElementById('treatingDoctorsCount').innerText = data.total_count || allTreatingList.length;
                    filterTreatingDoctors();
                }} catch (e) {{
                    console.error("Erreur de chargement des médecins traitants:", e);
                }}
            }};

            window.filterTreatingDoctors = function() {{
                const q = (document.getElementById('treatingDoctorSearch')?.value || '').toLowerCase().trim();
                if (!q) {{
                    filteredTreatingList = allTreatingList;
                }} else {{
                    filteredTreatingList = allTreatingList.filter(d => 
                        (d.nom && d.nom.toLowerCase().includes(q)) ||
                        (d.prenom && d.prenom.toLowerCase().includes(q)) ||
                        (d.telephone && d.telephone.includes(q)) ||
                        (d.email && d.email.toLowerCase().includes(q)) ||
                        (d.wilaya && d.wilaya.toLowerCase().includes(q)) ||
                        (d.commune && d.commune.toLowerCase().includes(q)) ||
                        String(d.id).includes(q)
                    );
                }}
                currentTreatingPage = 1;
                renderTreatingDoctorsTable();
            }};

            window.renderTreatingDoctorsTable = function() {{
                const tbody = document.getElementById('treatingDoctorsTableBody');
                tbody.innerHTML = '';

                const totalItems = filteredTreatingList.length;
                const totalPages = Math.ceil(totalItems / TREATING_PER_PAGE) || 1;

                if (currentTreatingPage > totalPages) currentTreatingPage = totalPages;
                if (currentTreatingPage < 1) currentTreatingPage = 1;

                const startIndex = (currentTreatingPage - 1) * TREATING_PER_PAGE;
                const endIndex = startIndex + TREATING_PER_PAGE;
                const pageItems = filteredTreatingList.slice(startIndex, endIndex);

                if (pageItems.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="7" class="text-center py-8 text-slate-400">Aucun médecin traitant trouvé.</td></tr>';
                }} else {{
                    pageItems.forEach(d => {{
                        const tr = document.createElement('tr');
                        tr.className = "hover:bg-slate-50/80 dark:hover:bg-white/[0.02] transition-colors";
                        tr.innerHTML = `
                            <td class="py-3.5 pl-2">
                                <div class="font-bold text-slate-800 dark:text-slate-100 text-xs">${{d.grade || 'Dr'}} ${{d.nom}} ${{d.prenom}}</div>
                                <div class="text-[10px] text-slate-400 font-mono">ID: #${{d.id}}</div>
                            </td>
                            <td class="py-3.5 text-slate-600 dark:text-slate-300 font-medium">${{d.telephone || '-'}}</td>
                            <td class="py-3.5 text-slate-600 dark:text-slate-400 font-mono text-[11px]">${{d.email || '-'}}</td>
                            <td class="py-3.5 text-slate-600 dark:text-slate-400 truncate max-w-[180px]">${{d.adresse || '-'}}</td>
                            <td class="py-3.5">
                                <span class="px-2.5 py-1 rounded-xl text-[10px] font-bold bg-sky-50 dark:bg-sky-950/40 text-sky-600 dark:text-sky-300 border border-sky-200 dark:border-sky-800/40">${{d.wilaya || '-'}}</span>
                            </td>
                            <td class="py-3.5">
                                <span class="px-2.5 py-1 rounded-xl text-[10px] font-bold bg-brand-neon/20 text-emerald-800 dark:text-brand-neon border border-brand-neon/30">${{d.commune || '-'}}</span>
                            </td>
                            <td class="py-3.5 pr-2 text-right">
                                <div class="inline-flex items-center gap-1.5">
                                    <button onclick="editTreatingDoctor(${{d.id}})" class="p-1.5 text-sky-500 hover:bg-sky-50 dark:hover:bg-sky-500/10 rounded-lg"><i class="ph-bold ph-pencil-simple text-sm"></i></button>
                                    <button onclick="deleteTreatingDoctor(${{d.id}})" class="p-1.5 text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-500/10 rounded-lg"><i class="ph-bold ph-trash text-sm"></i></button>
                                </div>
                            </td>
                        `;
                        tbody.appendChild(tr);
                    }});
                }}

                // GESTION DE LA BARRE DE PAGINATION
                const paginationWrapper = document.getElementById('treatingPaginationWrapper');
                const info = document.getElementById('treatingPaginationInfo');
                const controls = document.getElementById('treatingPaginationControls');

                if (totalItems > 0) {{
                    paginationWrapper.classList.remove('hidden');
                    info.innerHTML = `Affichage de <strong>${{startIndex + 1}}</strong> à <strong>${{Math.min(endIndex, totalItems)}}</strong> sur <strong>${{totalItems}}</strong> médecins`;

                    if (totalItems > TREATING_PER_PAGE) {{
                        let html = '';
                        const prevDisabled = currentTreatingPage <= 1 ? 'opacity-40 cursor-not-allowed' : 'hover:bg-slate-100 dark:hover:bg-white/10 cursor-pointer';
                        html += `<button onclick="setTreatingPage(${{currentTreatingPage - 1}})" ${{currentTreatingPage <= 1 ? 'disabled' : ''}} class="px-3 py-1 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-semibold ${{prevDisabled}}">◀ Précédent</button>`;

                        for (let i = 1; i <= totalPages; i++) {{
                            if (i === 1 || i === totalPages || (i >= currentTreatingPage - 1 && i <= currentTreatingPage + 1)) {{
                                const activeClass = i === currentTreatingPage 
                                    ? 'bg-brand-neon text-black font-bold shadow-sm' 
                                    : 'border border-slate-200 dark:border-white/10 hover:bg-slate-100 dark:hover:bg-white/5 text-slate-700 dark:text-slate-300';
                                html += `<button onclick="setTreatingPage(${{i}})" class="w-7 h-7 rounded-xl text-xs flex items-center justify-center transition ${{activeClass}}">${{i}}</button>`;
                            }} else if (i === currentTreatingPage - 2 || i === currentTreatingPage + 2) {{
                                html += `<span class="px-1 text-slate-400">...</span>`;
                            }}
                        }}

                        const nextDisabled = currentTreatingPage >= totalPages ? 'opacity-40 cursor-not-allowed' : 'hover:bg-slate-100 dark:hover:bg-white/10 cursor-pointer';
                        html += `<button onclick="setTreatingPage(${{currentTreatingPage + 1}})" ${{currentTreatingPage >= totalPages ? 'disabled' : ''}} class="px-3 py-1 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-semibold ${{nextDisabled}}">Suivant ▶</button>`;
                        controls.innerHTML = html;
                    }} else {{
                        controls.innerHTML = '';
                    }}
                }} else {{
                    paginationWrapper.classList.add('hidden');
                }}
            }};

            window.setTreatingPage = function(p) {{
                currentTreatingPage = p;
                renderTreatingDoctorsTable();
            }};

            // INTERACTION DYNAMIQUE WILAYA ➔ COMMUNE (IDENTIQUE AU MODULE PATIENT)
            window.handleTreatingWilayaChange = function() {{
                const wilayaSelect = document.getElementById('treatingDoctorWilaya');
                const communeSelect = document.getElementById('treatingDoctorCommune');
                if (!wilayaSelect || !communeSelect) return;

                const selected = wilayaSelect.value || "16 - Alger";
                const communes = ALGERIA_WILAYAS_DATA[selected] || ["Centre-Ville"];

                communeSelect.innerHTML = communes.map(c => `<option value="${{c}}">${{c}}</option>`).join('');
            }};

            window.openTreatingDoctorModal = function(d) {{
                document.getElementById('treatingDoctorForm').reset();
                document.getElementById('treatingDoctorId').value = d?.id || '';
                document.getElementById('treatingDoctorGrade').value = d?.grade || 'Dr';
                document.getElementById('treatingDoctorNom').value = d?.nom || '';
                document.getElementById('treatingDoctorPrenom').value = d?.prenom || '';
                document.getElementById('treatingDoctorTelephone').value = d?.telephone || '';
                document.getElementById('treatingDoctorEmail').value = d?.email || '';
                document.getElementById('treatingDoctorAdresse').value = d?.adresse || '';

                document.getElementById('treatingDoctorWilaya').value = d?.wilaya || '16 - Alger';
                handleTreatingWilayaChange();
                if (d?.commune) {{
                    document.getElementById('treatingDoctorCommune').value = d.commune;
                }}

                document.getElementById('treatingDoctorModalTitle').innerText = d?.id ? 'Modifier le Médecin Traitant' : 'Nouveau Médecin Traitant';
                document.getElementById('treatingDoctorModal').classList.remove('hidden');
            }};

            window.closeTreatingDoctorModal = function() {{
                document.getElementById('treatingDoctorModal').classList.add('hidden');
            }};

            window.editTreatingDoctor = async function(id) {{
                try {{
                    const res = await fetch(`/api/treating-doctors/${{id}}`);
                    const data = await res.json();
                    if (!res.ok || !data.success) throw new Error(data.message || 'Médecin introuvable.');
                    openTreatingDoctorModal(data.doctor);
                }} catch (e) {{
                    alert("Erreur de récupération : " + e.message);
                }}
            }};

            window.saveTreatingDoctor = async function(e) {{
                e.preventDefault();
                const payload = {{
                    id: document.getElementById('treatingDoctorId').value ? parseInt(document.getElementById('treatingDoctorId').value) : null,
                    grade: document.getElementById('treatingDoctorGrade').value,
                    nom: document.getElementById('treatingDoctorNom').value,
                    prenom: document.getElementById('treatingDoctorPrenom').value,
                    telephone: document.getElementById('treatingDoctorTelephone').value,
                    email: document.getElementById('treatingDoctorEmail').value,
                    adresse: document.getElementById('treatingDoctorAdresse').value,
                    wilaya: document.getElementById('treatingDoctorWilaya').value,
                    commune: document.getElementById('treatingDoctorCommune').value
                }};

                try {{
                    const res = await fetch('/api/treating-doctors/save', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify(payload)
                    }});
                    const data = await res.json();
                    if (!res.ok || !data.success) throw new Error(data.message || 'Erreur lors de la sauvegarde.');
                    closeTreatingDoctorModal();
                    await loadTreatingDoctorsModule();
                }} catch (err) {{
                    alert("Erreur : " + err.message);
                }}
            }};

            window.deleteTreatingDoctor = async function(id) {{
                const d = allTreatingList.find(x => x.id === id);
                const label = d ? `${{d.grade || 'Dr'}} ${{d.nom}} ${{d.prenom}}` : 'ce médecin';
                if (!confirm(`Voulez-vous supprimer ${{label}} ?`)) return;

                try {{
                    const res = await fetch('/api/treating-doctors/delete', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ id }})
                    }});
                    const data = await res.json();
                    if (!res.ok || !data.success) throw new Error(data.message || 'Suppression impossible.');
                    await loadTreatingDoctorsModule();
                }} catch (e) {{
                    alert("Erreur : " + e.message);
                }}
            }};

            loadTreatingDoctorsModule();
        }})();
    </script>
    """