
import base64
import csv
from datetime import datetime
import io
import logging
import os
from pathlib import Path
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from database import get_db_cursor

try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False

logger = logging.getLogger("MedicationsModule")

medications_router = APIRouter(prefix="/api/medications", tags=["Médicaments"])


# ============================================================================
# LOGIQUE MÉTIER POSTGRESQL
# ============================================================================
class MedicationManager:
    """Gestionnaire métier de la pharmacopée et des substances actives."""

    def get_all(self, search: str = "") -> List[Dict[str, Any]]:
        """Récupère tous les médicaments avec recherche optionnelle."""
        with get_db_cursor() as cursor:
            if search:
                pattern = f"%{search.strip().lower()}%"
                query = """
                    SELECT id, nom_commercial, dci, forme, dosage 
                    FROM medications 
                    WHERE LOWER(nom_commercial) LIKE %s 
                       OR LOWER(dci) LIKE %s 
                       OR LOWER(forme) LIKE %s 
                       OR LOWER(dosage) LIKE %s
                    ORDER BY nom_commercial ASC;
                """
                cursor.execute(query, (pattern, pattern, pattern, pattern))
            else:
                cursor.execute("""
                    SELECT id, nom_commercial, dci, forme, dosage 
                    FROM medications 
                    ORDER BY nom_commercial ASC;
                """)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def count(self) -> int:
        """Retourne le nombre total de médicaments enregistrés."""
        with get_db_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total FROM medications;")
            row = cursor.fetchone()
            return row["total"] if row else 0

    def save(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Ajoute ou modifie un médicament."""
        med_id = data.get("id")
        nom_commercial = data.get("nom_commercial", "").strip().upper()
        dci = data.get("dci", "").strip()
        forme = data.get("forme", "").strip()
        dosage = data.get("dosage", "").strip()

        if not nom_commercial or not dci:
            return {
                "success": False,
                "message": "Le Nom Commercial et la DCI sont obligatoires.",
            }

        with get_db_cursor() as cursor:
            if med_id:
                query = """
                    UPDATE medications 
                    SET nom_commercial = %s, dci = %s, forme = %s, dosage = %s 
                    WHERE id = %s;
                """
                cursor.execute(query, (nom_commercial, dci, forme, dosage, med_id))
            else:
                query = """
                    INSERT INTO medications (nom_commercial, dci, forme, dosage)
                    VALUES (%s, %s, %s, %s);
                """
                cursor.execute(query, (nom_commercial, dci, forme, dosage))

        return {"success": True, "message": "Médicament enregistré avec succès !"}

    def delete(self, med_id: int) -> Dict[str, Any]:
        """Supprime un médicament."""
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM medications WHERE id = %s;", (med_id,))
        return {"success": True, "message": "Médicament supprimé."}

    def import_file(self, base64_content: str, filename: str) -> Dict[str, Any]:
        """Importe des médicaments par lot depuis un fichier CSV ou Excel."""
        try:
            if not base64_content:
                return {"success": False, "message": "Aucun fichier reçu."}

            if "," in base64_content:
                base64_content = base64_content.split(",")[1]

            file_bytes = base64.b64decode(base64_content)
            records_to_insert: List[tuple] = []
            filename_lower = filename.lower()

            if filename_lower.endswith(".csv"):
                decoded_str = file_bytes.decode("utf-8-sig", errors="ignore")
                delimiter = ";" if ";" in decoded_str.splitlines()[0] else ","
                reader = csv.reader(io.StringIO(decoded_str), delimiter=delimiter)
                rows = list(reader)

                if not rows:
                    return {"success": False, "message": "Le fichier CSV est vide."}

                # Ignorer l'en-tête s'il existe
                start_index = 1 if len(rows) > 1 and ("nom" in rows[0][0].lower() or "desg" in rows[0][0].lower()) else 0
                for row in rows[start_index:]:
                    if len(row) >= 2:
                        nom = row[0].strip().upper()
                        dci = row[1].strip()
                        forme = row[2].strip() if len(row) > 2 else ""
                        dosage = row[3].strip() if len(row) > 3 else ""
                        if nom and dci:
                            records_to_insert.append((nom, dci, forme, dosage))

            elif filename_lower.endswith((".xlsx", ".xls")):
                if not PANDAS_AVAILABLE:
                    return {
                        "success": False,
                        "message": "Le module 'pandas' est requis pour les fichiers Excel. Exécutez : pip install pandas openpyxl",
                    }

                df = pd.read_excel(io.BytesIO(file_bytes))
                df.columns = [str(col).strip().lower() for col in df.columns]

                # Détection intelligente des colonnes
                col_nom = next((c for c in df.columns if any(k in c for k in ["nom", "desg", "commercial", "medicament"])), df.columns[0] if len(df.columns) > 0 else None)
                col_dci = next((c for c in df.columns if any(k in c for k in ["dci", "substance", "molecule", "principe"])), df.columns[1] if len(df.columns) > 1 else None)
                col_forme = next((c for c in df.columns if any(k in c for k in ["forme", "type", "presentation"])), df.columns[2] if len(df.columns) > 2 else None)
                col_dosage = next((c for c in df.columns if any(k in c for k in ["dosage", "dose", "puissance"])), df.columns[3] if len(df.columns) > 3 else None)

                for _, row in df.iterrows():
                    nom = str(row[col_nom]).strip().upper() if col_nom and pd.notna(row[col_nom]) else ""
                    dci = str(row[col_dci]).strip() if col_dci and pd.notna(row[col_dci]) else ""
                    forme = str(row[col_forme]).strip() if col_forme and pd.notna(row[col_forme]) else ""
                    dosage = str(row[col_dosage]).strip() if col_dosage and pd.notna(row[col_dosage]) else ""

                    if nom and dci and nom not in ["NAN", "NONE", ""]:
                        records_to_insert.append((nom, dci, forme, dosage))
            else:
                return {
                    "success": False,
                    "message": "Format non supporté. Veuillez importer un fichier .CSV ou .XLSX.",
                }

            if not records_to_insert:
                return {"success": False, "message": "Aucun médicament valide trouvé dans le fichier."}

            with get_db_cursor() as cursor:
                query = """
                    INSERT INTO medications (nom_commercial, dci, forme, dosage)
                    VALUES (%s, %s, %s, %s);
                """
                cursor.executemany(query, records_to_insert)

            return {
                "success": True,
                "count": len(records_to_insert),
                "message": f"Succès : {len(records_to_insert)} médicaments ont été importés dans PostgreSQL !",
            }
        except Exception as exc:
            logger.error("Erreur d'importation : %s", exc, exc_info=True)
            return {"success": False, "message": f"Erreur d'importation : {str(exc)}"}

    def export_csv(self) -> str:
        """Génère un export CSV standard encodé en UTF-8."""
        meds = self.get_all()
        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["ID", "Nom Commercial", "DCI (Substance Active)", "Forme", "Dosage"])

        for m in meds:
            writer.writerow([m["id"], m["nom_commercial"], m["dci"], m.get("forme", ""), m.get("dosage", "")])

        return output.getvalue()


med_manager = MedicationManager()


# ============================================================================
# POINTS D'ACCÈS FASTAPI (ROUTES JSON & TÉLÉCHARGEMENT)
# ============================================================================
@medications_router.get("")
async def api_get_medications(q: Optional[str] = Query(default="")):
    try:
        meds = med_manager.get_all(search=q)
        total = med_manager.count()
        return {"medications": meds, "total_count": total}
    except Exception as exc:
        logger.error("Erreur API Médicaments : %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@medications_router.post("/save")
async def api_save_medication(payload: Dict[str, Any]):
    return med_manager.save(payload)


@medications_router.post("/delete")
async def api_delete_medication(payload: Dict[str, Any]):
    return med_manager.delete(payload.get("id"))


@medications_router.post("/import")
async def api_import_medications(payload: Dict[str, Any]):
    return med_manager.import_file(payload.get("file_data", ""), payload.get("filename", ""))


@medications_router.get("/export/csv")
async def api_export_csv():
    try:
        csv_data = med_manager.export_csv()
        filename = f"medicaments_cliniva_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        return Response(
            content=csv_data.encode("utf-8-sig"),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@medications_router.get("/export/direct")
async def api_export_direct():
    """Écrit le fichier CSV dans Téléchargements et l'ouvre automatiquement dans Windows."""
    try:
        csv_data = med_manager.export_csv()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"medicaments_cliniva_{timestamp}.csv"

        downloads_dir = Path.home() / "Downloads"
        if not downloads_dir.exists():
            downloads_dir = Path.home() / "Desktop"

        target_filepath = downloads_dir / filename
        with open(target_filepath, "w", encoding="utf-8-sig") as f:
            f.write(csv_data)

        try:
            os.startfile(str(target_filepath))
        except Exception:
            pass

        return {
            "success": True,
            "filename": filename,
            "path": str(target_filepath),
            "message": f"Fichier généré et ouvert dans : {target_filepath}"
        }
    except Exception as exc:
        return {"success": False, "message": str(exc)}


# ============================================================================
# RENDU DU COMPOSANT HTML ET DU SCRIPT INTERACTIF
# ============================================================================
def render_html() -> str:
    """Génère l'HTML complet avec Dark Mode, Modale et Pagination stricte de 15."""
    return """
    <div id="view-medications" class="space-y-6">
        
        <!-- BANDEAU D'EN-TÊTE -->
        <div class="banner-glow rounded-3xl p-6 lg:p-8 text-white shadow-xl flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
            <div>
                <h1 class="text-2xl font-extrabold tracking-tight mb-1">Base Médicaments & DCI</h1>
                <p class="text-xs text-slate-300">Répertoire officiel des substances actives, formes galéniques, dosages et dénominations commerciales.</p>
            </div>
            
            <div class="flex items-center gap-3">
                <div class="bg-white/10 px-4 py-2 rounded-2xl backdrop-blur-sm border border-white/10 text-xs">
                    <span class="text-slate-300">Total Référencé:</span> 
                    <strong id="medsCounterBadge" class="text-brand-neon font-black text-sm ml-1">0</strong>
                </div>

                <button onclick="openMedicationModal()" class="flex items-center gap-2 bg-brand-neon hover:bg-brand-neonDark text-black px-5 py-2.5 rounded-2xl font-bold text-xs shadow-md shadow-brand-neon/20 transition">
                    <i class="ph-bold ph-plus-circle text-base"></i>
                    <span>Nouveau Médicament</span>
                </button>
            </div>
        </div>

        <!-- BARRE D'OUTILS : RECHERCHE, IMPORT & EXPORT -->
        <div class="flex flex-col md:flex-row items-center justify-between gap-3 bg-white dark:bg-[#121c15] p-3.5 rounded-3xl shadow-sm border border-slate-100 dark:border-white/5 transition-colors">
            <div class="flex items-center gap-2.5 bg-slate-50 dark:bg-black/30 rounded-2xl px-4 py-2 border border-slate-200/80 dark:border-white/10 w-full md:w-96">
                <i class="ph ph-magnifying-glass text-slate-400 text-base"></i>
                <input type="text" id="medSearchInput" oninput="searchMedications()" placeholder="Rechercher par nom, DCI, forme, dosage..." class="w-full text-xs outline-none bg-transparent text-slate-700 dark:text-slate-200">
            </div>

            <div class="flex flex-wrap items-center gap-2.5 w-full md:w-auto justify-end">
                <!-- Bouton Export Direct -->
                <button onclick="exportMedicationsDirect()" class="flex items-center gap-1.5 px-3.5 py-2 bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-600 dark:text-emerald-400 border border-emerald-500/20 rounded-xl text-xs font-bold transition shadow-sm" title="Exporter et ouvrir dans Excel">
                    <i class="ph-bold ph-file-csv text-base"></i>
                    <span>Exporter CSV</span>
                </button>

                <!-- Bouton Import Excel/CSV -->
                <label for="excelUploadInput" class="cursor-pointer flex items-center gap-1.5 px-3.5 py-2 bg-slate-100 dark:bg-white/5 hover:bg-slate-200 dark:hover:bg-white/10 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10 rounded-xl text-xs font-bold transition shadow-sm">
                    <i class="ph-bold ph-upload-simple text-base text-sky-500"></i>
                    <span id="importBtnLabel">Importer Excel / CSV</span>
                </label>
                <input type="file" id="excelUploadInput" accept=".xlsx,.xls,.csv" class="hidden" onchange="handleMedicationImport(event)">
            </div>
        </div>

        <!-- TABLEAU DES MÉDICAMENTS -->
        <div class="bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 flex flex-col transition-colors">
            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                    <thead>
                        <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-3">
                            <th class="pb-3 pl-2">ID</th>
                            <th class="pb-3">Nom Commercial (Désignation)</th>
                            <th class="pb-3">DCI (Substance Active)</th>
                            <th class="pb-3">Forme Galénique</th>
                            <th class="pb-3">Dosage</th>
                            <th class="pb-3 pr-2 text-right">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="medications-table-body" class="divide-y divide-slate-100 dark:divide-white/5 text-slate-700 dark:text-slate-300 font-medium"></tbody>
                </table>
            </div>

            <!-- PAGINATION STRICTE PAR 15 -->
            <div id="medPaginationWrapper" class="flex flex-col sm:flex-row items-center justify-between gap-3 pt-5 mt-3 border-t border-slate-100 dark:border-white/5 text-xs">
                <span id="paginationInfo" class="text-slate-500 dark:text-slate-400 font-medium">Affichage des résultats...</span>
                <div class="flex items-center gap-1.5" id="paginationControls"></div>
            </div>
        </div>

    </div>

    <!-- ================= MODALE MÉDICAMENT (AJOUT / MODIF / COPIE) ================= -->
    <div id="medModal" class="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center hidden z-50 p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-lg shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50/50 dark:bg-black/20">
                <div class="flex items-center space-x-2">
                    <div class="w-8 h-8 rounded-xl bg-brand-neon/20 text-brand-neon flex items-center justify-center font-bold text-sm">
                        <i class="ph-bold ph-pill"></i>
                    </div>
                    <h3 id="medModalTitle" class="text-base font-bold text-slate-800 dark:text-white">Ajouter un Médicament</h3>
                </div>
                <button onclick="closeMedModal()" class="text-slate-400 hover:text-white p-1 rounded-lg">
                    <i class="ph-bold ph-x text-lg"></i>
                </button>
            </div>

            <form id="medForm" onsubmit="handleMedSubmit(event)" class="p-6 space-y-4">
                <input type="hidden" id="medId">

                <div>
                    <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Nom Commercial (Désignation) *</label>
                    <input type="text" id="medNom" required placeholder="Ex: DOLIPRANE, AUGMENTIN..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-bold uppercase text-slate-800 dark:text-white">
                </div>

                <div>
                    <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">DCI (Substance Active) *</label>
                    <input type="text" id="medDci" required placeholder="Ex: Paracétamol, Amoxicilline..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-medium text-slate-800 dark:text-white">
                </div>

                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Forme</label>
                        <input type="text" id="medForme" placeholder="Ex: Comprimé, Sirop, Gélule..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Dosage</label>
                        <input type="text" id="medDosage" placeholder="Ex: 1000mg, 500mg/5ml..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                </div>

                <div class="flex items-center justify-end space-x-3 pt-4 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closeMedModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler</button>
                    <button type="submit" class="px-6 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md shadow-brand-neon/20">Enregistrer</button>
                </div>
            </form>
        </div>
    </div>

    <!-- SCRIPT JAVASCRIPT DU MODULE MÉDICAMENTS -->
    <script>
        (function() {
            let allMedsList = [];
            let filteredMedsList = [];
            let currentMedPage = 1;
            const MEDS_PER_PAGE = 15;

            window.loadMedicationsModule = async function() {
                try {
                    const res = await fetch('/api/medications');
                    const data = await res.json();
                    allMedsList = data.medications || [];
                    document.getElementById('medsCounterBadge').innerText = data.total_count || allMedsList.length;
                    searchMedications();
                } catch (e) {
                    console.error("Erreur de chargement des médicaments :", e);
                }
            };

            window.searchMedications = function() {
                const query = (document.getElementById('medSearchInput').value || '').toLowerCase().trim();
                if (!query) {
                    filteredMedsList = allMedsList;
                } else {
                    filteredMedsList = allMedsList.filter(m => 
                        (m.nom_commercial && m.nom_commercial.toLowerCase().includes(query)) ||
                        (m.dci && m.dci.toLowerCase().includes(query)) ||
                        (m.forme && m.forme.toLowerCase().includes(query)) ||
                        (m.dosage && m.dosage.toLowerCase().includes(query))
                    );
                }
                currentMedPage = 1;
                renderMedicationsTable();
            };

            window.renderMedicationsTable = function() {
                const tbody = document.getElementById('medications-table-body');
                tbody.innerHTML = '';

                const totalItems = filteredMedsList.length;
                const totalPages = Math.ceil(totalItems / MEDS_PER_PAGE) || 1;

                if (currentMedPage > totalPages) currentMedPage = totalPages;
                if (currentMedPage < 1) currentMedPage = 1;

                const startIndex = (currentMedPage - 1) * MEDS_PER_PAGE;
                const endIndex = startIndex + MEDS_PER_PAGE;
                const pageItems = filteredMedsList.slice(startIndex, endIndex);

                if (pageItems.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" class="text-center py-8 text-slate-400">Aucun médicament trouvé.</td></tr>';
                } else {
                    pageItems.forEach(m => {
                        const tr = document.createElement('tr');
                        tr.className = "hover:bg-slate-50/80 dark:hover:bg-white/[0.02] transition-colors";
                        tr.innerHTML = `
                            <td class="py-3 pl-2 text-slate-400 font-mono text-[11px]">#${m.id}</td>
                            <td class="py-3 font-bold text-slate-900 dark:text-slate-100">${m.nom_commercial}</td>
                            <td class="py-3 text-emerald-600 dark:text-brand-neon font-semibold">${m.dci}</td>
                            <td class="py-3">
                                <span class="px-2 py-0.5 rounded-lg text-[10px] font-bold bg-slate-100 dark:bg-black/30 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10">${m.forme || '-'}</span>
                            </td>
                            <td class="py-3 text-slate-600 dark:text-slate-300 font-medium">${m.dosage || '-'}</td>
                            <td class="py-3 pr-2 text-right">
                                <div class="inline-flex items-center gap-1.5">
                                    <button onclick="copyMedication(${m.id})" title="Copier et créer similaire" class="p-1.5 text-emerald-600 dark:text-brand-neon hover:bg-emerald-50 dark:hover:bg-emerald-500/10 rounded-lg transition"><i class="ph-bold ph-copy text-sm"></i></button>
                                    <button onclick="editMedication(${m.id})" title="Modifier" class="p-1.5 text-sky-500 hover:bg-sky-50 dark:hover:bg-sky-500/10 rounded-lg transition"><i class="ph-bold ph-pencil-simple text-sm"></i></button>
                                    <button onclick="deleteMedication(${m.id})" title="Supprimer" class="p-1.5 text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-500/10 rounded-lg transition"><i class="ph-bold ph-trash text-sm"></i></button>
                                </div>
                            </td>
                        `;
                        tbody.appendChild(tr);
                    });
                }

                // Affichage des contrôles de pagination
                const paginationWrapper = document.getElementById('medPaginationWrapper');
                const info = document.getElementById('paginationInfo');
                const controls = document.getElementById('paginationControls');

                if (totalItems > 0) {
                    paginationWrapper.classList.remove('hidden');
                    info.innerHTML = `Affichage de <strong>${startIndex + 1}</strong> à <strong>${Math.min(endIndex, totalItems)}</strong> sur <strong>${totalItems}</strong> médicaments`;
                    
                    if (totalItems > MEDS_PER_PAGE) {
                        let html = '';
                        const prevDisabled = currentMedPage <= 1 ? 'opacity-40 cursor-not-allowed' : 'hover:bg-slate-100 dark:hover:bg-white/10 cursor-pointer';
                        html += `<button onclick="setMedPage(${currentMedPage - 1})" ${currentMedPage <= 1 ? 'disabled' : ''} class="px-3 py-1 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-semibold ${prevDisabled}">◀ Précédent</button>`;

                        for (let i = 1; i <= totalPages; i++) {
                            if (i === 1 || i === totalPages || (i >= currentMedPage - 1 && i <= currentMedPage + 1)) {
                                const activeClass = i === currentMedPage 
                                    ? 'bg-brand-neon text-black font-bold shadow-sm' 
                                    : 'border border-slate-200 dark:border-white/10 hover:bg-slate-100 dark:hover:bg-white/5 text-slate-700 dark:text-slate-300';
                                html += `<button onclick="setMedPage(${i})" class="w-7 h-7 rounded-xl text-xs flex items-center justify-center transition ${activeClass}">${i}</button>`;
                            } else if (i === currentMedPage - 2 || i === currentMedPage + 2) {
                                html += `<span class="px-1 text-slate-400">...</span>`;
                            }
                        }

                        const nextDisabled = currentMedPage >= totalPages ? 'opacity-40 cursor-not-allowed' : 'hover:bg-slate-100 dark:hover:bg-white/10 cursor-pointer';
                        html += `<button onclick="setMedPage(${currentMedPage + 1})" ${currentMedPage >= totalPages ? 'disabled' : ''} class="px-3 py-1 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-semibold ${nextDisabled}">Suivant ▶</button>`;
                        controls.innerHTML = html;
                    } else {
                        controls.innerHTML = '';
                    }
                } else {
                    paginationWrapper.classList.add('hidden');
                }
            };

            window.setMedPage = function(p) {
                currentMedPage = p;
                renderMedicationsTable();
            };

            window.openMedicationModal = function() {
                document.getElementById('medForm').reset();
                document.getElementById('medId').value = '';
                document.getElementById('medModalTitle').innerText = 'Ajouter un Médicament';
                document.getElementById('medModal').classList.remove('hidden');
            };

            window.closeMedModal = function() {
                document.getElementById('medModal').classList.add('hidden');
            };

            window.copyMedication = function(id) {
                const m = allMedsList.find(x => x.id === id);
                if (!m) return;
                document.getElementById('medId').value = '';
                document.getElementById('medNom').value = m.nom_commercial;
                document.getElementById('medDci').value = m.dci;
                document.getElementById('medForme').value = m.forme || '';
                document.getElementById('medDosage').value = m.dosage || '';
                document.getElementById('medModalTitle').innerText = 'Copier & Dupliquer Médicament';
                document.getElementById('medModal').classList.remove('hidden');
            };

            window.editMedication = function(id) {
                const m = allMedsList.find(x => x.id === id);
                if (!m) return;
                document.getElementById('medId').value = m.id;
                document.getElementById('medNom').value = m.nom_commercial;
                document.getElementById('medDci').value = m.dci;
                document.getElementById('medForme').value = m.forme || '';
                document.getElementById('medDosage').value = m.dosage || '';
                document.getElementById('medModalTitle').innerText = `Modifier : ${m.nom_commercial}`;
                document.getElementById('medModal').classList.remove('hidden');
            };

            window.handleMedSubmit = async function(e) {
                e.preventDefault();
                const payload = {
                    id: document.getElementById('medId').value ? parseInt(document.getElementById('medId').value) : null,
                    nom_commercial: document.getElementById('medNom').value,
                    dci: document.getElementById('medDci').value,
                    forme: document.getElementById('medForme').value,
                    dosage: document.getElementById('medDosage').value
                };

                const res = await fetch('/api/medications/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                }).then(r => r.json());

                if (res.success) {
                    closeMedModal();
                    loadMedicationsModule();
                } else {
                    alert(res.message);
                }
            };

            window.deleteMedication = async function(id) {
                if (confirm("Voulez-vous supprimer ce médicament de la base ?")) {
                    await fetch('/api/medications/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id })
                    });
                    loadMedicationsModule();
                }
            };

            // Export direct vers Téléchargements avec ouverture automatique
            window.exportMedicationsDirect = async function() {
                try {
                    const res = await fetch('/api/medications/export/direct');
                    const data = await res.json();
                    if (data.success) {
                        alert("✅ " + data.message);
                    } else {
                        alert("❌ " + data.message);
                    }
                } catch (err) {
                    alert("Erreur lors de l'export : " + err.message);
                }
            };

            // Importation par lot
            window.handleMedicationImport = function(event) {
                const file = event.target.files[0];
                if (!file) return;

                const label = document.getElementById('importBtnLabel');
                const prevText = label.innerText;
                label.innerText = "Importation en cours...";

                const reader = new FileReader();
                reader.onload = async function(e) {
                    try {
                        const base64Data = e.target.result;
                        const res = await fetch('/api/medications/import', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ file_data: base64Data, filename: file.name })
                        }).then(r => r.json());

                        alert(res.message);
                        if (res.success) {
                            loadMedicationsModule();
                        }
                    } catch (error) {
                        alert("Erreur de transmission : " + error.message);
                    } finally {
                        label.innerText = prevText;
                    }
                };
                reader.readAsDataURL(file);
                event.target.value = '';
            };

            // Démarrage automatique à l'affichage
            loadMedicationsModule();
        })();
    </script>
    """