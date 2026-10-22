"""Module de gestion des spécialités médicales et nomenclature des actes.

Connecté à PostgreSQL, conforme strictement à PEP 8.
Gère les spécialités, actes, tarifications jour/nuit, filtres multicritères
et l'exportation directe vers les Téléchargements avec ouverture automatique Windows.
"""

from datetime import datetime
import io
import json
import logging
import os
from pathlib import Path
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, HTTPException, Query
from database import get_db_cursor

# Importations sécurisées des moteurs d'exportation
try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False

try:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

logger = logging.getLogger("SpecialitesModule")

specialites_router = APIRouter(prefix="/api/specialites-module", tags=["Spécialités"])


def number_format(val: Any) -> str:
    """Formatte proprement un nombre avec séparateurs de milliers."""
    try:
        num = int(float(val))
        return f"{num:,}".replace(",", " ")
    except (ValueError, TypeError):
        return "0"


# ============================================================================
# LOGIQUE MÉTIER POSTGRESQL
# ============================================================================
class SpecialitesManager:
    """Gestionnaire métier des spécialités, actes et référentiels."""

    def get_specialites(self) -> List[Dict[str, Any]]:
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT s.id, s.code, s.nom, s.description, s.tarif_base, s.tarif_nuit, s.actif,
                       COUNT(c.id) AS total_actes
                FROM ref_specialites s
                LEFT JOIN consultation_types c ON c.specialite = s.nom
                GROUP BY s.id, s.code, s.nom, s.description, s.tarif_base, s.tarif_nuit, s.actif
                ORDER BY s.nom ASC;
            """)
            return [dict(r) for r in cursor.fetchall()]

    def save_specialite(self, data: Dict[str, Any]) -> Dict[str, Any]:
        item_id = data.get("id")
        code = data.get("code", "").strip().upper()
        nom = data.get("nom", "").strip()
        description = data.get("description", "").strip()

        try:
            tarif_base = float(data.get("tarif_base", 0.0))
            tarif_nuit = float(data.get("tarif_nuit", 0.0))
        except (ValueError, TypeError):
            tarif_base, tarif_nuit = 0.0, 0.0

        if not nom:
            return {"success": False, "message": "Le nom de la spécialité est obligatoire."}

        with get_db_cursor() as cursor:
            if item_id:
                query = """
                    UPDATE ref_specialites 
                    SET code = %s, nom = %s, description = %s, tarif_base = %s, tarif_nuit = %s 
                    WHERE id = %s;
                """
                cursor.execute(query, (code, nom, description, tarif_base, tarif_nuit, item_id))
            else:
                query = """
                    INSERT INTO ref_specialites (code, nom, description, tarif_base, tarif_nuit)
                    VALUES (%s, %s, %s, %s, %s);
                """
                cursor.execute(query, (code, nom, description, tarif_base, tarif_nuit))

        return {"success": True, "message": "Spécialité enregistrée avec succès !"}

    def delete_specialite(self, item_id: int) -> Dict[str, Any]:
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM ref_specialites WHERE id = %s;", (item_id,))
        return {"success": True, "message": "Spécialité supprimée."}

    def get_all_consultations(
        self,
        search_query: str = "",
        filter_cat: str = "",
        filter_spe: str = "",
        filter_mod: str = ""
    ) -> List[Dict[str, Any]]:
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT id, designation, categorie, type_consultation, transmettre_hl7, duree, modalite, tarif, tarif_nuit, specialite, salles_dediees
                FROM consultation_types 
                ORDER BY id ASC;
            """)
            rows = cursor.fetchall()

        items = []
        for r in rows:
            salles = r["salles_dediees"]
            if isinstance(salles, str):
                try:
                    salles = json.loads(salles)
                except Exception:
                    salles = []

            item = {
                "id": r["id"],
                "designation": r["designation"],
                "categorie": r["categorie"],
                "type_consultation": r.get("type_consultation") or "Consultation",
                "transmettre_hl7": bool(r.get("transmettre_hl7")),
                "duree": r["duree"] or "-",
                "modalite": r["modalite"],
                "tarif": float(r["tarif"] or 0),
                "tarif_nuit": float(r["tarif_nuit"] or 0),
                "specialite": r["specialite"],
                "salles_dediees": salles or [],
            }

            if filter_cat and item["categorie"] != filter_cat:
                continue
            if filter_spe and item["specialite"] != filter_spe:
                continue
            if filter_mod and item["modalite"] != filter_mod:
                continue
            if search_query:
                q = search_query.lower()
                text_combined = f"{item['designation']} {item['categorie']} {item['type_consultation']} {item['specialite']} {item['modalite']} {' '.join(item['salles_dediees'])}".lower()
                if q not in text_combined:
                    continue

            items.append(item)

        return items

    def save_consultation(self, data: Dict[str, Any]) -> Dict[str, Any]:
        item_id = data.get("id")
        designation = data.get("designation", "").strip()
        categorie = data.get("categorie", "").strip()
        type_consultation = data.get("type_consultation", "Consultation").strip() or "Consultation"
        transmettre_hl7 = bool(data.get("transmettre_hl7", False))
        duree = data.get("duree", "").strip()
        modalite = data.get("modalite", "").strip()
        specialite = data.get("specialite", "").strip()
        salles_list = data.get("salles_dediees", [])

        try:
            tarif = float(data.get("tarif", 0.0))
            tarif_nuit = float(data.get("tarif_nuit", 0.0))
        except (ValueError, TypeError):
            tarif, tarif_nuit = 0.0, 0.0

        if not designation or not categorie or not type_consultation or not modalite or not specialite:
            return {
                "success": False,
                "message": "Désignation, Catégorie, Modalité et Spécialité sont obligatoires.",
            }

        salles_json = json.dumps(salles_list)

        with get_db_cursor() as cursor:
            if item_id:
                query = """
                    UPDATE consultation_types SET 
                        designation = %s, categorie = %s, type_consultation = %s, transmettre_hl7 = %s, duree = %s, modalite = %s,
                        tarif = %s, tarif_nuit = %s, specialite = %s, salles_dediees = %s::jsonb
                    WHERE id = %s;
                """
                cursor.execute(query, (designation, categorie, type_consultation, transmettre_hl7, duree, modalite, tarif, tarif_nuit, specialite, salles_json, item_id))
            else:
                query = """
                    INSERT INTO consultation_types 
                    (designation, categorie, type_consultation, transmettre_hl7, duree, modalite, tarif, tarif_nuit, specialite, salles_dediees)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb);
                """
                cursor.execute(query, (designation, categorie, type_consultation, transmettre_hl7, duree, modalite, tarif, tarif_nuit, specialite, salles_json))

        return {"success": True, "message": "Acte médical enregistré avec succès !"}

    def delete_consultation(self, item_id: int) -> Dict[str, Any]:
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM consultation_types WHERE id = %s;", (item_id,))
        return {"success": True, "message": "Acte supprimé."}

    def get_references(self) -> Dict[str, Any]:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT id, nom FROM ref_categories ORDER BY nom ASC;")
            categories = [dict(r) for r in cursor.fetchall()]

            cursor.execute("SELECT id, nom FROM ref_modalites ORDER BY nom ASC;")
            modalites = [dict(r) for r in cursor.fetchall()]

            cursor.execute("SELECT id, nom FROM ref_types_consultation ORDER BY CASE WHEN nom = 'Consultation' THEN 0 ELSE 1 END, nom ASC;")
            types_consultation = [dict(r) for r in cursor.fetchall()]

            cursor.execute("SELECT id, nom FROM ref_specialites ORDER BY nom ASC;")
            specialites = [dict(r) for r in cursor.fetchall()]

            cursor.execute("SELECT id, designation, etage FROM rooms ORDER BY designation ASC;")
            salles = [dict(r) for r in cursor.fetchall()]

        return {
            "categories": categories,
            "modalites": modalites,
            "types_consultation": types_consultation,
            "specialites": specialites,
            "salles_disponibles": salles,
        }

    def save_ref_item(self, table_name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        if table_name not in ["ref_categories", "ref_modalites", "ref_types_consultation"]:
            return {"success": False, "message": "Table invalide."}

        item_id = data.get("id")
        nom = data.get("nom", "").strip()

        if not nom:
            return {"success": False, "message": "Le nom est obligatoire."}

        with get_db_cursor() as cursor:
            if item_id:
                cursor.execute(f"UPDATE {table_name} SET nom = %s WHERE id = %s;", (nom, item_id))
            else:
                cursor.execute(f"INSERT INTO {table_name} (nom) VALUES (%s) ON CONFLICT (nom) DO NOTHING;", (nom,))

        return {"success": True, "message": "Référence enregistrée !"}

    def delete_ref_item(self, table_name: str, item_id: int) -> Dict[str, Any]:
        if table_name not in ["ref_categories", "ref_modalites", "ref_types_consultation"]:
            return {"success": False, "message": "Table invalide."}

        with get_db_cursor() as cursor:
            cursor.execute(f"DELETE FROM {table_name} WHERE id = %s;", (item_id,))
        return {"success": True, "message": "Élément supprimé."}


manager = SpecialitesManager()


# ============================================================================
# MOTEURS D'EXPORTATION (EXCEL & PDF)
# ============================================================================
def generate_actes_excel(actes: List[Dict[str, Any]]) -> io.BytesIO:
    """Génère un classeur Excel pour la nomenclature des actes médicaux."""
    if not OPENPYXL_AVAILABLE:
        raise RuntimeError("La bibliothèque 'openpyxl' n'est pas installée. Tapez : pip install openpyxl")

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Nomenclature Actes"
    sheet.views.sheetView[0].showGridLines = True

    fill_header = PatternFill(start_color="121C15", end_color="121C15", fill_type="solid")
    font_header = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    font_title = Font(name="Calibri", size=15, bold=True, color="8BE140")
    font_subtitle = Font(name="Calibri", size=10, italic=True, color="64748B")
    font_data = Font(name="Calibri", size=10)
    fill_zebra = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

    thin_border = Border(
        left=Side(style='thin', color='E2E8F0'),
        right=Side(style='thin', color='E2E8F0'),
        top=Side(style='thin', color='E2E8F0'),
        bottom=Side(style='thin', color='E2E8F0')
    )

    sheet.merge_cells("A1:H1")
    sheet["A1"] = "CLINIVA - NOMENCLATURE DES ACTES & CONSULTATIONS MÉDICALES"
    sheet["A1"].font = font_title
    sheet["A1"].alignment = Alignment(horizontal="center", vertical="center")
    sheet.row_dimensions[1].height = 32

    sheet.merge_cells("A2:H2")
    sheet["A2"] = f"Document extrait le {datetime.now().strftime('%d/%m/%Y à %H:%M')} | Total actes inclus : {len(actes)}"
    sheet["A2"].font = font_subtitle
    sheet["A2"].alignment = Alignment(horizontal="center", vertical="center")
    sheet.row_dimensions[2].height = 18

    headers = [
        "ID",
        "Désignation de l'Acte",
        "Catégorie",
        "Spécialité",
        "Modalité",
        "Tarif Jour (DA)",
        "Tarif Nuit (DA)",
        "Salles Dédiées"
    ]
    header_row_idx = 4
    sheet.row_dimensions[header_row_idx].height = 24

    for col_idx, header_title in enumerate(headers, start=1):
        cell = sheet.cell(row=header_row_idx, column=col_idx, value=header_title)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = Alignment(horizontal="center", vertical="center")

    current_row = header_row_idx + 1
    for act in actes:
        sheet.row_dimensions[current_row].height = 20
        salles_str = ", ".join(act.get("salles_dediees", [])) or "Non assignée"

        row_values = [
            f"#{act['id']}",
            act["designation"],
            act["categorie"],
            act["specialite"],
            act["modalite"],
            act["tarif"],
            act["tarif_nuit"] if act["tarif_nuit"] > 0 else "-",
            salles_str
        ]

        for col_idx, val in enumerate(row_values, start=1):
            cell = sheet.cell(row=current_row, column=col_idx, value=val)
            cell.font = font_data
            cell.border = thin_border
            cell.alignment = Alignment(
                horizontal="center" if col_idx in [1, 3, 5, 6, 7] else "left",
                vertical="center"
            )
            if current_row % 2 == 0:
                cell.fill = fill_zebra

        current_row += 1

    for col in sheet.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        sheet.column_dimensions[col_letter].width = max(max_len + 4, 13)

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return output


def generate_actes_pdf(actes: List[Dict[str, Any]]) -> io.BytesIO:
    """Génère un rapport PDF officiel en format A4 Paysage (Landscape)."""
    if not REPORTLAB_AVAILABLE:
        raise RuntimeError("La bibliothèque 'reportlab' n'est pas installée. Tapez : pip install reportlab")

    output = io.BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        leftMargin=30,
        rightMargin=30,
        topMargin=30,
        bottomMargin=30
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=16,
        textColor=colors.HexColor("#121c15"),
        spaceAfter=4
    )
    subtitle_style = ParagraphStyle(
        "DocSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        textColor=colors.HexColor("#64748b"),
        spaceAfter=15
    )
    cell_style = ParagraphStyle(
        "CellData",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#1e293b")
    )
    cell_header_style = ParagraphStyle(
        "CellHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=11,
        textColor=colors.white
    )

    elements = []
    elements.append(Paragraph("CLINIVA - Répertoire & Nomenclature des Actes Médicaux", title_style))
    date_str = datetime.now().strftime("%d/%m/%Y à %H:%M")
    elements.append(Paragraph(f"Édité le {date_str} — Nombre d'actes inclus : <b>{len(actes)}</b>", subtitle_style))

    table_headers = [
        Paragraph("ID", cell_header_style),
        Paragraph("Désignation de l'Acte", cell_header_style),
        Paragraph("Catégorie", cell_header_style),
        Paragraph("Spécialité", cell_header_style),
        Paragraph("Modalité", cell_header_style),
        Paragraph("Tarif Jour", cell_header_style),
        Paragraph("Tarif Nuit", cell_header_style),
        Paragraph("Salles Dédiées", cell_header_style)
    ]

    table_data = [table_headers]

    for act in actes:
        salles_text = ", ".join(act.get("salles_dediees", [])) or "Non assignée"
        row = [
            Paragraph(f"#{act['id']}", cell_style),
            Paragraph(f"<b>{act['designation']}</b>", cell_style),
            Paragraph(act['categorie'], cell_style),
            Paragraph(act['specialite'], cell_style),
            Paragraph(act['modalite'], cell_style),
            Paragraph(f"{number_format(act['tarif'])} DA", cell_style),
            Paragraph(f"{number_format(act['tarif_nuit'])} DA" if act['tarif_nuit'] > 0 else "-", cell_style),
            Paragraph(salles_text, cell_style)
        ]
        table_data.append(row)

    col_widths = [40, 180, 95, 120, 85, 80, 80, 100]
    actes_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    actes_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#121c15")),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
    ]))

    elements.append(actes_table)
    doc.build(elements)
    output.seek(0)
    return output


# ============================================================================
# POINTS D'ACCÈS FASTAPI (ROUTES JSON & EXPORT WINDOWS NATIVE)
# ============================================================================
@specialites_router.get("/data")
async def api_get_all_data():
    try:
        return {
            "specialites": manager.get_specialites(),
            "consultations": manager.get_all_consultations(),
            "references": manager.get_references(),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@specialites_router.post("/specialite/save")
async def api_save_specialite(payload: Dict[str, Any]):
    return manager.save_specialite(payload)


@specialites_router.post("/specialite/delete")
async def api_delete_specialite(payload: Dict[str, Any]):
    return manager.delete_specialite(payload.get("id"))


@specialites_router.post("/consultation/save")
async def api_save_consultation(payload: Dict[str, Any]):
    return manager.save_consultation(payload)


@specialites_router.post("/consultation/delete")
async def api_delete_consultation(payload: Dict[str, Any]):
    return manager.delete_consultation(payload.get("id"))


@specialites_router.post("/ref/save")
async def api_save_ref(payload: Dict[str, Any]):
    return manager.save_ref_item(payload.get("table"), payload.get("data", {}))


@specialites_router.post("/ref/delete")
async def api_delete_ref(payload: Dict[str, Any]):
    return manager.delete_ref_item(payload.get("table"), payload.get("id"))


# ⭐ ROUTE NATIVE DESKTOP QUI ENREGISTRE DANS TÉLÉCHARGEMENTS ET LANCE LE FICHIER ⭐
@specialites_router.get("/export/actes/direct", summary="Export direct vers Téléchargements et ouverture Windows")
async def export_actes_direct(
    format: str = Query(..., pattern="^(xlsx|pdf)$"),
    q: Optional[str] = Query(default=""),
    categorie: Optional[str] = Query(default=""),
    specialite: Optional[str] = Query(default=""),
    modalite: Optional[str] = Query(default="")
):
    """Génère le fichier, l'écrit dans Téléchargements et l'ouvre immédiatement sur Windows."""
    try:
        # Vérification des dépendances
        if format == "xlsx" and not OPENPYXL_AVAILABLE:
            return {
                "success": False,
                "message": "La bibliothèque 'openpyxl' n'est pas installée. Dans le terminal, tapez : pip install openpyxl"
            }
        if format == "pdf" and not REPORTLAB_AVAILABLE:
            return {
                "success": False,
                "message": "La bibliothèque 'reportlab' n'est pas installée. Dans le terminal, tapez : pip install reportlab"
            }

        # Récupération des données filtrées
        actes = manager.get_all_consultations(
            search_query=q,
            filter_cat=categorie,
            filter_spe=specialite,
            filter_mod=modalite
        )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"actes_cliniva_{timestamp}.{format}"

        # Détermination du dossier Téléchargements sous Windows
        downloads_dir = Path.home() / "Downloads"
        if not downloads_dir.exists():
            downloads_dir = Path.home() / "Desktop"

        target_filepath = downloads_dir / filename

        # Génération du contenu binaire
        if format == "xlsx":
            buffer = generate_actes_excel(actes)
        else:
            buffer = generate_actes_pdf(actes)

        # Écriture physique sur le disque Windows
        with open(target_filepath, "wb") as f:
            f.write(buffer.getvalue())

        # Ouverture automatique avec le logiciel par défaut (Excel ou Lecteur PDF)
        try:
            os.startfile(str(target_filepath))
        except Exception as open_err:
            logger.warning("Ouverture automatique impossible : %s", open_err)

        return {
            "success": True,
            "filename": filename,
            "path": str(target_filepath),
            "message": f"Fichier généré et ouvert avec succès dans : {target_filepath}"
        }

    except Exception as exc:
        logger.error("Erreur lors de l'exportation directe : %s", exc, exc_info=True)
        return {
            "success": False,
            "message": f"Erreur interne : {str(exc)}"
        }


# ============================================================================
# RENDU DU COMPOSANT HTML ET JAVASCRIPT
# ============================================================================
def render_html() -> str:
    """Génère le fragment HTML complet avec la barre de filtres et boutons d'export."""
    return """
    <div id="view-specialites" class="space-y-6">
        
        <!-- BANDEAU D'EN-TÊTE -->
        <div class="banner-glow rounded-3xl p-6 lg:p-8 text-white shadow-xl flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
            <div>
                <h1 class="text-2xl font-extrabold tracking-tight mb-1">Spécialités Médicales & Nomenclature</h1>
                <p class="text-xs text-slate-300">Organisation des disciplines médicales, actes, forfaits jour/nuit et salles dédiées.</p>
            </div>
            
            <div class="flex flex-wrap items-center gap-2.5">
                <div class="bg-white/10 px-4 py-2 rounded-2xl backdrop-blur-sm border border-white/10 text-xs">
                    <span class="text-slate-300">Total Actes:</span> 
                    <strong id="stat-total-actes" class="text-brand-neon font-black text-sm ml-1">0</strong>
                </div>

                <!-- Boutons d'export Excel & PDF interactifs -->
                <button id="btn-export-excel" onclick="exportActesDirect('xlsx')" class="flex items-center gap-1.5 px-3.5 py-2.5 bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-300 border border-emerald-500/30 rounded-2xl text-xs font-bold transition shadow-sm" title="Générer et ouvrir dans Excel">
                    <i class="ph-bold ph-file-xls text-base text-emerald-400"></i>
                    <span>Excel</span>
                </button>
                <button id="btn-export-pdf" onclick="exportActesDirect('pdf')" class="flex items-center gap-1.5 px-3.5 py-2.5 bg-rose-500/20 hover:bg-rose-500/30 text-rose-300 border border-rose-500/30 rounded-2xl text-xs font-bold transition shadow-sm" title="Générer et ouvrir en PDF">
                    <i class="ph-bold ph-file-pdf text-base text-rose-400"></i>
                    <span>PDF</span>
                </button>

                <button onclick="openConsultationModal()" class="flex items-center gap-2 bg-brand-neon hover:bg-brand-neonDark text-black px-4 py-2.5 rounded-2xl font-bold text-xs shadow-md shadow-brand-neon/20 transition">
                    <i class="ph-bold ph-plus-circle text-base"></i>
                    <span>Nouvel Acte</span>
                </button>
            </div>
        </div>

        <!-- SÉLECTEUR D'ONGLETS -->
        <div class="flex items-center gap-2 p-1.5 bg-white dark:bg-[#121c15] border border-slate-200/80 dark:border-white/10 rounded-2xl shadow-sm w-fit">
            <button id="spe-tab-btn-actes" onclick="switchSpeTab('actes')" class="spe-tab-btn flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-xs bg-brand-neon text-black shadow-md shadow-brand-neon/20 transition-all">
                <i class="ph-bold ph-clipboard-text text-base"></i>
                <span>Types d'Actes & Consultations</span>
            </button>
            <button id="spe-tab-btn-specialites" onclick="switchSpeTab('specialites')" class="spe-tab-btn flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-xs text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white hover:bg-slate-100 dark:hover:bg-white/5 transition-all">
                <i class="ph-bold ph-stethoscope text-base"></i>
                <span>Disciplines & Spécialités</span>
            </button>
            <button id="spe-tab-btn-refs" onclick="switchSpeTab('refs')" class="spe-tab-btn flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-xs text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white hover:bg-slate-100 dark:hover:bg-white/5 transition-all">
                <i class="ph-bold ph-tag text-base"></i>
                <span>Catégories & Modalités</span>
            </button>
        </div>

        <!-- ONGLET 1 : ACTES & CONSULTATIONS -->
        <div id="spe-tab-content-actes" class="spe-tab-content space-y-4">
            
            <div class="bg-white dark:bg-[#121c15] p-4 rounded-3xl shadow-sm border border-slate-100 dark:border-white/5 space-y-3 transition-colors">
                
                <div class="flex flex-col lg:flex-row items-center justify-between gap-3">
                    <div class="flex items-center gap-2.5 bg-slate-50 dark:bg-black/30 rounded-2xl px-4 py-2 border border-slate-200/80 dark:border-white/10 w-full lg:w-80">
                        <i class="ph ph-magnifying-glass text-slate-400 text-base"></i>
                        <input type="text" id="searchActesInput" oninput="filterActesTable()" placeholder="Rechercher acte, salle, tarif..." class="w-full text-xs outline-none bg-transparent text-slate-700 dark:text-slate-200">
                    </div>

                    <div class="flex items-center gap-2 w-full lg:w-auto justify-end">
                        <button onclick="openManageRefModal('ref_categories', 'Catégories')" class="px-3 py-2 bg-slate-50 dark:bg-black/20 hover:bg-slate-100 dark:hover:bg-white/5 text-slate-700 dark:text-slate-300 border border-slate-200/80 dark:border-white/10 rounded-xl text-xs font-semibold flex items-center gap-1.5 transition">
                            <i class="ph ph-tag text-sky-500"></i>
                            <span>+ Catégories</span>
                        </button>
                        <button onclick="openManageRefModal('ref_modalites', 'Modalités')" class="px-3 py-2 bg-slate-50 dark:bg-black/20 hover:bg-slate-100 dark:hover:bg-white/5 text-slate-700 dark:text-slate-300 border border-slate-200/80 dark:border-white/10 rounded-xl text-xs font-semibold flex items-center gap-1.5 transition">
                            <i class="ph ph-activity text-amber-500"></i>
                            <span>+ Modalités</span>
                        </button>
                        <button onclick="openManageRefModal('ref_types_consultation', 'Types de consultation')" class="px-3 py-2 bg-slate-50 dark:bg-black/20 hover:bg-slate-100 dark:hover:bg-white/5 text-slate-700 dark:text-slate-300 border border-slate-200/80 dark:border-white/10 rounded-xl text-xs font-semibold flex items-center gap-1.5 transition">
                            <i class="ph ph-list-plus text-emerald-500"></i>
                            <span>+ Types</span>
                        </button>
                    </div>
                </div>

                <!-- LIGNE DES 3 FILTRES DÉROULANTS -->
                <div class="grid grid-cols-1 sm:grid-cols-3 md:grid-cols-4 gap-2.5 pt-2 border-t border-slate-100 dark:border-white/5">
                    <div>
                        <select id="filterCategorieSelect" onchange="filterActesTable()" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-slate-200 font-medium outline-none focus:border-brand-neon">
                            <option value="">🏷️ Toutes les Catégories</option>
                        </select>
                    </div>
                    <div>
                        <select id="filterSpecialiteSelect" onchange="filterActesTable()" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-slate-200 font-medium outline-none focus:border-brand-neon">
                            <option value="">🩺 Toutes les Spécialités</option>
                        </select>
                    </div>
                    <div>
                        <select id="filterModaliteSelect" onchange="filterActesTable()" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-slate-200 font-medium outline-none focus:border-brand-neon">
                            <option value="">⚡ Toutes les Modalités</option>
                        </select>
                    </div>
                    <div>
                        <button onclick="resetActesFilters()" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 hover:bg-slate-100 dark:hover:bg-white/5 text-slate-500 dark:text-slate-400 font-semibold flex items-center justify-center gap-1.5 transition">
                            <i class="ph ph-arrow-counter-clockwise text-sm"></i>
                            <span>Réinitialiser</span>
                        </button>
                    </div>
                </div>

            </div>

            <!-- Tableau des Actes -->
            <div class="bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 overflow-x-auto transition-colors">
                <table class="w-full text-left text-xs">
                    <thead>
                        <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-3">
                            <th class="pb-3 pl-2">ID</th>
                            <th class="pb-3">Désignation de l'Acte</th>
                            <th class="pb-3">Catégorie</th>
                            <th class="pb-3">Type</th>
                            <th class="pb-3">Worklist HL7</th>
                            <th class="pb-3">Spécialité</th>
                            <th class="pb-3">Modalité</th>
                            <th class="pb-3">Tarif Jour</th>
                            <th class="pb-3">Tarif Nuit</th>
                            <th class="pb-3">Salles Dédiées</th>
                            <th class="pb-3 pr-2 text-right">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="actes-table-body" class="divide-y divide-slate-100 dark:divide-white/5 text-slate-700 dark:text-slate-300"></tbody>
                </table>
            </div>
        </div>

        <!-- ONGLET 2 : SPÉCIALITÉS -->
        <div id="spe-tab-content-specialites" class="spe-tab-content hidden space-y-4">
            <div class="flex items-center justify-between bg-white dark:bg-[#121c15] p-4 rounded-3xl shadow-sm border border-slate-100 dark:border-white/5">
                <div>
                    <h3 class="text-sm font-bold text-slate-800 dark:text-white">Répertoire des Spécialités Médicales</h3>
                    <p class="text-[11px] text-slate-400">Pôles cliniques et tarifs de référence.</p>
                </div>
                <button onclick="openSpecialiteModal()" class="flex items-center gap-2 bg-brand-neon text-black px-4 py-2 rounded-xl font-bold text-xs shadow-md shadow-brand-neon/20 hover:opacity-90 transition">
                    <i class="ph-bold ph-plus"></i>
                    <span>Nouvelle Spécialité</span>
                </button>
            </div>

            <div class="bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 overflow-x-auto transition-colors">
                <table class="w-full text-left text-xs">
                    <thead>
                        <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-3">
                            <th class="pb-3 pl-2">Code</th>
                            <th class="pb-3">Discipline / Spécialité</th>
                            <th class="pb-3">Description</th>
                            <th class="pb-3">Tarif Réf. Jour</th>
                            <th class="pb-3">Tarif Réf. Nuit</th>
                            <th class="pb-3">Actes Rattachés</th>
                            <th class="pb-3 pr-2 text-right">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="specialites-table-body" class="divide-y divide-slate-100 dark:divide-white/5 text-slate-700 dark:text-slate-300"></tbody>
                </table>
            </div>
        </div>

        <!-- ONGLET 3 : RÉFÉRENTIELS -->
        <div id="spe-tab-content-refs" class="spe-tab-content hidden grid grid-cols-1 md:grid-cols-3 gap-6">
            <div class="bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 space-y-4">
                <div class="flex items-center justify-between border-b border-slate-100 dark:border-white/5 pb-3">
                    <h3 class="text-sm font-bold text-slate-800 dark:text-white flex items-center gap-2">
                        <i class="ph-bold ph-tag text-sky-500"></i>
                        Catégories d'Actes
                    </h3>
                    <button onclick="openManageRefModal('ref_categories', 'Catégories')" class="text-xs font-bold text-brand-neon hover:underline">+ Ajouter</button>
                </div>
                <ul id="list-categories-preview" class="divide-y divide-slate-100 dark:divide-white/5 text-xs"></ul>
            </div>

            <div class="bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 space-y-4">
                <div class="flex items-center justify-between border-b border-slate-100 dark:border-white/5 pb-3">
                    <h3 class="text-sm font-bold text-slate-800 dark:text-white flex items-center gap-2">
                        <i class="ph-bold ph-activity text-amber-500"></i>
                        Modalités de Consultation
                    </h3>
                    <button onclick="openManageRefModal('ref_modalites', 'Modalités')" class="text-xs font-bold text-brand-neon hover:underline">+ Ajouter</button>
                </div>
                <ul id="list-modalites-preview" class="divide-y divide-slate-100 dark:divide-white/5 text-xs"></ul>
            </div>

            <div class="bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 space-y-4">
                <div class="flex items-center justify-between border-b border-slate-100 dark:border-white/5 pb-3">
                    <h3 class="text-sm font-bold text-slate-800 dark:text-white flex items-center gap-2"><i class="ph-bold ph-list-plus text-emerald-500"></i>Types de consultation</h3>
                    <button onclick="openManageRefModal('ref_types_consultation', 'Types de consultation')" class="text-xs font-bold text-brand-neon hover:underline">+ Ajouter</button>
                </div>
                <ul id="list-consultation-types-preview" class="divide-y divide-slate-100 dark:divide-white/5 text-xs"></ul>
            </div>
        </div>

    </div>

    <!-- MODAL ACTE -->
    <div id="consultationModal" class="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center hidden z-50 p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-2xl max-h-[92vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50/50 dark:bg-black/20">
                <div class="flex items-center space-x-2">
                    <div class="w-8 h-8 rounded-xl bg-brand-neon/20 text-brand-neon flex items-center justify-center font-bold text-sm">
                        <i class="ph-bold ph-first-aid"></i>
                    </div>
                    <h3 id="consultModalTitle" class="text-base font-bold text-slate-800 dark:text-white">Ajouter un Type d'Acte</h3>
                </div>
                <button onclick="closeConsultationModal()" class="text-slate-400 hover:text-white p-1 rounded-lg">
                    <i class="ph-bold ph-x text-lg"></i>
                </button>
            </div>

            <form id="consultationForm" onsubmit="handleConsultationSubmit(event)" class="p-6 overflow-y-auto space-y-4">
                <input type="hidden" id="actId">

                <div>
                    <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Désignation de l'Acte *</label>
                    <input type="text" id="actDesignation" required placeholder="Ex: Consultation Spécialisée..." class="w-full text-xs p-3 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white">
                </div>

                <div>
                    <div class="flex items-center justify-between mb-1">
                        <label for="actTypeConsultation" class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300">Type de consultation *</label>
                        <button type="button" onclick="openManageRefModal('ref_types_consultation', 'Types de consultation')" class="text-[10px] font-bold text-emerald-600 hover:underline">+ Ajouter un type</button>
                    </div>
                    <select id="actTypeConsultation" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium"></select>
                </div>

                <label class="flex items-start gap-3 p-3 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 cursor-pointer">
                    <input type="checkbox" id="actTransmettreHl7" class="mt-0.5 rounded border-slate-300 text-emerald-600 focus:ring-emerald-500">
                    <span><span class="block text-xs font-semibold text-slate-700 dark:text-slate-200">Transmettre cet acte à la worklist HL7</span><span class="block mt-0.5 text-[10px] text-slate-500">Marque cet acte comme transmissible lors de la préparation des passages.</span></span>
                </label>

                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Catégorie *</label>
                        <select id="actCategorie" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium"></select>
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Spécialité *</label>
                        <select id="actSpecialite" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium"></select>
                    </div>
                </div>

                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Modalité *</label>
                        <select id="actModalite" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium"></select>
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Durée Prévue</label>
                        <input type="text" id="actDuree" placeholder="Ex: 30 min..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium">
                    </div>
                </div>

                <div class="grid grid-cols-2 gap-3 bg-brand-neon/10 dark:bg-brand-neon/5 p-4 rounded-2xl border border-brand-neon/20">
                    <div>
                        <label class="block text-[11px] font-bold text-slate-700 dark:text-slate-200 mb-1">☀️ Tarif Jour (DA) *</label>
                        <input type="number" step="100" id="actTarif" required placeholder="3500" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-white dark:bg-black/40 font-bold text-emerald-600 dark:text-brand-neon">
                    </div>
                    <div>
                        <label class="block text-[11px] font-bold text-slate-700 dark:text-slate-200 mb-1">🌙 Tarif Nuit / Garde (DA)</label>
                        <input type="number" step="100" id="actTarifNuit" placeholder="5000" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-white dark:bg-black/40 font-bold text-purple-600 dark:text-purple-300">
                    </div>
                </div>

                <div>
                    <label class="block text-[11px] font-semibold text-slate-700 dark:text-slate-300 mb-2">🏥 Salles d'examen dédiées</label>
                    <div id="dedicatedRoomsContainer" class="grid grid-cols-2 gap-2 max-h-36 overflow-y-auto p-3 bg-slate-50 dark:bg-black/30 rounded-2xl border border-slate-200 dark:border-white/10"></div>
                </div>

                <div class="flex items-center justify-end space-x-3 pt-4 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closeConsultationModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler</button>
                    <button type="submit" class="px-6 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md shadow-brand-neon/20">Enregistrer</button>
                </div>
            </form>
        </div>
    </div>

    <!-- MODAL SPÉCIALITÉ -->
    <div id="specialiteModal" class="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center hidden z-50 p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-md shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50/50 dark:bg-black/20">
                <div class="flex items-center space-x-2">
                    <div class="w-8 h-8 rounded-xl bg-brand-neon/20 text-brand-neon flex items-center justify-center font-bold text-sm">
                        <i class="ph-bold ph-stethoscope"></i>
                    </div>
                    <h3 id="speModalTitle" class="text-base font-bold text-slate-800 dark:text-white">Ajouter une Spécialité</h3>
                </div>
                <button onclick="closeSpecialiteModal()" class="text-slate-400 hover:text-white p-1 rounded-lg">
                    <i class="ph-bold ph-x text-lg"></i>
                </button>
            </div>
            <form id="specialiteForm" onsubmit="handleSpecialiteSubmit(event)" class="p-6 space-y-3.5">
                <input type="hidden" id="speId">
                <div class="grid grid-cols-3 gap-2.5">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Code</label>
                        <input type="text" id="speCode" placeholder="CARD" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none uppercase font-mono bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                    <div class="col-span-2">
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Nom Spécialité *</label>
                        <input type="text" id="speNom" required placeholder="Cardiologie" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none font-bold bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                </div>
                <div>
                    <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Description</label>
                    <input type="text" id="speDesc" placeholder="Description courte..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                </div>
                <div class="grid grid-cols-2 gap-2.5">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Tarif Réf. Jour</label>
                        <input type="number" step="100" id="speTarifBase" placeholder="3500" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none font-bold text-emerald-600 dark:text-brand-neon bg-slate-50 dark:bg-black/30">
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Tarif Réf. Nuit</label>
                        <input type="number" step="100" id="speTarifNuit" placeholder="5000" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none font-bold text-purple-600 dark:text-purple-300 bg-slate-50 dark:bg-black/30">
                    </div>
                </div>
                <div class="flex items-center justify-end space-x-3 pt-4 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closeSpecialiteModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler</button>
                    <button type="submit" class="px-6 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md shadow-brand-neon/20">Enregistrer</button>
                </div>
            </form>
        </div>
    </div>

    <!-- MODAL RÉFÉRENCES -->
    <div id="manageRefModal" class="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center hidden z-50 p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-md shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden flex flex-col max-h-[85vh]">
            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50/50 dark:bg-black/20">
                <div class="flex items-center space-x-2">
                    <div class="w-8 h-8 rounded-xl bg-brand-neon/20 text-brand-neon flex items-center justify-center font-bold text-sm">
                        <i class="ph-bold ph-gear"></i>
                    </div>
                    <h3 id="manageRefModalTitle" class="text-base font-bold text-slate-800 dark:text-white">Gérer les Références</h3>
                </div>
                <button onclick="closeManageRefModal()" class="text-slate-400 hover:text-white p-1 rounded-lg">
                    <i class="ph-bold ph-x text-lg"></i>
                </button>
            </div>
            <div class="p-6 overflow-y-auto space-y-4">
                <form onsubmit="handleRefSubmit(event)" class="flex items-center gap-2">
                    <input type="hidden" id="currentRefTable">
                    <input type="hidden" id="currentRefId">
                    <input type="text" id="currentRefNom" required placeholder="Nouveau libellé..." class="flex-1 text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium">
                    <button type="submit" id="btnRefSubmit" class="bg-brand-neon text-black px-4 py-2.5 rounded-xl text-xs font-bold shadow-md shadow-brand-neon/20">+ Ajouter</button>
                </form>
                <div class="border border-slate-100 dark:border-white/10 rounded-2xl overflow-hidden bg-slate-50/50 dark:bg-black/20">
                    <ul id="refItemsList" class="divide-y divide-slate-100 dark:divide-white/5 text-xs"></ul>
                </div>
            </div>
        </div>
    </div>

    <!-- SCRIPT JS COMPLET AVEC EXPORT NATIVE WINDOWS -->
    <script>
        (function() {
            let globalData = { specialites: [], consultations: [], references: { categories: [], modalites: [], types_consultation: [], specialites: [], salles_disponibles: [] } };

            window.switchSpeTab = function(tabName) {
                document.querySelectorAll('.spe-tab-content').forEach(el => el.classList.add('hidden'));
                document.querySelectorAll('.spe-tab-btn').forEach(btn => {
                    btn.className = "spe-tab-btn flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-xs text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white hover:bg-slate-100 dark:hover:bg-white/5 transition-all";
                });

                document.getElementById('spe-tab-content-' + tabName).classList.remove('hidden');
                document.getElementById('spe-tab-btn-' + tabName).className = "spe-tab-btn flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-xs bg-brand-neon text-black shadow-md shadow-brand-neon/20 transition-all";
            };

            window.loadSpecialitesModule = async function() {
                try {
                    const res = await fetch('/api/specialites-module/data');
                    globalData = await res.json();

                    document.getElementById('stat-total-actes').innerText = globalData.consultations.length;
                    renderActesTable(globalData.consultations);
                    renderSpecialitesTable(globalData.specialites);
                    renderRefsPreviews();
                    populateDropdowns();
                    populateFilterDropdowns();
                    filterActesTable();
                } catch (e) {
                    console.error("Erreur de chargement:", e);
                }
            };

            // FONCTION D'EXPORTATION NATIVE PAR PYTHON : ÉCRIT DANS TÉLÉCHARGEMENTS ET OUVRE WINDOWS
            window.exportActesDirect = async function(format) {
                const btnId = format === 'xlsx' ? 'btn-export-excel' : 'btn-export-pdf';
                const btn = document.getElementById(btnId);
                const originalHtml = btn ? btn.innerHTML : '';

                if (btn) {
                    btn.innerHTML = '<i class="ph-bold ph-spinner animate-spin text-sm"></i> Génération...';
                    btn.disabled = true;
                }

                try {
                    const q = encodeURIComponent((document.getElementById('searchActesInput').value || '').trim());
                    const cat = encodeURIComponent((document.getElementById('filterCategorieSelect').value || '').trim());
                    const spe = encodeURIComponent((document.getElementById('filterSpecialiteSelect').value || '').trim());
                    const mod = encodeURIComponent((document.getElementById('filterModaliteSelect').value || '').trim());

                    const url = `/api/specialites-module/export/actes/direct?format=${format}&q=${q}&categorie=${cat}&specialite=${spe}&modalite=${mod}`;
                    const response = await fetch(url);
                    const result = await response.json();

                    if (result.success) {
                        alert("✅ " + result.message);
                    } else {
                        alert("❌ " + result.message);
                    }

                } catch (error) {
                    alert("Erreur lors de l'exportation : " + error.message);
                } finally {
                    if (btn) {
                        btn.innerHTML = originalHtml;
                        btn.disabled = false;
                    }
                }
            };

            window.populateFilterDropdowns = function() {
                const fCat = document.getElementById('filterCategorieSelect');
                const fSpe = document.getElementById('filterSpecialiteSelect');
                const fMod = document.getElementById('filterModaliteSelect');

                const curCat = fCat.value;
                const curSpe = fSpe.value;
                const curMod = fMod.value;

                fCat.innerHTML = '<option value="">🏷️ Toutes les Catégories</option>' + 
                    (globalData.references.categories || []).map(c => `<option value="${c.nom}">${c.nom}</option>`).join('');

                fSpe.innerHTML = '<option value="">🩺 Toutes les Spécialités</option>' + 
                    (globalData.specialites || []).map(s => `<option value="${s.nom}">${s.nom}</option>`).join('');

                fMod.innerHTML = '<option value="">⚡ Toutes les Modalités</option>' + 
                    (globalData.references.modalites || []).map(m => `<option value="${m.nom}">${m.nom}</option>`).join('');

                fCat.value = curCat;
                fSpe.value = curSpe;
                fMod.value = curMod;
            };

            window.filterActesTable = function() {
                const q = (document.getElementById('searchActesInput').value || '').toLowerCase().trim();
                const selCat = (document.getElementById('filterCategorieSelect').value || '').trim();
                const selSpe = (document.getElementById('filterSpecialiteSelect').value || '').trim();
                const selMod = (document.getElementById('filterModaliteSelect').value || '').trim();

                const filtered = (globalData.consultations || []).filter(c => {
                    const matchesQuery = !q || (
                        (c.designation && c.designation.toLowerCase().includes(q)) ||
                        (c.categorie && c.categorie.toLowerCase().includes(q)) ||
                        (c.type_consultation && c.type_consultation.toLowerCase().includes(q)) ||
                        (c.specialite && c.specialite.toLowerCase().includes(q)) ||
                        (c.modalite && c.modalite.toLowerCase().includes(q)) ||
                        String(c.tarif).includes(q) ||
                        String(c.tarif_nuit).includes(q) ||
                        (c.salles_dediees && c.salles_dediees.some(s => s.toLowerCase().includes(q)))
                    );

                    const matchesCat = !selCat || c.categorie === selCat;
                    const matchesSpe = !selSpe || c.specialite === selSpe;
                    const matchesMod = !selMod || c.modalite === selMod;

                    return matchesQuery && matchesCat && matchesSpe && matchesMod;
                });

                renderActesTable(filtered);
            };

            window.resetActesFilters = function() {
                document.getElementById('searchActesInput').value = '';
                document.getElementById('filterCategorieSelect').value = '';
                document.getElementById('filterSpecialiteSelect').value = '';
                document.getElementById('filterModaliteSelect').value = '';
                renderActesTable(globalData.consultations);
            };

            window.renderActesTable = function(items) {
                const tbody = document.getElementById('actes-table-body');
                tbody.innerHTML = '';

                if (!items || items.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="11" class="text-center py-6 text-slate-400">Aucun acte médical ne correspond aux critères sélectionnés.</td></tr>';
                    return;
                }

                items.forEach(c => {
                    const tr = document.createElement('tr');
                    tr.className = "hover:bg-slate-50/80 dark:hover:bg-white/[0.02] transition-colors";

                    const sallesBadges = (c.salles_dediees && c.salles_dediees.length > 0)
                        ? c.salles_dediees.map(s => `<span class="px-2 py-0.5 rounded-md text-[10px] font-bold bg-slate-100 dark:bg-black/30 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10">🚪 ${s}</span>`).join(' ')
                        : `<span class="text-[11px] text-slate-400 italic">Non assignée</span>`;

                    const tarifNuitBadge = (c.tarif_nuit && c.tarif_nuit > 0)
                        ? `<span class="font-bold text-purple-600 dark:text-purple-300 font-mono">${Number(c.tarif_nuit).toLocaleString()} DA</span>`
                        : `<span class="text-slate-400 text-[11px]">-</span>`;

                    tr.innerHTML = `
                        <td class="py-3.5 pl-2 text-slate-400 font-mono text-[11px]">#${c.id}</td>
                        <td class="py-3.5 font-bold text-slate-800 dark:text-slate-100">${c.designation}</td>
                        <td class="py-3.5"><span class="px-2.5 py-1 rounded-xl text-[10px] font-bold bg-sky-50 dark:bg-sky-950/40 text-sky-600 dark:text-sky-300 border border-sky-200 dark:border-sky-800/40">${c.categorie}</span></td>
                        <td class="py-3.5"><span class="px-2.5 py-1 rounded-xl text-[10px] font-bold bg-violet-50 dark:bg-violet-950/40 text-violet-600 dark:text-violet-300 border border-violet-200 dark:border-violet-800/40">${c.type_consultation || 'Consultation'}</span></td>
                        <td class="py-3.5"><span class="px-2.5 py-1 rounded-xl text-[10px] font-bold ${c.transmettre_hl7 ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' : 'bg-slate-100 text-slate-500 border border-slate-200'}">${c.transmettre_hl7 ? 'Oui' : 'Non'}</span></td>
                        <td class="py-3.5"><span class="px-2.5 py-1 rounded-xl text-[10px] font-bold bg-brand-neon/20 text-emerald-800 dark:text-brand-neon border border-brand-neon/30">${c.specialite}</span></td>
                        <td class="py-3.5"><span class="px-2.5 py-1 rounded-xl text-[10px] font-bold bg-amber-50 dark:bg-amber-950/40 text-amber-600 dark:text-amber-300 border border-amber-200 dark:border-amber-800/40">${c.modalite}</span></td>
                        <td class="py-3.5 font-bold text-emerald-600 dark:text-brand-neon font-mono">${Number(c.tarif).toLocaleString()} DA</td>
                        <td class="py-3.5">${tarifNuitBadge}</td>
                        <td class="py-3.5 flex flex-wrap gap-1 items-center max-w-xs">${sallesBadges}</td>
                        <td class="py-3.5 pr-2 text-right">
                            <div class="inline-flex items-center gap-1.5">
                                <button onclick="editConsultation(${c.id})" class="p-1.5 text-sky-500 hover:bg-sky-50 dark:hover:bg-sky-500/10 rounded-lg"><i class="ph-bold ph-pencil-simple text-sm"></i></button>
                                <button onclick="deleteConsultation(${c.id})" class="p-1.5 text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-500/10 rounded-lg"><i class="ph-bold ph-trash text-sm"></i></button>
                            </div>
                        </td>
                    `;
                    tbody.appendChild(tr);
                });
            };

            window.renderSpecialitesTable = function(items) {
                const tbody = document.getElementById('specialites-table-body');
                tbody.innerHTML = '';

                items.forEach(s => {
                    const tr = document.createElement('tr');
                    tr.className = "hover:bg-slate-50/80 dark:hover:bg-white/[0.02] transition-colors";
                    tr.innerHTML = `
                        <td class="py-3.5 pl-2 font-mono font-bold text-brand-neon">${s.code || '-'}</td>
                        <td class="py-3.5 font-bold text-slate-800 dark:text-slate-100">${s.nom}</td>
                        <td class="py-3.5 text-slate-500 dark:text-slate-400 text-xs">${s.description || '-'}</td>
                        <td class="py-3.5 font-bold font-mono text-emerald-600 dark:text-brand-neon">${Number(s.tarif_base || 0).toLocaleString()} DA</td>
                        <td class="py-3.5 font-bold font-mono text-purple-600 dark:text-purple-300">${Number(s.tarif_nuit || 0).toLocaleString()} DA</td>
                        <td class="py-3.5"><span class="px-2.5 py-0.5 rounded-lg text-[10px] font-bold bg-slate-100 dark:bg-black/30 border border-slate-200 dark:border-white/10 text-slate-700 dark:text-slate-300">${s.total_actes || 0} acte(s)</span></td>
                        <td class="py-3.5 pr-2 text-right">
                            <div class="inline-flex items-center gap-1.5">
                                <button onclick="editSpecialite(${s.id})" class="p-1.5 text-sky-500 hover:bg-sky-50 dark:hover:bg-sky-500/10 rounded-lg"><i class="ph-bold ph-pencil-simple text-sm"></i></button>
                                <button onclick="deleteSpecialite(${s.id})" class="p-1.5 text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-500/10 rounded-lg"><i class="ph-bold ph-trash text-sm"></i></button>
                            </div>
                        </td>
                    `;
                    tbody.appendChild(tr);
                });
            };

            window.renderRefsPreviews = function() {
                const listCat = document.getElementById('list-categories-preview');
                const listMod = document.getElementById('list-modalites-preview');
                const listConsultTypes = document.getElementById('list-consultation-types-preview');

                listCat.innerHTML = (globalData.references.categories || []).map(c => `
                    <li class="py-2.5 flex items-center justify-between text-slate-700 dark:text-slate-300 font-medium">
                        <span>${c.nom}</span>
                        <button onclick="deleteRefItem('ref_categories', ${c.id})" class="text-rose-500 hover:opacity-80"><i class="ph-bold ph-trash text-xs"></i></button>
                    </li>
                `).join('');

                listMod.innerHTML = (globalData.references.modalites || []).map(m => `
                    <li class="py-2.5 flex items-center justify-between text-slate-700 dark:text-slate-300 font-medium">
                        <span>${m.nom}</span>
                        <button onclick="deleteRefItem('ref_modalites', ${m.id})" class="text-rose-500 hover:opacity-80"><i class="ph-bold ph-trash text-xs"></i></button>
                    </li>
                `).join('');
                if (listConsultTypes) listConsultTypes.innerHTML = (globalData.references.types_consultation || []).map(t => `
                    <li class="py-2.5 flex items-center justify-between text-slate-700 dark:text-slate-300 font-medium">
                        <span>${t.nom}</span>
                        <button onclick="deleteRefItem('ref_types_consultation', ${t.id})" class="text-rose-500 hover:opacity-80"><i class="ph-bold ph-trash text-xs"></i></button>
                    </li>
                `).join('');
            };

            window.populateDropdowns = function() {
                const sCat = document.getElementById('actCategorie');
                const sSpe = document.getElementById('actSpecialite');
                const sMod = document.getElementById('actModalite');
                const sConsultType = document.getElementById('actTypeConsultation');

                sCat.innerHTML = (globalData.references.categories || []).map(c => `<option value="${c.nom}">${c.nom}</option>`).join('');
                sSpe.innerHTML = (globalData.specialites || []).map(s => `<option value="${s.nom}">${s.nom}</option>`).join('');
                sMod.innerHTML = (globalData.references.modalites || []).map(m => `<option value="${m.nom}">${m.nom}</option>`).join('');
                if (sConsultType) sConsultType.innerHTML = (globalData.references.types_consultation || []).map(t => `<option value="${t.nom}">${t.nom}</option>`).join('');

                const roomsContainer = document.getElementById('dedicatedRoomsContainer');
                if (globalData.references.salles_disponibles && globalData.references.salles_disponibles.length > 0) {
                    roomsContainer.innerHTML = globalData.references.salles_disponibles.map(r => `
                        <label class="flex items-center gap-2 bg-white dark:bg-[#121c15] p-2 rounded-xl border border-slate-200 dark:border-white/10 cursor-pointer">
                            <input type="checkbox" value="${r.designation}" class="act-room-check rounded text-brand-neon focus:ring-brand-neon">
                            <span class="text-xs font-bold text-slate-700 dark:text-slate-200 truncate">${r.designation} (${r.etage})</span>
                        </label>
                    `).join('');
                } else {
                    roomsContainer.innerHTML = '<span class="text-xs text-slate-400 italic col-span-2 py-2">Aucune salle disponible.</span>';
                }
            };

            window.openConsultationModal = function() {
                document.getElementById('consultationForm').reset();
                document.getElementById('actId').value = '';
                document.getElementById('consultModalTitle').innerText = "Ajouter un Type d'Acte";
                populateDropdowns();
                document.querySelectorAll('.act-room-check').forEach(cb => cb.checked = false);
                document.getElementById('consultationModal').classList.remove('hidden');
            };

            window.closeConsultationModal = function() {
                document.getElementById('consultationModal').classList.add('hidden');
            };

            window.editConsultation = function(id) {
                const c = globalData.consultations.find(x => x.id === id);
                if (!c) return;

                populateDropdowns();
                document.getElementById('actId').value = c.id;
                document.getElementById('actDesignation').value = c.designation;
                document.getElementById('actCategorie').value = c.categorie;
                document.getElementById('actTypeConsultation').value = c.type_consultation || 'Consultation';
                document.getElementById('actTransmettreHl7').checked = c.transmettre_hl7 === true;
                document.getElementById('actSpecialite').value = c.specialite;
                document.getElementById('actModalite').value = c.modalite;
                document.getElementById('actDuree').value = c.duree !== '-' ? c.duree : '';
                document.getElementById('actTarif').value = c.tarif;
                document.getElementById('actTarifNuit').value = c.tarif_nuit > 0 ? c.tarif_nuit : '';

                document.querySelectorAll('.act-room-check').forEach(cb => {
                    cb.checked = (c.salles_dediees || []).includes(cb.value);
                });

                document.getElementById('consultModalTitle').innerText = "Modifier l'Acte : " + c.designation;
                document.getElementById('consultationModal').classList.remove('hidden');
            };

            window.handleConsultationSubmit = async function(e) {
                e.preventDefault();
                const rooms = [];
                document.querySelectorAll('.act-room-check:checked').forEach(cb => rooms.push(cb.value));

                const payload = {
                    id: document.getElementById('actId').value ? parseInt(document.getElementById('actId').value) : null,
                    designation: document.getElementById('actDesignation').value,
                    categorie: document.getElementById('actCategorie').value,
                    type_consultation: document.getElementById('actTypeConsultation').value,
                    transmettre_hl7: document.getElementById('actTransmettreHl7').checked,
                    specialite: document.getElementById('actSpecialite').value,
                    modalite: document.getElementById('actModalite').value,
                    duree: document.getElementById('actDuree').value,
                    tarif: document.getElementById('actTarif').value,
                    tarif_nuit: document.getElementById('actTarifNuit').value || 0.0,
                    salles_dediees: rooms
                };

                const res = await fetch('/api/specialites-module/consultation/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                }).then(r => r.json());

                if (res.success) {
                    closeConsultationModal();
                    loadSpecialitesModule();
                } else {
                    alert(res.message);
                }
            };

            window.deleteConsultation = async function(id) {
                if (confirm("Voulez-vous supprimer cet acte ?")) {
                    await fetch('/api/specialites-module/consultation/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id })
                    });
                    loadSpecialitesModule();
                }
            };

            window.openSpecialiteModal = function() {
                document.getElementById('specialiteForm').reset();
                document.getElementById('speId').value = '';
                document.getElementById('speModalTitle').innerText = "Ajouter une Spécialité";
                document.getElementById('specialiteModal').classList.remove('hidden');
            };

            window.closeSpecialiteModal = function() {
                document.getElementById('specialiteModal').classList.add('hidden');
            };

            window.editSpecialite = function(id) {
                const s = globalData.specialites.find(x => x.id === id);
                if (!s) return;

                document.getElementById('speId').value = s.id;
                document.getElementById('speCode').value = s.code || '';
                document.getElementById('speNom').value = s.nom;
                document.getElementById('speDesc').value = s.description || '';
                document.getElementById('speTarifBase').value = s.tarif_base || '';
                document.getElementById('speTarifNuit').value = s.tarif_nuit || '';

                document.getElementById('speModalTitle').innerText = "Modifier : " + s.nom;
                document.getElementById('specialiteModal').classList.remove('hidden');
            };

            window.handleSpecialiteSubmit = async function(e) {
                e.preventDefault();
                const payload = {
                    id: document.getElementById('speId').value ? parseInt(document.getElementById('speId').value) : null,
                    code: document.getElementById('speCode').value,
                    nom: document.getElementById('speNom').value,
                    description: document.getElementById('speDesc').value,
                    tarif_base: document.getElementById('speTarifBase').value || 0.0,
                    tarif_nuit: document.getElementById('speTarifNuit').value || 0.0
                };

                const res = await fetch('/api/specialites-module/specialite/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                }).then(r => r.json());

                if (res.success) {
                    closeSpecialiteModal();
                    loadSpecialitesModule();
                } else {
                    alert(res.message);
                }
            };

            window.deleteSpecialite = async function(id) {
                if (confirm("Supprimer cette spécialité ?")) {
                    await fetch('/api/specialites-module/specialite/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id })
                    });
                    loadSpecialitesModule();
                }
            };

            window.openManageRefModal = function(tableName, label) {
                document.getElementById('currentRefTable').value = tableName;
                document.getElementById('currentRefId').value = '';
                document.getElementById('currentRefNom').value = '';
                document.getElementById('manageRefModalTitle').innerText = 'Gérer les ' + label;
                renderRefItemsList(tableName);
                document.getElementById('manageRefModal').classList.remove('hidden');
            };

            window.closeManageRefModal = function() {
                document.getElementById('manageRefModal').classList.add('hidden');
            };

            window.renderRefItemsList = function(tableName) {
                const listEl = document.getElementById('refItemsList');
                listEl.innerHTML = '';
                const items = tableName === 'ref_categories' ? globalData.references.categories : (tableName === 'ref_modalites' ? globalData.references.modalites : (globalData.references.types_consultation || []));

                items.forEach(item => {
                    const li = document.createElement('li');
                    li.className = "flex items-center justify-between p-3 hover:bg-slate-100/50 dark:hover:bg-white/[0.02]";
                    li.innerHTML = `
                        <span class="font-bold text-slate-700 dark:text-slate-200">${item.nom}</span>
                        <button onclick="deleteRefItem('${tableName}', ${item.id})" class="p-1 text-rose-500 hover:opacity-80"><i class="ph-bold ph-trash text-sm"></i></button>
                    `;
                    listEl.appendChild(li);
                });
            };

            window.handleRefSubmit = async function(e) {
                e.preventDefault();
                const table = document.getElementById('currentRefTable').value;
                const nom = document.getElementById('currentRefNom').value;

                const res = await fetch('/api/specialites-module/ref/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ table, data: { nom } })
                }).then(r => r.json());

                if (res.success) {
                    document.getElementById('currentRefNom').value = '';
                    await loadSpecialitesModule();
                    renderRefItemsList(table);
                } else {
                    alert(res.message);
                }
            };

            window.deleteRefItem = async function(table, id) {
                if (confirm("Supprimer cet élément ?")) {
                    await fetch('/api/specialites-module/ref/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ table, id })
                    });
                    await loadSpecialitesModule();
                    renderRefItemsList(table);
                }
            };

            loadSpecialitesModule();
        })();
    </script>
    """
