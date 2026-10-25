from datetime import date, datetime
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import platform
import random
import re
import shutil
import subprocess
import time
import unicodedata
import uuid
from decimal import Decimal, InvalidOperation
from typing import Dict, Any, List, Optional
import zipfile

from PIL import Image
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape as xml_escape


from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, Response
from database import get_db_cursor
from app.auth import get_current_user_optional, track_action

logger = logging.getLogger("DmeModule")

dme_router = APIRouter(prefix="/api/dme", tags=["Dossier Médical"])

BASE_DIR = Path(__file__).resolve().parent.parent
WHO_GROWTH_REFERENCE = BASE_DIR / "resources" / "who_growth" / "growth_reference.json"

from modules.physiotherapy_bilan import FIELDS as PHYSIOTHERAPY_FIELDS
from modules.reeducation_classique import FIELDS as CLASSIC_REEDUCATION_FIELDS

DME_SPECIALTY_FIELDS = {
    "physiotherapie": PHYSIOTHERAPY_FIELDS,
    "reeducation_classique": CLASSIC_REEDUCATION_FIELDS,
    "gastro": {
        "symptomes_digestifs", "transit_selles", "symptomes_associes", "signes_alerte_digestifs",
        "explorations_digestives", "examen_abdominal", "plan_digestif",
    },
    "anesthesie": {
        "intervention_prevue", "antecedents_anesthesiques", "voies_aeriennes", "evaluation_cardiorespiratoire",
        "capacite_fonctionnelle", "classe_asa", "examens_preoperatoires", "consentement_anesthesique", "plan_anesthesique",
    },
    "dermatologie": {
        "lesion_localisation", "lesion_evolution", "lesion_description", "lesion_symptomes", "examen_cutane",
        "dermoscopie_histologie", "photos_cliniques", "plan_dermatologie",
    },
    "gynecologie": {
        "dernieres_regles", "cycles_menstruels", "antecedents_grossesse", "contraception",
        "depistage_col", "symptomes_gynecologiques", "examen_gynecologique", "plan_gynecologique",
    },
    "general": {
        "antecedents_familiaux", "revue_systemes", "autonomie_fonctionnelle", "prevention_depistage",
        "vaccinations", "suivi_orientation",
    },
    "medecine_physique": {
        "motif_reeducation", "histoire_fonctionnelle", "douleur_reeducation", "limitations_activites",
        "autonomie_deplacements", "examen_articulaire_moteur", "examen_neurologique_fonctionnel",
        "equilibre_marche", "aides_techniques", "evaluations_fonctionnelles", "objectifs_reeducation",
        "type_reeducation", "seances_par_semaine", "nombre_total_seances", "duree_seance_minutes",
        "kine_staff_id", "kine_assigne_label", "plans_reeducation_json", "programme_reeducation", "suivi_reeducation",
    },
    "medecine_sport": {
        "sport_pratique", "niveau_pratique_charge", "contexte_blessure_sport", "antecedents_sportifs",
        "douleur_sport", "examen_musculosquelettique_sport", "tests_fonctionnels_sport",
        "aptitude_restrictions_sport", "plan_reprise_sport", "suivi_sport",
    },
}

DME_SPECIALTY_LABELS = {
    "physiotherapie": "Bilan de rééducation / physiothérapie",
    "reeducation_classique": "Rééducation classique",
    "gastro": "Gastro-entérologie",
    "anesthesie": "Anesthésie",
    "dermatologie": "Dermatologie",
    "gynecologie": "Gynécologie",
    "general": "Médecine générale",
    "medecine_physique": "Médecine physique et réadaptation",
    "medecine_sport": "Médecine du sport",
}


def _is_livret_prestation_allowed(prestation: Dict[str, Any]) -> bool:
    text = " ".join(str(prestation.get(key) or "") for key in ("designation", "categorie", "specialite"))
    normalized = "".join(char for char in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(char))
    excluded_terms = ("contrast", "gadolinium", "produit iode", "sedat", "anesth", "soin", "infirm")
    return not any(term in normalized for term in excluded_terms)


def _normalize_prestation_name(value: Any) -> str:
    normalized = "".join(char for char in unicodedata.normalize("NFKD", str(value or "").lower()) if not unicodedata.combining(char))
    return " ".join(normalized.split())


@dme_router.get("/growth-reference")
async def get_growth_reference():
    """Références OMS embarquées pour les courbes pédiatriques hors ligne."""
    if not WHO_GROWTH_REFERENCE.exists():
        raise HTTPException(status_code=503, detail="Les références OMS de croissance ne sont pas installées.")
    return json.loads(WHO_GROWTH_REFERENCE.read_text(encoding="utf-8"))


@dme_router.get("/rehabilitation/kinesitherapeutes")
async def get_rehabilitation_therapists(request: Request):
    """Liste les kinésithérapeutes actifs pour l’affectation du plan de rééducation."""
    if not get_current_user_optional(request):
        raise HTTPException(status_code=401, detail="Session expirée ou utilisateur non connecté.")
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT id, nom, prenom, type_personnel, service_nom
            FROM clinic_staff
            WHERE COALESCE(actif, TRUE) IS DISTINCT FROM FALSE
              AND LOWER(COALESCE(type_personnel, '')) LIKE '%kin%'
            ORDER BY nom ASC, prenom ASC;
        """)
        therapists = [
            {
                "id": row.get("id"),
                "nom": str(row.get("nom") or "").strip(),
                "prenom": str(row.get("prenom") or "").strip(),
                "type_personnel": str(row.get("type_personnel") or "").strip(),
                "service_nom": str(row.get("service_nom") or "").strip(),
            }
            for row in cursor.fetchall()
        ]
    return {"success": True, "therapists": therapists}


@dme_router.get("/rehabilitation/types")
async def get_rehabilitation_types(request: Request):
    """Retourne les types liés à la rééducation via la catégorie, le type ou la spécialité."""
    if not get_current_user_optional(request):
        raise HTTPException(status_code=401, detail="Session expirée ou utilisateur non connecté.")
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT designation, categorie, type_consultation, specialite
            FROM consultation_types
            ORDER BY designation;
        """)
        rows = cursor.fetchall()
        cursor.execute("SELECT nom FROM ref_specialites WHERE actif IS DISTINCT FROM FALSE ORDER BY nom;")
        specialties = cursor.fetchall()

    rehab_terms = ("reeducation", "readaptation", "rehabilitation", "kinesitherapie", "kine", "fonctionnelle", "physiotherapie", "physio")
    related_categories = {
        _normalize_prestation_name(row.get("categorie"))
        for row in rows
        if _normalize_prestation_name(row.get("categorie"))
        and any(term in _normalize_prestation_name(row.get(field)) for field in ("designation", "categorie", "type_consultation", "specialite") for term in rehab_terms)
    }
    related_types = {
        _normalize_prestation_name(row.get("type_consultation"))
        for row in rows
        if _normalize_prestation_name(row.get("type_consultation"))
        and any(term in _normalize_prestation_name(row.get(field)) for field in ("designation", "categorie", "type_consultation", "specialite") for term in rehab_terms)
    }
    related_specialties = {
        _normalize_prestation_name(row.get("specialite"))
        for row in rows
        if _normalize_prestation_name(row.get("specialite"))
        and any(term in _normalize_prestation_name(row.get(field)) for field in ("designation", "categorie", "type_consultation", "specialite") for term in rehab_terms)
    }
    types = {
        str(row.get("designation") or "").strip()
        for row in rows
        if str(row.get("designation") or "").strip()
        and (
            _normalize_prestation_name(row.get("categorie")) in related_categories
            or _normalize_prestation_name(row.get("type_consultation")) in related_types
            or _normalize_prestation_name(row.get("specialite")) in related_specialties
            or any(term in _normalize_prestation_name(row.get(field)) for field in ("designation", "categorie", "type_consultation", "specialite") for term in rehab_terms)
        )
    }
    # Certaines installations ont la spécialité de rééducation au référentiel
    # sans disposer encore d'un acte associé dans consultation_types.
    types.update(
        str(row.get("nom") or "").strip()
        for row in specialties
        if str(row.get("nom") or "").strip()
        and any(term in _normalize_prestation_name(row.get("nom")) for term in rehab_terms)
    )
    return {"success": True, "types": sorted(types, key=str.casefold)}


@dme_router.post("/specialty-sections")
async def api_save_dme_specialty_sections(payload: Dict[str, Any], request: Request):
    """Persiste immédiatement les sections spécialisées ajoutées à une consultation en cours."""
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée ou utilisateur non connecté.")
    patient_id = payload.get("id_patient")
    passage_id = payload.get("id_passage")
    requested_sections = payload.get("sections")
    if not patient_id or not passage_id or not isinstance(requested_sections, list):
        raise HTTPException(status_code=400, detail="Patient, passage ou sections manquants.")

    valid_sections = [key for key in dict.fromkeys(requested_sections) if key in DME_SPECIALTY_FIELDS]
    if not valid_sections:
        raise HTTPException(status_code=400, detail="Aucune section spécialisée valide n'a été fournie.")

    dme_manager.ensure_dme_schema()
    with get_db_cursor() as cursor:
        cursor.execute("SELECT id_patient FROM patient_passages WHERE id = %s FOR UPDATE;", (passage_id,))
        passage = cursor.fetchone()
        if not passage or int(passage.get("id_patient") or 0) != int(patient_id):
            raise HTTPException(status_code=404, detail="Passage introuvable pour ce patient.")
        cursor.execute("""
            SELECT id, specialite_consultation_json, verrouille
            FROM patient_dme_records
            WHERE id_patient = %s AND id_passage = %s
            ORDER BY id DESC LIMIT 1 FOR UPDATE;
        """, (patient_id, passage_id))
        record = cursor.fetchone()
        if not record:
            raise HTTPException(status_code=409, detail="Démarrez d'abord la consultation pour ajouter des sections spécialisées.")
        if record.get("verrouille", True):
            raise HTTPException(status_code=409, detail="Cette consultation est verrouillée en lecture seule.")

        saved = record.get("specialite_consultation_json") or {}
        if isinstance(saved, str):
            try:
                saved = json.loads(saved)
            except (TypeError, ValueError):
                saved = {}
        saved = saved if isinstance(saved, dict) else {}
        sections = saved.get("sections") if isinstance(saved.get("sections"), dict) else {}
        sections = dict(sections)
        for key in valid_sections:
            sections.setdefault(key, {})
        saved["sections"] = sections
        if not saved.get("specialite"):
            saved["specialite"] = valid_sections[0]
        cursor.execute("""
            UPDATE patient_dme_records
            SET specialite_consultation_json = %s::jsonb
            WHERE id = %s AND id_patient = %s AND id_passage = %s;
        """, (json.dumps(saved, ensure_ascii=False), record["id"], patient_id, passage_id))

    track_action(request, "AJOUT_SECTIONS_DME", f"Sections spécialisées ajoutées à la consultation #{passage_id}")
    return {"success": True, "donnees_specialisees": saved}


@dme_router.post("/rehabilitation/bilan-pdf")
async def preview_physiotherapy_bilan_pdf(payload: Dict[str, Any], request: Request):
    from modules.physiotherapy_bilan import build_bilan_pdf, validate_values
    from modules.dme_display import display_permissions
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée.")
    permissions = display_permissions(user)
    if user.get("role") != "Administrateur" and not (permissions.get("dme_view") or permissions.get("dme_consult")):
        raise HTTPException(status_code=403, detail="Accès au dossier médical non autorisé.")
    patient_id, passage_id = payload.get("patient_id"), payload.get("passage_id")
    if not patient_id or not passage_id:
        raise HTTPException(status_code=400, detail="Patient ou passage manquant.")
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT p.nom, p.prenom, p.date_naissance, pp.id AS passage_id
            FROM patients p JOIN patient_passages pp ON pp.id_patient=p.id
            WHERE p.id=%s AND pp.id=%s;
        """, (patient_id, passage_id))
        patient = cursor.fetchone()
    if not patient:
        raise HTTPException(status_code=404, detail="Passage introuvable pour ce patient.")
    raw = payload.get("values") or {}
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Fiche invalide.")
    try:
        values = validate_values(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(build_bilan_pdf(values, dict(patient)), media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="bilan-physiotherapie.pdf"', "Cache-Control": "no-store"})


@dme_router.post("/rehabilitation/plan-pdf")
async def preview_rehabilitation_plan_pdf(payload: Dict[str, Any], request: Request):
    """Génère l’aperçu PDF du plan saisi, sans l’ajouter aux comptes rendus archivés."""
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée ou utilisateur non connecté.")
    patient_id = payload.get("patient_id")
    passage_id = payload.get("passage_id")
    if not patient_id or not passage_id:
        raise HTTPException(status_code=400, detail="Patient ou passage manquant.")

    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT p.nom, p.prenom, p.date_naissance, pp.date_passage, pp.heure_passage
            FROM patients p
            JOIN patient_passages pp ON pp.id_patient = p.id
            WHERE p.id = %s AND pp.id = %s;
        """, (patient_id, passage_id))
        patient = cursor.fetchone()
        cursor.execute("SELECT nom_raison_sociale, adresse, telephone, entete_pdf, pied_page_pdf FROM clinic_info WHERE id = 1;")
        clinic = cursor.fetchone() or {}
    if not patient:
        raise HTTPException(status_code=404, detail="Le passage du patient est introuvable.")

    rows = payload.get("plans") if isinstance(payload.get("plans"), list) else []
    rows = [row for row in rows if isinstance(row, dict) and str(row.get("type") or "").strip()]
    if not rows:
        raise HTTPException(status_code=400, detail="Ajoutez au moins une ligne au plan de rééducation.")
    if len(rows) > 80:
        raise HTTPException(status_code=400, detail="Le plan ne peut pas contenir plus de 80 lignes.")

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=(landscape(A4)), rightMargin=13 * mm, leftMargin=13 * mm,
        topMargin=13 * mm, bottomMargin=17 * mm, title="Plan de rééducation Cliniva",
        author="Cliniva",
    )
    base_styles = getSampleStyleSheet()
    title_style = ParagraphStyle("RehabTitle", parent=base_styles["Title"], fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=colors.HexColor("#173d2b"), alignment=0, spaceAfter=4)
    subtitle_style = ParagraphStyle("RehabSubtitle", parent=base_styles["Normal"], fontName="Helvetica", fontSize=8.5, leading=12, textColor=colors.HexColor("#64748b"), spaceAfter=12)
    cell_style = ParagraphStyle("RehabCell", parent=base_styles["BodyText"], fontName="Helvetica", fontSize=8, leading=10, textColor=colors.HexColor("#1e293b"))
    cell_bold_style = ParagraphStyle("RehabCellBold", parent=cell_style, fontName="Helvetica-Bold")
    small_style = ParagraphStyle("RehabSmall", parent=cell_style, fontSize=8.5, leading=12)

    def safe_paragraph(value: Any, style=cell_style) -> Paragraph:
        text = xml_escape(str(value or "")).replace("\n", "<br/>")
        return Paragraph(text or "—", style)

    clinic_name = str(clinic.get("nom_raison_sociale") or "Clinique").strip()
    story = [safe_paragraph(clinic_name, ParagraphStyle("ClinicTitle", parent=title_style, fontSize=10, leading=13, textColor=colors.HexColor("#475569"))),
             safe_paragraph("PLAN DE RÉÉDUCATION", title_style)]
    clinic_details = " · ".join(part for part in (str(clinic.get("adresse") or "").strip(), str(clinic.get("telephone") or "").strip()) if part)
    if clinic_details:
        story.append(safe_paragraph(clinic_details, subtitle_style))
    passage_date = patient.get("date_passage")
    if hasattr(passage_date, "strftime"):
        passage_date = passage_date.strftime("%d/%m/%Y")
    identity = [
        [safe_paragraph("Patient", cell_bold_style), safe_paragraph(f"{patient.get('nom') or ''} {patient.get('prenom') or ''}".strip(), small_style), safe_paragraph("Date du passage", cell_bold_style), safe_paragraph(f"{passage_date or '—'} {str(patient.get('heure_passage') or '')[:5]}".strip(), small_style)],
        [safe_paragraph("N° dossier", cell_bold_style), safe_paragraph(patient_id, small_style), safe_paragraph("Praticien", cell_bold_style), safe_paragraph(f"{user.get('prenom') or ''} {user.get('nom') or ''}".strip() or "—", small_style)],
    ]
    identity_table = Table(identity, colWidths=[27 * mm, 88 * mm, 31 * mm, 88 * mm])
    identity_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f5f9")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f1f5f9")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#dbe3ed")),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#dbe3ed")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([identity_table, Spacer(1, 10)])

    goal = str(payload.get("objectifs") or "").strip()
    notes = str(payload.get("consignes") or "").strip()
    if goal:
        story.extend([safe_paragraph("Objectifs", cell_bold_style), safe_paragraph(goal, small_style), Spacer(1, 7)])
    data = [[safe_paragraph(label, ParagraphStyle("RehabHeader", parent=cell_bold_style, textColor=colors.white)) for label in ("Type de rééducation", "Séances / semaine", "Nombre total", "Durée / séance", "Kinésithérapeute")]]
    for row in rows:
        data.append([
            safe_paragraph(row.get("type"), cell_bold_style),
            safe_paragraph(row.get("seances_par_semaine") or "—"),
            safe_paragraph(row.get("nombre_total_seances") or "—"),
            safe_paragraph(f"{row.get('duree_seance_minutes')} min" if row.get("duree_seance_minutes") else "—"),
            safe_paragraph(row.get("kine") or "N’importe quelle kiné"),
        ])
    plan_table = Table(data, colWidths=[77 * mm, 38 * mm, 35 * mm, 35 * mm, 49 * mm], repeatRows=1, hAlign="LEFT")
    plan_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#173d2b")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#dbe3ed")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([plan_table, Spacer(1, 10)])
    if notes:
        story.extend([safe_paragraph("Programme, précautions et consignes", cell_bold_style), safe_paragraph(notes, small_style), Spacer(1, 10)])
    if clinic.get("pied_page_pdf"):
        story.append(safe_paragraph(clinic.get("pied_page_pdf"), subtitle_style))
    document.build(story)
    pdf_bytes = buffer.getvalue()
    filename = f"Plan_Reeducation_Passage_{passage_id}.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="{filename}"',
        "Cache-Control": "no-store",
    })


def calculate_age_str(dob_val: Any) -> str:
    """Calcule l'âge en années à partir de la date de naissance."""
    if not dob_val or str(dob_val).strip() in ("-", "", "None"):
        return ""
    try:
        if isinstance(dob_val, (date, datetime)):
            born = dob_val if isinstance(dob_val, date) else dob_val.date()
        else:
            clean_str = str(dob_val).split(" ")[0].strip()
            if "/" in clean_str:
                born = datetime.strptime(clean_str, "%d/%m/%Y").date()
            else:
                born = datetime.strptime(clean_str, "%Y-%m-%d").date()

        today = date.today()
        years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        return f"{years} ans"
    except Exception:
        return ""


def format_date_display(dt_val: Any) -> str:
    """Formate une date en JJ/MM/AAAA sans erreur de type."""
    if not dt_val or str(dt_val).strip() in ("-", "", "None"):
        return datetime.now().strftime("%d/%m/%Y")
    try:
        if isinstance(dt_val, (date, datetime)):
            return dt_val.strftime("%d/%m/%Y")
        clean_str = str(dt_val).split(" ")[0].strip()
        if "/" in clean_str:
            return clean_str
        parsed = datetime.strptime(clean_str, "%Y-%m-%d")
        return parsed.strftime("%d/%m/%Y")
    except Exception:
        return str(dt_val)


def convert_docx_to_pdf_windows(docx_path: str, pdf_path: str) -> bool:
    """Convertit un fichier Word (.docx) en PDF sous Windows via Microsoft Word COM."""
    if not os.path.exists(docx_path):
        return False

    if platform.system() == "Windows":
        try:
            import win32com.client
            import pythoncom
            pythoncom.CoInitialize()
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = False
            abs_docx = os.path.abspath(docx_path)
            abs_pdf = os.path.abspath(pdf_path)
            doc = word.Documents.Open(abs_docx)
            doc.SaveAs(abs_pdf, FileFormat=17)  # 17 = wdFormatPDF
            doc.Close()
            word.Quit()
            pythoncom.CoUninitialize()
            return True
        except Exception as err:
            logger.warning("Erreur conversion Word vers PDF via COM : %s", err)

    # Tentative de secours avec docx2pdf si installé
    try:
        from docx2pdf import convert
        convert(docx_path, pdf_path)
        return True
    except Exception:
        pass

    return False


def generate_livret_pdf_file(
    output_pdf_path: str,
    patient_info: Dict[str, str],
    cr_record: Dict[str, Any],
    selected_echo_paths: List[Any],
    cover_page_path: Optional[str] = None,
    images_title: str = "IMAGES ÉCHOGRAPHIQUES",
    image_layout: str = "echographique",
) -> bool:
    """Assemble le livret A4 en insérant le compte rendu original exact via pypdf."""
    import tempfile
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors

    model_dir = Path(BASE_DIR) / "uploads" / "model_templates"
    img_page1 = Path(cover_page_path) if cover_page_path else model_dir / "Page_De_Garde" / "1.png"
    if not img_page1.exists() and not cover_page_path:
        img_page1 = model_dir / "1.png"
    img_page2 = model_dir / "2.png"
    img_page99 = model_dir / "99.png"
    img_page100 = model_dir / "100.png"
    if not img_page100.exists():
        img_page100 = model_dir / "1.png"

    width, height = A4  # 595.27 x 841.89 points

    # --- A. Préparation du PDF original exact du Compte Rendu ---
    cr_pdf_path = ""
    docx_path = cr_record.get("chemin_docx") or ""
    if docx_path and os.path.exists(docx_path):
        candidate_pdf = docx_path.rsplit(".", 1)[0] + ".pdf"
        if not os.path.exists(candidate_pdf) or os.path.getmtime(docx_path) > os.path.getmtime(candidate_pdf):
            convert_docx_to_pdf_windows(docx_path, candidate_pdf)
        if os.path.exists(candidate_pdf):
            cr_pdf_path = candidate_pdf

    # --- B. Génération des pages de garde (Page 1 & Page 2) ---
    temp_dir = tempfile.mkdtemp()
    temp_cover_pdf = os.path.join(temp_dir, "cover.pdf")
    c_cover = canvas.Canvas(temp_cover_pdf, pagesize=A4)

    # PAGE 1 : 1.png + Texte dans le rectangle noir en bas à gauche
    if img_page1.exists():
        c_cover.drawImage(str(img_page1), 0, 0, width=width, height=height)

    c_cover.setFillColor(colors.HexColor("#0c1a10"))
    c_cover.setFont("Helvetica-Bold", 12.5)

    # Coordonnées calées dans le rectangle noir du modèle 1.png
    rect_x = 65
    c_cover.drawString(rect_x, 165, f"Patient : {patient_info.get('nom_prenom', '')}")
    c_cover.drawString(rect_x, 135, f"Date : {patient_info.get('date_examen', '')}")
    c_cover.drawString(rect_x, 105, f"Type Examen : {patient_info.get('type_examen', '')}")
    c_cover.showPage()

    # PAGE 2 : 2.png
    if img_page2.exists():
        c_cover.drawImage(str(img_page2), 0, 0, width=width, height=height)
    c_cover.showPage()
    c_cover.save()

    # --- C. Génération des pages d'images selon le type propre à chaque cliché ---
    temp_echo_pdf = os.path.join(temp_dir, "echos.pdf")
    valid_echoes = []
    for item in selected_echo_paths:
        path = item.get("path") if isinstance(item, dict) else item
        image_type = str(item.get("type_image") or image_layout) if isinstance(item, dict) else image_layout
        if path and os.path.exists(path):
            valid_echoes.append({"path": str(path), "type_image": image_type})

    if valid_echoes:
        c_echo = canvas.Canvas(temp_echo_pdf, pagesize=A4)
        i = 0
        while i < len(valid_echoes):
            current_type = valid_echoes[i]["type_image"]
            images_per_page = 1 if current_type == "radiologique" else 3 if current_type == "planche_trois" else 2
            batch = []
            while i < len(valid_echoes) and len(batch) < images_per_page and valid_echoes[i]["type_image"] == current_type:
                batch.append(valid_echoes[i]["path"])
                i += 1

            if current_type == "radiologique":
                try:
                    margin = 5 * mm
                    c_echo.drawImage(
                        batch[0], margin, margin,
                        width=width - 2 * margin, height=height - 2 * margin,
                        preserveAspectRatio=True, anchor="c"
                    )
                except Exception as e:
                    logger.error("Erreur insertion image radiologique dans le livret : %s", e)
                c_echo.showPage()
                continue

            if current_type == "planche_trois":
                c_echo.setFillColor(colors.HexColor("#0f172a"))
                c_echo.setFont("Helvetica-Bold", 10.5)
                c_echo.drawString(30, height - 28, f"PLANCHE A3 — {patient_info.get('nom_prenom', '')}")
                slot_height = (height - 70) / 3
                for index, image_path in enumerate(batch):
                    slot_y = height - 35 - ((index + 1) * slot_height)
                    try:
                        c_echo.drawImage(image_path, 30, slot_y, width=width - 60, height=slot_height - 4,
                                         preserveAspectRatio=True, anchor="c")
                    except Exception as e:
                        logger.error("Erreur insertion d'une image dans la planche à trois : %s", e)
                c_echo.showPage()
                continue

            # Deux clichés échographiques par page, dans une zone A4 avec 1 cm de marge.
            margin = 10 * mm
            gap = 4 * mm
            img_w = width - 2 * margin
            img_h = (height - 2 * margin - gap) / 2
            positions = [
                (margin, height - margin - img_h),
                (margin, margin),
            ]

            for idx_img, img_path in enumerate(batch):
                pos_x, pos_y = positions[idx_img]
                try:
                    c_echo.drawImage(
                        img_path,
                        pos_x,
                        pos_y,
                        width=img_w,
                        height=img_h,
                        preserveAspectRatio=True,
                        anchor='c'
                    )
                except Exception as e_draw:
                    logger.warning("Erreur dessin cliché : %s", e_draw)

            c_echo.showPage()
        c_echo.save()
    # --- D. Génération des pages finales (99.png & 100.png) ---
    temp_end_pdf = os.path.join(temp_dir, "end.pdf")
    c_end = canvas.Canvas(temp_end_pdf, pagesize=A4)

    if img_page99.exists():
        c_end.drawImage(str(img_page99), 0, 0, width=width, height=height)
        c_end.showPage()

    if img_page100.exists():
        c_end.drawImage(str(img_page100), 0, 0, width=width, height=height)
        c_end.showPage()
    c_end.save()

    # --- E. Assemblage vectoriel avec pypdf ---
    try:
        import pypdf
        writer = pypdf.PdfWriter()

        # 1. Pages 1 & 2
        for page in pypdf.PdfReader(temp_cover_pdf).pages:
            writer.add_page(page)

        # 2. Pages du Compte Rendu original (100% identique au document Word)
        if cr_pdf_path and os.path.exists(cr_pdf_path):
            for page in pypdf.PdfReader(cr_pdf_path).pages:
                writer.add_page(page)

        # 3. Pages des clichés échographiques agrandis
        if valid_echoes and os.path.exists(temp_echo_pdf):
            for page in pypdf.PdfReader(temp_echo_pdf).pages:
                writer.add_page(page)

        # 4. Pages 99.png et 100.png
        for page in pypdf.PdfReader(temp_end_pdf).pages:
            writer.add_page(page)

        with open(output_pdf_path, "wb") as f_out:
            writer.write(f_out)

        shutil.rmtree(temp_dir, ignore_errors=True)
        return True
    except Exception as err_merge:
        logger.error("Erreur fusion pypdf : %s", err_merge, exc_info=True)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return False

@dme_router.get("/admission-document/{patient_id}/{passage_id}/{document_index}")
async def api_admission_document_pdf(patient_id: int, passage_id: int, document_index: int):
    """Renvoie un document scanné à l'admission au format PDF, sans exposer les autres passages."""
    if document_index < 0:
        raise HTTPException(status_code=404, detail="Document introuvable.")
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT documents_scannes FROM patient_passages WHERE id = %s AND id_patient = %s;",
            (passage_id, patient_id)
        )
        row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Passage introuvable.")
    raw_docs = row.get("documents_scannes")
    docs = raw_docs if isinstance(raw_docs, list) else json.loads(raw_docs or "[]")
    if document_index >= len(docs) or not isinstance(docs[document_index], dict):
        raise HTTPException(status_code=404, detail="Document introuvable.")

    import base64
    import io
    data_uri = str(docs[document_index].get("data") or "")
    if "," not in data_uri or not data_uri.startswith("data:"):
        raise HTTPException(status_code=415, detail="Le document d'admission n'est pas disponible.")
    header, encoded = data_uri.split(",", 1)
    try:
        file_bytes = base64.b64decode(encoded)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Le document scanné est illisible.") from exc

    if "application/pdf" in header.lower() or file_bytes.startswith(b"%PDF-"):
        pdf_bytes = file_bytes
    elif header.lower().startswith("data:image/"):
        try:
            image = Image.open(io.BytesIO(file_bytes))
            frames = []
            try:
                for frame in range(getattr(image, "n_frames", 1)):
                    image.seek(frame)
                    frames.append(image.convert("RGB"))
                output = io.BytesIO()
                frames[0].save(output, format="PDF", save_all=True, append_images=frames[1:], resolution=150)
                pdf_bytes = output.getvalue()
            finally:
                image.close()
        except Exception as exc:
            raise HTTPException(status_code=422, detail="L'image scannée ne peut pas être convertie en PDF.") from exc
    else:
        raise HTTPException(status_code=415, detail="Format de document d'admission non pris en charge.")

    filename = f"document_admission_{passage_id}_{document_index + 1}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'}
    )


LIVRET_COVER_DIR = BASE_DIR / "uploads" / "model_templates" / "Page_De_Garde"
LIVRET_COVER_EXTENSIONS = {".png", ".jpg", ".jpeg"}


def _resolve_livret_cover(filename: str) -> Path:
    if not filename or Path(filename).name != filename:
        raise HTTPException(status_code=400, detail="Page de garde invalide.")
    cover_dir = LIVRET_COVER_DIR.resolve()
    candidate = (cover_dir / filename).resolve()
    if candidate.parent != cover_dir or candidate.suffix.lower() not in LIVRET_COVER_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Page de garde invalide.")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Page de garde introuvable.")
    return candidate


@dme_router.get("/livret/covers")
async def api_list_livret_covers(request: Request):
    user = get_current_user_optional(request)
    if not user or user.get("status") != "Actif":
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    with get_db_cursor() as cursor:
        cursor.execute("SELECT default_livret_cover FROM users WHERE id = %s;", (user["id"],))
        preference = cursor.fetchone()
    user_default = str((preference or {}).get("default_livret_cover") or "").strip()
    cover_dir = LIVRET_COVER_DIR
    if not cover_dir.is_dir():
        return {"covers": [], "default": "", "user_default": ""}
    files = [p.name for p in cover_dir.iterdir() if p.is_file() and p.suffix.lower() in LIVRET_COVER_EXTENSIONS]
    files.sort(key=lambda name: [(0, int(part)) if part.isdigit() else (1, part.casefold()) for part in re.split(r"(\d+)", name)])
    if user_default not in files:
        user_default = ""
    fallback_default = "1.png" if "1.png" in files else (files[0] if files else "")
    return {
        "covers": [
            {"filename": name, "url": f"/api/dme/livret/covers/{name}"}
            for name in files
        ],
        "default": user_default or fallback_default,
        "user_default": user_default,
    }


@dme_router.post("/livret/covers/default")
async def api_save_default_livret_cover(payload: Dict[str, Any], request: Request):
    user = get_current_user_optional(request)
    if not user or user.get("status") != "Actif":
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    cover_name = str(payload.get("filename") or "").strip()
    _resolve_livret_cover(cover_name)
    with get_db_cursor() as cursor:
        cursor.execute("UPDATE users SET default_livret_cover = %s WHERE id = %s;", (cover_name, user["id"]))
    return {"success": True, "default": cover_name}


@dme_router.get("/livret/covers/{filename}")
async def api_get_livret_cover(filename: str, request: Request):
    user = get_current_user_optional(request)
    if not user or user.get("status") != "Actif":
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    path = _resolve_livret_cover(filename)
    return FileResponse(path, media_type="image/png" if path.suffix.lower() == ".png" else "image/jpeg")


@dme_router.post("/livret/create")
async def api_create_livret(payload: Dict[str, Any], request: Request):
    """Génère le livret PDF multipage et l'archive dans patient_cr_records."""
    dme_manager.ensure_dme_schema()

    patient_id = payload.get("id_patient")
    passage_id = payload.get("id_passage")
    require_consultation_unlocked(passage_id)
    with get_db_cursor() as cursor:
        cursor.execute("SELECT actes_json FROM patient_passages WHERE id = %s AND id_patient = %s;", (passage_id, patient_id))
        passage_row = cursor.fetchone()
        if not passage_row:
            raise HTTPException(status_code=404, detail="Le passage ne correspond pas au patient sélectionné.")
    raw_passage_acts = passage_row.get("actes_json")
    if isinstance(raw_passage_acts, list):
        passage_acts = raw_passage_acts
    else:
        try:
            passage_acts = json.loads(raw_passage_acts or "[]")
        except (TypeError, ValueError):
            passage_acts = []
    group_id = str(payload.get("groupe_id") or "").strip()
    cr_id = payload.get("cr_id")
    try:
        selected_echo_ids = list(dict.fromkeys(int(value) for value in (payload.get("selected_echo_ids") or [])))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Sélection de clichés invalide.")
    cover_name = str(payload.get("page_de_garde") or "").strip()
    if not cover_name:
        raise HTTPException(status_code=400, detail="Choisissez une page de garde pour le livret.")
    cover_path = _resolve_livret_cover(cover_name)
    if not group_id and selected_echo_ids:
        raise HTTPException(status_code=400, detail="Choisissez le groupe contenant les images à intégrer au livret.")

    nom_prenom = str(payload.get("nom_prenom") or "").strip().upper()
    date_examen = str(payload.get("date_examen") or "").strip()
    try:
        prestation_id = int(payload.get("prestation_id") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Sélectionnez une prestation valide pour le livret.")
    if not prestation_id:
        raise HTTPException(status_code=400, detail="Sélectionnez une prestation pour le livret.")
    with get_db_cursor() as cursor:
        cursor.execute("SELECT id, designation, categorie, specialite FROM consultation_types WHERE id = %s;", (prestation_id,))
        selected_prestation = cursor.fetchone()
    if not selected_prestation or not _is_livret_prestation_allowed(dict(selected_prestation)):
        raise HTTPException(status_code=400, detail="Cette prestation ne peut pas être sélectionnée pour un livret.")
    prestation_name = str(selected_prestation.get("designation") or "").strip()
    current_consultation_names = {
        _normalize_prestation_name(act.get("nom") or act.get("designation") or act.get("name"))
        for act in passage_acts if isinstance(act, dict)
    }
    if _normalize_prestation_name(prestation_name) not in current_consultation_names:
        raise HTTPException(status_code=400, detail="La prestation choisie ne fait pas partie de la consultation courante.")
    type_examen = str(payload.get("type_examen") or "").strip()[:200]
    if not type_examen:
        raise HTTPException(status_code=400, detail="Saisissez le type d’examen du livret.")


    cr = dme_manager.get_cr_record_by_id(cr_id) if cr_id else {}
    if not cr:
        raise HTTPException(status_code=404, detail="Compte rendu de référence introuvable.")
    if int(cr.get("id_patient") or 0) != int(patient_id) or int(cr.get("id_passage") or 0) != int(passage_id):
        raise HTTPException(status_code=404, detail="Le compte rendu ne correspond pas au patient et au passage sélectionnés.")

    # Le groupe peut contenir plusieurs types; chaque cliché conserve son type pour sa mise en page.
    echo_images = []
    if group_id:
        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM patient_echographies WHERE groupe_id = %s AND id_patient = %s AND id_passage = %s LIMIT 1;",
                (group_id, patient_id, passage_id)
            )
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Le groupe d’images ne correspond pas à ce patient et ce passage.")
    if selected_echo_ids:
        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT id, chemin_image, type_image, groupe_id FROM patient_echographies WHERE id = ANY(%s) AND id_patient = %s AND id_passage = %s AND groupe_id = %s;",
                (selected_echo_ids, patient_id, passage_id, group_id)
            )
            selected_rows = cursor.fetchall()
            rows_by_id = {int(row["id"]): row for row in selected_rows}
            if len(rows_by_id) != len(selected_echo_ids):
                raise HTTPException(status_code=400, detail="Une ou plusieurs images ne correspondent pas au groupe choisi.")
            echo_images = [
                {"path": rows_by_id[image_id]["chemin_image"], "type_image": str(rows_by_id[image_id].get("type_image") or "echographique").lower()}
                for image_id in selected_echo_ids
            ]

    # Dossier de sauvegarde du Livret
    patient_folder = Path(BASE_DIR) / "uploads" / "dossiers_patients" / str(patient_id)
    patient_folder.mkdir(parents=True, exist_ok=True)

    rand_suffix = random.randint(100000, 999999)
    clean_nom = re.sub(r'[^\w\-]', '_', nom_prenom)
    filename = f"LIVRET_{clean_nom}_{rand_suffix}.pdf"
    output_pdf = str(patient_folder / filename)

    patient_info = {
        "nom_prenom": nom_prenom,
        "date_examen": date_examen,
        "type_examen": type_examen,
    }

    success = generate_livret_pdf_file(output_pdf, patient_info, cr, echo_images, str(cover_path), "IMAGES DU GROUPE", "echographique")
    if not success:
        raise HTTPException(status_code=500, detail="Échec lors de l'assemblage du livret PDF.")

    now_str = datetime.now().strftime("%d/%m/%Y à %H:%M")
    user = get_current_user_optional(request)
    user_nom = f"{user['prenom']} {user['nom']}" if user else "Dr. Praticien"
    draft_token = uuid.uuid4().hex
    with get_db_cursor() as cursor:
        cursor.execute("""
                       INSERT INTO patient_livret_drafts
                       (token, id_patient, id_passage, template_id, titre, modalite,
                        medecin_prescripteur, medecin_signataire, proprietaire,
                        chemin_pdf, nom_fichier, date_heure_str)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                       """, (
                           draft_token, patient_id, passage_id, cr.get("template_id"), f"Livret - {type_examen}",
                           cr.get("modalite", "Échographie"), cr.get("medecin_prescripteur", ""),
                           cr.get("medecin_signataire", ""), user_nom,
                           output_pdf, filename, now_str
                       ))

    return {
        "success": True,
        "message": f"Aperçu du livret '{filename}' prêt à être validé.",
        "draft_token": draft_token
    }


@dme_router.get("/livret/preview/{draft_token}")
async def api_preview_livret_draft(draft_token: str):
    with get_db_cursor() as cursor:
        cursor.execute("SELECT id_patient, chemin_pdf, nom_fichier FROM patient_livret_drafts WHERE token = %s;", (draft_token,))
        draft = cursor.fetchone()
    if not draft:
        raise HTTPException(status_code=404, detail="Aperçu de livret introuvable ou déjà quitté.")
    patient_folder = os.path.realpath(os.path.join(str(BASE_DIR), "uploads", "dossiers_patients", str(draft["id_patient"])))
    pdf_path = os.path.realpath(str(draft["chemin_pdf"] or ""))
    try:
        safe_path = os.path.commonpath([patient_folder, pdf_path]) == patient_folder
    except ValueError:
        safe_path = False
    if not safe_path or not os.path.isfile(pdf_path):
        raise HTTPException(status_code=404, detail="Le fichier d’aperçu du livret est introuvable.")
    return FileResponse(pdf_path, media_type="application/pdf", headers={"Content-Disposition": f"inline; filename=\"{os.path.basename(draft['nom_fichier'])}\"", "Cache-Control": "no-cache"})


@dme_router.post("/livret/validate")
async def api_validate_livret_draft(payload: Dict[str, Any]):
    token = str(payload.get("draft_token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Brouillon de livret manquant.")
    with get_db_cursor() as cursor:
        cursor.execute("SELECT * FROM patient_livret_drafts WHERE token = %s FOR UPDATE;", (token,))
        draft = cursor.fetchone()
        if not draft:
            raise HTTPException(status_code=404, detail="Ce brouillon de livret n’existe plus.")
        require_consultation_unlocked(draft["id_passage"], draft["id_patient"])
        if not os.path.isfile(str(draft["chemin_pdf"])):
            raise HTTPException(status_code=404, detail="Le fichier du livret est introuvable.")
        cursor.execute("""
            INSERT INTO patient_cr_records
            (id_patient, id_passage, template_id, titre, type_modele, modalite,
             medecin_prescripteur, medecin_signataire, proprietaire, contenu_html,
             chemin_docx, nom_fichier, date_heure_str)
            VALUES (%s, %s, %s, %s, 'Livret', %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id;
        """, (
            draft["id_patient"], draft["id_passage"], draft["template_id"], draft["titre"],
            draft["modalite"], draft["medecin_prescripteur"], draft["medecin_signataire"],
            draft["proprietaire"], "", draft["chemin_pdf"], draft["nom_fichier"], draft["date_heure_str"]
        ))
        livret_id = cursor.fetchone()["id"]
        cursor.execute("DELETE FROM patient_livret_drafts WHERE token = %s;", (token,))
    return {"success": True, "livret_id": livret_id, "message": "Livret validé et ajouté aux archives."}


@dme_router.post("/livret/discard")
async def api_discard_livret_draft(payload: Dict[str, Any]):
    token = str(payload.get("draft_token") or "").strip()
    if not token:
        return {"success": True, "discarded": False}
    with get_db_cursor() as cursor:
        cursor.execute("SELECT id_patient, chemin_pdf FROM patient_livret_drafts WHERE token = %s;", (token,))
        draft = cursor.fetchone()
        if draft:
            cursor.execute("DELETE FROM patient_livret_drafts WHERE token = %s;", (token,))
    if draft:
        patient_folder = os.path.realpath(os.path.join(str(BASE_DIR), "uploads", "dossiers_patients", str(draft["id_patient"])))
        pdf_path = os.path.realpath(str(draft["chemin_pdf"] or ""))
        try:
            if os.path.commonpath([patient_folder, pdf_path]) == patient_folder and os.path.isfile(pdf_path):
                os.remove(pdf_path)
        except (OSError, ValueError):
            logger.warning("Impossible de supprimer le fichier du brouillon de livret %s", token)
    return {"success": True, "discarded": bool(draft)}

def replace_docx_variables_robust(docx_path: str, replacements: Dict[str, str]) -> bool:
    """Remplace les variables texte dans un document Word même si elles sont morcelées dans le XML."""
    if not os.path.exists(docx_path):
        return False

    # 1. Passe via python-docx si installé
    try:
        import docx
        doc = docx.Document(docx_path)

        def process_p(p):
            for k, v in replacements.items():
                if k in p.text:
                    replaced = False
                    for r in p.runs:
                        if k in r.text:
                            r.text = r.text.replace(k, v)
                            replaced = True
                    if not replaced and len(p.runs) > 0:
                        full_t = "".join(r.text for r in p.runs)
                        if k in full_t:
                            full_t = full_t.replace(k, v)
                            p.runs[0].text = full_t
                            for r in p.runs[1:]:
                                r.text = ""

        for p in doc.paragraphs:
            process_p(p)
        for t in doc.tables:
            for row in t.rows:
                for cell in row.cells:
                    for p in cell.paragraphs:
                        process_p(p)
        for s in doc.sections:
            for p in s.header.paragraphs:
                process_p(p)
            for p in s.footer.paragraphs:
                process_p(p)

        doc.save(docx_path)
    except Exception as err_docx:
        logger.warning("python-docx notice : %s", err_docx)

    # 2. Passe universelle regex XML dans le package ZIP
    try:
        temp_zip = docx_path + ".temp"
        with zipfile.ZipFile(docx_path, 'r') as zin:
            with zipfile.ZipFile(temp_zip, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    content = zin.read(item.filename)
                    if item.filename.startswith("word/") and item.filename.endswith(".xml"):
                        xml_str = content.decode("utf-8", errors="ignore")

                        def xml_regex_replace(match):
                            var_tag = "${" + match.group(1) + "}"
                            return replacements.get(var_tag, match.group(0))

                        regex_split = re.compile(r'\$(?:<[^>]+>)*\s*\{(?:<[^>]+>)*\s*([a-zA-Z0-9_]+)\s*(?:<[^>]+>)*\}')
                        xml_str = regex_split.sub(xml_regex_replace, xml_str)

                        for k, v in replacements.items():
                            xml_str = xml_str.replace(k, v)

                        content = xml_str.encode("utf-8")
                    zout.writestr(item, content)

        shutil.move(temp_zip, docx_path)
        return True
    except Exception as err_zip:
        logger.error("Erreur remplacement XML docx : %s", err_zip)
        return False


# ============================================================================
# LOGIQUE MÉTIER POSTGRESQL AVEC AUTO-MIGRATION
# ============================================================================
class DmeManager:
    """Gestionnaire de persistance et de synchronisation du DME."""

    @staticmethod
    def ensure_dme_schema():
        """Vérifie et met à jour automatiquement les colonnes et tables."""
        colonnes_cr = [
            "id_patient INT",
            "id_passage INT",
            "id_consultation INT",
            "template_id INT",
            "titre TEXT DEFAULT ''",
            "type_modele TEXT DEFAULT 'Word'",
            "modalite TEXT DEFAULT ''",
            "medecin_prescripteur TEXT DEFAULT ''",
            "medecin_signataire TEXT DEFAULT ''",
            "proprietaire TEXT DEFAULT 'admin'",
            "contenu_html TEXT DEFAULT ''",
            "chemin_docx TEXT DEFAULT ''",
            "nom_fichier TEXT DEFAULT ''",
            "date_heure_str TEXT DEFAULT ''"
        ]

        try:
            with get_db_cursor() as cursor:
                # 1. Consultation clinique
                cursor.execute("""
                               CREATE TABLE IF NOT EXISTS patient_dme_records
                               (
                                   id
                                   SERIAL
                                   PRIMARY
                                   KEY,
                                   id_patient
                                   INT
                                   NOT
                                   NULL,
                                   id_passage
                                   INT
                                   NOT
                                   NULL,
                                   praticien
                                   TEXT
                                   DEFAULT
                                   'Dr',
                                   constantes_json
                                   JSONB
                                   DEFAULT
                                   '{}',
                                   allergies_intolerances
                                   TEXT
                                   DEFAULT
                                   '',
                                   traitement_en_cours
                                   TEXT
                                   DEFAULT
                                   '',
                                   antecedents_medicaux
                                   TEXT
                                   DEFAULT
                                   '',
                                   antecedents_chirurgicaux
                                   TEXT
                                   DEFAULT
                                   '',
                                   notes_habitudes
                                   TEXT
                                   DEFAULT
                                   '',
                                   motif
                                   TEXT
                                   DEFAULT
                                   '',
                                   examen_clinique
                                   TEXT
                                   DEFAULT
                                   '',
                                   diagnostic
                                   TEXT
                                   DEFAULT
                                   '',
                                   ordonnance_text
                                   TEXT
                                   DEFAULT
                                   '',
                                   conduite_a_tenir
                                   TEXT
                                   DEFAULT
                                   '',
                                   remarques
                                   TEXT
                                   DEFAULT
                                   '',
                                   date_consultation
                                   TIMESTAMP
                                   WITH
                                   TIME
                                   ZONE
                                   DEFAULT
                                   CURRENT_TIMESTAMP,
                                   verrouille BOOLEAN NOT NULL DEFAULT TRUE
                               );
                               """)

                # 2. Comptes rendus
                cursor.execute("""
                               CREATE TABLE IF NOT EXISTS patient_cr_records
                               (
                                   id
                                   SERIAL
                                   PRIMARY
                                   KEY,
                                   id_patient
                                   INT
                                   NOT
                                   NULL,
                                   id_passage
                                   INT,
                                   titre
                                   TEXT
                                   DEFAULT
                                   '',
                                   created_at
                                   TIMESTAMP
                                   WITH
                                   TIME
                                   ZONE
                                   DEFAULT
                                   CURRENT_TIMESTAMP
                               );
                               """)

                # 3. Ordonnances
                cursor.execute("""
                               CREATE TABLE IF NOT EXISTS patient_prescriptions
                               (
                                   id
                                   SERIAL
                                   PRIMARY
                                   KEY,
                                   id_patient
                                   INT
                                   NOT
                                   NULL,
                                   id_passage
                                   INT,
                                   id_consultation INT REFERENCES patient_dme_records(id) ON DELETE CASCADE,
                                   medecin_nom
                                   TEXT
                                   DEFAULT
                                   '',
                                   medecin_specialite
                                   TEXT
                                   DEFAULT
                                   '',
                                   lignes_json
                                   JSONB
                                   DEFAULT
                                   '[]',
                                   statut
                                   TEXT
                                   DEFAULT
                                   'Valide',
                                   supprime_par
                                   TEXT
                                   DEFAULT
                                   '',
                                   date_suppression
                                   TIMESTAMP
                                   WITH
                                   TIME
                                   ZONE,
                                   created_at
                                   TIMESTAMP
                                   WITH
                                   TIME
                                   ZONE
                                   DEFAULT
                                   CURRENT_TIMESTAMP
                               );
                               """)

                # 4. Table des clichés échographiques (PNG)
                cursor.execute("""
                               CREATE TABLE IF NOT EXISTS patient_echographies
                               (
                                   id
                                   SERIAL
                                   PRIMARY
                                   KEY,
                                   id_patient
                                   INT
                                   NOT
                                   NULL,
                                   id_passage
                                   INT
                                   NOT
                                   NULL,
                                   chemin_image
                                   TEXT
                                   NOT
                                   NULL,
                                   nom_fichier
                                   TEXT
                                   DEFAULT
                                   '',
                                   titre
                                   TEXT
                                   DEFAULT
                                   '',
                                   date_heure_str
                                   TEXT
                                   DEFAULT
                                   '',
                                   created_at
                                   TIMESTAMP
                                   WITH
                                   TIME
                                   ZONE
                                   DEFAULT
                                   CURRENT_TIMESTAMP
                               );
                               """)
                cursor.execute("ALTER TABLE patient_echographies ADD COLUMN IF NOT EXISTS type_image TEXT NOT NULL DEFAULT 'echographique';")
                cursor.execute("ALTER TABLE patient_echographies ADD COLUMN IF NOT EXISTS categorie TEXT NOT NULL DEFAULT 'Général';")
                cursor.execute("ALTER TABLE patient_echographies ADD COLUMN IF NOT EXISTS ordre_affichage INTEGER NOT NULL DEFAULT 0;")
                cursor.execute("ALTER TABLE patient_echographies ADD COLUMN IF NOT EXISTS groupe_id TEXT;")
                cursor.execute("""
                    UPDATE patient_echographies
                    SET groupe_id = CONCAT('legacy_', MD5(CONCAT(id_patient, ':', id_passage, ':', COALESCE(NULLIF(type_image, ''), 'echographique'), ':', COALESCE(NULLIF(categorie, ''), 'Général'))))
                    WHERE groupe_id IS NULL OR BTRIM(groupe_id) = '';
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS patient_livret_drafts (
                        token TEXT PRIMARY KEY,
                        id_patient INT NOT NULL,
                        id_passage INT NOT NULL,
                        template_id INT,
                        titre TEXT NOT NULL,
                        modalite TEXT DEFAULT '',
                        medecin_prescripteur TEXT DEFAULT '',
                        medecin_signataire TEXT DEFAULT '',
                        proprietaire TEXT DEFAULT '',
                        chemin_pdf TEXT NOT NULL,
                        nom_fichier TEXT NOT NULL,
                        date_heure_str TEXT DEFAULT '',
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)
        except Exception:
            pass

        # Migration des liens d'ordonnance vers la consultation clinique précise.
        # Le défaut TRUE conserve le comportement verrouillé des anciennes fiches DME;
        # le démarrage d'une nouvelle consultation crée explicitement un brouillon non verrouillé.
        try:
            with get_db_cursor() as cursor:
                cursor.execute("ALTER TABLE patient_dme_records ADD COLUMN IF NOT EXISTS verrouille BOOLEAN NOT NULL DEFAULT TRUE;")
                cursor.execute("ALTER TABLE patient_dme_records ADD COLUMN IF NOT EXISTS specialite_consultation_json JSONB NOT NULL DEFAULT '{}'::jsonb;")
                cursor.execute("ALTER TABLE patient_dme_records ADD COLUMN IF NOT EXISTS reset_history_json JSONB NOT NULL DEFAULT '[]'::jsonb;")
                cursor.execute("ALTER TABLE patient_dme_records ADD COLUMN IF NOT EXISTS note_reprise TEXT NOT NULL DEFAULT ''; ")
                cursor.execute("ALTER TABLE patient_dme_records ADD COLUMN IF NOT EXISTS note_medecin TEXT NOT NULL DEFAULT ''; ")
                cursor.execute("ALTER TABLE patient_prescriptions ADD COLUMN IF NOT EXISTS id_consultation INT REFERENCES patient_dme_records(id) ON DELETE CASCADE;")
                cursor.execute("""
                    UPDATE patient_prescriptions AS ordonnance
                    SET id_consultation = consultation.id
                    FROM (
                        SELECT DISTINCT ON (id_patient, id_passage) id, id_patient, id_passage
                        FROM patient_dme_records
                        ORDER BY id_patient, id_passage, id DESC
                    ) AS consultation
                    WHERE ordonnance.id_consultation IS NULL
                      AND ordonnance.id_patient = consultation.id_patient
                      AND ordonnance.id_passage = consultation.id_passage;
                """)
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_patient_prescriptions_scope ON patient_prescriptions (id_patient, id_passage, id_consultation);")
        except Exception as exc:
            logger.warning("Migration du rattachement consultation des ordonnances impossible : %s", exc)

        # Ajout des colonnes sécurisé
        for col_def in colonnes_cr:
            try:
                with get_db_cursor() as cursor:
                    cursor.execute(f"ALTER TABLE patient_cr_records ADD COLUMN IF NOT EXISTS {col_def};")
            except Exception:
                pass

    def is_consultation_locked(self, passage_id: int) -> bool:
        self.ensure_dme_schema()
        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT verrouille AS locked FROM patient_dme_records WHERE id_passage = %s ORDER BY id DESC LIMIT 1;",
                (passage_id,)
            )
            row = cursor.fetchone()
        return bool(row and row.get("locked", True))

    def get_patient_dme_data(self, patient_id: int, passage_id: int) -> Dict[str, Any]:
        """Récupère l'ensemble des données du patient et de sa consultation."""
        self.ensure_dme_schema()

        # Rattrapage des anciens comptes rendus orphelins si présents
        try:
            with get_db_cursor() as cursor:
                cursor.execute("""
                               UPDATE patient_cr_records
                               SET id_passage = %s
                               WHERE id_patient = %s
                                 AND (id_passage IS NULL OR id_passage = 0);
                               """, (passage_id, patient_id))
        except Exception:
            pass

        with get_db_cursor() as cursor:
            # 1. Patient
            cursor.execute("""
                           SELECT id,
                                  nom,
                                  prenom,
                                  date_naissance,
                                  genre,
                                  telephone_1,
                                  telephone_2,
                                  email,
                                  wilaya,
                                  commune,
                                  groupe_sanguin,
                                  avatar,
                                  photo_face
                           FROM patients
                           WHERE id = %s;
                           """, (patient_id,))
            patient = cursor.fetchone()

            # 2. Passage
            cursor.execute("""
                           SELECT p.id as id_passage,
                                  p.date_passage,
                                  p.heure_passage,
                                  p.type_passage,
                                  p.statut,
                                  p.actes_json,
                p.questionnaire_json,
                                  p.montant_total,
                                  p.remise,
                                  p.versement,
                                  p.reste,
                                  p.medecin_traitant_id,
                                  p.consultation_utilisateur,
                                  p.questionnaire_json,
                                  p.documents_scannes
                           FROM patient_passages p
                           WHERE p.id = %s;
                           """, (passage_id,))
            passage = cursor.fetchone()

            medecin_info = {"medecin_nom": "", "medecin_prenom": "", "medecin_grade": "Dr.", "medecin_adresse": "-"}
            if passage and passage.get("medecin_traitant_id"):
                try:
                    cursor.execute("""
                                   SELECT nom     as medecin_nom,
                                          prenom  as medecin_prenom,
                                          grade   as medecin_grade,
                                          adresse as medecin_adresse
                                   FROM treating_doctors
                                   WHERE id = %s;
                                   """, (passage["medecin_traitant_id"],))
                    doc = cursor.fetchone()
                    if doc:
                        medecin_info.update(dict(doc))
                except Exception:
                    pass

            # 3. Enregistrement clinique DME
            cursor.execute("""
                           SELECT id,
                                  constantes_json,
                                  allergies_intolerances,
                                  traitement_en_cours,
                                  antecedents_medicaux,
                                  antecedents_chirurgicaux,
                                  notes_habitudes,
                                  motif,
                                  examen_clinique,
                                  diagnostic,
                                  conduite_a_tenir,
                                  specialite_consultation_json,
                                  praticien,
                                  verrouille,
                                  note_reprise,
                                  note_medecin
                           FROM patient_dme_records
                           WHERE id_patient = %s AND id_passage = %s
                           ORDER BY id DESC LIMIT 1;
                           """, (patient_id, passage_id))
            dme_record = cursor.fetchone()
            if dme_record and dme_record.get("verrouille", True) and passage and passage.get("statut") != "Terminé":
                cursor.execute("UPDATE patient_passages SET statut = 'Terminé' WHERE id = %s;", (passage_id,))
                passage["statut"] = "Terminé"

            # 4. Historique des passages
            cursor.execute("""
                           SELECT p.id as id_passage,
                                  p.date_passage,
                                  p.heure_passage,
                                  p.type_passage,
                                  p.statut,
                                  d.diagnostic,
                                  d.praticien,
                                  d.constantes_json
                           FROM patient_passages p
                                    LEFT JOIN LATERAL (
                                        SELECT diagnostic, praticien, constantes_json
                                        FROM patient_dme_records
                                        WHERE id_passage = p.id
                                        ORDER BY id DESC LIMIT 1
                                    ) d ON TRUE
                           WHERE p.id_patient = %s
                           ORDER BY p.id DESC;
                           """, (patient_id,))
            raw_histo = cursor.fetchall()
            historique = []
            for h in raw_histo:
                h_dict = dict(h)
                raw_growth = h_dict.pop("constantes_json", None)
                h_dict["constantes"] = raw_growth if isinstance(raw_growth, dict) else json.loads(raw_growth or "{}")
                raw_date = h_dict.get("date_passage")
                h_dict["date_passage_iso"] = raw_date.isoformat() if hasattr(raw_date, "isoformat") else str(raw_date or "")[:10]
                h_dict["date_passage"] = format_date_display(h_dict.get("date_passage"))
                historique.append(h_dict)

            # 5. Prestations
            try:
                cursor.execute(
                    "SELECT id, designation, categorie, tarif, tarif_nuit, specialite, modalite FROM consultation_types ORDER BY designation ASC;")
                consultations = [dict(r) for r in cursor.fetchall()]
            except Exception:
                consultations = []

            # 6. Modèles CR
            try:
                cursor.execute(
                    "SELECT id, designation, type_modele, modalite, chemin, contenu_html FROM cr_templates ORDER BY designation ASC;")
                cr_templates = [dict(r) for r in cursor.fetchall()]
            except Exception:
                cr_templates = []

            # 7. Radiologues / Staff
            try:
                cursor.execute("""
                               SELECT id, nom, prenom, type_personnel, specialite
                               FROM clinic_staff
                               WHERE actif = TRUE
                                 AND (type_personnel IN ('Docteur', 'Médecin', 'Professeur')
                                      OR specialite ILIKE '%%radio%%')
                               ORDER BY nom ASC;
                               """)
                radiologues = [dict(r) for r in cursor.fetchall()]
            except Exception:
                radiologues = []

            # 8. Comptes rendus archivés STRICTEMENT limités à CE patient et CE passage
            try:
                cursor.execute("""
                               SELECT id,
                                      titre,
                                      type_modele,
                                      modalite,
                                      medecin_signataire,
                                      proprietaire,
                                      date_heure_str,
                                      chemin_docx,
                                      contenu_html,
                                      nom_fichier,
                                      id_passage,
                                      id_consultation
                               FROM patient_cr_records
                               WHERE id_patient = %s
                                 AND id_passage = %s
                               ORDER BY id DESC;
                               """, (patient_id, passage_id))
                archived_crs = [dict(r) for r in cursor.fetchall()]
            except Exception:
                archived_crs = []

            # 9. Clichés Échographiques STRICTEMENT rattachés à ce patient et passage
            try:
                cursor.execute("""
                               SELECT id, chemin_image, nom_fichier, titre, date_heure_str, type_image, categorie, ordre_affichage, groupe_id, created_at
                               FROM patient_echographies
                               WHERE id_patient = %s
                                 AND id_passage = %s
                               ORDER BY created_at, id;
                               """, (patient_id, passage_id))
                echographies = [dict(r) for r in cursor.fetchall()]
            except Exception:
                echographies = []

        if patient:
            patient_data = dict(patient)
            raw_dob = patient_data.get("date_naissance")
            patient_data["date_naissance_iso"] = raw_dob.isoformat() if hasattr(raw_dob, "isoformat") else str(raw_dob or "")[:10]
            patient_data["date_naissance"] = format_date_display(patient_data.get("date_naissance"))
        else:
            patient_data = {
                "id": patient_id, "nom": "Patient", "prenom": f"#{patient_id}",
                "date_naissance": "-", "genre": "Homme", "telephone_1": "-",
                "wilaya": "", "commune": "", "groupe_sanguin": "O+", "avatar": ""
            }

        if passage:
            passage_data = dict(passage)
            raw_passage_date = passage_data.get("date_passage")
            passage_data["date_passage_iso"] = raw_passage_date.isoformat() if hasattr(raw_passage_date, "isoformat") else str(raw_passage_date or "")[:10]
            passage_data["date_passage"] = format_date_display(passage_data.get("date_passage"))
            passage_data.update(medecin_info)
            actes = passage_data.get("actes_json")
            passage_data["actes"] = actes if isinstance(actes, list) else json.loads(actes or "[]")
            modalites_by_act = {
                str(item.get("designation") or "").strip().casefold(): str(item.get("modalite") or "").strip()
                for item in consultations
                if item.get("designation")
            }
            for act in passage_data["actes"]:
                if isinstance(act, dict) and not act.get("modalite"):
                    act_name = str(act.get("nom") or act.get("designation") or "").strip().casefold()
                    if act_name in modalites_by_act:
                        act["modalite"] = modalites_by_act[act_name]
            questionnaire = passage_data.get("questionnaire_json")
            passage_data["questionnaire"] = questionnaire if isinstance(questionnaire, dict) else json.loads(questionnaire or "{}")
            raw_documents = passage_data.pop("documents_scannes", None)
            documents = raw_documents if isinstance(raw_documents, list) else json.loads(raw_documents or "[]")
            passage_data["documents_scannes"] = [
                {"type": doc.get("type") or "Document d'admission", "heure": doc.get("heure") or ""}
                for doc in documents if isinstance(doc, dict)
            ]
        else:
            passage_data = {
                "id_passage": passage_id, "date_passage": datetime.now().strftime("%d/%m/%Y"),
                "heure_passage": datetime.now().strftime("%H:%M"), "type_passage": "Consultation",
                "statut": "En cours", "actes": [], **medecin_info
            }

        record_data = dict(dme_record) if dme_record else {}
        c = record_data.get("constantes_json")
        record_data["constantes"] = c if isinstance(c, dict) else json.loads(c or "{}")
        specialty_data = record_data.get("specialite_consultation_json")
        record_data["donnees_specialisees"] = specialty_data if isinstance(specialty_data, dict) else json.loads(specialty_data or "{}")

        return {
            "patient": patient_data,
            "passage": passage_data,
            "dme": record_data,
            "historique": historique,
            "consultations": consultations,
            "cr_templates": cr_templates,
            "radiologues": radiologues,
            "archived_crs": archived_crs,
            "echographies": echographies,
        }

    def get_archived_consultation(self, patient_id: int, passage_id: int) -> Optional[Dict[str, Any]]:
        """Charge l'archive clinique et les documents rattachés à un seul passage."""
        self.ensure_dme_schema()
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT p.id AS id_passage,
                       p.date_passage,
                       p.heure_passage,
                       p.type_passage,
                       p.statut,
                       p.actes_json,
                       p.questionnaire_json,
                       p.montant_total,
                       p.remise,
                       p.versement,
                       p.reste,
                       d.id AS id_consultation,
                       d.constantes_json,
                       d.allergies_intolerances,
                       d.traitement_en_cours,
                       d.antecedents_medicaux,
                       d.antecedents_chirurgicaux,
                       d.notes_habitudes,
                       d.motif,
                       d.examen_clinique,
                       d.diagnostic,
                       d.conduite_a_tenir,
                       d.specialite_consultation_json,
                       d.praticien
                FROM patient_passages p
                LEFT JOIN LATERAL (
                    SELECT * FROM patient_dme_records
                    WHERE id_passage = p.id
                    ORDER BY id DESC LIMIT 1
                ) d ON TRUE
                WHERE p.id = %s AND p.id_patient = %s;
            """, (passage_id, patient_id))
            consultation_row = cursor.fetchone()
            if not consultation_row:
                return None

            cursor.execute("""
                SELECT id, medecin_nom, medecin_specialite, lignes_json, statut,
                       id_patient, id_passage, id_consultation,
                       TO_CHAR(created_at, 'DD/MM/YYYY HH24:MI') AS date_creation_str
                FROM patient_prescriptions
                WHERE id_patient = %s AND id_passage = %s
                  AND id_consultation IS NOT DISTINCT FROM %s
                ORDER BY id DESC;
            """, (patient_id, passage_id, consultation_row.get("id_consultation")))
            prescriptions = [dict(row) for row in cursor.fetchall()]

            cursor.execute("""
                SELECT id, titre, type_modele, modalite, medecin_signataire,
                       proprietaire, date_heure_str, nom_fichier
                FROM patient_cr_records
                WHERE id_patient = %s AND id_passage = %s
                ORDER BY id DESC;
            """, (patient_id, passage_id))
            archived_documents = [dict(row) for row in cursor.fetchall()]

        consultation = dict(consultation_row)
        consultation["date_passage"] = format_date_display(consultation.get("date_passage"))
        raw_questionnaire = consultation.pop("questionnaire_json", None)
        consultation["questionnaire"] = raw_questionnaire if isinstance(raw_questionnaire, dict) else json.loads(raw_questionnaire or "{}")
        raw_constantes = consultation.pop("constantes_json", None)
        consultation["constantes"] = raw_constantes if isinstance(raw_constantes, dict) else json.loads(raw_constantes or "{}")
        raw_specialty = consultation.pop("specialite_consultation_json", None)
        consultation["donnees_specialisees"] = raw_specialty if isinstance(raw_specialty, dict) else json.loads(raw_specialty or "{}")
        raw_actes = consultation.pop("actes_json", None)
        consultation["actes"] = raw_actes if isinstance(raw_actes, list) else json.loads(raw_actes or "[]")

        for prescription in prescriptions:
            raw_lines = prescription.pop("lignes_json", None)
            prescription["lignes"] = raw_lines if isinstance(raw_lines, list) else json.loads(raw_lines or "[]")

        return {
            "consultation": consultation,
            "prescriptions": prescriptions,
            "documents": archived_documents,
        }

    def save_consultation(self, data: Dict[str, Any], user_fullname: str, *, finalize: bool = True,
                          handoff_note: Optional[str] = None, handoff_doctor: Optional[str] = None) -> Dict[str, Any]:
        """Enregistre les données cliniques et synchronise les actes avec la caisse."""
        self.ensure_dme_schema()

        patient_id = data.get("id_patient")
        passage_id = data.get("id_passage")
        requested_consultation_id = data.get("id_consultation")
        constantes = json.dumps(data.get("constantes", {}))

        allergies = str(data.get("allergies_intolerances") or "").strip()
        traitement = str(data.get("traitement_en_cours") or "").strip()
        ant_med = str(data.get("antecedents_medicaux") or "").strip()
        ant_chir = str(data.get("antecedents_chirurgicaux") or "").strip()
        habitudes = str(data.get("notes_habitudes") or "").strip()
        motif = str(data.get("motif") or "").strip()
        diagnostic = str(data.get("diagnostic") or "").strip()
        conduite = str(data.get("conduite_a_tenir") or "").strip()

        raw_specialty_data = data.get("donnees_specialisees") or {}
        specialty_key = str(raw_specialty_data.get("specialite") or "").strip().lower() if isinstance(raw_specialty_data, dict) else ""
        if specialty_key not in DME_SPECIALTY_FIELDS:
            specialty_key = ""
        raw_sections = raw_specialty_data.get("sections") if isinstance(raw_specialty_data, dict) else {}
        cleaned_sections = {}
        if isinstance(raw_sections, dict):
            for section_key, section_values in raw_sections.items():
                if section_key not in DME_SPECIALTY_FIELDS or not isinstance(section_values, dict):
                    continue
                cleaned_sections[section_key] = {
                    field: str(value or "").strip()[:20000]
                    for field, value in section_values.items()
                    if field in DME_SPECIALTY_FIELDS[section_key]
                }
                if section_key == 'physiotherapie':
                    from modules.physiotherapy_bilan import validate_values
                    try:
                        cleaned_sections[section_key] = validate_values(section_values)
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
                if section_key == 'reeducation_classique':
                    from modules.reeducation_classique import validate_values
                    try:
                        cleaned_sections[section_key] = validate_values(section_values)
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
        specialty_json = json.dumps({"specialite": specialty_key, "sections": cleaned_sections}, ensure_ascii=False)

        actes_list = data.get("actes", [])
        actes_json = json.dumps(actes_list)

        if not patient_id or not passage_id:
            return {"success": False, "message": "Identifiants manquants."}

        with get_db_cursor() as cursor:
            cursor.execute("SELECT id_patient, statut, consultation_utilisateur FROM patient_passages WHERE id = %s FOR UPDATE;", (passage_id,))
            passage_row = cursor.fetchone()
            if not passage_row or passage_row.get("statut") != "En cours":
                return {"success": False, "message": "Le passage doit être en cours pour enregistrer la consultation."}
            if int(passage_row.get("id_patient")) != int(patient_id):
                return {"success": False, "message": "Ce passage ne correspond pas au patient sélectionné."}
            owner = str(passage_row.get("consultation_utilisateur") or "").strip()
            if owner and owner.casefold() != str(user_fullname or "").strip().casefold():
                return {"success": False, "message": f"Consultation en cours par {owner}. Seul son médecin responsable peut la modifier."}

            if requested_consultation_id:
                cursor.execute("""
                    SELECT id, verrouille, praticien FROM patient_dme_records
                    WHERE id = %s AND id_patient = %s AND id_passage = %s
                    FOR UPDATE;
                """, (requested_consultation_id, patient_id, passage_id))
            else:
                cursor.execute("""
                    SELECT id, verrouille, praticien FROM patient_dme_records
                    WHERE id_patient = %s AND id_passage = %s
                    ORDER BY id DESC LIMIT 1 FOR UPDATE;
                """, (patient_id, passage_id))
            existing = cursor.fetchone()

            if requested_consultation_id and not existing:
                return {"success": False, "message": "Cette consultation ne correspond pas au patient et au passage sélectionnés."}

            existing_owner = str((existing or {}).get("praticien") or "").strip()
            if existing_owner and existing_owner.casefold() != str(user_fullname or "").strip().casefold():
                return {"success": False, "message": f"Consultation en cours par {existing_owner}. Seul son médecin responsable peut la modifier."}

            if existing and existing.get("verrouille", True):
                return {"success": False, "message": "Cette consultation est déjà enregistrée et verrouillée en lecture seule."}

            consultation_values = (
                user_fullname, constantes, allergies, traitement, ant_med, ant_chir,
                habitudes, motif, diagnostic, conduite, specialty_json
            )
            if existing:
                consultation_id = existing["id"]
                cursor.execute("""
                    UPDATE patient_dme_records
                    SET praticien = %s, constantes_json = %s::jsonb,
                        allergies_intolerances = %s, traitement_en_cours = %s,
                        antecedents_medicaux = %s, antecedents_chirurgicaux = %s,
                        notes_habitudes = %s, motif = %s, diagnostic = %s, conduite_a_tenir = %s,
                        specialite_consultation_json = %s::jsonb,
                        note_reprise = COALESCE(%s, note_reprise),
                        note_medecin = COALESCE(%s, note_medecin),
                        verrouille = %s,
                        date_consultation = CURRENT_TIMESTAMP
                    WHERE id = %s AND id_patient = %s AND id_passage = %s;
                """, (*consultation_values, handoff_note, handoff_doctor, not finalize, consultation_id, patient_id, passage_id))
            else:
                cursor.execute("""
                    INSERT INTO patient_dme_records
                    (id_patient, id_passage, praticien, constantes_json, allergies_intolerances,
                     traitement_en_cours, antecedents_medicaux, antecedents_chirurgicaux,
                     notes_habitudes, motif, diagnostic, conduite_a_tenir,
                     specialite_consultation_json, verrouille, note_reprise, note_medecin)
                    VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, COALESCE(%s, ''), COALESCE(%s, ''))
                    RETURNING id;
                """, (patient_id, passage_id, *consultation_values, not finalize, handoff_note, handoff_doctor))
                consultation_id = cursor.fetchone()["id"]

            # Attache les anciennes ordonnances du même patient et passage
            # qui n'avaient pas encore de lien vers une fiche clinique.
            cursor.execute("""
                UPDATE patient_prescriptions
                SET id_consultation = %s
                WHERE id_patient = %s AND id_passage = %s AND id_consultation IS NULL;
            """, (consultation_id, patient_id, passage_id))

            cursor.execute("SELECT remise, versement FROM patient_passages WHERE id = %s;", (passage_id,))
            pass_row = cursor.fetchone()

            if pass_row:
                new_montant_total = sum(float(a.get("sous_total", 0.0)) for a in actes_list)
                remise = float(pass_row.get("remise") or 0.0)
                versement = float(pass_row.get("versement") or 0.0)
                net = max(0.0, new_montant_total - remise)
                new_reste = max(0.0, net - versement)
                statut_paiement = "Payé" if new_reste <= 0.01 and net > 0 else (
                    "Partiel" if versement > 0 else "Non payé")

                cursor.execute("""
                               UPDATE patient_passages
                               SET actes_json = %s::jsonb, montant_total = %s, reste = %s, statut_paiement = %s
                               WHERE id = %s;
                               """, (actes_json, new_montant_total, new_reste, statut_paiement, passage_id))

            cursor.execute("UPDATE patient_passages SET statut = %s WHERE id = %s;", ("Terminé" if finalize else "En cours", passage_id))

        return {"success": True,
                "message": "Consultation enregistrée et verrouillée en lecture seule !" if finalize else "Consultation conservée en cours avec la note de reprise.",
                "statut": "Terminé" if finalize else "En cours", "verrouille": finalize,
                "id_consultation": consultation_id, "note_reprise": handoff_note, "note_medecin": handoff_doctor}

    def get_prescriptions_patient(self, patient_id: int, passage_id: int, consultation_id: Optional[int]) -> List[Dict[str, Any]]:
        self.ensure_dme_schema()
        with get_db_cursor() as cursor:
            cursor.execute("""
                           SELECT id,
                                  id_patient,
                                  id_passage,
                                  id_consultation,
                                  medecin_nom,
                                  medecin_specialite,
                                  lignes_json,
                                  statut,
                                  supprime_par,
                                  TO_CHAR(date_suppression, 'YYYY-MM-DD (HH24:MI)') AS date_suppression_str,
                                  TO_CHAR(created_at, 'YYYY-MM-DD (HH24:MI)')       AS date_creation_str
                           FROM patient_prescriptions
                           WHERE id_patient = %s
                             AND id_passage = %s
                             AND id_consultation IS NOT DISTINCT FROM %s
                           ORDER BY id DESC;
                           """, (patient_id, passage_id, consultation_id))
            rows = cursor.fetchall()

        ordonnances = []
        for r in rows:
            item = dict(r)
            lignes = item.get("lignes_json")
            item["lignes"] = lignes if isinstance(lignes, list) else json.loads(lignes or "[]")
            ordonnances.append(item)
        return ordonnances

    def save_prescription(self, data: Dict[str, Any], user_fullname: str, user_role: str) -> Dict[str, Any]:
        self.ensure_dme_schema()
        patient_id = data.get("id_patient")
        passage_id = data.get("id_passage")
        consultation_id = data.get("id_consultation")
        lignes = data.get("lignes", [])
        med_nom = data.get("medecin_nom") or user_fullname
        med_spe = data.get("medecin_specialite") or user_role

        if not patient_id or not passage_id or not consultation_id:
            raise HTTPException(status_code=400, detail="Les identifiants patient, passage et consultation sont obligatoires.")
        if not lignes:
            return {"success": False, "message": "Veuillez ajouter au moins un médicament."}

        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT d.verrouille
                FROM patient_dme_records d
                JOIN patient_passages p ON p.id = d.id_passage AND p.id_patient = d.id_patient
                WHERE d.id = %s AND d.id_patient = %s AND d.id_passage = %s
                FOR UPDATE OF d;
            """, (consultation_id, patient_id, passage_id))
            consultation = cursor.fetchone()
            if not consultation:
                raise HTTPException(status_code=404, detail="La consultation ne correspond pas au patient et au passage sélectionnés.")
            if consultation.get("verrouille", True):
                raise HTTPException(status_code=409, detail="Consultation verrouillée : ordonnance en lecture seule.")
            cursor.execute("""
                           INSERT INTO patient_prescriptions
                           (id_patient, id_passage, id_consultation, medecin_nom, medecin_specialite, lignes_json, statut)
                           VALUES (%s, %s, %s, %s, %s, %s::jsonb, 'Valide') RETURNING id;
                           """, (patient_id, passage_id, consultation_id, med_nom, med_spe, json.dumps(lignes)))
            res = cursor.fetchone()

        return {"success": True, "message": "Ordonnance enregistrée !", "ord_id": res["id"]}

    def soft_delete_prescription(self, ord_id: int, patient_id: int, passage_id: int, consultation_id: Optional[int], user_login: str) -> Dict[str, Any]:
        self.ensure_dme_schema()
        with get_db_cursor() as cursor:
            cursor.execute("""
                           UPDATE patient_prescriptions
                           SET statut           = 'Supprimée',
                               supprime_par     = %s,
                               date_suppression = CURRENT_TIMESTAMP
                           WHERE id = %s AND id_patient = %s AND id_passage = %s AND id_consultation IS NOT DISTINCT FROM %s;
                           """, (user_login, ord_id, patient_id, passage_id, consultation_id))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Ordonnance introuvable dans cette consultation.")
        return {"success": True, "message": "Ordonnance marquée comme supprimée."}

    def get_prescription_by_id(self, ord_id: int, patient_id: int, passage_id: int, consultation_id: Optional[int]) -> Optional[Dict[str, Any]]:
        self.ensure_dme_schema()
        with get_db_cursor() as cursor:
            cursor.execute("""
                           SELECT o.*,
                                  TO_CHAR(o.created_at, 'YYYY-MM-DD') as date_ord,
                                  p.nom,
                                  p.prenom,
                                  p.date_naissance,
                                  p.genre,
                                  p.wilaya,
                                  p.commune
                           FROM patient_prescriptions o
                                    JOIN patients p ON p.id = o.id_patient
                           WHERE o.id = %s
                             AND o.id_patient = %s
                             AND o.id_passage = %s
                             AND o.id_consultation IS NOT DISTINCT FROM %s;
                           """, (ord_id, patient_id, passage_id, consultation_id))
            row = cursor.fetchone()

        if not row:
            return None
        item = dict(row)
        lignes = item.get("lignes_json")
        item["lignes"] = lignes if isinstance(lignes, list) else json.loads(lignes or "[]")
        item["age_str"] = calculate_age_str(item.get("date_naissance"))
        item["date_naissance"] = format_date_display(item.get("date_naissance"))
        return item

    # =========================================================================
    # GESTION DES COMPTES RENDUS : COPIE, RENOMMAGE (6 CHIFFRES) & REMPLACEMENT
    # =========================================================================
    def generate_cr_record(self, data: Dict[str, Any], user_fullname: str) -> Dict[str, Any]:
        """Copie le modèle dans le dossier du patient, remplace les 8 macros et enregistre."""
        self.ensure_dme_schema()
        patient_id = data.get("id_patient")
        passage_id = data.get("id_passage")
        template_id = data.get("template_id")
        med_prescripteur = str(data.get("medecin_prescripteur") or "").strip()
        med_signataire = str(data.get("medecin_signataire") or "").strip()

        if not template_id or not patient_id or not passage_id:
            return {"success": False, "message": "Veuillez sélectionner un modèle de compte rendu."}

        with get_db_cursor() as cursor:
            cursor.execute("SELECT * FROM cr_templates WHERE id = %s;", (template_id,))
            tpl = cursor.fetchone()
            if not tpl:
                return {"success": False, "message": "Modèle introuvable."}

            cursor.execute("SELECT nom, prenom, date_naissance FROM patients WHERE id = %s;", (patient_id,))
            pat = cursor.fetchone()

            cursor.execute("SELECT date_passage, heure_passage FROM patient_passages WHERE id = %s;", (passage_id,))
            pass_info = cursor.fetchone()

        if not pat:
            return {"success": False, "message": "Patient introuvable."}

        now_str = datetime.now().strftime("%d/%m/%Y à %H:%M")
        date_examen = format_date_display(pass_info.get("date_passage") if pass_info else None)

        dob_display = format_date_display(pat.get("date_naissance"))
        age_str = calculate_age_str(pat.get("date_naissance"))
        dob_age_str = f"{dob_display} ({age_str})" if age_str else dob_display
        patient_nom_complet = f"{pat['nom']} {pat['prenom']}".strip()

        # LES 8 MACROS EXACTES DEMANDÉES
        replacements = {
            "${Date}": date_examen,
            "${Nom}": patient_nom_complet,
            "${Date_nais}": dob_age_str,
            "${med}": med_prescripteur or "Dr. Prescripteur",
            "${sec}": user_fullname or "Secrétaire",
            "${id_av}": f"#PASS-{passage_id:04d}",
            "${cor}": "(Cordialement)",
            "${signe}": med_signataire or "Dr. Radiologue",
            "${Titre}": tpl["designation"].upper()
        }

        # Nomenclature : Nom_Prenom_6chiffres
        clean_nom = re.sub(r'[^\w\-]', '_', pat['nom'].strip().upper())
        clean_prenom = re.sub(r'[^\w\-]', '_', pat['prenom'].strip().title())
        rand_suffix = f"{random.randint(100000, 999999)}"
        base_name = f"{clean_nom}_{clean_prenom}_{rand_suffix}"

        patient_folder = os.path.join(str(BASE_DIR), "uploads", "dossiers_patients", str(patient_id))
        os.makedirs(patient_folder, exist_ok=True)

        chemin_final_docx = ""
        contenu_final_html = tpl.get("contenu_html") or ""

        if tpl["type_modele"] == "Word":
            file_name = f"{base_name}.docx"
            chemin_final_docx = os.path.join(patient_folder, file_name)

            src_docx = tpl.get("chemin")
            if src_docx and os.path.exists(src_docx):
                shutil.copy2(src_docx, chemin_final_docx)
            else:
                master_path = os.path.join(str(BASE_DIR), "uploads", "model_templates", "BASE_CR_TYPE.docx")
                if not os.path.exists(master_path) or not zipfile.is_zipfile(master_path):
                    return {"success": False, "message": "Le fichier Word du modèle est introuvable et le modèle maître est absent ou invalide. Aucun compte rendu vierge n'a été créé."}
                shutil.copy2(master_path, chemin_final_docx)

            if not zipfile.is_zipfile(chemin_final_docx):
                try:
                    os.remove(chemin_final_docx)
                except OSError:
                    pass
                return {"success": False, "message": "Le document Word du modèle est invalide. Aucun compte rendu vierge n'a été créé."}

            # Remplacement garanti des 8 variables dans le Word
            if not replace_docx_variables_robust(chemin_final_docx, replacements):
                try:
                    os.remove(chemin_final_docx)
                except OSError:
                    pass
                return {"success": False, "message": "Impossible de personnaliser le modèle Word sélectionné."}

        else:
            file_name = f"{base_name}.html"
            for k, v in replacements.items():
                contenu_final_html = contenu_final_html.replace(k, v)

            chemin_html_file = os.path.join(patient_folder, file_name)
            try:
                with open(chemin_html_file, "w", encoding="utf-8") as f_html:
                    f_html.write(contenu_final_html)
            except Exception:
                pass

        # Insertion finale dans PostgreSQL
        with get_db_cursor() as cursor:
            cursor.execute("""
                           INSERT INTO patient_cr_records
                           (id_patient, id_passage, template_id, titre, type_modele, modalite,
                            medecin_prescripteur, medecin_signataire, proprietaire, contenu_html,
                            chemin_docx, nom_fichier, date_heure_str)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
                           """, (
                               patient_id, passage_id, template_id, tpl["designation"], tpl["type_modele"],
                               tpl.get("modalite", "IRM"), med_prescripteur, med_signataire, user_fullname,
                               contenu_final_html, chemin_final_docx, file_name, now_str
                           ))
            new_id = cursor.fetchone()["id"]

        return {
            "success": True,
            "message": f"Compte rendu '{file_name}' enregistré avec succès !",
            "record_id": new_id,
            "type_modele": tpl["type_modele"],
            "nom_fichier": file_name,
            "chemin_docx": chemin_final_docx
        }

    def delete_cr_record(self, record_id: int) -> Dict[str, Any]:
        self.ensure_dme_schema()
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM patient_cr_records WHERE id = %s;", (record_id,))
        return {"success": True, "message": "Compte rendu supprimé."}

    def get_cr_record_by_id(self, record_id: int) -> Optional[Dict[str, Any]]:
        self.ensure_dme_schema()
        with get_db_cursor() as cursor:
            cursor.execute("SELECT * FROM patient_cr_records WHERE id = %s;", (record_id,))
            row = cursor.fetchone()
            return dict(row) if row else None


dme_manager = DmeManager()


def require_consultation_unlocked(passage_id: Any, patient_id: Any = None, consultation_id: Any = None) -> None:
    if not passage_id:
        raise HTTPException(status_code=400, detail="Identifiant de passage manquant.")
    if consultation_id:
        dme_manager.ensure_dme_schema()
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT verrouille FROM patient_dme_records
                WHERE id = %s AND id_passage = %s AND id_patient = %s;
            """, (consultation_id, passage_id, patient_id))
            consultation = cursor.fetchone()
        if not consultation:
            raise HTTPException(status_code=404, detail="Consultation introuvable pour ce patient et ce passage.")
        if consultation.get("verrouille", True):
            raise HTTPException(status_code=409, detail="Consultation verrouillée : ordonnance en lecture seule.")
        return
    if dme_manager.is_consultation_locked(int(passage_id)):
        raise HTTPException(status_code=409, detail="Consultation verrouillée : ouverture en lecture seule uniquement.")


# ============================================================================
# POINTS D'ACCÈS FASTAPI
# ============================================================================
@dme_router.get("/record/{patient_id}/{passage_id}")
async def api_get_dme(patient_id: int, passage_id: int, request: Request):
    try:
        from modules.dme_display import display_permissions
        user = get_current_user_optional(request)
        if not user:
            raise HTTPException(status_code=401, detail="Session expirée.")
        data = dme_manager.get_patient_dme_data(patient_id, passage_id)
        current_user_name = " ".join(part for part in (str(user.get("prenom") or "").strip(), str(user.get("nom") or "").strip()) if part).strip()
        current_user_name = current_user_name or str(user.get("login") or "").strip()
        consultation = data.get("dme") or {}
        passage = data.get("passage") or {}
        consultation_owner = str(passage.get("consultation_utilisateur") or consultation.get("praticien") or "").strip()
        consultation["consultation_en_cours_par_autre"] = bool(
            consultation.get("id") and not consultation.get("verrouille", True)
            and passage.get("statut") == "En cours"
            and consultation_owner and consultation_owner.casefold() != current_user_name.casefold()
        )
        consultation["consultation_owner"] = consultation_owner
        data["dme"] = consultation
        data["dme_display_permissions"] = display_permissions(user)
        from modules.dme_reset import user_can_reset_dme
        data["dme_can_reset"] = user_can_reset_dme(user)
        return data
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Erreur lecture DME : %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Erreur SQL DME: {str(exc)}")


@dme_router.get("/archive/{patient_id}/{passage_id}")
async def api_get_archived_consultation(patient_id: int, passage_id: int):
    archived = dme_manager.get_archived_consultation(patient_id, passage_id)
    if not archived:
        raise HTTPException(status_code=404, detail="Archive de consultation introuvable.")
    return archived


@dme_router.post("/consultation/start")
async def api_start_dme_consultation(payload: Dict[str, Any], request: Request):
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    dme_manager.ensure_dme_schema()
    patient_id = payload.get("id_patient")
    passage_id = payload.get("id_passage")
    if not patient_id or not passage_id:
        raise HTTPException(status_code=400, detail="Identifiants patient et passage obligatoires.")
    user_fullname = " ".join(
        part for part in (str(user.get("prenom") or "").strip(), str(user.get("nom") or "").strip()) if part
    ) or str(user.get("login") or "Utilisateur connecté").strip()
    with get_db_cursor() as cursor:
        cursor.execute("SELECT id_patient, statut, consultation_utilisateur FROM patient_passages WHERE id = %s FOR UPDATE;", (passage_id,))
        passage = cursor.fetchone()
        if not passage:
            raise HTTPException(status_code=404, detail="Passage introuvable.")
        if int(passage.get("id_patient")) != int(patient_id):
            raise HTTPException(status_code=403, detail="Ce passage ne correspond pas à ce patient.")
        cursor.execute("""
            SELECT id, verrouille, praticien FROM patient_dme_records
            WHERE id_patient = %s AND id_passage = %s
            ORDER BY id DESC LIMIT 1 FOR UPDATE;
        """, (patient_id, passage_id))
        consultation = cursor.fetchone()
        if consultation and consultation.get("verrouille", True):
            raise HTTPException(status_code=409, detail="Cette consultation est déjà enregistrée en lecture seule.")
        current_owner = str(
            passage.get("consultation_utilisateur")
            or ((consultation.get("praticien") if consultation else "") if passage.get("statut") == "En cours" else "")
            or ""
        ).strip()
        if consultation and current_owner and current_owner.casefold() != user_fullname.casefold():
            raise HTTPException(status_code=409, detail=f"Consultation déjà en cours par {current_owner}. Vous pouvez uniquement l’ouvrir en lecture seule.")
        if consultation:
            consultation_id = consultation["id"]
            # Reprise autorisée pour le médecin responsable; un autre utilisateur
            # ne peut ni prendre le verrou ni remplacer le praticien.
            cursor.execute("UPDATE patient_dme_records SET praticien = %s WHERE id = %s;", (user_fullname, consultation_id))
        else:
            cursor.execute("""
                INSERT INTO patient_dme_records (id_patient, id_passage, praticien, verrouille)
                VALUES (%s, %s, %s, FALSE)
                RETURNING id;
            """, (patient_id, passage_id, user_fullname))
            consultation_id = cursor.fetchone()["id"]
        cursor.execute("""
            UPDATE patient_prescriptions
            SET id_consultation = %s
            WHERE id_patient = %s AND id_passage = %s AND id_consultation IS NULL;
        """, (consultation_id, patient_id, passage_id))
        cursor.execute(
            "UPDATE patient_passages SET statut = 'En cours', consultation_utilisateur = %s WHERE id = %s;",
            (user_fullname, passage_id),
        )
    track_action(request, "CONSULTATION_DEMARREE", f"Consultation démarrée pour le passage #{passage_id}")
    return {"success": True, "statut": "En cours", "id_consultation": consultation_id}


@dme_router.post("/consultation/keep")
async def api_keep_dme_consultation(payload: Dict[str, Any], request: Request):
    user = get_current_user_optional(request)
    if not user or user.get("status") != "Actif":
        raise HTTPException(status_code=401, detail="Session expirée ou compte inactif.")
    note = str(payload.get("note_reprise") or "").strip()
    doctor_id = payload.get("note_medecin_id")
    if not note:
        raise HTTPException(status_code=400, detail="Écrivez une note avant de garder la consultation en cours.")
    if not doctor_id:
        raise HTTPException(status_code=400, detail="Choisissez le médecin à consulter.")
    try:
        doctor_id = int(doctor_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Médecin sélectionné invalide.")
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT prenom, nom, specialite FROM clinic_staff
            WHERE id = %s AND actif = TRUE
              AND (type_personnel IN ('Docteur', 'Médecin', 'Professeur') OR specialite ILIKE '%%radio%%')
        """, (doctor_id,))
        doctor = cursor.fetchone()
    if not doctor:
        raise HTTPException(status_code=404, detail="Le médecin sélectionné est introuvable ou inactif.")
    doctor_name = " ".join(part for part in (str(doctor.get("prenom") or "").strip(), str(doctor.get("nom") or "").strip()) if part)
    if doctor.get("specialite"):
        doctor_name += f" — {doctor['specialite']}"
    user_fullname = " ".join(part for part in (str(user.get("prenom") or "").strip(), str(user.get("nom") or "").strip()) if part) or str(user.get("login") or "Utilisateur connecté")
    result = dme_manager.save_consultation(
        payload, user_fullname, finalize=False,
        handoff_note=note[:5000], handoff_doctor=doctor_name[:300]
    )
    if not result.get("success"):
        raise HTTPException(status_code=409, detail=result.get("message") or "Impossible de garder cette consultation.")
    track_action(request, "CONSULTATION_MISE_EN_ATTENTE", f"Consultation #{result.get('id_consultation')} conservée en cours; avis demandé à {doctor_name}")
    return result


@dme_router.post("/consultation/cancel-start")
async def api_cancel_dme_consultation_start(payload: Dict[str, Any], request: Request):
    """Annule un démarrage accidentel en sauvegardant le brouillon et en libérant le passage."""
    user = get_current_user_optional(request)
    if not user or user.get("status") != "Actif":
        raise HTTPException(status_code=401, detail="Session expirée ou compte inactif.")
    user_fullname = " ".join(
        part for part in (str(user.get("prenom") or "").strip(), str(user.get("nom") or "").strip()) if part
    ) or str(user.get("login") or "Utilisateur connecté").strip()

    # Enregistre les champs actuellement saisis comme brouillon avant de libérer
    # l'affectation : aucune donnée clinique n'est supprimée.
    result = dme_manager.save_consultation(payload, user_fullname, finalize=False)
    if not result.get("success"):
        raise HTTPException(status_code=409, detail=result.get("message") or "Impossible de conserver le brouillon.")

    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT id_patient, statut, consultation_utilisateur
            FROM patient_passages WHERE id = %s FOR UPDATE;
        """, (payload.get("id_passage"),))
        passage = cursor.fetchone()
        if (not passage or int(passage.get("id_patient") or 0) != int(payload.get("id_patient") or 0)
                or passage.get("statut") != "En cours"):
            raise HTTPException(status_code=409, detail="Le passage a changé d’état. Rechargez le dossier avant de réessayer.")
        owner = str(passage.get("consultation_utilisateur") or "").strip()
        if owner and owner.casefold() != user_fullname.casefold():
            raise HTTPException(status_code=409, detail=f"Cette consultation est maintenant attribuée à {owner}.")
        cursor.execute("""
            UPDATE patient_passages
            SET statut = 'En attente', consultation_utilisateur = NULL
            WHERE id = %s AND id_patient = %s AND statut = 'En cours';
        """, (payload.get("id_passage"), payload.get("id_patient")))
        if cursor.rowcount != 1:
            raise HTTPException(status_code=409, detail="Impossible de libérer le passage. Rechargez le dossier.")

    track_action(request, "DEMARRAGE_CONSULTATION_ANNULE", f"Démarrage annulé par erreur pour le passage #{payload.get('id_passage')}; brouillon conservé")
    result["statut"] = "En attente"
    result["verrouille"] = False
    result["message"] = "Démarrage annulé. Le passage est remis en attente et le brouillon est conservé."
    return result


@dme_router.post("/consultation/reset")
async def api_reset_dme_consultation(payload: Dict[str, Any], request: Request):
    from modules.dme_reset import reset_active_consultation, user_can_reset_dme
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée.")
    if not user_can_reset_dme(user):
        raise HTTPException(status_code=403, detail="Autorisation de réinitialisation DME manquante.")
    dme_manager.ensure_dme_schema()
    with get_db_cursor() as cursor:
        record = reset_active_consultation(cursor, user, payload.get("id_patient"),
                                          payload.get("id_passage"), payload.get("id_consultation"))
    track_action(request, "CONSULTATION_REINITIALISEE", f"Réinitialisation complète de la consultation #{record['id']}")
    data = dme_manager.get_patient_dme_data(payload['id_patient'], payload['id_passage'])
    from modules.dme_display import display_permissions
    data['dme_display_permissions'] = display_permissions(user)
    data['dme_can_reset'] = user_can_reset_dme(user)
    return {"success": True, "data": data, "message": "Consultation remise à zéro. Elle reste en cours, prête à recommencer."}


@dme_router.post("/save")
async def api_save_dme(payload: Dict[str, Any], request: Request):
    user = get_current_user_optional(request)
    if payload.get("sections_only"):
        return await api_save_dme_specialty_sections(payload, request)
    praticien = f"{user['prenom']} {user['nom']}" if user else "Dr Praticien"
    res = dme_manager.save_consultation(payload, praticien)
    if res.get("success"):
        track_action(request, "EDITION_DME", f"Mise à jour consultation #{payload.get('id_passage')}")
        res["praticien"] = praticien
    return res


@dme_router.get("/prescriptions/{patient_id}/{passage_id}/{consultation_id}")
async def api_get_patient_prescriptions(patient_id: int, passage_id: int, consultation_id: int):
    return {"prescriptions": dme_manager.get_prescriptions_patient(patient_id, passage_id, consultation_id or None)}


@dme_router.get("/consumptions/options")
async def api_dme_consumption_options(request: Request):
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée ou utilisateur non connecté.")
    permissions = user.get("permissions") or {}
    if isinstance(permissions, str):
        try:
            permissions = json.loads(permissions)
        except (TypeError, ValueError):
            permissions = {}
    if user.get("role") != "Administrateur" and not (permissions.get("dme_view") or permissions.get("dme_consult")):
        raise HTTPException(status_code=403, detail="Votre compte ne peut pas consulter les consommations DME.")
    with get_db_cursor() as cursor:
        cursor.execute("SELECT id, designation FROM rooms ORDER BY id;")
        rooms = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT nom FROM ref_specialites WHERE actif IS DISTINCT FROM FALSE ORDER BY nom;")
        services = [row["nom"] for row in cursor.fetchall()]
        cursor.execute("SELECT DISTINCT utilisateur FROM patient_consumptions WHERE utilisateur <> '' ORDER BY utilisateur;")
        users = [row["utilisateur"] for row in cursor.fetchall()]
    return {
        "rooms": rooms,
        "services": services,
        "users": users,
        "current_user": {
            "id": user.get("id"),
            "name": f"{user.get('prenom') or ''} {user.get('nom') or ''}".strip(),
        },
    }


@dme_router.get("/consumptions/report")
async def api_dme_consumption_report(request: Request):
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée ou utilisateur non connecté.")
    permissions = user.get("permissions") or {}
    if isinstance(permissions, str):
        try:
            permissions = json.loads(permissions)
        except (TypeError, ValueError):
            permissions = {}
    if user.get("role") != "Administrateur" and not (permissions.get("dme_view") or permissions.get("dme_consult")):
        raise HTTPException(status_code=403, detail="Votre compte ne peut pas consulter les rapports de consommation.")
    params = request.query_params
    filters, values = [], []
    for key, column in (("date_debut", "c.consomme_le::date >="), ("date_fin", "c.consomme_le::date <="),
                        ("service", "c.service ="), ("utilisateur", "c.utilisateur ="), ("salle_id", "c.salle_id =")):
        raw = (params.get(key) or "").strip()
        if raw:
            filters.append(f"{column} %s")
            values.append(int(raw) if key == "salle_id" else raw)
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    with get_db_cursor() as cursor:
        cursor.execute(f"""
            SELECT c.id, c.id_patient, c.id_passage, c.id_consultation,
                   c.type_produit, c.nom_produit, c.quantite, c.unite,
                   c.service, c.utilisateur, c.lot, c.notes, c.consomme_le,
                   p.nom AS patient_nom, p.prenom AS patient_prenom,
                   r.designation AS salle
            FROM patient_consumptions c
            JOIN patients p ON p.id=c.id_patient
            LEFT JOIN rooms r ON r.id=c.salle_id
            {where}
            ORDER BY c.consomme_le DESC, c.id DESC
            LIMIT 10000;
        """, tuple(values))
        rows = [dict(row) for row in cursor.fetchall()]
    for row in rows:
        row["consomme_le"] = row["consomme_le"].isoformat() if row.get("consomme_le") else ""
        row["quantite"] = float(row["quantite"] or 0)
    return {"consumptions": rows}


@dme_router.get("/consumptions/{patient_id}/{passage_id}/{consultation_id}")
async def api_get_dme_consumptions(patient_id: int, passage_id: int, consultation_id: int, request: Request):
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée ou utilisateur non connecté.")
    permissions = user.get("permissions") or {}
    if isinstance(permissions, str):
        try:
            permissions = json.loads(permissions)
        except (TypeError, ValueError):
            permissions = {}
    if user.get("role") != "Administrateur" and not (permissions.get("dme_view") or permissions.get("dme_consult")):
        raise HTTPException(status_code=403, detail="Votre compte ne peut pas consulter les consommations DME.")
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT c.id, c.id_patient, c.id_passage, c.id_consultation,
                   c.type_produit, c.nom_produit, c.quantite, c.unite,
                   c.salle_id, r.designation AS salle, c.service, c.utilisateur,
                   c.lot, c.notes, c.consomme_le
            FROM patient_consumptions c
            LEFT JOIN rooms r ON r.id=c.salle_id
            WHERE c.id_patient=%s AND c.id_passage=%s AND c.id_consultation=%s
            ORDER BY c.consomme_le DESC, c.id DESC;
        """, (patient_id, passage_id, consultation_id))
        rows = [dict(row) for row in cursor.fetchall()]
    for row in rows:
        row["consomme_le"] = row["consomme_le"].isoformat() if row.get("consomme_le") else ""
        row["quantite"] = float(row["quantite"] or 0)
    return {"consumptions": rows}


@dme_router.post("/consumptions/save")
async def api_save_dme_consumption(payload: Dict[str, Any], request: Request):
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée ou utilisateur non connecté.")
    permissions = user.get("permissions") or {}
    if isinstance(permissions, str):
        try:
            permissions = json.loads(permissions)
        except (TypeError, ValueError):
            permissions = {}
    if user.get("role") != "Administrateur" and not permissions.get("dme_consult"):
        raise HTTPException(status_code=403, detail="Votre compte ne peut pas enregistrer une consommation DME.")
    patient_id, passage_id, consultation_id = payload.get("id_patient"), payload.get("id_passage"), payload.get("id_consultation")
    require_consultation_unlocked(passage_id, patient_id, consultation_id)
    product_type = str(payload.get("type_produit") or "").strip()
    product_name = str(payload.get("nom_produit") or "").strip()
    service = str(payload.get("service") or "").strip()
    unit = str(payload.get("unite") or "").strip()
    try:
        quantity = Decimal(str(payload.get("quantite") or ""))
        patient_id, passage_id, consultation_id = int(patient_id), int(passage_id), int(consultation_id)
        room_id = int(payload["salle_id"]) if payload.get("salle_id") else None
    except (InvalidOperation, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="La quantité et les identifiants sont invalides.")
    if product_type not in ("Médicament", "Dispositif médical"):
        raise HTTPException(status_code=400, detail="Choisissez le type de produit consommé.")
    if not product_name or len(product_name) > 200 or not service or len(service) > 120 or not unit or len(unit) > 30:
        raise HTTPException(status_code=400, detail="Le produit, le service et l'unité sont obligatoires.")
    if not quantity.is_finite() or quantity <= 0:
        raise HTTPException(status_code=400, detail="La quantité doit être supérieure à zéro.")
    with get_db_cursor() as cursor:
        cursor.execute("SELECT 1 FROM patients WHERE id=%s;", (patient_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Patient introuvable.")
        cursor.execute("SELECT id_patient FROM patient_passages WHERE id=%s;", (passage_id,))
        passage = cursor.fetchone()
        if not passage or int(passage["id_patient"]) != patient_id:
            raise HTTPException(status_code=404, detail="Passage introuvable pour ce patient.")
        cursor.execute("SELECT 1 FROM ref_specialites WHERE nom=%s AND actif IS DISTINCT FROM FALSE;", (service,))
        if not cursor.fetchone():
            raise HTTPException(status_code=400, detail="Le service sélectionné n'existe plus.")
        if room_id:
            cursor.execute("SELECT 1 FROM rooms WHERE id=%s;", (room_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=400, detail="La salle sélectionnée n'existe plus.")
        cursor.execute("""
            INSERT INTO patient_consumptions
                (id_patient,id_passage,id_consultation,type_produit,nom_produit,quantite,unite,
                 salle_id,service,utilisateur_id,utilisateur,lot,notes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id;
        """, (
            patient_id, passage_id, consultation_id, product_type, product_name, quantity, unit,
            room_id, service, user.get("id"),
            f"{user.get('prenom') or ''} {user.get('nom') or ''}".strip() or str(user.get("login") or ""),
            str(payload.get("lot") or "").strip()[:100], str(payload.get("notes") or "").strip()[:1000],
        ))
        consumption_id = cursor.fetchone()["id"]
    track_action(request, "DME_CONSUMPTION", f"Consommation #{consumption_id} enregistrée pour le passage #{passage_id}")
    return {"success": True, "id": consumption_id}


@dme_router.post("/prescriptions/save")
async def api_save_prescription(payload: Dict[str, Any], request: Request):
    require_consultation_unlocked(payload.get("id_passage"), payload.get("id_patient"), payload.get("id_consultation"))
    user = get_current_user_optional(request)
    nom = f"Dr. {user['nom']} {user['prenom']}" if user else "Dr. Praticien"
    role = user.get("role", "Médecin") if user else "Médecin"
    res = dme_manager.save_prescription(payload, nom, role)
    if res.get("success"):
        track_action(request, "PRESCRIPTION", f"Nouvelle ordonnance #{res.get('ord_id')}")
    return res


@dme_router.post("/prescriptions/delete")
async def api_delete_prescription(payload: Dict[str, Any], request: Request):
    user = get_current_user_optional(request)
    login = user.get("login", "admin") if user else "admin"
    ord_id = payload.get("id")
    patient_id = payload.get("id_patient")
    passage_id = payload.get("id_passage")
    consultation_id = payload.get("id_consultation")
    ord_data = dme_manager.get_prescription_by_id(ord_id, patient_id, passage_id, consultation_id)
    if not ord_data:
        raise HTTPException(status_code=404, detail="Ordonnance introuvable.")
    require_consultation_unlocked(passage_id, patient_id, consultation_id)
    track_action(request, "SUPPRESSION_ORDONNANCE", f"Annulation ordonnance #{ord_id}")
    return dme_manager.soft_delete_prescription(ord_id, patient_id, passage_id, consultation_id, login)


@dme_router.get("/prescriptions/print/{patient_id}/{passage_id}/{consultation_id}/{ord_id}", response_class=HTMLResponse)
async def view_prescription_print(patient_id: int, passage_id: int, consultation_id: int, ord_id: int):
    scoped_consultation_id = consultation_id or None
    ord_data = dme_manager.get_prescription_by_id(ord_id, patient_id, passage_id, scoped_consultation_id)
    if not ord_data:
        raise HTTPException(status_code=404, detail="Ordonnance introuvable.")

    img_path = os.path.join(str(BASE_DIR), "uploads", "model_templates", "ORD.png")
    img_b64 = ""
    if os.path.exists(img_path):
        import base64
        with open(img_path, "rb") as f:
            img_b64 = f"data:image/png;base64,{base64.b64encode(f.read()).decode('utf-8')}"

    is_deleted = ord_data.get("statut") == "Supprimée"

    lignes_html = ""
    for idx, l in enumerate(ord_data.get("lignes", []), start=1):
        med = (l.get("medicament") or "").upper()
        poso = (l.get("posologie") or "").upper()
        duree = l.get("duree") or ""
        qte = l.get("quantite") or "1"
        boite_str = f"{qte} boîte" if int(qte or 1) <= 1 else f"{qte} boîtes"
        duree_html = f'<div style="font-size: 11.5px; font-weight: 600; color: #475569; margin-top: 2px;">(Pendant : {duree})</div>' if duree and duree != "-" else ""

        lignes_html += f"""
        <div style="margin-bottom: 22px; font-family: 'Plus Jakarta Sans', Arial, sans-serif;">
            <div style="font-size: 13.5px; font-weight: 800; color: #0c1a10;">
                {idx}. {med}
            </div>
            <div style="font-size: 12px; color: #1e293b; padding-left: 14px; margin-top: 3px;">
                <span style="color: #64748b;">•</span> <strong>Posologie :</strong> <strong>{poso}</strong>
                {duree_html}
            </div>
            <div style="font-size: 12px; color: #059669; font-weight: 800; padding-left: 14px; margin-top: 3px;">
                • Quantité : {boite_str}
            </div>
        </div>
        """

    watermark_html = ""
    if is_deleted:
        watermark_html = """
        <div style="position: absolute; top: 48%; left: 50%; transform: translate(-50%, -50%) rotate(-32deg);
                    font-size: 80px; font-weight: 900; color: rgba(220, 38, 38, 0.38); border: 8px solid rgba(220, 38, 38, 0.38);
                    padding: 12px 45px; border-radius: 24px; z-index: 50; pointer-events: none; text-transform: uppercase;">
            SUPPRIMÉE
        </div>
        """

    nom_complet = f"{ord_data.get('nom', '')} {ord_data.get('prenom', '')}".upper()
    dob = ord_data.get("date_naissance") or "-"
    age_str = ord_data.get("age_str") or ""
    age_display = f"(Âge : <strong>{age_str}</strong>)" if age_str else ""
    date_ord = ord_data.get("date_ord") or ""
    med_nom = ord_data.get("medecin_nom") or "Dr. Praticien"
    med_spe = ord_data.get("medecin_specialite") or "Médecine"

    return f"""
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="UTF-8">
        <title>Ordonnance #{ord_data['id']:04d} - {nom_complet}</title>
        <style>
            @page {{ size: A4 portrait; margin: 0; }}
            * {{ box-sizing: border-box; }}
            body {{
                margin: 0; padding: 0; background: #525659;
                display: flex; justify-content: center;
                font-family: 'Plus Jakarta Sans', Arial, sans-serif;
            }}
            .page {{
                position: relative; width: 210mm; height: 297mm;
                background: white; overflow: hidden;
            }}
            .bg-template {{
                position: absolute; top: 0; left: 0;
                width: 100%; height: 100%; z-index: 1; pointer-events: none;
            }}
            .no-print {{
                position: fixed; top: 15px; right: 15px; z-index: 9999;
                display: flex; gap: 10px;
            }}
            .btn {{
                border: none; padding: 10px 18px; border-radius: 10px; font-weight: bold;
                cursor: pointer; font-size: 13px; box-shadow: 0 4px 10px rgba(0,0,0,0.3);
            }}
            .btn-print {{ background: #121c15; color: #8be140; }}
            .btn-close {{ background: #dc2626; color: white; }}
            @media print {{
                .no-print {{ display: none !important; }}
                body {{ background: white; }}
            }}
        </style>
    </head>
    <body>
        <div class="no-print">
            <button class="btn btn-print" onclick="window.print()">🖨️ Imprimer l'Ordonnance</button>
            <button class="btn btn-close" onclick="window.parent && window.parent.closePrescriptionModal ? window.parent.closePrescriptionModal() : window.close()">✕ Quitter</button>
        </div>

        <div class="page">
            {f'<img src="{img_b64}" class="bg-template">' if img_b64 else ''}
            {watermark_html}

            <div style="position: absolute; top: 18.2%; left: 12.5%; right: 5.5%; z-index: 10; font-size: 12.5px; color: #334155;">
                <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                    <div>
                        Patient : <strong style="color: #0f172a; font-size: 14px;">{nom_complet}</strong>
                    </div>
                    <div style="font-weight: 800; font-size: 13px; color: #0284c7; font-family: monospace;">
                        #ORD - {ord_data['id']:04d}
                    </div>
                </div>

                <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 6px;">
                    <div>
                        Date de Naissance : <strong>{dob}</strong> {age_display}
                    </div>
                    <div style="font-size: 12px; color: #475569;">
                        Date : <strong style="color: #0f172a;">{date_ord}</strong>
                    </div>
                </div>

                <div style="border-bottom: 2px solid #0284c7; margin-top: 10px; width: 100%;"></div>
            </div>

            <div style="position: absolute; top: 25.8%; left: 12.5%; right: 5.5%; height: 52%; z-index: 10; overflow: hidden;">
                {lignes_html}
            </div>

            <div style="position: absolute; bottom: 12.5%; left: 12.5%; right: 5.5%; z-index: 10;">
                <div style="border-top: 1px dotted #cbd5e1; margin-bottom: 12px; width: 100%;"></div>

                <div style="text-align: right;">
                    <div style="font-size: 11.5px; font-weight: 700; color: #64748b; margin-bottom: 3px;">
                        Le Médecin Prescripteur
                    </div>
                    <div style="font-size: 13.5px; font-weight: 900; color: #059669;">
                        {med_nom} ({med_spe})
                    </div>
                    <div style="font-size: 10.5px; font-style: italic; color: #94a3b8; margin-top: 4px;">
                        (Signature & Griffe)
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """


# ============================================================================
# COMPTES RENDUS & NOUVELLE VISIONNEUSE PDF DE WORD
# ============================================================================
@dme_router.post("/cr/generate")
async def api_generate_cr(payload: Dict[str, Any], request: Request):
    require_consultation_unlocked(payload.get("id_passage"))
    try:
        user = get_current_user_optional(request)
        nom = f"{user['prenom']} {user['nom']}" if user else "Dr. Praticien"
        return dme_manager.generate_cr_record(payload, nom)
    except Exception as exc:
        logger.error("Erreur serveur lors de la génération du CR : %s", exc, exc_info=True)
        return {"success": False, "message": f"Erreur serveur : {str(exc)}"}


@dme_router.post("/cr/open-word")
async def api_open_word_cr(payload: Dict[str, Any]):
    record_id = payload.get("id")
    cr = dme_manager.get_cr_record_by_id(record_id)
    if not cr or not cr.get("chemin_docx") or not os.path.exists(cr["chemin_docx"]):
        raise HTTPException(status_code=404, detail="Fichier Word introuvable sur le disque.")

    return {"success": True, "download_url": f"/api/dme/cr/download-word/{int(record_id)}",
            "message": "Le compte rendu est prêt à être ouvert sur le poste client."}


@dme_router.get("/cr/download-word/{record_id}")
async def api_download_word_cr(record_id: int):
    """Envoie le DOCX au poste connecté pour ouverture locale dans Office Word."""
    cr = dme_manager.get_cr_record_by_id(record_id)
    path = str((cr or {}).get("chemin_docx") or "")
    if not cr or not path or not path.lower().endswith(".docx") or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Fichier Word introuvable sur le serveur.")
    filename = os.path.basename(path)
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        filename=filename, headers={"Cache-Control": "private, no-store"})


@dme_router.post("/cr/upload-word/{record_id}")
async def api_upload_word_cr(record_id: int, request: Request, file: UploadFile = File(...)):
    """Remplace le compte rendu patient par sa version Word sauvegardée sur le poste client."""
    user = get_current_user_optional(request)
    if not user or user.get("status") != "Actif":
        raise HTTPException(status_code=401, detail="Session expirée ou compte inactif.")
    cr = dme_manager.get_cr_record_by_id(record_id)
    if not cr or str(cr.get("type_modele") or "").lower() != "word":
        raise HTTPException(status_code=404, detail="Compte rendu Word introuvable.")
    target_path = Path(str(cr.get("chemin_docx") or "")).resolve()
    patient_root = (BASE_DIR / "uploads" / "dossiers_patients" / str(cr.get("id_patient"))).resolve()
    if not target_path.is_relative_to(patient_root) or target_path.suffix.lower() != ".docx" or not target_path.is_file():
        raise HTTPException(status_code=404, detail="Le fichier du compte rendu n'est pas disponible dans le dossier de ce patient.")

    contents = await file.read(100 * 1024 * 1024 + 1)
    if not contents or len(contents) > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Le document Word est vide ou dépasse 100 Mo.")
    try:
        with zipfile.ZipFile(BytesIO(contents)) as document:
            names = set(document.namelist())
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise zipfile.BadZipFile("Structure DOCX manquante")
    except (zipfile.BadZipFile, OSError):
        raise HTTPException(status_code=400, detail="Le fichier envoyé n'est pas un DOCX valide. Le compte rendu serveur n'a pas été modifié.")

    temporary_path = target_path.with_name(target_path.name + ".upload.tmp")
    try:
        temporary_path.write_bytes(contents)
        os.replace(temporary_path, target_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    track_action(request, "COMPTE_RENDU_PATIENT_MIS_A_JOUR", f"Compte rendu Word #{record_id} synchronisé depuis un poste client")
    return {"success": True, "message": "Le compte rendu patient a été synchronisé sur le serveur."}


@dme_router.post("/cr/open-word-server")
async def api_open_word_cr_server(payload: Dict[str, Any], request: Request):
    """Ouvre le fichier original du compte rendu dans Word sur le serveur local."""
    user = get_current_user_optional(request)
    if not user or user.get("status") != "Actif":
        raise HTTPException(status_code=401, detail="Session expirée ou compte inactif.")
    cr = dme_manager.get_cr_record_by_id(payload.get("id"))
    path = str((cr or {}).get("chemin_docx") or "")
    if not cr or str(cr.get("type_modele") or "").lower() != "word" or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Fichier Word introuvable sur le serveur.")
    try:
        if os.name != "nt":
            raise RuntimeError("L'ouverture locale de Microsoft Word est disponible sur Windows.")
        os.startfile(path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Impossible d'ouvrir Word sur le serveur : {exc}")
    return {"success": True, "message": "Le fichier original du compte rendu est ouvert dans Word sur le serveur."}


@dme_router.get("/cr/view-pdf/{record_id}")
async def view_word_cr_as_pdf(record_id: int):
    """Affiche soit un Livret PDF existant, soit convertit le fichier Word en PDF pour le visionneur interne."""
    cr = dme_manager.get_cr_record_by_id(record_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Document introuvable.")

    # -------------------------------------------------------------
    # CAS 1 : C'est un Livret (déjà au format PDF multipage)
    # -------------------------------------------------------------
    if cr.get("type_modele") == "Livret":
        pdf_path = cr.get("chemin_docx")
        if not pdf_path or not os.path.exists(pdf_path):
            return HTMLResponse(
                """
                <div style="font-family:sans-serif; padding:40px; text-align:center; color:#ef4444;">
                    <h3>Fichier Livret PDF introuvable sur le disque.</h3>
                </div>
                """,
                status_code=404
            )
        filename = os.path.basename(pdf_path)
        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"inline; filename=\"{filename}\"",
                "Cache-Control": "no-cache"
            }
        )

    # -------------------------------------------------------------
    # CAS 2 : C'est un document Word (.docx) à convertir en PDF
    # -------------------------------------------------------------
    docx_path = cr.get("chemin_docx")
    if not docx_path or not os.path.exists(docx_path):
        return HTMLResponse(
            """
            <div style="font-family:sans-serif; padding:40px; text-align:center; color:#ef4444;">
                <h3>Fichier Word introuvable sur le disque.</h3>
            </div>
            """,
            status_code=404
        )

    pdf_path = docx_path.rsplit(".", 1)[0] + ".pdf"

    # Conversion Word vers PDF si le PDF n'existe pas ou si le DOCX a été modifié
    if not os.path.exists(pdf_path) or os.path.getmtime(docx_path) > os.path.getmtime(pdf_path):
        success = convert_docx_to_pdf_windows(docx_path, pdf_path)
        if not success or not os.path.exists(pdf_path):
            return HTMLResponse(
                """
                <!DOCTYPE html>
                <html>
                <body style="font-family:sans-serif; background:#0f1712; color:#f8fafc; padding:40px; text-align:center; display:flex; flex-direction:column; justify-content:center; height:80vh;">
                    <div style="font-size:36px; margin-bottom:10px;">⚠️</div>
                    <h2 style="color:#8be140; margin-bottom:8px;">Conversion PDF indisponible</h2>
                    <p style="font-size:13px; color:#94a3b8; max-width:480px; margin:0 auto 20px auto; line-height:1.5;">
                        Pour visualiser le PDF d'un document Word dans la fenêtre intégrée, la bibliothèque <b>pywin32</b> doit être installée.
                    </p>
                    <div style="background:#000; padding:12px; border-radius:12px; font-family:monospace; font-size:12px; color:#8be140; max-width:320px; margin:0 auto 20px auto;">
                        pip install pywin32
                    </div>
                </body>
                </html>
                """,
                status_code=200
            )

    filename = os.path.basename(pdf_path)
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"inline; filename=\"{filename}\"",
            "Cache-Control": "no-cache"
        }
    )

@dme_router.post("/cr/delete")
async def api_delete_cr(payload: Dict[str, Any]):
    cr = dme_manager.get_cr_record_by_id(payload.get("id"))
    if not cr:
        raise HTTPException(status_code=404, detail="Compte rendu introuvable.")
    require_consultation_unlocked(cr.get("id_passage"))
    return dme_manager.delete_cr_record(payload.get("id"))


@dme_router.get("/cr/print/{record_id}", response_class=HTMLResponse)
async def view_cr_print(record_id: int):
    cr = dme_manager.get_cr_record_by_id(record_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Compte rendu introuvable.")

    return f"""
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="UTF-8">
        <title>{cr['titre']}</title>
        <style>
            @page {{ size: A4 portrait; margin: 15mm 20mm; }}
            body {{
                margin: 0; padding: 20px; font-family: 'Times New Roman', Times, serif;
                background: #525659; display: flex; justify-content: center;
            }}
            .sheet {{
                width: 210mm; min-height: 297mm; background: white; padding: 20mm 25mm;
                box-sizing: border-box; box-shadow: 0 4px 15px rgba(0,0,0,0.2); font-size: 12pt; line-height: 1.5; color: #111827;
            }}
            .no-print {{ position: fixed; top: 15px; right: 15px; z-index: 9999; display: flex; gap: 10px; }}
            .btn {{ border: none; padding: 10px 18px; border-radius: 10px; font-weight: bold; cursor: pointer; }}
            .btn-print {{ background: #121c15; color: #8be140; }}
            .btn-close {{ background: #dc2626; color: white; }}
            @media print {{
                .no-print {{ display: none !important; }}
                body {{ background: white; padding: 0; }}
                .sheet {{ box-shadow: none; padding: 0; width: 100%; }}
            }}
        </style>
    </head>
    <body>
        <div class="no-print">
            <button class="btn btn-print" onclick="window.print()">🖨️ Imprimer le Compte Rendu</button>
            <button class="btn btn-close" onclick="window.parent && window.parent.closeCrPreviewModal ? window.parent.closeCrPreviewModal() : window.close()">✕ Quitter</button>
        </div>
        <div class="sheet">
            {cr['contenu_html']}
        </div>
    </body>
    </html>
    """


# ============================================================================
# CLICHÉS ÉCHOGRAPHIQUES (GESTION, UPLOAD & CARROUSEL)
# ============================================================================
@dme_router.post("/echographies/upload")
async def api_upload_echographies(
        request: Request,
        id_patient: int,
        id_passage: int,
        type_image: str = Form("echographique"),
        categorie: str = Form("Général"),
        groupe_id: Optional[str] = Form(None),
        files: List[UploadFile] = File(...)
):
    """Enregistre les images échographiques PNG/JPG pour le patient et le passage en cours."""
    dme_manager.ensure_dme_schema()
    require_consultation_unlocked(id_passage)

    type_image = (type_image or "echographique").strip().lower()
    if type_image not in {"echographique", "radiologique", "planche_trois"}:
        raise HTTPException(status_code=400, detail="Type d’image invalide.")
    categorie = (categorie or "Général").strip()[:100] or "Général"

    if not files:
        raise HTTPException(status_code=400, detail="Aucun fichier image reçu.")

    patient_folder = os.path.join(str(BASE_DIR), "uploads", "dossiers_patients", str(id_patient), "echographies")
    os.makedirs(patient_folder, exist_ok=True)

    now_str = datetime.now().strftime("%d/%m/%Y (%H:%M)")
    saved_count = 0

    target_group_id = (groupe_id or "").strip()
    with get_db_cursor() as cursor:
        if target_group_id:
            cursor.execute(
                "SELECT groupe_id FROM patient_echographies WHERE groupe_id = %s AND id_patient = %s AND id_passage = %s LIMIT 1;",
                (target_group_id, id_patient, id_passage)
            )
            target_group = cursor.fetchone()
            if not target_group:
                raise HTTPException(status_code=404, detail="Le groupe d’images cible est introuvable dans cette consultation.")
            group_id = str(target_group["groupe_id"])
        else:
            group_id = ""
        cursor.execute(
            "SELECT COUNT(*) AS total FROM patient_echographies WHERE id_patient = %s AND id_passage = %s AND type_image = %s;",
            (id_patient, id_passage, type_image)
        )
        next_number = int(cursor.fetchone()["total"] or 0) + 1
        if group_id:
            cursor.execute(
                "SELECT COALESCE(MAX(ordre_affichage), 0) + 1 AS next_order FROM patient_echographies WHERE id_patient = %s AND id_passage = %s AND groupe_id = %s;",
                (id_patient, id_passage, group_id)
            )
            next_order = int(cursor.fetchone()["next_order"] or 1)
        else:
            cursor.execute(
                "SELECT COALESCE(MAX(ordre_affichage), 0) + 1 AS next_order FROM patient_echographies WHERE id_patient = %s AND id_passage = %s AND type_image = %s AND categorie = %s;",
                (id_patient, id_passage, type_image, categorie)
            )
            next_order = int(cursor.fetchone()["next_order"] or 1)
            cursor.execute(
                "SELECT groupe_id FROM patient_echographies WHERE id_patient = %s AND id_passage = %s AND type_image = %s AND categorie = %s AND groupe_id IS NOT NULL LIMIT 1;",
                (id_patient, id_passage, type_image, categorie)
            )
            group_row = cursor.fetchone()
            group_id = str(group_row["groupe_id"]) if group_row and group_row.get("groupe_id") else f"g_{uuid.uuid4().hex}"

    for file in files:
        ext = file.filename.split(".")[-1].lower() if "." in file.filename else "png"
        clean_base = f"ECHO_{id_patient}_{id_passage}_{int(time.time() * 1000)}_{random.randint(100, 999)}.{ext}"
        dest_path = os.path.join(patient_folder, clean_base)
        display_name = {"echographique": f"Image échographique {next_number}", "radiologique": f"Image radiologique {next_number}", "planche_trois": f"Image Planche A3 {next_number}"}[type_image]

        content = await file.read()
        with open(dest_path, "wb") as f_out:
            f_out.write(content)

        # Enregistrement dans PostgreSQL
        with get_db_cursor() as cursor:
            cursor.execute("""
                           INSERT INTO patient_echographies
                           (id_patient, id_passage, chemin_image, nom_fichier, titre, date_heure_str, type_image, categorie, ordre_affichage, groupe_id)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                           """,
                           (id_patient, id_passage, dest_path, display_name, display_name, now_str, type_image, categorie, next_order, group_id))
            saved_count += 1
        next_number += 1
        next_order += 1

    return {"success": True, "message": f"{saved_count} image(s) importée(s) avec succès dans la catégorie {categorie}."}


@dme_router.post("/echographies/import")
async def api_import_echographies(payload: Dict[str, Any]):
    """Ajoute au groupe cible des références vers des images d'autres groupes du même passage."""
    dme_manager.ensure_dme_schema()
    patient_id = int(payload.get("id_patient") or 0)
    passage_id = int(payload.get("id_passage") or 0)
    require_consultation_unlocked(passage_id)
    group_id = str(payload.get("groupe_id") or "").strip()
    try:
        image_ids = list(dict.fromkeys(int(value) for value in (payload.get("image_ids") or [])))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Sélection d’images invalide.")
    if not patient_id or not passage_id:
        raise HTTPException(status_code=400, detail="Groupe de destination invalide.")
    if not image_ids:
        raise HTTPException(status_code=400, detail="Sélectionnez au moins une image à importer.")

    with get_db_cursor() as cursor:
        if group_id:
            cursor.execute(
                "SELECT groupe_id, categorie FROM patient_echographies WHERE groupe_id = %s AND id_patient = %s AND id_passage = %s LIMIT 1;",
                (group_id, patient_id, passage_id)
            )
        elif str(payload.get("type_image") or "").strip().lower() in {"echographique", "radiologique", "planche_trois"} and str(payload.get("categorie") or "").strip():
            cursor.execute(
                "SELECT groupe_id, categorie FROM patient_echographies WHERE id_patient = %s AND id_passage = %s AND type_image = %s AND categorie = %s LIMIT 1;",
                (patient_id, passage_id, str(payload.get("type_image")).strip().lower(), str(payload.get("categorie")).strip()[:100])
            )
        else:
            raise HTTPException(status_code=400, detail="Groupe de destination invalide.")
        target_group = cursor.fetchone()
        if not target_group:
            raise HTTPException(status_code=404, detail="Le groupe de destination est introuvable.")
        group_id = group_id or str(target_group["groupe_id"])
        categorie = str(target_group["categorie"] or "Général")
        cursor.execute(
            "SELECT id, chemin_image, date_heure_str, type_image FROM patient_echographies WHERE id = ANY(%s) AND id_patient = %s AND id_passage = %s;",
            (image_ids, patient_id, passage_id)
        )
        selected = {int(row["id"]): row for row in cursor.fetchall()}
        if len(selected) != len(image_ids):
            raise HTTPException(status_code=400, detail="Une image sélectionnée n’appartient pas à ce dossier.")
        source_types = {str(row.get("type_image") or "echographique").strip().lower() for row in selected.values()}
        if not source_types.issubset({"echographique", "radiologique", "planche_trois"}):
            raise HTTPException(status_code=400, detail="Une image a un type non reconnu.")
        next_numbers = {}
        cursor.execute(
            "SELECT COALESCE(MAX(ordre_affichage), 0) + 1 AS next_order FROM patient_echographies WHERE groupe_id = %s AND id_patient = %s AND id_passage = %s;",
            (group_id, patient_id, passage_id)
        )
        next_order = int(cursor.fetchone()["next_order"] or 1)
        for source_type in source_types:
            cursor.execute(
                "SELECT COUNT(*) AS total FROM patient_echographies WHERE id_patient = %s AND id_passage = %s AND type_image = %s;",
                (patient_id, passage_id, source_type)
            )
            next_numbers[source_type] = int(cursor.fetchone()["total"] or 0) + 1
        for image_id in image_ids:
            row = selected[image_id]
            source_type = str(row.get("type_image") or "echographique").strip().lower()
            next_number = next_numbers[source_type]
            display_name = {"echographique": f"Image échographique {next_number}", "radiologique": f"Image radiologique {next_number}", "planche_trois": f"Image Planche A3 {next_number}"}[source_type]
            cursor.execute(
                """INSERT INTO patient_echographies
                   (id_patient, id_passage, chemin_image, nom_fichier, titre, date_heure_str, type_image, categorie, ordre_affichage, groupe_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);""",
                (patient_id, passage_id, row["chemin_image"], display_name, display_name,
                 row["date_heure_str"] or datetime.now().strftime("%d/%m/%Y (%H:%M)"), source_type, categorie, next_order, group_id)
            )
            next_numbers[source_type] += 1
            next_order += 1
    return {"success": True, "message": f"{len(image_ids)} image(s) ajoutée(s) à {categorie}, en conservant leur type d’origine."}


@dme_router.post("/echographies/reorder")
async def api_reorder_echographies(payload: Dict[str, Any]):
    """Enregistre l'ordre manuel des images dans un groupe du passage."""
    dme_manager.ensure_dme_schema()
    patient_id = int(payload.get("id_patient") or 0)
    passage_id = int(payload.get("id_passage") or 0)
    require_consultation_unlocked(passage_id)
    group_id = str(payload.get("groupe_id") or "").strip()
    try:
        image_ids = list(dict.fromkeys(int(value) for value in (payload.get("image_ids") or [])))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Ordre d’images invalide.")
    if not patient_id or not passage_id or not group_id:
        raise HTTPException(status_code=400, detail="Groupe d’images invalide.")
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT id FROM patient_echographies WHERE id_patient = %s AND id_passage = %s AND groupe_id = %s;",
            (patient_id, passage_id, group_id)
        )
        group_ids = {int(row["id"]) for row in cursor.fetchall()}
        if group_ids != set(image_ids):
            raise HTTPException(status_code=400, detail="La liste ne correspond pas entièrement au groupe à réordonner.")
        for position, image_id in enumerate(image_ids, start=1):
            cursor.execute(
                "UPDATE patient_echographies SET ordre_affichage = %s WHERE id = %s AND id_patient = %s AND id_passage = %s AND groupe_id = %s;",
                (position, image_id, patient_id, passage_id, group_id)
            )
    return {"success": True, "message": "Ordre des images enregistré."}


@dme_router.get("/echographies/file/{echo_id}")
async def api_get_echographie_file(echo_id: int):
    """Sert le fichier image échographique pour affichage dans la galerie et le carrousel."""
    with get_db_cursor() as cursor:
        cursor.execute("SELECT chemin_image FROM patient_echographies WHERE id = %s;", (echo_id,))
        row = cursor.fetchone()

    if not row or not os.path.exists(row["chemin_image"]):
        raise HTTPException(status_code=404, detail="Image introuvable.")

    media_type = "image/png"
    if row["chemin_image"].lower().endswith((".jpg", ".jpeg")):
        media_type = "image/jpeg"
    elif row["chemin_image"].lower().endswith(".webp"):
        media_type = "image/webp"

    return FileResponse(row["chemin_image"], media_type=media_type)


@dme_router.post("/echographies/delete")
async def api_delete_echographie(payload: Dict[str, Any]):
    echo_id = payload.get("id")
    with get_db_cursor() as cursor:
        cursor.execute("SELECT chemin_image, id_passage FROM patient_echographies WHERE id = %s;", (echo_id,))
        row = cursor.fetchone()
    if row:
        require_consultation_unlocked(row.get("id_passage"))
    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM patient_echographies WHERE id = %s;", (echo_id,))
        if row:
            cursor.execute("SELECT COUNT(*) AS total FROM patient_echographies WHERE chemin_image = %s;", (row["chemin_image"],))
            still_referenced = int(cursor.fetchone()["total"] or 0) > 0
        else:
            still_referenced = True
    if row and not still_referenced and os.path.exists(row["chemin_image"]):
        try:
            os.remove(row["chemin_image"])
        except Exception:
            pass

    return {"success": True, "message": "Cliché supprimé."}


@dme_router.post("/echographies/delete-group")
async def api_delete_echographie_group(payload: Dict[str, Any]):
    """Supprime les images d'un seul groupe, sans effacer les fichiers encore référencés ailleurs."""
    dme_manager.ensure_dme_schema()
    try:
        patient_id = int(payload.get("id_patient") or 0)
        passage_id = int(payload.get("id_passage") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Dossier patient invalide.")
    group_id = str(payload.get("groupe_id") or "").strip()
    if not patient_id or not passage_id or not group_id:
        raise HTTPException(status_code=400, detail="Groupe d’images invalide.")
    require_consultation_unlocked(passage_id, patient_id)

    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT id, chemin_image FROM patient_echographies WHERE id_patient = %s AND id_passage = %s AND groupe_id = %s;",
            (patient_id, passage_id, group_id)
        )
        group_rows = [dict(row) for row in cursor.fetchall()]
        if not group_rows:
            return {"success": True, "deleted": 0, "message": "Ce groupe ne contient déjà plus d’image."}
        cursor.execute(
            "DELETE FROM patient_echographies WHERE id_patient = %s AND id_passage = %s AND groupe_id = %s;",
            (patient_id, passage_id, group_id)
        )

    patient_folder = os.path.realpath(os.path.join(str(BASE_DIR), "uploads", "dossiers_patients", str(patient_id), "echographies"))
    for image_path in {str(row.get("chemin_image") or "") for row in group_rows} - {""}:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total FROM patient_echographies WHERE chemin_image = %s;", (image_path,))
            still_referenced = int(cursor.fetchone()["total"] or 0) > 0
        if still_referenced:
            continue
        resolved_path = os.path.realpath(image_path)
        try:
            if os.path.commonpath([patient_folder, resolved_path]) == patient_folder and os.path.isfile(resolved_path):
                os.remove(resolved_path)
        except (OSError, ValueError):
            logger.warning("Impossible de supprimer le fichier d'image du groupe DME: %s", image_path)

    return {"success": True, "deleted": len(group_rows), "message": f"{len(group_rows)} image(s) supprimée(s) du groupe."}


# ============================================================================
# RENDU DU COMPOSANT HTML ET JAVASCRIPT COMPLET
# ============================================================================
def _render_html() -> str:
    """Génère l'HTML du DME complet avec carrousel échographie et boutons Vision Pdf."""
    return """
    <div id="view-dme" class="space-y-6">

        <!-- BANDEAU D'EN-TÊTE DU MODULE -->
        <div class="banner-glow rounded-3xl p-6 lg:p-8 text-white shadow-xl flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
            <div>
                <h1 class="text-2xl font-extrabold tracking-tight mb-1">Dossier Médical Électronique (DME)</h1>
                <p class="text-xs text-slate-300">Consultation clinique, ordonnances, comptes rendus, carrousel échographique et historique.</p>
            </div>

            <div class="flex items-center gap-3">
                <button type="button" onclick="loadModule('gestion_passages')" class="flex items-center gap-2 bg-white/10 hover:bg-white/20 text-white border border-white/10 px-4 py-2.5 rounded-2xl font-bold text-xs shadow-sm transition cursor-pointer">
                    <i class="ph-bold ph-arrow-left text-base"></i>
                    <span>Retour à Accueil & Passages</span>
                </button>
            </div>
        </div>

        <!-- BARRE DES DOSSIERS PATIENTS OUVERTS -->
        <div class="flex items-center gap-2 overflow-x-auto pb-1" id="dmeTabsBar"></div>

        <!-- ZONE DU DOSSIER ACTIF -->
        <div id="dmePatientContainer" class="hidden space-y-6">

            <!-- EN-TÊTE DU PATIENT ACTIF -->
            <div class="bg-white dark:bg-[#121c15] p-6 rounded-3xl border border-slate-100 dark:border-white/5 shadow-sm flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
                <div class="flex items-center space-x-4">
                    <img id="dme_hdr_avatar" src="/static/images/avatar-placeholder.svg?w=120" class="w-16 h-16 rounded-2xl object-cover border-2 border-brand-neon shadow-md">
                    <div>
                        <div class="flex items-center gap-2">
                            <h2 id="dme_hdr_fullname" class="text-lg font-black text-slate-900 dark:text-white">Patient</h2>
                            <span id="dme_hdr_blood" class="px-2 py-0.5 rounded-lg text-[10px] font-extrabold bg-rose-50 text-rose-600 border border-rose-200">O+</span>
                            <span id="dme_hdr_dossier" class="px-2 py-0.5 rounded-lg text-[10px] font-bold bg-slate-100 dark:bg-black/30 border border-slate-200 dark:border-white/10 text-slate-700 dark:text-slate-300 font-mono">#DOS-0000</span>
                        </div>
                        <div class="text-xs text-slate-400 mt-1 flex flex-wrap items-center gap-3">
                            <span>🎂 <span id="dme_hdr_dob">-</span></span>
                            <span>📞 <span id="dme_hdr_tel">-</span></span>
                            <span>📍 <span id="dme_hdr_location">-</span></span>
                            <span>🩺 Médecin Réf: <span id="dme_hdr_medecin">Aucun</span></span>
                        </div>
                        <div class="mt-3 flex flex-wrap items-center gap-2">
                            <span class="text-[10px] font-extrabold uppercase tracking-wide text-slate-500 dark:text-slate-400">Prestation(s) de ce passage :</span>
                            <div id="dme_hdr_prestations" class="flex flex-wrap items-center gap-1.5">
                                <span class="text-[11px] text-slate-400">Chargement…</span>
                            </div>
                        </div>
                        <div id="dmeHandoffNoteBanner" class="hidden mt-3 max-w-3xl rounded-xl border border-amber-200 bg-amber-50 px-3 py-2.5 text-xs text-amber-950 dark:border-amber-900/50 dark:bg-amber-950/30 dark:text-amber-100">
                            <div class="flex items-start gap-2"><i class="ph-bold ph-notepad text-base text-amber-600"></i><div><b>Note de reprise</b><div id="dmeHandoffNoteText" class="mt-1 whitespace-pre-wrap"></div><div id="dmeHandoffDoctorText" class="mt-1 text-[10px] font-bold"></div></div></div>
                        </div>
                    </div>
                </div>

                <div class="flex flex-wrap items-center gap-3">
                <button type="button" onclick="window.openDmeDicomPrint()" class="inline-flex items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-[10px] font-extrabold text-emerald-800 shadow-sm transition hover:bg-emerald-100 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-300"><i class="ph-bold ph-images text-base"></i><span>DICOM Print</span></button>
                <span id="dmeAccessModeBadge" class="px-3 py-2 rounded-xl text-[10px] font-bold bg-slate-100 text-slate-600 dark:bg-white/5 dark:text-slate-300">Lecture seule</span>
                <div class="flex flex-col gap-1">
                    <button type="button" id="dmeResetConsultationButton" onclick="resetActiveDmeConsultation()" disabled class="inline-flex items-center gap-2 rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-xs font-bold text-rose-700 hover:bg-rose-100"><i class="ph-bold ph-arrow-counter-clockwise"></i>Réinitialiser la consultation</button>
                    <span id="dmeResetConsultationHint" class="text-[10px] text-slate-500">Vérification des droits…</span>
                </div>
                <button type="button" id="dmeSaveConsultationButton" onclick="saveActiveDmeConsultation()" class="px-6 py-2.5 bg-brand-neon hover:bg-brand-neonDark text-black font-extrabold text-xs rounded-2xl shadow-md shadow-brand-neon/20 transition flex items-center gap-2 cursor-pointer">
                    <i class="ph-bold ph-floppy-disk text-base"></i>
                    <span>Enregistrer la Consultation</span>
                </button>
                </div>
            </div>

            <!-- LES 5 SOUS-ONGLETS -->
            <div class="flex items-center gap-2 overflow-x-auto pb-1" id="dmeSubTabsBar">
                <button type="button" onclick="switchDmeSubTab('consultation')" id="subtab-btn-consultation" class="dme-subtab-btn flex items-center gap-2 px-4 py-2.5 rounded-2xl text-xs font-bold transition cursor-pointer bg-brand-neon text-black shadow-md shadow-brand-neon/20">
                    <i class="ph-bold ph-stethoscope text-sm"></i>
                    <span>1. Consultation Clinique & Constantes</span>
                </button>
                <button type="button" onclick="switchDmeSubTab('consommations')" id="subtab-btn-consommations" class="dme-subtab-btn flex items-center gap-2 px-4 py-2.5 rounded-2xl text-xs font-bold transition cursor-pointer bg-white dark:bg-[#121c15] text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white border border-slate-200 dark:border-white/10">
                    <i class="ph-bold ph-first-aid-kit text-sm"></i>
                    <span>Produits consommés</span>
                </button>
                <button type="button" onclick="switchDmeSubTab('ordonnance')" id="subtab-btn-ordonnance" class="dme-subtab-btn flex items-center gap-2 px-4 py-2.5 rounded-2xl text-xs font-bold transition cursor-pointer bg-white dark:bg-[#121c15] text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white border border-slate-200 dark:border-white/10">
                    <i class="ph-bold ph-pill text-sm"></i>
                    <span>2. Rédiger Ordonnance</span>
                </button>
                <button type="button" onclick="switchDmeSubTab('compterendu')" id="subtab-btn-compterendu" class="dme-subtab-btn flex items-center gap-2 px-4 py-2.5 rounded-2xl text-xs font-bold transition cursor-pointer bg-white dark:bg-[#121c15] text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white border border-slate-200 dark:border-white/10">
                    <i class="ph-bold ph-file-text text-sm"></i>
                    <span>3. Compte Rendu & Livret</span>
                </button>
                <button type="button" onclick="switchDmeSubTab('echographie')" id="subtab-btn-echographie" class="dme-subtab-btn flex items-center gap-2 px-4 py-2.5 rounded-2xl text-xs font-bold transition cursor-pointer bg-white dark:bg-[#121c15] text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white border border-slate-200 dark:border-white/10">
                    <i class="ph-bold ph-image text-sm"></i>
                    <span>4. Dossier Échographie (PNG)</span>
                </button>
                <button type="button" onclick="switchDmeSubTab('historique')" id="subtab-btn-historique" class="dme-subtab-btn flex items-center gap-2 px-4 py-2.5 rounded-2xl text-xs font-bold transition cursor-pointer bg-white dark:bg-[#121c15] text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white border border-slate-200 dark:border-white/10">
                    <i class="ph-bold ph-clock-counter-clockwise text-sm"></i>
                    <span>5. Historique & Archives</span>
                </button>
                <button type="button" onclick="switchDmeSubTab('croissance')" id="subtab-btn-croissance" class="dme-subtab-btn hidden flex items-center gap-2 px-4 py-2.5 rounded-2xl text-xs font-bold transition cursor-pointer bg-white dark:bg-[#121c15] text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white border border-slate-200 dark:border-white/10">
                    <i class="ph-bold ph-chart-line-up text-sm"></i>
                    <span>Courbes de croissance</span>
                </button>
            </div>

            <!-- PANNEAU : CONSOMMATIONS RÉELLES -->
            <div id="dme-panel-consommations" class="dme-subtab-panel space-y-5 hidden">
                <section class="rounded-3xl border border-emerald-100 bg-white p-5 shadow-sm dark:border-white/10 dark:bg-[#121c15]">
                    <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
                        <div><h3 class="flex items-center gap-2 text-sm font-extrabold text-slate-800 dark:text-white"><i class="ph-bold ph-first-aid-kit text-emerald-600"></i>Médicaments et dispositifs réellement consommés</h3><p class="mt-1 text-[11px] text-slate-500">Cette fiche est rattachée au patient, au passage et à la consultation active. Elle ne remplace pas l’ordonnance.</p></div>
                        <span id="dmeConsumptionUserBadge" class="rounded-xl bg-indigo-50 px-3 py-2 text-[10px] font-extrabold text-indigo-700 dark:bg-indigo-400/10 dark:text-indigo-200">Utilisateur connecté</span>
                    </div>
                    <form id="dmeConsumptionForm" class="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
                        <label class="text-[10px] font-bold uppercase text-slate-500">Type<select id="dmeConsumptionType" required class="mt-1 w-full rounded-xl border bg-slate-50 p-2.5 text-xs dark:border-white/10 dark:bg-black/20 dark:text-white"><option>Médicament</option><option>Dispositif médical</option></select></label>
                        <label class="text-[10px] font-bold uppercase text-slate-500 xl:col-span-2">Produit consommé<input id="dmeConsumptionProduct" required maxlength="200" placeholder="Nom du médicament ou dispositif" class="mt-1 w-full rounded-xl border bg-slate-50 p-2.5 text-xs dark:border-white/10 dark:bg-black/20 dark:text-white"></label>
                        <div class="grid grid-cols-2 gap-2"><label class="text-[10px] font-bold uppercase text-slate-500">Quantité<input id="dmeConsumptionQty" type="number" min="0.001" step="0.001" required placeholder="1" class="mt-1 w-full rounded-xl border bg-slate-50 p-2.5 text-xs dark:border-white/10 dark:bg-black/20 dark:text-white"></label><label class="text-[10px] font-bold uppercase text-slate-500">Unité<input id="dmeConsumptionUnit" required maxlength="30" list="dmeConsumptionUnits" placeholder="ml, boîte…" class="mt-1 w-full rounded-xl border bg-slate-50 p-2.5 text-xs dark:border-white/10 dark:bg-black/20 dark:text-white"><datalist id="dmeConsumptionUnits"><option value="boîte"><option value="unité"><option value="ml"><option value="cc"><option value="mg"><option value="g"><option value="ampoule"><option value="flacon"><option value="seringue"><option value="comprimé"><option value="poche"></datalist></label></div>
                        <label class="text-[10px] font-bold uppercase text-slate-500">Service<select id="dmeConsumptionService" required class="mt-1 w-full rounded-xl border bg-slate-50 p-2.5 text-xs dark:border-white/10 dark:bg-black/20 dark:text-white"></select></label>
                        <label class="text-[10px] font-bold uppercase text-slate-500">Salle<select id="dmeConsumptionRoom" class="mt-1 w-full rounded-xl border bg-slate-50 p-2.5 text-xs dark:border-white/10 dark:bg-black/20 dark:text-white"></select></label>
                        <label class="text-[10px] font-bold uppercase text-slate-500">Enregistré par<input id="dmeConsumptionUser" readonly class="mt-1 w-full rounded-xl border bg-slate-100 p-2.5 text-xs dark:border-white/10 dark:bg-white/5 dark:text-white"></label>
                        <label class="text-[10px] font-bold uppercase text-slate-500">N° de lot (facultatif)<input id="dmeConsumptionLot" maxlength="100" class="mt-1 w-full rounded-xl border bg-slate-50 p-2.5 text-xs dark:border-white/10 dark:bg-black/20 dark:text-white"></label>
                        <label class="text-[10px] font-bold uppercase text-slate-500 md:col-span-2 xl:col-span-3">Notes<textarea id="dmeConsumptionNotes" maxlength="1000" rows="1" class="mt-1 w-full rounded-xl border bg-slate-50 p-2.5 text-xs dark:border-white/10 dark:bg-black/20 dark:text-white"></textarea></label>
                        <div class="flex items-end"><button id="dmeConsumptionSave" type="submit" class="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-brand-neon px-4 py-2.5 text-xs font-extrabold text-black"><i class="ph-bold ph-floppy-disk"></i>Enregistrer la consommation</button></div>
                    </form>
                    <p id="dmeConsumptionMessage" class="mt-3 hidden rounded-xl p-3 text-xs font-semibold"></p>
                </section>
                <section class="rounded-3xl border border-slate-100 bg-white p-5 shadow-sm dark:border-white/10 dark:bg-[#121c15]">
                    <h4 class="mb-3 text-xs font-extrabold uppercase tracking-wide text-slate-700 dark:text-white">Consommations de cette consultation</h4>
                    <div class="overflow-x-auto"><table class="w-full min-w-[760px] text-left text-xs"><thead class="bg-slate-50 text-[10px] uppercase text-slate-500 dark:bg-white/5"><tr><th class="p-2.5">Date</th><th class="p-2.5">Produit</th><th class="p-2.5">Quantité</th><th class="p-2.5">Service</th><th class="p-2.5">Salle</th><th class="p-2.5">Utilisateur</th><th class="p-2.5">Lot</th></tr></thead><tbody id="dmeConsumptionRows" class="divide-y divide-slate-100 dark:divide-white/5"><tr><td colspan="7" class="p-5 text-center text-slate-400">Aucune consommation enregistrée.</td></tr></tbody></table></div>
                </section>
            </div>

            <!-- ================= PANNEAU 1 : CONSULTATION & CONSTANTES ================= -->
            <div id="dme-panel-consultation" class="dme-subtab-panel space-y-6">
                <div class="bg-emerald-50/70 dark:bg-emerald-950/20 p-5 rounded-3xl border border-emerald-200/70 dark:border-emerald-900/40 space-y-3">
                    <h3 class="text-xs font-extrabold uppercase tracking-wider text-emerald-900 dark:text-emerald-200 flex items-center gap-2"><i class="ph-bold ph-clipboard-text text-base"></i>Observation médicale & renseignements cliniques du passage</h3>
                    <p id="dme_passage_observation" class="text-sm text-slate-700 dark:text-slate-200 whitespace-pre-wrap">Aucune observation médicale saisie.</p>
                    <div id="dme_passage_clinical_info" class="flex flex-wrap gap-2"></div>
                </div>
                <!-- Constantes vitales et IMC -->
                <div class="bg-brand-neon/10 dark:bg-brand-neon/5 p-5 rounded-3xl border border-brand-neon/20 space-y-3">
                    <h4 class="text-xs font-bold text-emerald-950 dark:text-brand-neon uppercase tracking-wider flex items-center gap-1.5">
                        <i class="ph-bold ph-heartbeat text-base"></i>
                        <span>Constantes Vitales & Paramètres Biométriques</span>
                    </h4>
                    <div class="grid grid-cols-2 sm:grid-cols-4 md:grid-cols-7 gap-3">
                        <div>
                            <label class="block text-[10px] font-bold text-slate-500 uppercase mb-1">Tension (TA)</label>
                            <input type="text" id="dme_ta" placeholder="12/8" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/40 text-center font-bold text-slate-800 dark:text-white outline-none">
                        </div>
                        <div>
                            <label class="block text-[10px] font-bold text-slate-500 uppercase mb-1">Pouls (BPM)</label>
                            <input type="text" id="dme_pouls" placeholder="75" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/40 text-center font-bold text-slate-800 dark:text-white outline-none">
                        </div>
                        <div>
                            <label class="block text-[10px] font-bold text-slate-500 uppercase mb-1">Température (°C)</label>
                            <input type="text" id="dme_temp" placeholder="37.0" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/40 text-center font-bold text-slate-800 dark:text-white outline-none">
                        </div>
                        <div>
                            <label class="block text-[10px] font-bold text-slate-500 uppercase mb-1">Poids (kg)</label>
                            <input type="number" step="0.5" id="dme_poids" oninput="calculateImc()" placeholder="70" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/40 text-center font-bold text-slate-800 dark:text-white outline-none">
                        </div>
                        <div>
                            <label class="block text-[10px] font-bold text-slate-500 uppercase mb-1">Taille (cm)</label>
                            <input type="number" step="1" id="dme_taille" oninput="calculateImc()" placeholder="175" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/40 text-center font-bold text-slate-800 dark:text-white outline-none">
                        </div>
                        <div>
                            <label class="block text-[10px] font-bold text-brand-neon uppercase mb-1">IMC (kg/m²)</label>
                            <input type="text" id="dme_imc" readonly placeholder="Auto" class="w-full text-xs p-2.5 rounded-xl border border-brand-neon/40 bg-brand-neon/20 text-center font-black text-emerald-900 dark:text-brand-neon outline-none">
                        </div>
                        <div>
                            <label class="block text-[10px] font-bold text-slate-500 uppercase mb-1">Glycémie (g/L)</label>
                            <input type="text" id="dme_glycemie" placeholder="0.95" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/40 text-center font-bold text-slate-800 dark:text-white outline-none">
                        </div>
                    </div>
                </div>

                <!-- Les 8 champs ordonnés en 2 colonnes de 50% -->
                <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
                    <div class="bg-rose-500/10 p-5 rounded-3xl border border-rose-500/30 space-y-2">
                        <label class="block text-xs font-black text-rose-600 dark:text-rose-400 uppercase tracking-wider flex items-center gap-1.5">
                            <i class="ph-bold ph-warning-circle text-base"></i>
                            <span>1. Allergies & Intolérances Médicamenteuses / Alimentaires</span>
                        </label>
                        <textarea id="dme_allergies" rows="2" placeholder="Allergies (Pénicilline, AINS...), intolérances..." class="w-full text-xs p-3 rounded-2xl border border-rose-300 dark:border-rose-900/40 bg-white dark:bg-black/40 text-slate-800 dark:text-white font-bold outline-none focus:border-rose-500"></textarea>
                    </div>

                    <div class="bg-white dark:bg-[#121c15] p-5 rounded-3xl border border-slate-100 dark:border-white/5 space-y-2">
                        <label class="block text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5">
                            <i class="ph-bold ph-pill text-brand-neon"></i>
                            <span>2. Traitement en cours</span>
                        </label>
                        <textarea id="dme_traitement" rows="2" placeholder="Traitements actuels, posologies..." class="w-full text-xs p-3 rounded-2xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium"></textarea>
                    </div>

                    <div class="bg-white dark:bg-[#121c15] p-5 rounded-3xl border border-slate-100 dark:border-white/5 space-y-2">
                        <label class="block text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5">
                            <i class="ph-bold ph-first-aid text-sky-500"></i>
                            <span>3. Antécédents Médicaux</span>
                        </label>
                        <textarea id="dme_ant_med" rows="2" placeholder="HTA, Diabète, Asthme, pathologies..." class="w-full text-xs p-3 rounded-2xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium"></textarea>
                    </div>

                    <div class="bg-white dark:bg-[#121c15] p-5 rounded-3xl border border-slate-100 dark:border-white/5 space-y-2">
                        <label class="block text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5">
                            <i class="ph-bold ph-scissors text-amber-500"></i>
                            <span>4. Antécédents Chirurgicaux & Hospitalisations</span>
                        </label>
                        <textarea id="dme_ant_chir" rows="2" placeholder="Chirurgies passées, césariennes..." class="w-full text-xs p-3 rounded-2xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium"></textarea>
                    </div>

                    <div class="bg-white dark:bg-[#121c15] p-5 rounded-3xl border border-slate-100 dark:border-white/5 space-y-2">
                        <label class="block text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5">
                            <i class="ph-bold ph-notepad text-purple-500"></i>
                            <span>5. Notes Particulières & Habitudes de vie</span>
                        </label>
                        <textarea id="dme_habitudes" rows="2" placeholder="Tabac, profession, mode de vie..." class="w-full text-xs p-3 rounded-2xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium"></textarea>
                    </div>

                    <div class="bg-white dark:bg-[#121c15] p-5 rounded-3xl border border-slate-100 dark:border-white/5 space-y-2">
                        <label class="block text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5">
                            <i class="ph-bold ph-chat-circle-dots text-emerald-500"></i>
                            <span>6. Motif de Consultation</span>
                        </label>
                        <textarea id="dme_motif" rows="2" placeholder="Symptômes décrits, anamnèse..." class="w-full text-xs p-3 rounded-2xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium"></textarea>
                    </div>

                    <div class="bg-white dark:bg-[#121c15] p-5 rounded-3xl border border-slate-100 dark:border-white/5 space-y-2">
                        <label class="block text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5">
                            <i class="ph-bold ph-stethoscope text-sky-500"></i>
                            <span>7. Diagnostic & Hypothèse</span>
                        </label>
                        <textarea id="dme_diagnostic" rows="2" placeholder="Diagnostic principal..." class="w-full text-xs p-3 rounded-2xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-bold"></textarea>
                    </div>

                    <div class="bg-white dark:bg-[#121c15] p-5 rounded-3xl border border-slate-100 dark:border-white/5 space-y-2">
                        <label class="block text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5">
                            <i class="ph-bold ph-arrow-circle-right text-brand-neon"></i>
                            <span>8. Conduite à Tenir</span>
                        </label>
                        <textarea id="dme_conduite" rows="2" placeholder="Examens complémentaires, surveillance..." class="w-full text-xs p-3 rounded-2xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium"></textarea>
                    </div>
                </div>

                <style>
                    #dme-panel-consultation .dme-specialty-field { display:block; padding:12px; border:1px solid #e2e8f0; border-radius:14px; background:#fff; color:#475569; font-size:10px; font-weight:800; }
                    #dme-panel-consultation .dme-specialty-field textarea, #dme-panel-consultation .dme-specialty-field select, #dme-panel-consultation .dme-specialty-field input:not([type="hidden"]) { display:block; width:100%; margin-top:8px; min-height:40px; padding:9px 10px; border:1px solid #dbe3ed; border-radius:10px; background:#f8fafc; color:#1e293b; font-size:11px; font-weight:500; outline:none; }
                    #dme-panel-consultation .dme-specialty-field textarea:focus, #dme-panel-consultation .dme-specialty-field select:focus, #dme-panel-consultation .dme-specialty-field input:not([type="hidden"]):focus { border-color:#818cf8; box-shadow:0 0 0 3px rgba(99,102,241,.12); }
                    .dark #dme-panel-consultation .dme-specialty-field { border-color:rgba(255,255,255,.1); background:#121c15; color:#cbd5e1; }
                    .dark #dme-panel-consultation .dme-specialty-field textarea, .dark #dme-panel-consultation .dme-specialty-field select, .dark #dme-panel-consultation .dme-specialty-field input:not([type="hidden"]) { border-color:rgba(255,255,255,.1); background:rgba(0,0,0,.25); color:#f8fafc; }
                </style>
                <section class="rounded-3xl border border-indigo-100 bg-indigo-50/50 p-5 dark:border-indigo-900/40 dark:bg-indigo-950/15 space-y-4">
                    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                        <div><h3 class="flex items-center gap-2 text-xs font-extrabold uppercase tracking-wider text-indigo-950 dark:text-indigo-200"><i class="ph-bold ph-first-aid-kit text-base"></i>Examen clinique & sections spécialisées</h3><p class="mt-1 text-[11px] text-slate-500 dark:text-slate-400">Le formulaire commun reste inchangé. Ajoutez une ou plusieurs sections spécialisées à la demande.</p></div>
                        <button type="button" onclick="openDmeSpecialtyPicker()" class="inline-flex shrink-0 items-center justify-center gap-2 rounded-xl bg-indigo-600 px-4 py-2.5 text-[11px] font-extrabold text-white shadow-sm transition hover:bg-indigo-700"><i class="ph-bold ph-plus-circle text-base"></i>Ajouter une spécialité</button>
                    </div>
                    <div id="dmeSpecialtyPanel-gastro" data-dme-specialty-panel="gastro" class="hidden grid grid-cols-1 gap-3 md:grid-cols-2">
                        <label class="dme-specialty-field">Symptômes digestifs, début, durée et évolution<textarea data-dme-specialty="gastro" data-dme-specialty-field="symptomes_digestifs" rows="2" placeholder="Douleur, reflux, dysphagie, nausées, vomissements…"></textarea></label>
                        <label class="dme-specialty-field">Transit et selles<textarea data-dme-specialty="gastro" data-dme-specialty-field="transit_selles" rows="2" placeholder="Fréquence, consistance, diarrhée/constipation, sang ou glaires…"></textarea></label>
                        <label class="dme-specialty-field">Symptômes associés et contexte<textarea data-dme-specialty="gastro" data-dme-specialty-field="symptomes_associes" rows="2" placeholder="Fièvre, alimentation, voyage, antibiotiques récents…"></textarea></label>
                        <label class="dme-specialty-field">Signes d’alerte recherchés<textarea data-dme-specialty="gastro" data-dme-specialty-field="signes_alerte_digestifs" rows="2" placeholder="Perte de poids, saignement, anémie connue, vomissements persistants…"></textarea></label>
                        <label class="dme-specialty-field">Antécédents et explorations digestives<textarea data-dme-specialty="gastro" data-dme-specialty-field="explorations_digestives" rows="2" placeholder="Endoscopie, coloscopie, imagerie, biopsies, H. pylori, résultats…"></textarea></label>
                        <label class="dme-specialty-field">Examen abdominal<textarea data-dme-specialty="gastro" data-dme-specialty-field="examen_abdominal" rows="2" placeholder="Constatations de l’examen abdominal…"></textarea></label>
                        <label class="dme-specialty-field md:col-span-2">Évaluation et plan digestif<textarea data-dme-specialty="gastro" data-dme-specialty-field="plan_digestif" rows="2" placeholder="Examens envisagés, prise en charge et suivi décidés par le médecin…"></textarea></label>
                    </div>

                    <div id="dmeSpecialtyPanel-anesthesie" data-dme-specialty-panel="anesthesie" class="hidden grid grid-cols-1 gap-3 md:grid-cols-2">
                        <label class="dme-specialty-field">Intervention prévue, date et indication<textarea data-dme-specialty="anesthesie" data-dme-specialty-field="intervention_prevue" rows="2"></textarea></label>
                        <label class="dme-specialty-field">Antécédents anesthésiques<textarea data-dme-specialty="anesthesie" data-dme-specialty-field="antecedents_anesthesiques" rows="2" placeholder="Anesthésies antérieures, incidents ou complications…"></textarea></label>
                        <label class="dme-specialty-field">Évaluation des voies aériennes et dentition<textarea data-dme-specialty="anesthesie" data-dme-specialty-field="voies_aeriennes" rows="2"></textarea></label>
                        <label class="dme-specialty-field">Évaluation cardio-respiratoire<textarea data-dme-specialty="anesthesie" data-dme-specialty-field="evaluation_cardiorespiratoire" rows="2"></textarea></label>
                        <label class="dme-specialty-field">Capacité fonctionnelle et éléments de risque<textarea data-dme-specialty="anesthesie" data-dme-specialty-field="capacite_fonctionnelle" rows="2"></textarea></label>
                        <label class="dme-specialty-field">Classe ASA évaluée par le médecin<select data-dme-specialty="anesthesie" data-dme-specialty-field="classe_asa"><option value="">Non renseignée</option><option>ASA I</option><option>ASA II</option><option>ASA III</option><option>ASA IV</option><option>ASA V</option><option>ASA VI</option></select></label>
                        <label class="dme-specialty-field">Examens préopératoires et consultations consultés<textarea data-dme-specialty="anesthesie" data-dme-specialty-field="examens_preoperatoires" rows="2"></textarea></label>
                        <label class="dme-specialty-field">Consentement et information<textarea data-dme-specialty="anesthesie" data-dme-specialty-field="consentement_anesthesique" rows="2"></textarea></label>
                        <label class="dme-specialty-field md:col-span-2">Plan anesthésique et consignes<textarea data-dme-specialty="anesthesie" data-dme-specialty-field="plan_anesthesique" rows="2" placeholder="Plan et consignes définis par l’anesthésiste…"></textarea></label>
                    </div>

                    <div id="dmeSpecialtyPanel-dermatologie" data-dme-specialty-panel="dermatologie" class="hidden grid grid-cols-1 gap-3 md:grid-cols-2">
                        <label class="dme-specialty-field">Localisation de la lésion<textarea data-dme-specialty="dermatologie" data-dme-specialty-field="lesion_localisation" rows="2" placeholder="Région anatomique, côté…"></textarea></label>
                        <label class="dme-specialty-field">Apparition et évolution<textarea data-dme-specialty="dermatologie" data-dme-specialty-field="lesion_evolution" rows="2" placeholder="Date/durée, changement de taille, forme ou couleur…"></textarea></label>
                        <label class="dme-specialty-field">Description clinique<textarea data-dme-specialty="dermatologie" data-dme-specialty-field="lesion_description" rows="2" placeholder="Nombre, taille, forme, couleur, limites, aspect…"></textarea></label>
                        <label class="dme-specialty-field">Symptômes et facteurs contextuels<textarea data-dme-specialty="dermatologie" data-dme-specialty-field="lesion_symptomes" rows="2" placeholder="Prurit, douleur, saignement, exposition solaire…"></textarea></label>
                        <label class="dme-specialty-field">Étendue de l’examen cutané<textarea data-dme-specialty="dermatologie" data-dme-specialty-field="examen_cutane" rows="2" placeholder="Examen ciblé ou cutané complet, zones examinées…"></textarea></label>
                        <label class="dme-specialty-field">Antécédents dermatologiques et familiaux / examens<textarea data-dme-specialty="dermatologie" data-dme-specialty-field="dermoscopie_histologie" rows="2" placeholder="Dermoscopie, biopsie/anatomopathologie et antécédents utiles…"></textarea></label>
                        <label class="dme-specialty-field">Photos cliniques / dermoscopie<textarea data-dme-specialty="dermatologie" data-dme-specialty-field="photos_cliniques" rows="2" placeholder="Référence ou commentaire sur les images liées au dossier…"></textarea></label>
                        <label class="dme-specialty-field">Évaluation et suivi<textarea data-dme-specialty="dermatologie" data-dme-specialty-field="plan_dermatologie" rows="2" placeholder="Diagnostic différentiel, prélèvement, traitement et suivi…"></textarea></label>
                    </div>

                    <div id="dmeSpecialtyPanel-gynecologie" data-dme-specialty-panel="gynecologie" class="hidden grid grid-cols-1 gap-3 md:grid-cols-2">
                        <label class="dme-specialty-field">Dernières règles<textarea data-dme-specialty="gynecologie" data-dme-specialty-field="dernieres_regles" rows="2" placeholder="Date et informations utiles…"></textarea></label>
                        <label class="dme-specialty-field">Cycles et symptômes menstruels<textarea data-dme-specialty="gynecologie" data-dme-specialty-field="cycles_menstruels" rows="2" placeholder="Régularité, durée, abondance, douleurs…"></textarea></label>
                        <label class="dme-specialty-field">Antécédents de grossesse et d’accouchement<textarea data-dme-specialty="gynecologie" data-dme-specialty-field="antecedents_grossesse" rows="2" placeholder="Gestité/parité, issues, terme, voie d’accouchement, complications…"></textarea></label>
                        <label class="dme-specialty-field">Contraception et contexte hormonal<textarea data-dme-specialty="gynecologie" data-dme-specialty-field="contraception" rows="2"></textarea></label>
                        <label class="dme-specialty-field">Dépistage du col et antécédents gynécologiques<textarea data-dme-specialty="gynecologie" data-dme-specialty-field="depistage_col" rows="2" placeholder="Date/résultat du dernier dépistage, anomalies ou gestes antérieurs…"></textarea></label>
                        <label class="dme-specialty-field">Symptômes gynécologiques et ménopause<textarea data-dme-specialty="gynecologie" data-dme-specialty-field="symptomes_gynecologiques" rows="2"></textarea></label>
                        <label class="dme-specialty-field">Examen gynécologique<textarea data-dme-specialty="gynecologie" data-dme-specialty-field="examen_gynecologique" rows="2" placeholder="Examen réalisé et constatations pertinentes…"></textarea></label>
                        <label class="dme-specialty-field">Évaluation, plan et suivi<textarea data-dme-specialty="gynecologie" data-dme-specialty-field="plan_gynecologique" rows="2"></textarea></label>
                    </div>

                    <div id="dmeSpecialtyPanel-general" data-dme-specialty-panel="general" class="hidden grid grid-cols-1 gap-3 md:grid-cols-2">
                        <label class="dme-specialty-field">Antécédents familiaux<textarea data-dme-specialty="general" data-dme-specialty-field="antecedents_familiaux" rows="2"></textarea></label>
                        <label class="dme-specialty-field">Revue des symptômes par appareil<textarea data-dme-specialty="general" data-dme-specialty-field="revue_systemes" rows="2" placeholder="Systèmes pertinents et symptômes rapportés…"></textarea></label>
                        <label class="dme-specialty-field">Autonomie et capacité fonctionnelle<textarea data-dme-specialty="general" data-dme-specialty-field="autonomie_fonctionnelle" rows="2"></textarea></label>
                        <label class="dme-specialty-field">Prévention et dépistages<textarea data-dme-specialty="general" data-dme-specialty-field="prevention_depistage" rows="2" placeholder="Dépistages discutés/réalisés et date si connue…"></textarea></label>
                        <label class="dme-specialty-field">Vaccinations<textarea data-dme-specialty="general" data-dme-specialty-field="vaccinations" rows="2" placeholder="Statut connu, éléments à vérifier…"></textarea></label>
                        <label class="dme-specialty-field">Suivi et orientation<textarea data-dme-specialty="general" data-dme-specialty-field="suivi_orientation" rows="2" placeholder="Prochain suivi, orientation ou référent…"></textarea></label>
                    </div>

                    <div id="dmeSpecialtyPanel-medecine_physique" data-dme-specialty-panel="medecine_physique" class="hidden grid grid-cols-1 gap-3 md:grid-cols-2">
                        <label class="dme-specialty-field">Motif de rééducation et objectifs du patient<textarea data-dme-specialty="medecine_physique" data-dme-specialty-field="motif_reeducation" rows="2" placeholder="Motif d’orientation, attentes et objectifs exprimés…"></textarea></label>
                        <label class="dme-specialty-field">Histoire fonctionnelle et évolution<textarea data-dme-specialty="medecine_physique" data-dme-specialty-field="histoire_fonctionnelle" rows="2" placeholder="Début, évolution, événements déclenchants, soins déjà réalisés…"></textarea></label>
                        <label class="dme-specialty-field">Douleur<textarea data-dme-specialty="medecine_physique" data-dme-specialty-field="douleur_reeducation" rows="2" placeholder="Si présente : localisation, côté, intensité /10, fréquence et facteurs aggravants ou soulageants…"></textarea></label>
                        <label class="dme-specialty-field">Limitations dans les activités quotidiennes<textarea data-dme-specialty="medecine_physique" data-dme-specialty-field="limitations_activites" rows="2" placeholder="Autosoins, activités domestiques, travail/études, loisirs et participation…"></textarea></label>
                        <label class="dme-specialty-field">Autonomie, transferts et déplacements<textarea data-dme-specialty="medecine_physique" data-dme-specialty-field="autonomie_deplacements" rows="2" placeholder="Niveau d’aide, transferts, marche, endurance et distance si évaluée…"></textarea></label>
                        <label class="dme-specialty-field">Examen articulaire et moteur<textarea data-dme-specialty="medecine_physique" data-dme-specialty-field="examen_articulaire_moteur" rows="2" placeholder="Mobilités, amplitudes, force, tonus, trophicité et constatations pertinentes…"></textarea></label>
                        <label class="dme-specialty-field">Examen neurologique fonctionnel<textarea data-dme-specialty="medecine_physique" data-dme-specialty-field="examen_neurologique_fonctionnel" rows="2" placeholder="Sensibilité, motricité, coordination et autres constatations utiles…"></textarea></label>
                        <label class="dme-specialty-field">Équilibre et marche<textarea data-dme-specialty="medecine_physique" data-dme-specialty-field="equilibre_marche" rows="2" placeholder="Équilibre, démarche, aides utilisées et résultats des tests réalisés…"></textarea></label>
                        <label class="dme-specialty-field">Aides techniques, orthèses et appareillages<textarea data-dme-specialty="medecine_physique" data-dme-specialty-field="aides_techniques" rows="2" placeholder="Type, côté, tolérance, usage et besoin d’adaptation…"></textarea></label>
                        <label class="dme-specialty-field">Échelles et mesures fonctionnelles<textarea data-dme-specialty="medecine_physique" data-dme-specialty-field="evaluations_fonctionnelles" rows="2" placeholder="Nom de l’échelle/test, date et résultat (ex. autonomie, mobilité, fonction)…"></textarea></label>
                        <label class="dme-specialty-field">Objectifs de réadaptation<textarea data-dme-specialty="medecine_physique" data-dme-specialty-field="objectifs_reeducation" rows="2" placeholder="Objectifs individualisés, mesurables et échéance envisagée…"></textarea></label>
                        <div class="md:col-span-2 rounded-2xl border border-indigo-200 bg-white p-4 dark:border-indigo-900/50 dark:bg-black/20">
                            <div class="flex flex-wrap items-center justify-between gap-3">
                                <div><h4 class="flex items-center gap-2 text-[11px] font-extrabold text-indigo-900 dark:text-indigo-200"><i class="ph-bold ph-calendar-check text-base"></i>Plan de rééducation</h4><p class="mt-1 text-[10px] text-slate-500">Les types proposés viennent des prestations de la catégorie Rééducation.</p></div>
                                <div class="flex flex-wrap gap-2">
                                    <button type="button" onclick="window.addDmeReeducationPlanRow()" class="inline-flex items-center gap-1.5 rounded-xl border border-indigo-200 bg-indigo-50 px-3 py-2 text-[10px] font-extrabold text-indigo-800 hover:bg-indigo-100"><i class="ph-bold ph-plus"></i>Ajouter une ligne</button>
                                    <button type="button" onclick="window.previewDmeReeducationPlan()" class="inline-flex items-center gap-1.5 rounded-xl bg-emerald-700 px-3 py-2 text-[10px] font-extrabold text-white shadow-sm hover:bg-emerald-800"><i class="ph-bold ph-file-pdf"></i>Aperçu PDF</button>
                                </div>
                            </div>
                            <div class="mt-3 overflow-x-auto rounded-xl border border-slate-200 dark:border-white/10">
                                <table class="min-w-[900px] w-full text-left text-[10px]">
                                    <thead class="bg-slate-100 text-slate-600 dark:bg-white/5 dark:text-slate-300"><tr><th class="p-2.5">Type de rééducation</th><th class="p-2.5">Séances / semaine</th><th class="p-2.5">Total séances</th><th class="p-2.5">Durée (min)</th><th class="p-2.5">Kinésithérapeute</th><th class="w-10 p-2.5"></th></tr></thead>
                                    <tbody id="dmeReeducationPlanRows" class="divide-y divide-slate-100 dark:divide-white/10"><tr><td colspan="6" class="p-4 text-center text-slate-400">Chargement des types de rééducation…</td></tr></tbody>
                                </table>
                            </div>
                            <input type="hidden" data-dme-specialty="medecine_physique" data-dme-specialty-field="plans_reeducation_json">
                            <p id="dmeRehabilitationTherapistHint" class="mt-2 text-[10px] text-slate-500">« N’importe quelle kiné » permet de ne pas affecter le plan à une personne précise.</p>
                            <div class="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
                                <label class="dme-specialty-field">Objectifs de réadaptation<textarea data-dme-specialty="medecine_physique" data-dme-specialty-field="objectifs_reeducation" rows="2" placeholder="Objectifs individualisés et échéance envisagée…"></textarea></label>
                                <label class="dme-specialty-field">Programme, précautions et consignes<textarea data-dme-specialty="medecine_physique" data-dme-specialty-field="programme_reeducation" rows="2" placeholder="Précisions et consignes décidées par le médecin…"></textarea></label>
                            </div>
                        </div>
                        <label class="dme-specialty-field md:col-span-2">Suivi et évolution<textarea data-dme-specialty="medecine_physique" data-dme-specialty-field="suivi_reeducation" rows="2" placeholder="Évolution, réévaluation prévue et critères de progression définis…"></textarea></label>
                    </div>

                    <div id="dmeSpecialtyPanel-medecine_sport" data-dme-specialty-panel="medecine_sport" class="hidden grid grid-cols-1 gap-3 md:grid-cols-2">
                        <label class="dme-specialty-field">Sport pratiqué et rôle / poste<textarea data-dme-specialty="medecine_sport" data-dme-specialty-field="sport_pratique" rows="2" placeholder="Discipline, poste ou rôle, pratique individuelle/collective…"></textarea></label>
                        <label class="dme-specialty-field">Niveau et charge d’entraînement<textarea data-dme-specialty="medecine_sport" data-dme-specialty-field="niveau_pratique_charge" rows="2" placeholder="Loisir/compétition, fréquence, durée, intensité et évolution récente…"></textarea></label>
                        <label class="dme-specialty-field">Contexte de la blessure ou du symptôme<textarea data-dme-specialty="medecine_sport" data-dme-specialty-field="contexte_blessure_sport" rows="2" placeholder="Date, mécanisme, geste/situation, traumatisme ou surcharge, évolution…"></textarea></label>
                        <label class="dme-specialty-field">Antécédents sportifs et blessures<textarea data-dme-specialty="medecine_sport" data-dme-specialty-field="antecedents_sportifs" rows="2" placeholder="Blessures, récidives, interventions, arrêts et reprise antérieurs…"></textarea></label>
                        <label class="dme-specialty-field">Douleur et symptômes à l’effort<textarea data-dme-specialty="medecine_sport" data-dme-specialty-field="douleur_sport" rows="2" placeholder="Localisation/côté, intensité /10, circonstances, symptômes associés et récupération…"></textarea></label>
                        <label class="dme-specialty-field">Examen musculosquelettique<textarea data-dme-specialty="medecine_sport" data-dme-specialty-field="examen_musculosquelettique_sport" rows="2" placeholder="Inspection, palpation, mobilité, force, stabilité et constatations côté par côté…"></textarea></label>
                        <label class="dme-specialty-field">Tests fonctionnels / de performance réalisés<textarea data-dme-specialty="medecine_sport" data-dme-specialty-field="tests_fonctionnels_sport" rows="2" placeholder="Test, date, conditions et résultats mesurés…"></textarea></label>
                        <label class="dme-specialty-field">Aptitude, restrictions et consignes sportives<textarea data-dme-specialty="medecine_sport" data-dme-specialty-field="aptitude_restrictions_sport" rows="2" placeholder="Conclusion du médecin, restrictions temporaires et consignes communiquées…"></textarea></label>
                        <label class="dme-specialty-field">Plan de prise en charge et reprise<textarea data-dme-specialty="medecine_sport" data-dme-specialty-field="plan_reprise_sport" rows="2" placeholder="Plan individualisé, étapes/critères de reprise et coordination si nécessaire…"></textarea></label>
                        <label class="dme-specialty-field md:col-span-2">Suivi sportif<textarea data-dme-specialty="medecine_sport" data-dme-specialty-field="suivi_sport" rows="2" placeholder="Date ou échéance de réévaluation, évolution et décisions médicales…"></textarea></label>
                    </div>
                </section>

                <div id="dmeSpecialtyPickerModal" style="z-index:2147483640" class="fixed inset-0 hidden items-center justify-center bg-slate-950/70 p-4 backdrop-blur-sm" onclick="if(event.target===this)closeDmeSpecialtyPicker()">
                    <section role="dialog" aria-modal="true" aria-labelledby="dmeSpecialtyPickerTitle" class="w-full max-w-md overflow-hidden rounded-3xl border border-indigo-100 bg-white shadow-2xl dark:border-white/10 dark:bg-[#121c15]">
                        <header class="flex items-center justify-between border-b border-slate-100 p-5 dark:border-white/10"><div><h3 id="dmeSpecialtyPickerTitle" class="text-sm font-extrabold text-slate-900 dark:text-white">Ajouter une section spécialisée</h3><p class="mt-1 text-[11px] text-slate-500">Choisissez librement, indépendamment de la spécialité du passage.</p></div><button type="button" onclick="closeDmeSpecialtyPicker()" aria-label="Fermer" class="rounded-xl p-2 text-slate-500 hover:bg-slate-100 dark:hover:bg-white/10"><i class="ph-bold ph-x text-lg"></i></button></header>
                        <div class="space-y-4 p-5"><label for="dmeSpecialtyAddSelect" class="block text-[10px] font-extrabold uppercase tracking-wide text-slate-500">Spécialité à ajouter</label><select id="dmeSpecialtyAddSelect" class="w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-3 text-sm font-bold text-slate-800 outline-none focus:border-indigo-500 dark:border-white/10 dark:bg-black/30 dark:text-white"><option value="physiotherapie">Bilan de rééducation / physiothérapie</option><option value="gastro">Gastro-entérologie</option><option value="anesthesie">Anesthésie</option><option value="dermatologie">Dermatologie</option><option value="gynecologie">Gynécologie</option><option value="general">Médecine générale</option><option value="medecine_physique">Médecine physique et réadaptation</option><option value="medecine_sport">Médecine du sport</option></select><div class="flex justify-end gap-2"><button type="button" onclick="closeDmeSpecialtyPicker()" class="rounded-xl border border-slate-200 px-4 py-2.5 text-[11px] font-bold text-slate-600 dark:border-white/10 dark:text-slate-300">Annuler</button><button type="button" onclick="addDmeSpecialtySection()" class="inline-flex items-center gap-2 rounded-xl bg-indigo-600 px-4 py-2.5 text-[11px] font-extrabold text-white hover:bg-indigo-700"><i class="ph-bold ph-plus"></i>Ajouter les champs</button></div></div>
                    </section>
                </div>

                <!-- 9. Actes & prestations caisse -->
                <div class="bg-slate-50 dark:bg-black/30 p-5 rounded-3xl border border-slate-200/80 dark:border-white/10 space-y-4">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-slate-200/60 dark:border-white/10 pb-3">
                        <div>
                            <h4 class="text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5">
                                <i class="ph-bold ph-first-aid text-brand-neon text-base"></i>
                                <span>9. Actes Médicaux & Prestations Associés (Facturation Caisse)</span>
                            </h4>
                            <p class="text-[11px] text-slate-400">Les actes ajoutés ici sont synchronisés en direct avec la caisse du passage.</p>
                        </div>
                        <span id="dmeAutoTarifBadge" class="text-[10px] font-bold px-2.5 py-1 rounded-full bg-emerald-100 text-emerald-800 w-fit">Tarif auto selon l'heure</span>
                    </div>

                    <div class="grid grid-cols-12 gap-2 bg-white dark:bg-[#121c15] p-3 rounded-2xl border border-slate-200 dark:border-white/10 items-end">
                        <div class="col-span-12 sm:col-span-6 relative">
                            <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Rechercher une Prestation</label>
                            <input type="text" id="dmePrestationSearchInput" oninput="searchDmePrestationsLive()" placeholder="Tapez le nom de l'acte..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 font-medium text-slate-800 dark:text-white">
                            <div id="dmePrestationSearchResultsTable" class="hidden absolute top-full left-0 right-0 mt-1 bg-white dark:bg-[#121c15] rounded-2xl shadow-xl border border-slate-200 dark:border-white/10 max-h-48 overflow-y-auto z-30 divide-y divide-slate-100 dark:divide-white/5"></div>
                        </div>

                        <div class="col-span-6 sm:col-span-2">
                            <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Période</label>
                            <select id="dmeSelectAddPeriode" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 font-bold text-slate-700 dark:text-slate-300">
                                <option value="Jour">☀️ Jour</option>
                                <option value="Garde">🌙 Nuit / Garde</option>
                            </select>
                        </div>
                        <div class="col-span-6 sm:col-span-2">
                            <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Qté</label>
                            <input type="number" id="dmeInputAddQuantite" value="1" min="1" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-center font-bold text-slate-800 dark:text-white">
                        </div>
                        <div class="col-span-12 sm:col-span-2">
                            <button type="button" onclick="addSelectedDmePrestationToTable()" class="w-full bg-brand-neon text-black py-2.5 rounded-xl text-xs font-bold shadow-sm inline-flex items-center justify-center gap-1 hover:opacity-90 cursor-pointer">
                                <i class="ph-bold ph-plus"></i>
                                <span>Ajouter</span>
                            </button>
                        </div>
                    </div>

                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-xs bg-white dark:bg-[#121c15] rounded-2xl overflow-hidden border border-slate-200 dark:border-white/10">
                            <thead class="bg-slate-100 dark:bg-black/40 text-slate-500 font-semibold border-b border-slate-200 dark:border-white/10">
                                <tr>
                                    <th class="p-2.5">Prestation / Acte</th>
                                    <th class="p-2.5">Période</th>
                                    <th class="p-2.5 text-center">Qté</th>
                                    <th class="p-2.5">Tarif Unitaire</th>
                                    <th class="p-2.5 font-bold">Sous-total</th>
                                    <th class="p-2.5 text-right">Action</th>
                                </tr>
                            </thead>
                            <tbody id="dmeActsTableBody" class="divide-y divide-slate-100 dark:divide-white/5 font-medium"></tbody>
                        </table>
                    </div>

                    <div class="flex items-center justify-between p-3 rounded-2xl bg-white dark:bg-[#121c15] border border-slate-200 dark:border-white/10 text-xs">
                        <span class="font-bold text-slate-500">Total des prestations du passage :</span>
                        <strong id="dmeTotalActsDisplay" class="text-sm font-black text-emerald-600 dark:text-brand-neon font-mono">0.00 DA</strong>
                    </div>
                </div>
            </div>

            <!-- ================= COURBES DE CROISSANCE PÉDIATRIQUES ================= -->
            <div id="dme-panel-croissance" class="dme-subtab-panel space-y-5 hidden">
                <section class="bg-white dark:bg-[#121c15] p-5 md:p-6 rounded-3xl border border-slate-100 dark:border-white/5 shadow-sm space-y-4">
                    <div class="flex flex-col md:flex-row md:items-end justify-between gap-3">
                        <div><h3 class="text-sm font-extrabold text-slate-900 dark:text-white">Suivi de croissance pédiatrique</h3>
                            <p id="dmeGrowthPatientMeta" class="text-xs text-slate-500 mt-1">Références OMS adaptées à l’âge et au sexe.</p></div>
                        <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Indicateur
                            <select id="dmeGrowthIndicator" onchange="renderDmeGrowthChart()" class="ml-2 px-3 py-2 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/30 text-slate-800 dark:text-white">
                                <option value="height_age">Taille pour l’âge</option><option value="weight_age">Poids pour l’âge</option>
                                <option value="bmi_age">IMC pour l’âge</option><option value="head_age">Périmètre crânien pour l’âge</option>
                                <option value="arm_age">Périmètre brachial pour l’âge</option>
                            </select>
                        </label>
                        <button type="button" onclick="openDmeGrowthPrintView()" class="px-4 py-2.5 rounded-xl bg-emerald-700 hover:bg-emerald-800 text-white text-xs font-bold inline-flex items-center justify-center gap-2 shadow-sm">
                            <i class="ph-bold ph-printer"></i>Voir / imprimer / PDF
                        </button>
                    </div>
                    <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
                        <label class="text-[10px] font-bold text-slate-500 uppercase">Poids (kg)<input id="dme_growth_weight" type="number" step="0.01" oninput="document.getElementById('dme_poids').value=this.value; calculateImc(); renderDmeGrowthChart()" class="mt-1 w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/30 text-center font-bold"></label>
                        <label class="text-[10px] font-bold text-slate-500 uppercase">Taille / longueur (cm)<input id="dme_growth_height" type="number" step="0.1" oninput="document.getElementById('dme_taille').value=this.value; calculateImc(); renderDmeGrowthChart()" class="mt-1 w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/30 text-center font-bold"></label>
                        <label class="text-[10px] font-bold text-slate-500 uppercase">Périmètre crânien (cm)<input id="dme_growth_head" type="number" step="0.1" oninput="renderDmeGrowthChart()" class="mt-1 w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/30 text-center font-bold"></label>
                        <label class="text-[10px] font-bold text-slate-500 uppercase">Périmètre brachial (cm)<input id="dme_growth_arm" type="number" step="0.1" oninput="renderDmeGrowthChart()" class="mt-1 w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/30 text-center font-bold"></label>
                    </div>
                    <p id="dmeGrowthNotice" class="text-xs text-amber-700 bg-amber-50 dark:bg-amber-950/30 dark:text-amber-200 rounded-xl px-3 py-2 hidden"></p>
                    <div id="dmeGrowthChart" class="w-full min-h-[340px] overflow-x-auto rounded-2xl border border-slate-100 dark:border-white/10 bg-slate-50 dark:bg-black/20 p-2"></div>
                    <div id="dmeGrowthLegend" class="flex flex-wrap gap-x-4 gap-y-2 text-[10px] text-slate-500"></div>
                    <p class="text-[10px] leading-relaxed text-slate-400">Courbes de référence OMS exprimées en scores Z (écarts-types). Chaque indicateur a sa plage d’âge officielle (poids jusqu’à 10 ans, périmètres crânien et brachial jusqu’à 5 ans). La courbe aide au suivi et ne remplace pas l’évaluation clinique.</p>
                </section>
            </div>

            <!-- ================= PANNEAU 2 : RÉDIGER ORDONNANCE ================= -->
            <div id="dme-panel-ordonnance" class="dme-subtab-panel space-y-6 hidden">
                <div class="bg-white dark:bg-[#121c15] p-6 rounded-3xl border border-slate-100 dark:border-white/5 shadow-sm space-y-5">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between border-b border-slate-100 dark:border-white/5 pb-4 gap-3">
                        <div class="flex items-center gap-2.5">
                            <i class="ph-bold ph-link text-brand-neon text-lg"></i>
                            <h3 class="text-sm font-extrabold uppercase tracking-wider text-slate-800 dark:text-white">Rédaction d'Ordonnance Médicale</h3>
                        </div>
                        <div class="flex items-center gap-2.5">
                            <button type="button" onclick="saveActivePrescriptionOnly()" class="px-4 py-2 bg-brand-neon hover:bg-brand-neonDark text-black text-xs font-bold rounded-2xl shadow-md transition cursor-pointer">
                                <i class="ph-bold ph-floppy-disk"></i> Enregistrer
                            </button>
                            <button type="button" onclick="saveAndPrintActivePrescription()" class="px-4 py-2 bg-slate-900 hover:bg-black text-white text-xs font-bold rounded-2xl shadow-sm inline-flex items-center gap-2 transition cursor-pointer">
                                <i class="ph-bold ph-printer text-brand-neon"></i> Voir & Imprimer PDF (Fond KACEL)
                            </button>
                        </div>
                    </div>

                    <div class="grid grid-cols-12 gap-2 bg-slate-50 dark:bg-black/30 p-3 rounded-2xl border border-slate-200 dark:border-white/10 items-end">
                        <div class="col-span-12 sm:col-span-4 relative">
                            <div class="flex items-center justify-between gap-2 mb-1">
                                <label class="block text-[10px] font-bold text-slate-400 uppercase">Médicament & Dosage</label>
                                <button type="button" onclick="openDmeMedicationModal()" class="inline-flex items-center gap-1 text-[10px] font-bold text-emerald-700 dark:text-brand-neon hover:underline"><i class="ph-bold ph-plus-circle"></i> Ajouter un médicament</button>
                            </div>
                            <input type="text" id="ordMedSearchInput" oninput="searchOrdMedicationsLive()" placeholder="Tapez le nom..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-white dark:bg-[#121c15] text-slate-800 dark:text-white font-bold">
                            <div id="ordMedSearchResults" class="hidden absolute top-full left-0 right-0 mt-1 bg-white dark:bg-[#121c15] rounded-2xl shadow-xl border border-slate-200 dark:border-white/10 max-h-48 overflow-y-auto z-30 divide-y divide-slate-100 dark:divide-white/5"></div>
                        </div>
                        <div class="col-span-12 sm:col-span-4">
                            <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Posologie / Instructions</label>
                            <input type="text" id="ordPosologieInput" placeholder="Ex: 1 cp 3 fois par jour" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-white dark:bg-[#121c15] text-slate-800 dark:text-white">
                        </div>
                        <div class="col-span-6 sm:col-span-2">
                            <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Durée</label>
                            <input type="text" id="ordDureeInput" placeholder="Ex: 7 jours" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-white dark:bg-[#121c15] text-slate-800 dark:text-white">
                        </div>
                        <div class="col-span-4 sm:col-span-1">
                            <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Qté</label>
                            <input type="number" id="ordQteInput" value="1" min="1" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-white dark:bg-[#121c15] text-center font-bold text-slate-800 dark:text-white">
                        </div>
                        <div class="col-span-2 sm:col-span-1">
                            <button type="button" onclick="addMedicationToPrescription()" class="w-full bg-brand-neon text-black py-2.5 rounded-xl text-xs font-bold shadow-sm inline-flex items-center justify-center hover:opacity-90 cursor-pointer">
                                <i class="ph-bold ph-plus"></i>
                            </button>
                        </div>
                    </div>

                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-xs">
                            <thead>
                                <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-2">
                                    <th class="pb-2.5">Médicament</th>
                                    <th class="pb-2.5">Posologie</th>
                                    <th class="pb-2.5">Durée</th>
                                    <th class="pb-2.5 text-center text-emerald-600 dark:text-brand-neon">Quantité</th>
                                    <th class="pb-2.5 text-right pr-2">Action</th>
                                </tr>
                            </thead>
                            <tbody id="ordActiveLinesTableBody" class="divide-y divide-slate-100 dark:divide-white/5 font-medium"></tbody>
                        </table>
                    </div>
                </div>

                <div class="bg-white dark:bg-[#121c15] p-6 rounded-3xl border border-slate-100 dark:border-white/5 shadow-sm space-y-4">
                    <h4 class="text-xs font-extrabold uppercase tracking-wider text-slate-800 dark:text-white flex items-center gap-2">
                        <i class="ph-bold ph-clock-counter-clockwise text-brand-neon text-base"></i>
                        <span>Ordonnances Antérieures de ce Patient</span>
                    </h4>
                    <div id="ordHistoryCardsList" class="space-y-3"></div>
                </div>
            </div>

            <!-- ================= PANNEAU 3 : COMPTE RENDU & LIVRET ================= -->
            <div id="dme-panel-compterendu" class="dme-subtab-panel space-y-6 hidden">
                <div class="bg-white dark:bg-[#121c15] p-6 lg:p-7 rounded-3xl border border-slate-200/80 dark:border-white/10 shadow-sm space-y-5">
                    <div>
                        <h3 class="text-xs font-bold text-slate-800 dark:text-white uppercase tracking-wider flex items-center gap-2">
                            <i class="ph-bold ph-file-text text-sky-500 text-base"></i>
                            <span>GÉNÉRATION DE COMPTE RENDU WORD & CRÉATION DE LIVRET</span>
                        </h3>
                        <p class="text-[11px] text-slate-400 mt-1">Comptes rendus et livrets archivés dans le dossier du patient.</p>
                    </div>

                    <div class="p-4 rounded-2xl border border-slate-200/90 dark:border-white/10 bg-slate-50/50 dark:bg-black/20 flex flex-col lg:flex-row items-stretch lg:items-end gap-3.5">
                        <div id="crTemplateSearchWrap" class="flex-1 relative">
                            <label class="block text-[11px] font-bold text-slate-600 dark:text-slate-300 mb-1.5 uppercase">
                                1. MODÈLE DE COMPTE RENDU *
                            </label>
                            <input type="search" id="crTemplateSearchInput" autocomplete="off" onfocus="searchDmeCrTemplatesLive()" oninput="handleDmeCrTemplateSearchInput()" placeholder="Rechercher un compte rendu…" class="w-full text-xs p-3 rounded-2xl border border-slate-200 dark:border-white/10 bg-white dark:bg-[#121c15] font-bold text-slate-800 dark:text-white outline-none focus:border-brand-neon shadow-xs">
                            <input type="hidden" id="crTemplateSelect" value="">
                            <div id="crTemplateSearchResults" class="hidden absolute top-full left-0 right-0 mt-1 z-40 max-h-72 overflow-y-auto rounded-2xl border border-slate-200 dark:border-white/10 bg-white dark:bg-[#121c15] shadow-2xl"></div>
                        </div>
                        <div class="flex-1">
                            <label class="block text-[11px] font-bold text-slate-600 dark:text-slate-300 mb-1.5 uppercase">
                                2. MÉDECIN PRESCRIPTEUR (${MED}) *
                            </label>
                            <input type="text" id="crMedPrescripteurSelect" class="w-full text-xs p-3 rounded-2xl border border-slate-200 dark:border-white/10 bg-white dark:bg-[#121c15] font-semibold text-slate-800 dark:text-white outline-none focus:border-brand-neon shadow-xs">
                        </div>
                        <div class="flex-1">
                            <label class="block text-[11px] font-bold text-slate-600 dark:text-slate-300 mb-1.5 uppercase">
                                3. MÉDECIN RADIOLOGUE SIGNATAIRE (${SIGNE}) *
                            </label>
                            <select id="crMedSignataireSelect" class="w-full text-xs p-3 rounded-2xl border border-slate-200 dark:border-white/10 bg-white dark:bg-[#121c15] font-semibold text-slate-800 dark:text-white outline-none focus:border-brand-neon shadow-xs"></select>
                        </div>
                        <div class="flex-shrink-0">
                            <button type="button" id="btnGenerateCr" onclick="generateCrFromDme()" class="w-full lg:w-auto px-6 py-3 rounded-2xl bg-[#0c1a10] hover:bg-[#1a3822] text-[#9fe870] font-black text-xs shadow-md transition inline-flex items-center justify-center gap-2 cursor-pointer">
                                <i class="ph-bold ph-sparkle text-base"></i>
                                <span id="crGenerateBtnText">Générer Word</span>
                            </button>
                        </div>
                    </div>

                    <div class="space-y-3 pt-2">
                        <h4 class="text-xs font-bold text-slate-700 dark:text-slate-300 uppercase tracking-wider">
                            COMPTES RENDUS & LIVRETS ARCHIVÉS :
                        </h4>
                        <div id="dmeAdmissionDocs" class="space-y-2"></div>
                        <div class="overflow-x-auto border border-slate-200/80 dark:border-white/10 rounded-2xl bg-white dark:bg-[#121c15]">
                            <table class="w-full text-left text-xs">
                                <thead class="bg-slate-50 dark:bg-black/30 text-slate-500 font-semibold border-b border-slate-100 dark:border-white/5">
                                    <tr>
                                        <th class="py-3 pl-3">Date & Heure</th>
                                        <th class="py-3">Nom du Fichier CR</th>
                                        <th class="py-3">Médecin Signataire</th>
                                        <th class="py-3">Auteur</th>
                                        <th class="py-3">Format</th>
                                        <th class="py-3 text-right pr-4">Actions</th>
                                    </tr>
                                </thead>
                                <tbody id="dmeArchivedCrTableBody" class="divide-y divide-slate-100 dark:divide-white/5 font-medium"></tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>

            <!-- ========================================================================= -->
            <!-- ⭐ 4. PANNEAU : DOSSIER ÉCHOGRAPHIE (PNG) AVEC CARROUSEL ⭐ -->
            <!-- ========================================================================= -->
            <div id="dme-panel-echographie" class="dme-subtab-panel space-y-6 hidden">
                <div class="bg-white dark:bg-[#121c15] p-6 lg:p-7 rounded-3xl border border-slate-200/80 dark:border-white/10 shadow-sm space-y-5">

                    <div class="flex flex-col sm:flex-row sm:items-center justify-between border-b border-slate-100 dark:border-white/5 pb-4 gap-3">
                        <div>
                            <h3 class="text-xs font-bold text-slate-800 dark:text-white uppercase tracking-wider flex items-center gap-2">
                                <i class="ph-bold ph-image text-amber-500 text-base"></i>
                                <span>CLICHÉS ÉCHOGRAPHIQUES DU PASSAGE (PNG / HAUTE DÉFINITION)</span>
                            </h3>
                            <p class="text-[11px] text-slate-400 mt-1">Images échographiques acquises pour ce patient lors de cette consultation.</p>
                        </div>

                        <div class="flex flex-wrap items-end gap-2.5">
                            <label for="dmeEchoUploadInput" class="px-4 py-2 bg-brand-neon hover:bg-brand-neonDark text-black text-xs font-bold rounded-2xl shadow-md inline-flex items-center gap-2 transition cursor-pointer">
                                <i class="ph-bold ph-upload-simple text-base"></i><span>Choisir des images à importer</span>
                            </label>
                            <input type="file" id="dmeEchoUploadInput" accept="image/png,image/jpeg,image/webp" multiple class="hidden" onchange="handleEchoFilesUpload(event)">

                            <button type="button" onclick="startEchoCarousel()" id="btnStartCarousel" class="px-4 py-2 bg-slate-900 hover:bg-black text-white text-xs font-bold rounded-2xl shadow-sm inline-flex items-center gap-2 transition cursor-pointer">
                                <i class="ph-bold ph-slideshow text-brand-neon text-base"></i>
                                <span>Lancer Carrousel</span>
                            </button>
                        </div>
                    </div>

                    <!-- GALERIE DES IMAGES ÉCHOGRAPHIQUES -->
                    <div id="dmeEchoGalleryGrid" class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-4">
                        <!-- Rempli en JS -->
                    </div>

                </div>
            </div>

            <!-- ================= PANNEAU 5 : HISTORIQUE ================= -->
            <div id="dme-panel-historique" class="dme-subtab-panel space-y-4 hidden">
                <div class="bg-white dark:bg-[#121c15] p-6 rounded-3xl border border-slate-100 dark:border-white/5 shadow-sm">
                    <h3 class="text-sm font-bold text-slate-800 dark:text-white mb-4 flex items-center gap-2">
                        <i class="ph-bold ph-clock-counter-clockwise text-brand-neon"></i>
                        <span>Historique & Archives des Consultations</span>
                    </h3>
                    <p class="mb-4 text-[11px] text-slate-500 dark:text-slate-400">Ouvrez une archive pour consulter les champs cliniques, ordonnances, comptes rendus et livrets associés au passage.</p>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-xs">
                            <thead>
                                <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-2">
                                    <th class="pb-2">N° Passage</th>
                                    <th class="pb-2">Date & Heure</th>
                                    <th class="pb-2">Discipline</th>
                                    <th class="pb-2">Diagnostic Posé</th>
                                    <th class="pb-2">Praticien</th>
                                    <th class="pb-2">Statut</th>
                                    <th class="pb-2 text-right">Archive</th>
                                </tr>
                            </thead>
                            <tbody id="dmeHistoTableBody" class="divide-y divide-slate-100 dark:divide-white/5 text-slate-700 dark:text-slate-300"></tbody>
                        </table>
                    </div>
                </div>
            </div>

        </div>

        <!-- PLACEHOLDER VIDE PAR DÉFAUT -->
        <div id="dmeEmptyPlaceholder" class="bg-white dark:bg-[#121c15] p-12 rounded-3xl border border-slate-100 dark:border-white/5 shadow-sm text-center">
            <div class="w-16 h-16 rounded-2xl bg-brand-neon/10 text-brand-neon flex items-center justify-center text-3xl mx-auto mb-3">
                <i class="ph-bold ph-stethoscope"></i>
            </div>
            <h3 class="text-base font-bold text-slate-800 dark:text-white">Aucun dossier patient actif</h3>
            <p class="text-xs text-slate-400 mt-1">Ouvrez un dossier depuis <strong>Accueil & Passages</strong> en cliquant sur un patient.</p>
            <p id="dmeLoadingNotice" class="hidden mt-3 text-xs font-semibold text-sky-600 dark:text-sky-300"><i class="ph-bold ph-spinner-gap animate-spin mr-1"></i><span>Chargement du dossier patient…</span></p>
        </div>

    </div>

    <!-- CHOIX DU MODE D'OUVERTURE DU DME -->
    <div id="dmeOpenModeModal" class="fixed inset-0 bg-slate-950/65 backdrop-blur-sm hidden items-center justify-center z-[120] p-4">
        <div class="w-full max-w-lg rounded-3xl bg-white dark:bg-[#121c15] border border-slate-200 dark:border-white/10 shadow-2xl overflow-hidden">
            <div class="p-6 text-center">
                <div class="mx-auto mb-4 w-14 h-14 rounded-2xl bg-brand-neon/15 text-emerald-700 dark:text-brand-neon flex items-center justify-center"><i class="ph-bold ph-stethoscope text-2xl"></i></div>
                <h3 class="text-base font-extrabold text-slate-900 dark:text-white">Ouvrir le dossier médical</h3>
                <p id="dmeOpenModeMessage" class="mt-2 text-xs leading-5 text-slate-500 dark:text-slate-400">Choisissez comment ouvrir le dossier de ce patient.</p>
                <div class="mt-6 grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <button type="button" onclick="chooseDmeOpenMode('lecture')" class="p-4 rounded-2xl border border-slate-200 dark:border-white/10 hover:border-sky-400 hover:bg-sky-50/50 dark:hover:bg-white/5 text-left transition">
                        <span class="flex items-center gap-2 text-xs font-bold text-slate-800 dark:text-white"><i class="ph-bold ph-eye text-sky-500 text-base"></i>Lecture seule</span>
                        <span class="block mt-1 text-[10px] leading-4 text-slate-500">Consulter les informations et ouvrir les documents PDF.</span>
                    </button>
                    <button type="button" id="dmeOpenExecuteButton" onclick="chooseDmeOpenMode('execution')" class="p-4 rounded-2xl border border-brand-neon/50 bg-brand-neon/10 hover:bg-brand-neon/20 text-left transition">
                        <span class="flex items-center gap-2 text-xs font-bold text-emerald-800 dark:text-brand-neon"><i class="ph-bold ph-play-circle text-base"></i>Exécuter la consultation</span>
                        <span class="block mt-1 text-[10px] leading-4 text-slate-600 dark:text-slate-300">Passer le dossier en cours et saisir les informations médicales.</span>
                    </button>
                </div>
                <button type="button" onclick="chooseDmeOpenMode(null)" class="mt-4 px-4 py-2 rounded-xl text-xs font-semibold text-slate-400 hover:bg-slate-100 dark:hover:bg-white/5">Annuler</button>
            </div>
        </div>
    </div>

    <div id="dmeCloseConsultationModal" class="fixed inset-0 bg-slate-950/70 backdrop-blur-sm hidden items-center justify-center z-[2147483600] p-4" role="dialog" aria-modal="true" aria-labelledby="dmeCloseConsultationTitle">
        <div class="w-full max-w-2xl overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-2xl dark:border-white/10 dark:bg-[#121c15]">
            <div class="border-b border-slate-100 bg-slate-50/80 p-5 dark:border-white/10 dark:bg-black/20">
                <h3 id="dmeCloseConsultationTitle" class="text-base font-extrabold text-slate-900 dark:text-white">Fermer la consultation en cours</h3>
                <p class="mt-1 text-xs text-slate-500 dark:text-slate-400">Choisissez quoi faire des informations saisies avant de fermer ce dossier.</p>
            </div>
            <div class="space-y-3 p-5">
                <button type="button" onclick="saveAndCloseDmeConsultation()" class="flex w-full items-center gap-3 rounded-2xl border border-emerald-200 bg-emerald-50 p-4 text-left hover:bg-emerald-100 dark:border-emerald-900/50 dark:bg-emerald-950/20">
                    <i class="ph-bold ph-floppy-disk text-2xl text-emerald-700"></i><span><b class="block text-sm text-emerald-900 dark:text-emerald-200">Enregistrer la consultation</b><small class="text-[11px] text-slate-600 dark:text-slate-300">La consultation sera finalisée et verrouillée en lecture seule.</small></span>
                </button>
                <button type="button" onclick="cancelAccidentalDmeStart()" class="flex w-full items-center gap-3 rounded-2xl border border-amber-200 bg-amber-50 p-4 text-left hover:bg-amber-100 dark:border-amber-900/50 dark:bg-amber-950/20">
                    <i class="ph-bold ph-arrow-counter-clockwise text-2xl text-amber-700"></i><span><b class="block text-sm text-amber-900 dark:text-amber-200">Annuler le démarrage fait par erreur</b><small class="text-[11px] text-slate-600 dark:text-slate-300">Le passage revient en attente et les données saisies restent conservées en brouillon.</small></span>
                </button>
                <button id="dmeCloseResetButton" type="button" onclick="resetAndCloseDmeConsultation()" class="flex w-full items-center gap-3 rounded-2xl border border-rose-200 bg-rose-50 p-4 text-left hover:bg-rose-100 dark:border-rose-900/50 dark:bg-rose-950/20">
                    <i class="ph-bold ph-arrow-counter-clockwise text-2xl text-rose-700"></i><span><b class="block text-sm text-rose-900 dark:text-rose-200">Fermer et réinitialiser</b><small id="dmeCloseResetHint" class="text-[11px] text-slate-600 dark:text-slate-300">Les données saisies dans cette consultation seront effacées.</small></span>
                </button>
                <button id="dmeCloseKeepButton" type="button" onclick="showDmeHandoffFields()" class="flex w-full items-center gap-3 rounded-2xl border border-sky-200 bg-sky-50 p-4 text-left hover:bg-sky-100 dark:border-sky-900/50 dark:bg-sky-950/20">
                    <i class="ph-bold ph-pause-circle text-2xl text-sky-700"></i><span><b class="block text-sm text-sky-900 dark:text-sky-200">Garder en l’état et fermer</b><small class="text-[11px] text-slate-600 dark:text-slate-300">La consultation restera en cours pour pouvoir être reprise.</small></span>
                </button>
                <div id="dmeHandoffFields" class="hidden space-y-3 rounded-2xl border border-sky-200 bg-sky-50/60 p-4 dark:border-sky-900/50 dark:bg-sky-950/10">
                    <label class="block"><span class="mb-1 block text-[11px] font-bold text-slate-700 dark:text-slate-200">Note visible par les utilisateurs *</span><textarea id="dmeHandoffNoteInput" rows="3" maxlength="5000" placeholder="Ex. À reprendre demain, demander l’avis d’un autre médecin…" class="w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-xs text-slate-800 outline-none focus:border-sky-400 dark:border-white/10 dark:bg-black/20 dark:text-white"></textarea></label>
                    <label class="block"><span class="mb-1 block text-[11px] font-bold text-slate-700 dark:text-slate-200">Médecin à consulter *</span><select id="dmeHandoffDoctorSelect" class="w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-xs text-slate-800 outline-none focus:border-sky-400 dark:border-white/10 dark:bg-black/20 dark:text-white"><option value="">Chargement des médecins…</option></select></label>
                    <div class="flex justify-end"><button type="button" onclick="keepAndCloseDmeConsultation()" class="inline-flex items-center gap-2 rounded-xl bg-sky-700 px-4 py-2.5 text-xs font-extrabold text-white hover:bg-sky-800"><i class="ph-bold ph-pause"></i>Garder la consultation et fermer</button></div>
                </div>
            </div>
            <div class="flex justify-end border-t border-slate-100 p-4 dark:border-white/10"><button type="button" onclick="cancelDmeCloseConsultation()" class="rounded-xl px-4 py-2 text-xs font-bold text-slate-500 hover:bg-slate-100 dark:hover:bg-white/5">Continuer la consultation</button></div>
        </div>
    </div>

    <!-- AJOUT RAPIDE D'UN MÉDICAMENT DEPUIS LA GÉNÉRATION D'ORDONNANCE -->
    <div id="dmeAddMedicationModal" class="fixed inset-0 bg-black/60 backdrop-blur-sm hidden items-center justify-center z-[130] p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-lg shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50/50 dark:bg-black/20">
                <div class="flex items-center gap-2">
                    <div class="w-8 h-8 rounded-xl bg-brand-neon/20 text-brand-neon flex items-center justify-center"><i class="ph-bold ph-pill"></i></div>
                    <h3 class="text-base font-bold text-slate-800 dark:text-white">Ajouter un médicament</h3>
                </div>
                <button type="button" onclick="closeDmeMedicationModal()" class="text-slate-400 hover:text-slate-700 dark:hover:text-white p-1 rounded-lg"><i class="ph-bold ph-x text-lg"></i></button>
            </div>
            <form id="dmeMedicationForm" onsubmit="saveDmeMedication(event)" class="p-6 space-y-4">
                <div>
                    <label for="dmeMedicationName" class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Nom Commercial (Désignation) *</label>
                    <input type="text" id="dmeMedicationName" required placeholder="Ex: DOLIPRANE, AUGMENTIN..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-bold uppercase text-slate-800 dark:text-white">
                </div>
                <div>
                    <label for="dmeMedicationDci" class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">DCI (Substance Active) *</label>
                    <input type="text" id="dmeMedicationDci" required placeholder="Ex: Paracétamol, Amoxicilline..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                </div>
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label for="dmeMedicationForm" class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Forme</label>
                        <input type="text" id="dmeMedicationForm" placeholder="Ex: Comprimé, Sirop, Gélule..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                    <div>
                        <label for="dmeMedicationDosage" class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Dosage</label>
                        <input type="text" id="dmeMedicationDosage" placeholder="Ex: 1000mg, 500mg/5ml..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                </div>
                <div id="dmeMedicationError" class="hidden rounded-xl bg-rose-50 dark:bg-rose-950/20 px-3 py-2 text-[11px] font-semibold text-rose-600 dark:text-rose-300"></div>
                <div class="flex items-center justify-end gap-3 pt-4 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closeDmeMedicationModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-slate-700 dark:hover:text-white text-xs font-semibold">Annuler</button>
                    <button id="dmeMedicationSubmitButton" type="submit" class="px-6 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md shadow-brand-neon/20">Enregistrer</button>
                </div>
            </form>
        </div>
    </div>

    <!-- Visionneuse intégrée pour les pièces d'admission et demandes d'examen -->
    <div id="dmeAdmissionViewerModal" class="fixed inset-0 z-[120] hidden items-center justify-center bg-slate-950/80 backdrop-blur-sm p-3 sm:p-6" onclick="if(event.target===this) closeDmeAdmissionViewer()">
        <div class="flex h-[94vh] w-full max-w-6xl flex-col overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-2xl dark:border-white/10 dark:bg-[#121c15]">
            <div class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 bg-slate-50 px-4 py-3 dark:border-white/5 dark:bg-black/20 sm:px-6">
                <div class="min-w-0"><h3 id="dmeAdmissionViewerTitle" class="truncate text-sm font-bold text-slate-800 dark:text-white">Document patient</h3><p id="dmeAdmissionViewerCounter" class="text-[10px] text-slate-500"></p></div>
                <div class="flex items-center gap-1.5">
                    <button type="button" onclick="navigateDmeAdmissionViewer(-1)" class="rounded-lg bg-white px-3 py-2 text-xs font-bold text-slate-700 shadow-sm dark:bg-white/5 dark:text-white">←</button>
                    <button type="button" onclick="navigateDmeAdmissionViewer(1)" class="rounded-lg bg-white px-3 py-2 text-xs font-bold text-slate-700 shadow-sm dark:bg-white/5 dark:text-white">→</button>
                    <button type="button" onclick="changeDmeAdmissionZoom(-0.2)" class="rounded-lg bg-white px-3 py-2 text-xs font-bold dark:bg-white/5 dark:text-white">−</button>
                    <button type="button" onclick="resetDmeAdmissionZoom()" id="dmeAdmissionZoomLabel" class="rounded-lg bg-white px-3 py-2 text-[10px] font-bold dark:bg-white/5 dark:text-white">100%</button>
                    <button type="button" onclick="changeDmeAdmissionZoom(0.2)" class="rounded-lg bg-white px-3 py-2 text-xs font-bold dark:bg-white/5 dark:text-white">+</button>
                    <button type="button" onclick="closeDmeAdmissionViewer()" class="rounded-lg bg-rose-600 px-3 py-2 text-xs font-bold text-white">Fermer</button>
                </div>
            </div>
            <div class="min-h-0 flex-1 overflow-auto bg-slate-200 p-2 dark:bg-black/50 sm:p-4"><iframe id="dmeAdmissionViewerFrame" title="Document patient" class="h-full min-h-[70vh] w-full origin-top rounded-xl border-0 bg-white shadow-lg"></iframe></div>
        </div>
    </div>

    <!-- MODALE DE CONSULTATION DES ARCHIVES CLINIQUES -->
    <div id="dmeArchiveDetailModal" class="fixed inset-0 bg-slate-950/70 backdrop-blur-sm hidden items-center justify-center z-[90] p-3 sm:p-6" onclick="if(event.target===this) closeDmeArchiveDetailModal()">
        <div class="w-full max-w-6xl max-h-[94vh] flex flex-col overflow-hidden rounded-3xl bg-white dark:bg-[#121c15] border border-slate-200 dark:border-white/10 shadow-2xl">
            <div class="px-5 sm:px-7 py-4 border-b border-slate-100 dark:border-white/5 flex items-center justify-between bg-slate-50/80 dark:bg-black/20">
                <div class="flex items-center gap-3 min-w-0">
                    <div class="w-10 h-10 shrink-0 rounded-2xl bg-brand-neon/15 text-emerald-700 dark:text-brand-neon flex items-center justify-center"><i class="ph-bold ph-archive text-lg"></i></div>
                    <div class="min-w-0"><h3 id="dmeArchiveModalTitle" class="text-sm font-extrabold text-slate-900 dark:text-white truncate">Archive de consultation</h3><p id="dmeArchiveModalSubtitle" class="mt-0.5 text-[10px] text-slate-500 dark:text-slate-400">Chargement de l'archive...</p></div>
                </div>
                <button type="button" onclick="closeDmeArchiveDetailModal()" class="w-9 h-9 shrink-0 rounded-xl text-slate-400 hover:bg-slate-200/70 dark:hover:bg-white/10"><i class="ph-bold ph-x"></i></button>
            </div>
            <div id="dmeArchiveModalBody" class="p-5 sm:p-7 overflow-y-auto space-y-6">
                <div class="py-16 text-center text-xs text-slate-400"><i class="ph-bold ph-spinner animate-spin text-xl"></i><p class="mt-2">Chargement des informations archivées...</p></div>
            </div>
        </div>
    </div>
    
    <!-- MODALE INTERNE VISIONNEUR livret -->
    <!-- ================= MODALE CRÉATION LIVRET ================= -->
    <div id="livretCreationModal" style="z-index:2147483647" class="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center hidden z-[2147483647] p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-5xl max-h-[94vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex items-center justify-between bg-slate-50 dark:bg-black/20">
                <div class="flex items-center gap-2">
                    <i class="ph-bold ph-book-open text-brand-neon text-lg"></i>
                    <h3 class="text-sm font-bold text-slate-800 dark:text-white">Création du Livret Médical (A4 Multipage)</h3>
                </div>
                <button type="button" onclick="closeLivretModal()" class="text-slate-400 hover:text-white p-1 rounded-lg">✕</button>
            </div>

            <form onsubmit="handleConfirmLivret(event)" class="p-6 overflow-y-auto space-y-4">
                <input type="hidden" id="livretCrId">

                <!-- 1. Patient & Dates -->
                <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
                    <div class="sm:col-span-2">
                        <label class="block text-[11px] font-bold text-slate-600 dark:text-slate-300 mb-1">Nom & Prénom du Patient *</label>
                        <input type="text" id="livretNomPrenom" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white outline-none focus:border-brand-neon">
                    </div>
                    <div>
                        <label class="block text-[11px] font-bold text-slate-600 dark:text-slate-300 mb-1">Date d'Examen *</label>
                        <input type="text" id="livretDateExamen" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-semibold text-slate-800 dark:text-white outline-none focus:border-brand-neon">
                    </div>
                </div>

                <!-- 2. Type d'examen -->
                <div>
                    <label class="block text-[11px] font-bold text-slate-600 dark:text-slate-300 mb-1">Prestation de la consultation courante *</label>
                    <input type="text" id="livretTypeExamen" list="livretTypeOptions" required autocomplete="off" oninput="syncLivretPrestationSelection()" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white outline-none focus:border-brand-neon">
                    <datalist id="livretTypeOptions"></datalist>
                    <input type="hidden" id="livretPrestationId">
                </div>

                <div class="grid grid-cols-1 sm:grid-cols-[1fr_150px] gap-3 items-start pt-2 border-t border-slate-100 dark:border-white/5">
                    <div>
                        <label for="livretPageGardeSelect" class="block text-[11px] font-bold text-slate-600 dark:text-slate-300 mb-1">Page de garde *</label>
                        <select id="livretPageGardeSelect" required onchange="previewLivretPageGarde(); saveLivretDefaultCoverPreference()" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-semibold text-slate-800 dark:text-white outline-none focus:border-brand-neon">
                            <option value="">Chargement des pages de garde…</option>
                        </select>
                        <label class="mt-2 flex items-center gap-2 text-[11px] font-semibold text-slate-700 dark:text-slate-300 cursor-pointer">
                            <input id="livretUseDefaultCover" type="checkbox" onchange="saveLivretDefaultCoverPreference()" class="h-4 w-4 rounded border-slate-300 text-emerald-600 focus:ring-brand-neon">
                            <span>Utiliser cette page de garde par défaut pour mon compte</span>
                        </label>
                        <p id="livretPageGardeHint" class="mt-1 text-[10px] text-slate-400">Choisissez une page enregistrée dans model_templates/Page_De_Garde.</p>
                    </div>
                    <div class="h-56 sm:h-64 rounded-2xl overflow-hidden border border-slate-200 dark:border-white/10 bg-slate-100 dark:bg-black/30 flex items-center justify-center shadow-inner">
                        <img id="livretPageGardePreview" class="hidden w-full h-full object-contain" alt="Aperçu de la page de garde">
                        <span id="livretPageGardePlaceholder" class="px-3 text-center text-[10px] text-slate-400">Aperçu de la page sélectionnée</span>
                    </div>
                </div>

              <!-- 3. Sélection des images du groupe cible -->
                <div class="space-y-2 pt-2 border-t border-slate-100 dark:border-white/5">
                    <div class="flex items-center justify-between">
                        <label class="text-xs font-bold text-slate-700 dark:text-slate-300">
                            Sélectionner les images à intégrer au livret :
                            <span class="block mt-1 text-[10px] font-medium text-slate-400">Déplacez chaque image avec sa poignée ⋮⋮ pour définir l’ordre du livret.</span>
                        </label>
                        <div class="space-x-2 text-[11px]">
                            <button type="button" onclick="toggleAllLivretEchos(true)" class="text-emerald-500 font-bold hover:underline cursor-pointer">Tout cocher</button>
                            <span class="text-slate-400">|</span>
                            <button type="button" onclick="toggleAllLivretEchos(false)" class="text-slate-400 font-bold hover:underline cursor-pointer">Tout décocher</button>
                        </div>
                    </div>
                    
                    <!-- GRILLE EN GRAND FORMAT (Cartes spacieuses et images agrandies) -->
                    <div class="flex flex-wrap items-end gap-3">
                        <div>
                            <label for="livretImageGroup" class="block text-[11px] font-bold text-slate-600 dark:text-slate-300 mb-1">Groupe d’images à intégrer</label>
                            <select id="livretImageGroup" onchange="renderLivretImageChoices()" class="min-w-64 text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-semibold text-slate-800 dark:text-white">
                                <option value="">Aucun groupe disponible</option>
                            </select>
                        </div>
                        <p class="pb-2 text-[10px] text-slate-500">Toutes les images du groupe sont proposées, avec leur type d’origine.</p>
                    </div>
                    <div id="livretEchosCheckboxGrid" class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4 max-h-[58vh] overflow-y-auto p-4 bg-slate-50 dark:bg-black/30 rounded-2xl border border-slate-200 dark:border-white/10">
                        <!-- Rempli en JS avec de grandes images -->
                    </div>
                </div>

                <!-- Actions -->
                <div class="flex items-center justify-end gap-2.5 pt-3 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closeLivretModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler</button>
                    <button type="submit" id="btnSubmitLivret" class="px-6 py-2.5 rounded-xl bg-brand-neon hover:bg-brand-neonDark text-black text-xs font-bold shadow-md cursor-pointer inline-flex items-center gap-2">
                        <i class="ph-bold ph-check"></i>
                        <span>Confirmer & Générer le Livret</span>
                    </button>
                </div>
            </form>
        </div>
    </div>

    <!-- MODALE INTERNE VISIONNEUR ORDONNANCE -->
    <div id="prescriptionModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center hidden z-[100] p-2 sm:p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-4xl h-[95vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-5 py-3 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50 dark:bg-black/20 flex-shrink-0">
                <h3 class="text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5">
                    <i class="ph-bold ph-file-text text-brand-neon"></i>
                    <span id="dmePrescriptionModalTitle">Ordonnance Médicale Officielle (Fond KACEL ORD.png)</span>
                </h3>
                <div class="flex items-center space-x-2">
                    <button type="button" onclick="printPrescriptionIframe()" class="px-3.5 py-1.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-sm inline-flex items-center gap-1 cursor-pointer">
                        <i class="ph-bold ph-printer"></i>
                        <span>Imprimer</span>
                    </button>
                    <button type="button" onclick="closePrescriptionModal()" class="px-3.5 py-1.5 rounded-xl bg-rose-500 hover:bg-rose-600 text-white text-xs font-bold shadow-sm inline-flex items-center gap-1 cursor-pointer">
                        <i class="ph-bold ph-x-circle"></i>
                        <span>Quitter le visionneur</span>
                    </button>
                </div>
            </div>
            <div class="p-2 overflow-y-auto bg-slate-200 dark:bg-black/60 flex justify-center flex-1">
                <iframe id="prescriptionIframe" src="about:blank" class="w-full h-full rounded-2xl border-none shadow-md bg-white"></iframe>
            </div>
        </div>
    </div>

    <!-- MODALE INTERNE VISIONNEUR COMPTE RENDU / WORD CONVERTI EN PDF -->
    <div id="dmeCrPreviewModal" style="z-index:2147483647" class="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center hidden z-[2147483647] p-2 sm:p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-4xl h-[95vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-5 py-3 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50 dark:bg-black/20 flex-shrink-0">
                <h3 class="text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5">
                    <i class="ph-bold ph-file-text text-brand-neon"></i>
                    <span id="dmeCrModalTitle">Visionneur de Compte Rendu (Aperçu Intégré)</span>
                </h3>
                <div class="flex items-center space-x-2">
                    <button type="button" id="btnPrintCrIframe" onclick="printCrIframe()" class="px-3.5 py-1.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-sm inline-flex items-center gap-1 cursor-pointer">
                        <i class="ph-bold ph-printer"></i>
                        <span>Imprimer</span>
                    </button>
                    <button type="button" id="btnValidateLivretDraft" onclick="validateLivretDraft()" class="hidden px-3.5 py-1.5 rounded-xl bg-emerald-700 hover:bg-emerald-800 text-white text-xs font-bold shadow-sm inline-flex items-center gap-1 cursor-pointer">
                        <i class="ph-bold ph-check-circle"></i>
                        <span>Valider le livret</span>
                    </button>
                    <button type="button" onclick="closeCrPreviewModal()" class="px-3.5 py-1.5 rounded-xl bg-rose-500 hover:bg-rose-600 text-white text-xs font-bold shadow-sm inline-flex items-center gap-1 cursor-pointer">
                        <i class="ph-bold ph-x-circle"></i>
                        <span>Quitter le visionneur</span>
                    </button>
                </div>
            </div>
            <div class="p-2 overflow-y-auto bg-slate-200 dark:bg-black/60 flex justify-center flex-1">
                <iframe id="dmeCrIframe" src="about:blank" class="w-full h-full rounded-2xl border-none shadow-md bg-white"></iframe>
            </div>
        </div>
    </div>

    <!-- Confirmation des paramètres d'importation d'images -->
    <div id="echoUploadConfirmModal" class="hidden fixed inset-0 z-[115] bg-slate-950/75 p-3 sm:p-6 items-center justify-center">
        <section class="w-full max-w-5xl max-h-[94vh] overflow-hidden rounded-3xl bg-white dark:bg-[#101a13] shadow-2xl border border-slate-200 dark:border-white/10 flex flex-col">
            <header class="px-5 py-4 border-b border-slate-100 dark:border-white/10"><h3 class="font-extrabold text-slate-900 dark:text-white">Confirmer l’importation des images</h3><p id="echoUploadConfirmCount" class="mt-1 text-[11px] text-slate-500"></p></header>
            <div class="p-5 space-y-4 overflow-y-auto">
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div><label for="echoUploadConfirmType" class="block text-[11px] font-bold text-slate-600 dark:text-slate-300 mb-1">Type d’image</label><select id="echoUploadConfirmType" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs"><option value="echographique">Image échographique</option><option value="radiologique">Image livret radiologique</option><option value="planche_trois">Image Planche A3</option></select></div>
                    <div><label for="echoUploadConfirmCategory" class="block text-[11px] font-bold text-slate-600 dark:text-slate-300 mb-1">Catégorie</label><select id="echoUploadConfirmCategory" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs"><option>Livret 1</option><option>Livret 2</option><option>Livret 3</option><option>Livret 4</option><option>Livret 5</option><option>Livret 6</option></select></div>
                </div>
                <div>
                    <div class="mb-2 flex flex-wrap items-center justify-between gap-2"><h4 class="text-[11px] font-bold text-slate-600 dark:text-slate-300">Aperçu et sélection des images</h4><div class="flex gap-2"><button type="button" onclick="setEchoUploadSelection(true)" class="rounded-lg border border-slate-200 px-2.5 py-1.5 text-[10px] font-bold text-slate-600 hover:bg-slate-50 dark:border-white/10 dark:text-slate-300">Tout sélectionner</button><button type="button" onclick="setEchoUploadSelection(false)" class="rounded-lg border border-slate-200 px-2.5 py-1.5 text-[10px] font-bold text-slate-600 hover:bg-slate-50 dark:border-white/10 dark:text-slate-300">Tout décocher</button></div></div>
                    <div id="echoUploadConfirmFiles" class="grid max-h-[48vh] grid-cols-2 gap-3 overflow-y-auto rounded-2xl border border-slate-200 bg-slate-50 p-3 dark:border-white/10 dark:bg-black/20 sm:grid-cols-3 lg:grid-cols-4"></div>
                </div>
            </div>
            <footer class="flex flex-wrap justify-end gap-2 px-5 py-4 border-t border-slate-100 dark:border-white/10"><button type="button" onclick="cancelEchoUploadConfirmation()" class="px-4 py-2 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-bold text-slate-600 dark:text-slate-300">Annuler</button><button type="button" id="btnAddMoreDprintImages" onclick="addMoreDprintImagesToEchoUpload()" class="hidden px-4 py-2 rounded-xl border border-violet-200 bg-violet-50 text-xs font-bold text-violet-800 hover:bg-violet-100 dark:border-violet-900 dark:bg-violet-950/40 dark:text-violet-200"><i class="ph-bold ph-plus-circle mr-1"></i>Ajouter d’autres images DICOM Print</button><button type="button" id="btnConfirmEchoUpload" onclick="confirmEchoFilesUpload()" class="px-5 py-2 rounded-xl bg-brand-neon hover:bg-brand-neonDark text-black text-xs font-extrabold"><i class="ph-bold ph-upload-simple mr-1"></i>Confirmer et importer</button></footer>
        </section>
    </div>

    <!-- ========================================================================= -->
    <!-- Gestion plein écran des groupes d'images échographiques -->
    <div id="echoManagerModal" class="hidden fixed inset-0 z-[99999] bg-slate-950/95 p-2 sm:p-4">
        <section class="w-full h-full bg-white dark:bg-[#101a13] rounded-2xl sm:rounded-3xl flex flex-col overflow-hidden shadow-2xl border border-slate-200 dark:border-white/10">
            <header class="shrink-0 flex flex-wrap items-center justify-between gap-3 px-4 sm:px-6 py-3 border-b border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20">
                <div><h3 class="text-base font-extrabold text-slate-900 dark:text-white">Gestionnaire d’images grand format</h3><p class="text-[11px] text-slate-500">Réorganisez les images ou importez des images d’un autre groupe du même passage.</p></div>
                <button type="button" onclick="closeEchoManagerModal()" class="w-10 h-10 rounded-xl bg-white dark:bg-white/10 hover:bg-rose-500 hover:text-white text-slate-500"><i class="ph-bold ph-x"></i></button>
            </header>
            <div class="shrink-0 flex flex-wrap items-end gap-3 px-4 sm:px-6 py-3 border-b border-slate-100 dark:border-white/10">
                <div><label class="block text-[10px] font-bold text-slate-500 mb-1">Groupe d’images à organiser</label><select id="echoManagerGroup" onchange="renderEchoManager()" class="min-w-72 max-w-[70vw] px-3 py-2 rounded-xl border border-slate-200 bg-white text-xs"></select></div>
                <button type="button" onclick="saveEchoManagerOrder()" class="px-4 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-bold"><i class="ph-bold ph-floppy-disk mr-1"></i>Enregistrer l’ordre</button>
                <button type="button" onclick="toggleEchoImportPanel()" class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-bold"><i class="ph-bold ph-download-simple mr-1"></i>Importer d’un autre groupe</button>
            </div>
            <div id="echoManagerImportPanel" class="hidden shrink-0 px-4 sm:px-6 py-3 border-b border-indigo-100 bg-indigo-50/70 dark:bg-indigo-950/20 dark:border-white/10">
                <p class="mb-3 text-[11px] text-indigo-800 dark:text-indigo-200">Les images importées gardent leur type d’origine. Elles seront classées dans la catégorie du groupe affiché.</p>
                <div class="flex flex-wrap items-end gap-3">
                    <div><label class="block text-[10px] font-bold text-slate-500 mb-1">Images à prendre depuis</label><select id="echoManagerSourceGroup" onchange="renderEchoManagerSources()" class="min-w-64 px-3 py-2 rounded-xl border border-slate-200 bg-white text-xs"></select></div>
                    <button type="button" onclick="importSelectedEchoManagerImages()" class="px-4 py-2 rounded-xl bg-brand-neon hover:bg-brand-neonDark text-black text-xs font-bold"><i class="ph-bold ph-plus mr-1"></i>Importer la sélection vers le groupe affiché</button>
                    <span id="echoManagerImportHint" class="text-[11px] text-slate-500"></span>
                </div>
                <div id="echoManagerSourceGrid" class="mt-3 grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 2xl:grid-cols-8 gap-2 max-h-48 overflow-y-auto"></div>
            </div>
            <div id="echoManagerGrid" class="flex-1 min-h-0 overflow-auto p-3 sm:p-5 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-3 content-start"></div>
        </section>
    </div>

    <div id="echoManagerPositionMenu" class="hidden fixed z-[2147483647] w-36 max-h-64 overflow-y-auto rounded-xl border border-slate-200 bg-white p-1 shadow-2xl dark:border-white/10 dark:bg-slate-900"></div>

    <!-- ========================================================================= -->
    <!-- ⭐ MODALE CARROUSEL PLEIN ÉCRAN POUR CLICHÉS ÉCHOGRAPHIQUES ⭐ -->
    <!-- ========================================================================= -->
    <div id="echoCarouselModal" class="fixed inset-0 bg-black/90 backdrop-blur-md flex items-center justify-center hidden z-[110] p-4 select-none">
        <div class="relative w-full max-w-5xl h-[92vh] flex flex-col justify-between items-center">

            <!-- Barre supérieure carrousel -->
            <div class="w-full flex items-center justify-between text-white p-2">
                <div class="flex items-center gap-2">
                    <span class="w-2.5 h-2.5 rounded-full bg-brand-neon"></span>
                    <span id="carouselTitle" class="font-bold text-xs truncate max-w-md">Cliché Échographique</span>
                </div>
                <div class="flex items-center gap-3">
                    <span id="carouselCounter" class="text-xs font-mono font-bold bg-white/10 px-3 py-1 rounded-xl">1 / 1</span>
                    <button type="button" id="carouselPrintA3Button" onclick="printEchoCarouselImageA3()" class="inline-flex items-center gap-1.5 rounded-xl bg-lime-400 px-3 py-2 text-[11px] font-extrabold text-slate-950 shadow-sm hover:bg-lime-300"><i class="ph-bold ph-printer text-base"></i>Imprimer A3</button>
                    <button type="button" onclick="closeEchoCarousel()" class="text-slate-400 hover:text-white p-1 rounded-lg text-lg cursor-pointer">✕</button>
                </div>
            </div>
            <p id="carouselPrintStatus" class="hidden w-full rounded-xl px-3 py-2 text-center text-xs font-semibold"></p>

            <!-- Image centrale avec boutons précédent / suivant -->
            <div class="relative flex-1 w-full flex items-center justify-center overflow-hidden my-2">
                <button type="button" onclick="prevCarouselImage()" class="absolute left-4 top-1/2 -translate-y-1/2 p-3 rounded-2xl bg-black/60 hover:bg-brand-neon hover:text-black text-white text-xl transition z-20 cursor-pointer">
                    <i class="ph-bold ph-caret-left"></i>
                </button>

                <img id="carouselMainImg" src="" class="max-w-full max-h-full object-contain rounded-2xl shadow-2xl transition-all duration-300">

                <button type="button" onclick="nextCarouselImage()" class="absolute right-4 top-1/2 -translate-y-1/2 p-3 rounded-2xl bg-black/60 hover:bg-brand-neon hover:text-black text-white text-xl transition z-20 cursor-pointer">
                    <i class="ph-bold ph-caret-right"></i>
                </button>
            </div>

            <!-- Ruban miniature en bas -->
            <div id="carouselThumbnailsRibbon" class="w-full flex items-center justify-center gap-2 overflow-x-auto p-2 bg-black/40 rounded-2xl max-h-20">
                <!-- Rempli en JS -->
            </div>

        </div>
    </div>

    <!-- Aperçu d'impression intégré : aucun popup navigateur requis. -->
    <style id="dmeGrowthPrintStyles">
        @media print {
            @page { size: A4 landscape; margin: 12mm; }
            body * { visibility: hidden !important; }
            #dmeGrowthPrintModal, #dmeGrowthPrintModal * { visibility: visible !important; }
            #dmeGrowthPrintModal { display: block !important; position: fixed !important; inset: 0 !important; z-index: 99999 !important; padding: 0 !important; background: #fff !important; }
            #dmeGrowthPrintDialog { width: 100% !important; max-width: none !important; max-height: none !important; overflow: visible !important; padding: 0 !important; border: 0 !important; border-radius: 0 !important; box-shadow: none !important; background: #fff !important; }
            #dmeGrowthPrintToolbar { display: none !important; }
            #dmeGrowthPrintGraph { overflow: visible !important; }
            #dmeGrowthPrintGraph svg { display: block !important; width: 100% !important; min-width: 0 !important; height: auto !important; }
        }
    </style>
    <div id="dmeGrowthPrintModal" class="hidden fixed inset-0 z-[120] bg-black/60 p-3 md:p-6 items-center justify-center">
        <section id="dmeGrowthPrintDialog" class="w-full max-w-6xl max-h-[96vh] overflow-auto rounded-2xl bg-white text-slate-900 shadow-2xl">
            <div id="dmeGrowthPrintToolbar" class="sticky top-0 z-10 flex items-center justify-between gap-3 bg-emerald-900 px-5 py-3 text-white">
                <strong>Aperçu de la courbe de croissance</strong>
                <div class="flex gap-2"><button type="button" onclick="printDmeGrowthPdf()" class="rounded-lg bg-lime-300 px-4 py-2 text-xs font-bold text-emerald-950">Imprimer / Enregistrer PDF</button><button type="button" onclick="closeDmeGrowthPrintView()" class="rounded-lg bg-white/15 px-4 py-2 text-xs font-bold text-white">Fermer</button></div>
            </div>
            <main id="dmeGrowthPrintContent" class="p-6 md:p-9">
                <header class="mb-5 flex items-start justify-between gap-4 border-b-2 border-emerald-100 pb-4">
                    <div><h1 id="dmeGrowthPrintTitle" class="mb-2 text-2xl font-bold"></h1><p id="dmeGrowthPrintPatient" class="text-sm text-slate-600"></p><p id="dmeGrowthPrintBirth" class="text-xs text-slate-500"></p></div>
                    <span id="dmeGrowthPrintDate" class="text-xs text-slate-500"></span>
                </header>
                <div id="dmeGrowthPrintGraph" class="w-full overflow-hidden"></div>
                <div id="dmeGrowthPrintLegend" class="mt-4 flex flex-wrap gap-x-5 gap-y-2 text-xs text-slate-600"></div>
                <p class="mt-5 text-[10px] text-slate-500">Références de croissance OMS en scores Z. Ce graphique est un support de suivi et ne remplace pas l’évaluation clinique.</p>
            </main>
        </section>
    </div>

    <!-- Visionneuse DICOM Print superposée : la consultation DME reste ouverte derrière. -->
    <div id="dmeDicomPrintModal" style="z-index:2147482990" class="fixed inset-0 hidden items-center justify-center bg-slate-950/80 p-2 sm:p-4" onclick="if(event.target===this)window.closeDmeDicomPrint()">
      <section class="flex h-[97dvh] w-full max-w-[1720px] flex-col overflow-hidden rounded-2xl border border-white/10 bg-slate-100 shadow-2xl sm:h-[95dvh] sm:rounded-3xl" role="dialog" aria-modal="true" aria-label="DICOM Print dans la consultation">
        <div id="dmeDicomPrintMount" class="min-h-0 flex-1 overflow-y-auto bg-slate-50"><div class="p-10 text-center text-sm text-slate-500">Le module DICOM Print sera chargé à l’ouverture.</div></div>
      </section>
    </div>
    <!-- SCRIPT DU MOTEUR DME MULTI-ONGLETS -->
    <script>
        (function() {
            if (window._clinivaDprintKeyHandler) { document.removeEventListener('keydown', window._clinivaDprintKeyHandler); window._clinivaDprintKeyHandler = null; }
            const staleDmeModals = document.querySelectorAll('body > #livretCreationModal, body > #dmeCrPreviewModal, body > #dmeDicomPrintModal, body > #dmeSpecialtyPickerModal, body > #dprintCarouselModal, body > #dprintManagerModal, body > #dprintManagerPositionMenu, body > #dprintRowActionMenu');
            if (staleDmeModals.length) { staleDmeModals.forEach(modal => modal.remove()); document.body.style.overflow = ''; }
            function portalDmeModal(id) {
                const modal = document.getElementById(id);
                if (modal && modal.parentElement !== document.body) document.body.appendChild(modal);
                return modal;
            }
            let dmeDicomPrintLoaded = false;
            let dmeDicomPrintLoading = null;
            window.openDmeDicomPrint = async function() {
                const dmePrintTab = (window.activeDmeTabs || []).find(tab => tab.key === window.currentActiveDmeKey);
                window._clinivaDmeDicomPrintContext = dmePrintTab ? {
                    key: dmePrintTab.key,
                    patientId: dmePrintTab.patientId,
                    passageId: dmePrintTab.passageId,
                    consultationId: dmePrintTab.consultationId,
                    targetGroupId: pendingEchoUploadTargetGroupId
                } : null;
                const modal = portalDmeModal('dmeDicomPrintModal');
                const mount = document.getElementById('dmeDicomPrintMount');
                if (!modal || !mount) return;
                modal.classList.remove('hidden'); modal.classList.add('flex'); document.body.style.overflow = 'hidden';
                if (dmeDicomPrintLoaded) {
                    if (window.clinivaDprintRefresh) await window.clinivaDprintRefresh();
                    return;
                }
                if (!dmeDicomPrintLoading) {
                    dmeDicomPrintLoading = (async () => {
                        mount.innerHTML = '<div class="p-10 text-center text-sm text-slate-500"><i class="ph-bold ph-spinner animate-spin mr-2"></i>Chargement du module DICOM Print…</div>';
                        const response = await fetch('/api/modules/gestion_dicom_print');
                        if (!response.ok) {
                            const result = await response.json().catch(() => ({}));
                            throw new Error(result.detail || `Impossible de charger DICOM Print (HTTP ${response.status}).`);
                        }
                        mount.innerHTML = await response.text();
                        const dprintHeader = mount.querySelector('header');
                        const dprintActions = mount.querySelector('#dprintOpenConfig')?.parentElement || dprintHeader?.lastElementChild || dprintHeader;
                        if (dprintActions && !dprintActions.querySelector('[data-dme-dprint-return]')) {
                            const returnButton = document.createElement('button');
                            returnButton.type = 'button';
                            returnButton.dataset.dmeDprintReturn = 'true';
                            returnButton.className = 'inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-xl border border-white/25 bg-white/15 px-4 py-2.5 text-sm font-extrabold text-white shadow-sm hover:bg-white/25';
                            returnButton.innerHTML = '<i class="ph-bold ph-x-circle text-base"></i>Fermer';
                            returnButton.setAttribute('aria-label', 'Fermer DICOM Print et revenir à la consultation');
                            returnButton.addEventListener('click', window.closeDmeDicomPrint);
                            dprintActions.appendChild(returnButton);
                        }
                        mount.querySelectorAll('script').forEach(oldScript => {
                            const script = document.createElement('script');
                            Array.from(oldScript.attributes).forEach(attribute => script.setAttribute(attribute.name, attribute.value));
                            script.appendChild(document.createTextNode(oldScript.innerHTML));
                            oldScript.parentNode.replaceChild(script, oldScript);
                        });
                        dmeDicomPrintLoaded = true;
                    })().catch(error => {
                        mount.innerHTML = `<div class="m-5 rounded-2xl border border-rose-200 bg-rose-50 p-5 text-sm text-rose-800">${String(error.message || error).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]))}</div>`;
                        dmeDicomPrintLoading = null;
                    });
                }
                await dmeDicomPrintLoading;
            };
            window.closeDmeDicomPrint = function() {
                try { if (window.clinivaDprintCloseRowActions) window.clinivaDprintCloseRowActions(); } catch (_) {}
                try { if (window.clinivaDprintCloseManager) window.clinivaDprintCloseManager(); } catch (_) {}
                try { if (window.clinivaDprintCloseCarousel) window.clinivaDprintCloseCarousel(); } catch (_) {}
                try { if (window.clinivaDprintCloseConfig) window.clinivaDprintCloseConfig(); } catch (_) {}
                const modal = document.getElementById('dmeDicomPrintModal');
                if (modal) { modal.classList.add('hidden'); modal.classList.remove('flex'); }
                window._clinivaDmeDicomPrintContext = null;
                document.body.style.overflow = '';
            };
            window.clinivaDmeImportDprintGroup = async function(groupId) {
                const context = window._clinivaDmeDicomPrintContext;
                const tab = (window.activeDmeTabs || []).find(item => item.key === context?.key);
                if (!context || !tab || String(tab.patientId) !== String(context.patientId) || String(tab.passageId) !== String(context.passageId)) {
                    alert('La consultation DME cible n’est plus active. Rouvrez DICOM Print depuis la consultation souhaitée.');
                    return;
                }
                const bridge = window.pywebview?.api;
                if (!bridge?.dicom_print_group_images || !bridge?.dicom_print_group_image) {
                    alert('Le service local DICOM Print est indisponible.');
                    return;
                }
                try {
                    const images = await bridge.dicom_print_group_images(groupId);
                    if (!Array.isArray(images) || !images.length) throw new Error('Ce dossier DICOM Print ne contient aucune image.');
                    const files = [];
                    for (let index = 0; index < images.length; index++) {
                        const image = await bridge.dicom_print_group_image(groupId, index);
                        const encoded = String(image?.data_url || '').split(',', 2)[1];
                        if (!encoded) throw new Error(`Impossible de lire l’image ${index + 1}.`);
                        const binary = atob(encoded);
                        const bytes = new Uint8Array(binary.length);
                        for (let offset = 0; offset < binary.length; offset++) bytes[offset] = binary.charCodeAt(offset);
                        const baseName = String(image.name || images[index].name || `DICOM_Print_${index + 1}.png`).replace(/[\\/:*?"<>|]/g, '_');
                        files.push(new File([bytes], baseName.toLowerCase().endsWith('.png') ? baseName : `${baseName}.png`, {type: 'image/png'}));
                    }
                    const input = document.getElementById('dmeEchoUploadInput');
                    if (!input) throw new Error('Le formulaire d’importation du dossier échographique est indisponible.');
                    const transfer = new DataTransfer();
                    files.forEach(file => transfer.items.add(file));
                    input.files = transfer.files;
                    window._clinivaDmePrepareDprintAppend?.();
                    window.closeDmeDicomPrint();
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                } catch (error) {
                    alert(error.message || 'Impossible d’importer les images DICOM Print dans le DME.');
                }
            };
            window.activeDmeTabs = window.activeDmeTabs || [];
            window.currentActiveDmeKey = window.currentActiveDmeKey || null;

            let currentDmeActs = [];
            let currentMatchingDmePrestations = [];
            let selectedDmePrestationTemp = null;
            let currentOrdLines = [];
            let allMedicationsList = [];

            // Données carrousel échographie
            let activeCarouselImages = [];
            let currentCarouselIndex = 0;
            let pendingDmeModeResolve = null;
            let pendingEchoUploadFiles = [];
            let pendingEchoUploadPreviewUrls = [];
            let appendNextEchoUpload = false;
            let pendingEchoUploadCanAddDprint = false;
            let pendingEchoUploadTargetGroupId = null;

            window.addDprintImagesToEchoGroup = function(groupId) {
                const currentTab = window.activeDmeTabs.find(tab => tab.key === window.currentActiveDmeKey);
                const group = currentTab ? getEchoImageGroups(currentTab.data.echographies || []).find(item => String(item.id) === String(groupId)) : null;
                if (!currentTab || !group) { alert('Le groupe d’images cible est introuvable.'); return; }
                pendingEchoUploadPreviewUrls.forEach(url => URL.revokeObjectURL(url));
                pendingEchoUploadPreviewUrls = [];
                pendingEchoUploadFiles = [];
                appendNextEchoUpload = false;
                pendingEchoUploadTargetGroupId = String(groupId);
                const categorySelect = document.getElementById('echoUploadConfirmCategory');
                const groupCategory = group.category || 'Général';
                if (![...categorySelect.options].some(option => option.value === groupCategory)) categorySelect.add(new Option(groupCategory, groupCategory));
                categorySelect.value = groupCategory;
                document.getElementById('echoUploadConfirmType').value = group.items[0]?.type_image || 'echographique';
                window.openDmeDicomPrint();
            };

            function askDmeOpenMode(isLocked, isOwnedByAnotherUser = false, ownerName = '') {
                const modal = document.getElementById('dmeOpenModeModal');
                const message = document.getElementById('dmeOpenModeMessage');
                const executeButton = document.getElementById('dmeOpenExecuteButton');
                if (!modal) return Promise.resolve('lecture');
                if (isLocked || isOwnedByAnotherUser) {
                    const ownerText = isOwnedByAnotherUser && ownerName ? ` Consultation en cours par ${ownerName}.` : '';
                    if (message) message.textContent = isLocked
                        ? 'Cette consultation est déjà enregistrée et verrouillée. Vous pouvez consulter les informations et ouvrir les documents en lecture seule.'
                        : `Un autre utilisateur exécute cette consultation.${ownerText} Vous pouvez uniquement consulter le dossier en lecture seule.`;
                    if (executeButton) executeButton.classList.add('hidden');
                } else {
                    if (message) message.textContent = 'Choisissez entre la consultation du dossier en lecture seule ou le démarrage de la consultation médicale.';
                    if (executeButton) executeButton.classList.remove('hidden');
                }
                modal.classList.remove('hidden');
                modal.classList.add('flex');
                return new Promise(resolve => { pendingDmeModeResolve = resolve; });
            }

            window.chooseDmeOpenMode = function(mode) {
                const modal = document.getElementById('dmeOpenModeModal');
                if (modal) { modal.classList.add('hidden'); modal.classList.remove('flex'); }
                if (pendingDmeModeResolve) {
                    pendingDmeModeResolve(mode);
                    pendingDmeModeResolve = null;
                }
            };

            // 1. OUVERTURE D'UN PATIENT DEPUIS PASSAGES
            window.openPatientDme = async function(patientId, passageId) {
                const tabKey = `${patientId}_${passageId}`;
                const loadingNotice = document.getElementById('dmeLoadingNotice');
                if (loadingNotice) loadingNotice.classList.remove('hidden');

                try {
                    if (!Number.isInteger(Number(patientId)) || Number(patientId) <= 0 || !Number.isInteger(Number(passageId)) || Number(passageId) <= 0) {
                        throw new Error(`Identifiants patient/passage invalides (${patientId}/${passageId}).`);
                    }
                    const controller = new AbortController();
                    const requestTimeout = setTimeout(() => controller.abort(), 20000);
                    let res;
                    try {
                        res = await fetch(`/api/dme/record/${Number(patientId)}/${Number(passageId)}`, { signal: controller.signal });
                    } catch (fetchError) {
                        if (fetchError.name === 'AbortError') throw new Error('Le serveur met trop de temps à charger ce dossier. Vérifiez la connexion à la base de données puis réessayez.');
                        throw fetchError;
                    } finally {
                        clearTimeout(requestTimeout);
                    }
                    if (!res.ok) {
                        const errData = await res.json().catch(() => ({}));
                        throw new Error(errData.detail || `Erreur serveur HTTP ${res.status}`);
                    }
                    const data = await res.json();
                    if (!data.patient || !data.passage) throw new Error('Le serveur a renvoyé des données patient ou passage incomplètes.');
                    const isLocked = Boolean(data.dme && data.dme.id && data.dme.verrouille);
                    const isOwnedByAnotherUser = Boolean(data.dme?.consultation_en_cours_par_autre);
                    const openMode = await askDmeOpenMode(isLocked, isOwnedByAnotherUser, data.dme?.consultation_owner || '');
                    if (!openMode) {
                        if (loadingNotice) loadingNotice.classList.add('hidden');
                        return;
                    }

                    if (openMode === 'execution' && !isLocked && !isOwnedByAnotherUser) {
                        const startResponse = await fetch('/api/dme/consultation/start', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ id_patient: patientId, id_passage: passageId })
                        });
                        const startResult = await startResponse.json();
                        if (!startResponse.ok || !startResult.success) {
                            throw new Error(startResult.detail || startResult.message || 'Impossible de démarrer cette consultation.');
                        }
                        data.dme = { ...(data.dme || {}), id: startResult.id_consultation, verrouille: false };
                        data.passage.statut = startResult.statut || 'En cours';
                        data.historique = (data.historique || []).map(item =>
                            Number(item.id_passage) === Number(passageId)
                                ? { ...item, statut: data.passage.statut }
                                : item
                        );
                        if (typeof window.loadReceptionPassagesModule === 'function') window.loadReceptionPassagesModule();
                    }

                    const existingIndex = window.activeDmeTabs.findIndex(t => t.key === tabKey);
                    const tabObj = {
                        key: tabKey,
                        patientId: patientId,
                        passageId: passageId,
                        consultationId: data.dme?.id || null,
                        title: `${data.patient.nom} ${data.patient.prenom}`,
                        data: data,
                        mode: (isLocked || isOwnedByAnotherUser) ? 'lecture' : openMode,
                        currentSubTab: 'consultation'
                    };

                    if (existingIndex >= 0) {
                        window.activeDmeTabs[existingIndex] = tabObj;
                    } else {
                        window.activeDmeTabs.push(tabObj);
                    }

                    window.currentActiveDmeKey = tabKey;
                    renderDmeTabsBar();
                    displayActivePatientDme();
                    if (loadingNotice) loadingNotice.classList.add('hidden');
                } catch (err) {
                    if (loadingNotice) {
                        loadingNotice.classList.remove('hidden');
                        loadingNotice.innerHTML = `<i class="ph-bold ph-warning-circle mr-1 text-rose-500"></i><span class="text-rose-600 dark:text-rose-300">${escapeDmeArchiveHtml(err.message || 'Erreur de chargement.')}</span>`;
                    }
                    console.error("Erreur openPatientDme :", err);
                    window.chooseDmeOpenMode(null);
                    alert("❌ " + err.message);
                }
            };

            // 2. BARRE D'ONGLETS PATIENTS
            function renderDmeTabsBar() {
                const bar = document.getElementById('dmeTabsBar');
                if (!bar) return;
                bar.innerHTML = '';

                if (!window.activeDmeTabs || window.activeDmeTabs.length === 0) {
                    bar.innerHTML = '<span class="text-xs text-slate-400 italic">Aucun onglet ouvert.</span>';
                    return;
                }

                window.activeDmeTabs.forEach(t => {
                    const isActive = t.key === window.currentActiveDmeKey;
                    const btn = document.createElement('div');
                    btn.className = `flex items-center gap-2.5 px-4 py-2.5 rounded-2xl text-xs font-bold transition cursor-pointer flex-shrink-0 ${
                        isActive 
                            ? 'bg-brand-neon text-black shadow-md shadow-brand-neon/20' 
                            : 'bg-white dark:bg-[#121c15] text-slate-600 dark:text-slate-300 border border-slate-200 dark:border-white/10 hover:border-brand-neon'
                    }`;

                    btn.innerHTML = `
                        <i class="ph-bold ph-user text-sm"></i>
                        <span onclick="switchDmeTab('${t.key}')">${t.title} <span class="opacity-70 font-mono text-[10px]">#PASS-${String(t.passageId).padStart(4, '0')}</span></span>
                        <button type="button" onclick="closeDmeTab('${t.key}', event)" class="p-0.5 rounded-full hover:bg-black/20 text-xs">✕</button>
                    `;
                    bar.appendChild(btn);
                });
            }
            window.loadLivretPageGardeOptions = async function() {
            const select = document.getElementById('livretPageGardeSelect');
            const hint = document.getElementById('livretPageGardeHint');
            if (!select) return;
            select.replaceChildren();
            try {
                const response = await fetch('/api/dme/livret/covers');
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Impossible de charger les pages de garde.');
                const covers = data.covers || [];
                covers.forEach(cover => {
                    const option = document.createElement('option');
                    option.value = cover.filename;
                    option.textContent = cover.filename;
                    select.appendChild(option);
                });
                if (!covers.length) {
                    const option = document.createElement('option');
                    option.value = '';
                    option.textContent = 'Aucune page de garde disponible';
                    select.appendChild(option);
                    select.disabled = true;
                    const defaultCheckbox = document.getElementById('livretUseDefaultCover');
                    if (defaultCheckbox) { defaultCheckbox.checked = false; defaultCheckbox.disabled = true; }
                    if (hint) hint.textContent = 'Ajoutez une image PNG ou JPG dans uploads/model_templates/Page_De_Garde.';
                    document.getElementById('btnSubmitLivret').disabled = true;
                    previewLivretPageGarde();
                    return;
                }
                select.disabled = false;
                const defaultCheckbox = document.getElementById('livretUseDefaultCover');
                if (defaultCheckbox) {
                    defaultCheckbox.disabled = false;
                    defaultCheckbox.checked = Boolean(data.user_default);
                }
                document.getElementById('btnSubmitLivret').disabled = false;
                select.value = data.default || covers[0].filename;
                if (hint) hint.textContent = `${covers.length} page(s) de garde disponible(s).`;
                previewLivretPageGarde();
            } catch (error) {
                select.disabled = true;
                const option = document.createElement('option');
                option.value = '';
                option.textContent = 'Chargement impossible';
                select.appendChild(option);
                if (hint) hint.textContent = error.message || 'Impossible de charger les pages de garde.';
                document.getElementById('btnSubmitLivret').disabled = true;
            }
        };

        window.previewLivretPageGarde = function() {
            const select = document.getElementById('livretPageGardeSelect');
            const preview = document.getElementById('livretPageGardePreview');
            const placeholder = document.getElementById('livretPageGardePlaceholder');
            if (!select || !preview) return;
            if (!select.value) {
                preview.classList.add('hidden');
                if (placeholder) placeholder.classList.remove('hidden');
                preview.removeAttribute('src');
                return;
            }
            preview.src = '/api/dme/livret/covers/' + encodeURIComponent(select.value);
            preview.classList.remove('hidden');
            if (placeholder) placeholder.classList.add('hidden');
        };

        window.saveLivretDefaultCoverPreference = async function() {
            const checkbox = document.getElementById('livretUseDefaultCover');
            const select = document.getElementById('livretPageGardeSelect');
            const hint = document.getElementById('livretPageGardeHint');
            if (!checkbox || !checkbox.checked) return true;
            if (!select || !select.value) return false;
            try {
                const response = await fetch('/api/dme/livret/covers/default', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ filename: select.value })
                });
                const data = await response.json();
                if (!response.ok || !data.success) throw new Error(data.detail || 'Enregistrement impossible.');
                if (hint) hint.textContent = `Page de garde « ${select.value} » enregistrée par défaut pour votre compte.`;
                return true;
            } catch (error) {
                checkbox.checked = false;
                if (hint) hint.textContent = error.message || 'Impossible d’enregistrer cette page de garde par défaut.';
                return false;
            }
        };

        window.openLivretModal = async function(crId) {
            const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
            if (!currentTab) return;

            const p = currentTab.data.patient || {};
            const pass = currentTab.data.passage || {};
            const cr = (currentTab.data.archived_crs || []).find(c => c.id === crId);
            const echos = currentTab.data.echographies || [];

            document.getElementById('livretCrId').value = crId;
            document.getElementById('livretNomPrenom').value = `${p.nom || ''} ${p.prenom || ''}`.trim().toUpperCase();
            document.getElementById('livretDateExamen').value = pass.date_passage || new Date().toLocaleDateString('fr-FR');
            const prestationSelect = document.getElementById('livretTypeExamen');
            const prestationIdInput = document.getElementById('livretPrestationId');
            const prestationOptions = document.getElementById('livretTypeOptions');
            prestationSelect.value = '';
            prestationIdInput.value = '';
            const normalizePrestation = value => String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/\\s+/g, ' ').trim();
            const currentConsultationActs = Array.isArray(currentDmeActs) ? currentDmeActs : [];
            const currentConsultationNames = new Set(currentConsultationActs.map(act => normalizePrestation(act.nom || act.designation || act.name)).filter(Boolean));
            const allowedPrestations = (currentTab.data.consultations || []).filter(item => {
                const text = normalizePrestation(`${item.designation || ''} ${item.categorie || ''} ${item.specialite || ''}`);
                return currentConsultationNames.has(normalizePrestation(item.designation)) && !['contrast', 'gadolinium', 'produit iode', 'sedat', 'anesth', 'soin', 'infirm'].some(term => text.includes(term));
            });
            prestationOptions.innerHTML = allowedPrestations.map(item => `<option value="${item.designation}" data-prestation-id="${item.id}"></option>`).join('');
            if (allowedPrestations.length === 1) {
                prestationSelect.value = allowedPrestations[0].designation;
                prestationIdInput.value = String(allowedPrestations[0].id);
            }
            await loadLivretPageGardeOptions();


            const groupSelect = document.getElementById('livretImageGroup');
            const imageGroups = getEchoImageGroups(echos);
            if (groupSelect) {
                groupSelect.innerHTML = imageGroups.length
                    ? imageGroups.map(group => `<option value="${echoManagerEscape(group.id)}">${echoManagerEscape(group.label)}</option>`).join('')
                    : '<option value="">Aucun groupe disponible</option>';
                groupSelect.value = imageGroups[0]?.id || '';
            }
            window.renderLivretImageChoices();

            const livretModal = portalDmeModal('livretCreationModal');
            livretModal.classList.remove('hidden');
            livretModal.classList.add('flex');
            document.body.style.overflow = 'hidden';
        };
        window.syncLivretPrestationSelection = function() {
            const input = document.getElementById('livretTypeExamen');
            const option = [...(document.getElementById('livretTypeOptions')?.options || [])].find(item => item.value === input?.value);
            if (option) document.getElementById('livretPrestationId').value = option.dataset.prestationId || '';
        };
        window.renderLivretImageChoices = function() {
            const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
            const grid = document.getElementById('livretEchosCheckboxGrid');
            if (!grid || !currentTab) return;
            const allImages = currentTab.data.echographies || [];
            const groupId = String(document.getElementById('livretImageGroup')?.value || '');
            const echos = allImages.filter(ec => String(echoImageGroupId(ec)) === groupId);
            if (echos.length === 0) {
                grid.innerHTML = '<span class="text-xs text-slate-400 italic col-span-full py-8 text-center">Aucune image dans ce groupe pour ce passage.</span>';
            } else {
                grid.innerHTML = echos.map(ec => `
                        <label data-echo-id="${ec.id}" class="group relative flex flex-col gap-3 p-3 rounded-2xl border border-slate-200 dark:border-white/10 bg-white dark:bg-[#121c15] hover:border-brand-neon hover:shadow-md shadow-sm transition">
                            <button type="button" onpointerdown="livretImagePointerDown(event, ${ec.id})" onclick="event.preventDefault(); event.stopPropagation()" class="absolute top-2 right-2 z-10 w-9 h-9 rounded-xl bg-slate-900/80 hover:bg-slate-900 text-white cursor-grab active:cursor-grabbing touch-none select-none shadow-md" title="Maintenir et déplacer pour choisir l’ordre" aria-label="Déplacer cette image dans le livret"><i class="ph-bold ph-dots-six-vertical"></i></button>
                            <div class="w-full h-44 sm:h-52 rounded-xl overflow-hidden bg-slate-100 dark:bg-black/50 border border-slate-200 dark:border-white/10 flex items-center justify-center">
                                <img src="/api/dme/echographies/file/${ec.id}" alt="${ec.nom_fichier || 'Cliché échographique'}" class="max-w-full max-h-full object-contain group-hover:scale-[1.03] transition duration-200">
                            </div>
                            <div class="flex items-center gap-3 min-w-0 px-1 pb-1">
                                <input type="checkbox" value="${ec.id}" checked aria-label="Inclure ce cliché dans le livret" class="livret-echo-check h-6 w-6 shrink-0 rounded-md text-brand-neon focus:ring-brand-neon cursor-pointer">
                                <div class="flex-1 min-w-0">
                                    <span class="text-sm font-bold text-slate-800 dark:text-slate-100 block truncate" title="${ec.nom_fichier}">${ec.nom_fichier || 'Image'} · ${(ec.type_image || 'echographique') === 'radiologique' ? 'Radiologique' : (ec.type_image || '') === 'planche_trois' ? 'Planche A3' : 'Échographique'}</span>
                                    <span class="text-[11px] text-slate-500 dark:text-slate-400 block mt-1 font-mono">${ec.date_heure_str || ''}</span>
                                    <span class="mt-2 inline-block px-2 py-1 rounded-lg text-[10px] font-bold bg-brand-neon/20 text-emerald-800 dark:text-brand-neon">À inclure au livret</span>
                                </div>
                            </div>
                        </label>
                    `).join('');
            }
        };
        window.closeLivretModal = function() {
            const modal = document.getElementById('livretCreationModal');
            if (modal) { modal.classList.add('hidden'); modal.classList.remove('flex'); }
            document.body.style.overflow = '';
        };

        let livretPointerDragId = null;
        window.livretImagePointerDown = function(event, imageId) {
            event.preventDefault();
            event.stopPropagation();
            livretPointerDragId = String(imageId);
            const card = document.querySelector(`#livretEchosCheckboxGrid [data-echo-id="${imageId}"]`);
            if (card) card.classList.add('ring-2', 'ring-brand-neon', 'opacity-60');
            document.addEventListener('pointermove', window.livretImagePointerMove);
            document.addEventListener('pointerup', window.livretImagePointerUp, { once: true });
            document.addEventListener('pointercancel', window.livretImagePointerUp, { once: true });
        };
        window.livretImagePointerMove = function(event) {
            if (!livretPointerDragId) return;
            event.preventDefault();
            const grid = document.getElementById('livretEchosCheckboxGrid');
            const source = [...grid.querySelectorAll('[data-echo-id]')].find(card => card.dataset.echoId === livretPointerDragId);
            const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-echo-id]');
            if (!source || !target || source === target || !grid.contains(target)) return;
            const rect = target.getBoundingClientRect();
            const sameRow = Math.abs(rect.top - source.getBoundingClientRect().top) < rect.height / 2;
            const after = sameRow ? event.clientX > rect.left + rect.width / 2 : event.clientY > rect.top + rect.height / 2;
            grid.insertBefore(source, after ? target.nextElementSibling : target);
        };
        window.livretImagePointerUp = function() {
            document.removeEventListener('pointermove', window.livretImagePointerMove);
            document.removeEventListener('pointerup', window.livretImagePointerUp);
            document.removeEventListener('pointercancel', window.livretImagePointerUp);
            document.querySelectorAll('#livretEchosCheckboxGrid [data-echo-id]').forEach(card => card.classList.remove('ring-2', 'ring-brand-neon', 'opacity-60'));
            livretPointerDragId = null;
        };

        window.toggleAllLivretEchos = function(check) {
            document.querySelectorAll('.livret-echo-check').forEach(cb => cb.checked = check);
        };

        window.handleConfirmLivret = async function(e) {
            e.preventDefault();
            const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
            if (!currentTab) return;
            if (!await window.saveLivretDefaultCoverPreference()) {
                alert('Impossible d’enregistrer la page de garde par défaut pour votre compte. Vérifiez votre session puis réessayez.');
                return;
            }

            const selectedEchoIds = [];
            document.querySelectorAll('#livretEchosCheckboxGrid [data-echo-id]').forEach(card => {
                const checkbox = card.querySelector('.livret-echo-check');
                if (checkbox?.checked) selectedEchoIds.push(parseInt(checkbox.value));
            });

            const btn = document.getElementById('btnSubmitLivret');
            const originalHtml = btn ? btn.innerHTML : '';
            if (btn) {
                btn.disabled = true;
                btn.innerHTML = '<i class="ph-bold ph-spinner animate-spin"></i> Génération du livret...';
            }

            const payload = {
                id_patient: currentTab.patientId,
                id_passage: currentTab.passageId,
                cr_id: parseInt(document.getElementById('livretCrId').value),
                nom_prenom: document.getElementById('livretNomPrenom').value,
                date_examen: document.getElementById('livretDateExamen').value,
                prestation_id: parseInt(document.getElementById('livretPrestationId').value || '0'),
                type_examen: document.getElementById('livretTypeExamen').value,
                page_de_garde: document.getElementById('livretPageGardeSelect').value,
                groupe_id: document.getElementById('livretImageGroup').value,
                selected_echo_ids: selectedEchoIds
            };

            try {
                const res = await fetch('/api/dme/livret/create', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                }).then(r => r.json());

                if (res.success && res.draft_token) {
                    closeLivretModal();
                    openLivretDraftPreview(res.draft_token, document.getElementById('livretTypeExamen').value || 'Livret Médical');
                } else {
                    alert(res.message || "Erreur lors de la création du livret.");
                }
            } catch (err) {
                alert("Erreur lors de la génération du livret.");
            } finally {
                if (btn) {
                    btn.disabled = false;
                    btn.innerHTML = originalHtml;
                }
            }
        };

            window.switchDmeTab = function(key) {
                window.currentActiveDmeKey = key;
                renderDmeTabsBar();
                displayActivePatientDme();
            };

            let pendingDmeCloseTabKey = null;
            window.closeDmeTab = function(key, e) {
                e?.stopPropagation();
                const closingTab = window.activeDmeTabs.find(t => t.key === key);
                if (closingTab?.mode === 'execution' && !closingTab.data?.dme?.verrouille) {
                    window.currentActiveDmeKey = key;
                    displayActivePatientDme();
                    pendingDmeCloseTabKey = key;
                    openDmeCloseConsultation(closingTab);
                    return;
                }
                removeDmeTabNow(key);
            };

            function removeDmeTabNow(key) {
                window.activeDmeTabs = window.activeDmeTabs.filter(t => t.key !== key);
                if (window.currentActiveDmeKey === key) {
                    window.currentActiveDmeKey = window.activeDmeTabs.length > 0 ? window.activeDmeTabs[window.activeDmeTabs.length - 1].key : null;
                }
                renderDmeTabsBar();
                displayActivePatientDme();
            }

            function openDmeCloseConsultation(tab) {
                const modal = document.getElementById('dmeCloseConsultationModal');
                const resetButton = document.getElementById('dmeCloseResetButton');
                const resetHint = document.getElementById('dmeCloseResetHint');
                const doctorSelect = document.getElementById('dmeHandoffDoctorSelect');
                const noteInput = document.getElementById('dmeHandoffNoteInput');
                const canReset = getDmeResetPermission(tab) === true;
                if (resetButton) {
                    resetButton.disabled = !canReset;
                    resetButton.classList.toggle('opacity-50', !canReset);
                    resetButton.classList.toggle('cursor-not-allowed', !canReset);
                    resetButton.title = canReset ? '' : 'Autorisation « Réinitialiser une consultation DME en cours » requise.';
                }
                if (resetHint && !canReset) resetHint.textContent = 'Option réservée aux utilisateurs ayant le droit de réinitialiser une consultation DME.';
                if (noteInput) noteInput.value = '';
                if (doctorSelect) {
                    const doctors = (tab?.data?.radiologues || []).filter(doc => doc && doc.id);
                    doctorSelect.innerHTML = '<option value="">Choisir un médecin…</option>';
                    doctors.forEach(doc => {
                        const name = [doc.prenom, doc.nom].filter(Boolean).join(' ').trim() || 'Médecin';
                        const option = new Option(doc.specialite ? `${name} — ${doc.specialite}` : name, String(doc.id));
                        doctorSelect.add(option);
                    });
                    if (doctors.length === 0) doctorSelect.innerHTML = '<option value="">Aucun médecin actif dans le personnel</option>';
                }
                document.getElementById('dmeHandoffFields')?.classList.add('hidden');
                if (modal) {
                    modal.classList.remove('hidden');
                    modal.classList.add('flex');
                    document.body.style.overflow = 'hidden';
                }
            }

            window.cancelDmeCloseConsultation = function() {
                const modal = document.getElementById('dmeCloseConsultationModal');
                modal?.classList.add('hidden');
                modal?.classList.remove('flex');
                document.getElementById('dmeHandoffFields')?.classList.add('hidden');
                document.body.style.overflow = '';
                pendingDmeCloseTabKey = null;
            };

            window.showDmeHandoffFields = function() {
                document.getElementById('dmeHandoffFields')?.classList.remove('hidden');
                document.getElementById('dmeHandoffNoteInput')?.focus();
            };

            window.saveAndCloseDmeConsultation = async function() {
                const key = pendingDmeCloseTabKey;
                if (!key) return;
                const modal = document.getElementById('dmeCloseConsultationModal');
                modal?.classList.add('hidden');
                modal?.classList.remove('flex');
                document.body.style.overflow = '';
                if (await window.saveActiveDmeConsultation({ closeAfter: true })) pendingDmeCloseTabKey = null;
                else {
                    modal?.classList.remove('hidden');
                    modal?.classList.add('flex');
                    document.body.style.overflow = 'hidden';
                }
            };

            window.cancelAccidentalDmeStart = async function() {
                const pendingKey = pendingDmeCloseTabKey;
                if (!pendingKey || !confirm('Annuler le démarrage et remettre le passage en attente ? Les données saisies seront enregistrées en brouillon.')) return;
                window.currentActiveDmeKey = pendingKey;
                const modal = document.getElementById('dmeCloseConsultationModal');
                modal?.classList.add('hidden');
                modal?.classList.remove('flex');
                document.body.style.overflow = '';
                const cancelled = await window.saveActiveDmeConsultation({ closeAfter: true, cancelStart: true });
                if (cancelled) {
                    // La sauvegarde ferme déjà l'onglet courant. Ce contrôle
                    // garantit sa fermeture même si son implémentation évolue.
                    if (window.activeDmeTabs.some(tab => tab.key === pendingKey)) removeDmeTabNow(pendingKey);
                    pendingDmeCloseTabKey = null;
                } else {
                    modal?.classList.remove('hidden');
                    modal?.classList.add('flex');
                    document.body.style.overflow = 'hidden';
                }
            };

            window.keepAndCloseDmeConsultation = async function() {
                const key = pendingDmeCloseTabKey;
                const note = document.getElementById('dmeHandoffNoteInput')?.value?.trim() || '';
                const doctorId = document.getElementById('dmeHandoffDoctorSelect')?.value || '';
                if (!note) { alert('Écrivez une note de reprise pour les autres utilisateurs.'); return; }
                if (!doctorId) { alert('Choisissez le médecin à consulter.'); return; }
                const modal = document.getElementById('dmeCloseConsultationModal');
                modal?.classList.add('hidden');
                modal?.classList.remove('flex');
                document.body.style.overflow = '';
                if (await window.saveActiveDmeConsultation({ closeAfter: true, keepOpen: true, note, doctorId })) pendingDmeCloseTabKey = null;
                else {
                    modal?.classList.remove('hidden');
                    modal?.classList.add('flex');
                    document.body.style.overflow = 'hidden';
                }
            };

            window.resetAndCloseDmeConsultation = async function() {
                const key = pendingDmeCloseTabKey;
                const tab = window.activeDmeTabs.find(t => t.key === key);
                if (!tab || getDmeResetPermission(tab) !== true) return;
                const button = document.getElementById('dmeCloseResetButton');
                if (button) button.disabled = true;
                try {
                    const response = await fetch('/api/dme/consultation/reset', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({id_patient: Number(tab.patientId), id_passage: Number(tab.passageId), id_consultation: Number(tab.consultationId || tab.data?.dme?.id)})
                    });
                    const result = await response.json();
                    if (!response.ok || !result.success) throw new Error(result.detail || result.message || 'Réinitialisation impossible.');
                    const modal = document.getElementById('dmeCloseConsultationModal');
                    modal?.classList.add('hidden');
                    modal?.classList.remove('flex');
                    document.body.style.overflow = '';
                    pendingDmeCloseTabKey = null;
                    removeDmeTabNow(key);
                    if (typeof window.loadReceptionPassagesModule === 'function') window.loadReceptionPassagesModule();
                } catch (error) {
                    alert(error.message || 'Impossible de réinitialiser la consultation.');
                    if (button) button.disabled = false;
                    const modal = document.getElementById('dmeCloseConsultationModal');
                    modal?.classList.remove('hidden');
                    modal?.classList.add('flex');
                    document.body.style.overflow = 'hidden';
                }
            };

            function applyDmeAccessMode(currentTab) {
                const saved = Boolean(currentTab?.data?.dme?.verrouille);
                const canEdit = currentTab?.mode === 'execution' && !saved;
                const badge = document.getElementById('dmeAccessModeBadge');
                const saveButton = document.getElementById('dmeSaveConsultationButton');

                if (badge) {
                    badge.textContent = canEdit ? 'Consultation en cours · modifiable' : (saved ? 'Consultation enregistrée · lecture seule' : 'Lecture seule');
                    badge.className = canEdit
                        ? 'px-3 py-2 rounded-xl text-[10px] font-bold bg-brand-neon/20 text-emerald-800 dark:text-brand-neon'
                        : 'px-3 py-2 rounded-xl text-[10px] font-bold bg-slate-100 text-slate-600 dark:bg-white/5 dark:text-slate-300';
                }
                if (saveButton) saveButton.classList.toggle('hidden', !canEdit);
                const resetButton = document.getElementById('dmeResetConsultationButton');
                if (resetButton) {
                    const permission = getDmeResetPermission(currentTab);
                    const consultationId = currentTab?.consultationId || currentTab?.data?.dme?.id;
                    const enabled = canEdit && permission === true && Boolean(consultationId) && !currentTab?.resetting && !currentTab?.examRequestState?.saving;
                    const reason = permission === null
                        ? 'Droits non reçus : redémarrez le serveur puis rouvrez le DME.'
                        : permission !== true
                            ? 'Autorisation « Réinitialiser une consultation DME en cours » requise dans le profil.'
                            : !canEdit
                                ? 'Disponible uniquement pour une consultation en cours, en mode exécution.'
                                : !consultationId
                                    ? 'Démarrez la consultation pour activer le reset.'
                                    : currentTab?.resetting ? 'Réinitialisation en cours…' : currentTab?.examRequestState?.saving ? 'Enregistrement de la demande en cours…' : '';
                    resetButton.classList.remove('hidden');
                    resetButton.disabled = !enabled;
                    resetButton.title = reason || 'Remettre toute la consultation en cours à zéro';
                    resetButton.setAttribute('aria-describedby', 'dmeResetConsultationHint');
                    const hint = document.getElementById('dmeResetConsultationHint');
                    if (hint) { hint.textContent = reason; hint.classList.toggle('hidden', !reason); }
                }

                const viewOnlyActions = /viewArchivedConsultation|openWordPdfPreview|openCrPreviewModal|openPrescriptionModal|startEchoCarousel|openEchoCarouselAtIndex|openDmeAdmissionDocument|openDmeDemandeExamen|previewDmeReeducationPlan/;
                document.querySelectorAll('.dme-subtab-panel').forEach(panel => {
                    panel.querySelectorAll('input, textarea, select, button').forEach(control => {
                        if (canEdit) {
                            control.disabled = false;
                            control.classList.remove('opacity-50', 'cursor-not-allowed');
                            return;
                        }
                        const inlineAction = control.getAttribute('onclick') || '';
                        const isReadAction = (control.tagName === 'BUTTON' && viewOnlyActions.test(inlineAction))
                            || control.hasAttribute('data-dme-allow-readonly');
                        control.disabled = !isReadAction;
                        control.classList.toggle('opacity-50', !isReadAction);
                        control.classList.toggle('cursor-not-allowed', !isReadAction);
                    });
                });

                const uploadLabel = document.querySelector('label[for="dmeEchoUploadInput"]');
                if (uploadLabel) uploadLabel.classList.toggle('hidden', !canEdit);
            }

            let dmeAdmissionViewerItems = [];
            let dmeAdmissionViewerIndex = 0;
            let dmeAdmissionViewerZoom = 1;
            window.openDmeAdmissionViewer = function(items, index) {
                dmeAdmissionViewerItems = items || [];
                if (!dmeAdmissionViewerItems.length) return;
                dmeAdmissionViewerIndex = Math.max(0, Math.min(Number(index) || 0, dmeAdmissionViewerItems.length - 1));
                dmeAdmissionViewerZoom = 1;
                renderDmeAdmissionViewer();
                const modal = document.getElementById('dmeAdmissionViewerModal');
                modal.classList.remove('hidden'); modal.classList.add('flex');
            };
            window.renderDmeAdmissionViewer = function() {
                const item = dmeAdmissionViewerItems[dmeAdmissionViewerIndex];
                if (!item) return;
                document.getElementById('dmeAdmissionViewerTitle').textContent = item.title || 'Document patient';
                document.getElementById('dmeAdmissionViewerCounter').textContent = `${dmeAdmissionViewerIndex + 1} / ${dmeAdmissionViewerItems.length}`;
                document.getElementById('dmeAdmissionZoomLabel').textContent = `${Math.round(dmeAdmissionViewerZoom * 100)}%`;
                const frame = document.getElementById('dmeAdmissionViewerFrame');
                frame.src = item.url;
                frame.style.zoom = String(dmeAdmissionViewerZoom);
                frame.style.width = `${100 / dmeAdmissionViewerZoom}%`;
                frame.style.height = `${100 / dmeAdmissionViewerZoom}%`;
            };
            window.navigateDmeAdmissionViewer = function(delta) {
                const next = dmeAdmissionViewerIndex + Number(delta);
                if (next < 0 || next >= dmeAdmissionViewerItems.length) return;
                dmeAdmissionViewerIndex = next; dmeAdmissionViewerZoom = 1; renderDmeAdmissionViewer();
            };
            window.changeDmeAdmissionZoom = function(delta) {
                dmeAdmissionViewerZoom = Math.max(0.5, Math.min(2.5, dmeAdmissionViewerZoom + Number(delta)));
                renderDmeAdmissionViewer();
            };
            window.resetDmeAdmissionZoom = function() { dmeAdmissionViewerZoom = 1; renderDmeAdmissionViewer(); };
            window.closeDmeAdmissionViewer = function() {
                const modal = document.getElementById('dmeAdmissionViewerModal');
                modal.classList.add('hidden'); modal.classList.remove('flex');
                document.getElementById('dmeAdmissionViewerFrame').src = 'about:blank';
                dmeAdmissionViewerItems = [];
            };
            window.openDmeAdmissionDocument = function(patientId, passageId, documentIndex) {
                const items = [{title: 'Demande d’examen', url: `/api/reception/demande-examen/${Number(passageId)}`}];
                const docs = window.activeDmeTabs?.find(t => t.key === window.currentActiveDmeKey)?.data?.passage?.documents_scannes || [];
                docs.forEach((doc, index) => items.push({title: doc.type || `Document scanné ${index + 1}`, url: `/api/dme/admission-document/${Number(patientId)}/${Number(passageId)}/${index}`}));
                window.openDmeAdmissionViewer(items, Number(documentIndex) + 1);
            };
            window.openDmeDemandeExamen = function(passageId) {
                const tab = window.activeDmeTabs?.find(t => t.key === window.currentActiveDmeKey);
                const items = [{title: 'Demande d’examen', url: `/api/reception/demande-examen/${Number(passageId)}`}];
                (tab?.data?.passage?.documents_scannes || []).forEach((doc, index) => items.push({title: doc.type || `Document scanné ${index + 1}`, url: `/api/dme/admission-document/${Number(tab.patientId)}/${Number(passageId)}/${index}`}));
                window.openDmeAdmissionViewer(items, 0);
            };

            let dmeConsumptionOptions = null;
            const escapeDmeConsumption = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
            const dmeConsumptionDate = value => {
                if (!value) return '—';
                const date = new Date(value);
                return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('fr-FR');
            };
            window.loadDmeConsumptions = async function() {
                const tab = window.activeDmeTabs.find(item => item.key === window.currentActiveDmeKey);
                if (!tab) return;
                const consultationId = Number(tab.consultationId || tab.data?.dme?.id || 0);
                const rows = document.getElementById('dmeConsumptionRows');
                const form = document.getElementById('dmeConsumptionForm');
                try {
                    if (!dmeConsumptionOptions) {
                        const optionsResponse = await fetch('/api/dme/consumptions/options');
                        const optionsData = await optionsResponse.json();
                        if (!optionsResponse.ok) throw new Error(optionsData.detail || 'Chargement des salles et services impossible.');
                        dmeConsumptionOptions = optionsData;
                        const fillSelect = (id, options, firstLabel, getValue, getLabel) => {
                            const select = document.getElementById(id);
                            select.innerHTML = '<option value="">'+escapeDmeConsumption(firstLabel)+'</option>' + options.map(item => '<option value="'+escapeDmeConsumption(getValue(item))+'">'+escapeDmeConsumption(getLabel(item))+'</option>').join('');
                        };
                        fillSelect('dmeConsumptionService', optionsData.services || [], 'Choisir un service', value => value, value => value);
                        fillSelect('dmeConsumptionRoom', optionsData.rooms || [], 'Sans salle dédiée', item => item.id, item => item.designation);
                        const userName = optionsData.current_user?.name || 'Utilisateur connecté';
                        document.getElementById('dmeConsumptionUser').value = userName;
                        document.getElementById('dmeConsumptionUserBadge').textContent = 'Enregistré par · ' + userName;
                    }
                    const defaultService = tab.data?.passage?.type_passage || '';
                    if (defaultService && [...document.getElementById('dmeConsumptionService').options].some(option => option.value === defaultService)) {
                        document.getElementById('dmeConsumptionService').value = defaultService;
                    }
                    const response = consultationId
                        ? await fetch(`/api/dme/consumptions/${Number(tab.patientId)}/${Number(tab.passageId)}/${consultationId}`)
                        : null;
                    const data = response ? await response.json() : {consumptions: []};
                    if (response && !response.ok) throw new Error(data.detail || 'Chargement des consommations impossible.');
                    const items = data.consumptions || [];
                    rows.innerHTML = items.map(item => '<tr><td class="p-2.5">'+escapeDmeConsumption(dmeConsumptionDate(item.consomme_le))+'</td><td class="p-2.5"><strong>'+escapeDmeConsumption(item.nom_produit)+'</strong><div class="text-[10px] text-slate-400">'+escapeDmeConsumption(item.type_produit)+'</div></td><td class="p-2.5 font-bold">'+escapeDmeConsumption(item.quantite)+' '+escapeDmeConsumption(item.unite)+'</td><td class="p-2.5">'+escapeDmeConsumption(item.service)+'</td><td class="p-2.5">'+escapeDmeConsumption(item.salle || '—')+'</td><td class="p-2.5">'+escapeDmeConsumption(item.utilisateur)+'</td><td class="p-2.5">'+escapeDmeConsumption(item.lot || '—')+'</td></tr>').join('') || '<tr><td colspan="7" class="p-5 text-center text-slate-400">Aucune consommation enregistrée pour cette consultation.</td></tr>';
                    if (form) form.onsubmit = async event => {
                        event.preventDefault();
                        const activeTab = window.activeDmeTabs.find(item => item.key === window.currentActiveDmeKey);
                        if (!activeTab) return;
                        const saveButton = document.getElementById('dmeConsumptionSave');
                        const message = document.getElementById('dmeConsumptionMessage');
                        saveButton.disabled = true;
                        try {
                            const payload = {
                                id_patient: activeTab.patientId, id_passage: activeTab.passageId,
                                id_consultation: activeTab.consultationId || activeTab.data?.dme?.id,
                                type_produit: document.getElementById('dmeConsumptionType').value,
                                nom_produit: document.getElementById('dmeConsumptionProduct').value.trim(),
                                quantite: document.getElementById('dmeConsumptionQty').value,
                                unite: document.getElementById('dmeConsumptionUnit').value.trim(),
                                service: document.getElementById('dmeConsumptionService').value,
                                salle_id: document.getElementById('dmeConsumptionRoom').value || null,
                                lot: document.getElementById('dmeConsumptionLot').value,
                                notes: document.getElementById('dmeConsumptionNotes').value
                            };
                            const saveResponse = await fetch('/api/dme/consumptions/save', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
                            const saveData = await saveResponse.json();
                            if (!saveResponse.ok || !saveData.success) throw new Error(saveData.detail || 'Enregistrement impossible.');
                            form.reset();
                            document.getElementById('dmeConsumptionUser').value = dmeConsumptionOptions?.current_user?.name || '';
                            if (defaultService && [...document.getElementById('dmeConsumptionService').options].some(option => option.value === defaultService)) document.getElementById('dmeConsumptionService').value = defaultService;
                            message.textContent = 'Consommation enregistrée.';
                            message.className = 'mt-3 rounded-xl bg-emerald-50 p-3 text-xs font-semibold text-emerald-700';
                            await window.loadDmeConsumptions();
                        } catch (error) {
                            message.textContent = error.message || 'Enregistrement impossible.';
                            message.className = 'mt-3 rounded-xl bg-rose-50 p-3 text-xs font-semibold text-rose-700';
                        } finally { saveButton.disabled = false; }
                    };
                } catch (error) {
                    rows.innerHTML = '<tr><td colspan="7" class="p-5 text-center text-rose-500">'+escapeDmeConsumption(error.message)+'</td></tr>';
                }
            };
            // 3. NAVIGATION ENTRE LES SOUS-ONGLETS
            window.switchDmeSubTab = function(subTabId) {
                const permittedTabs = dmeVisibleTabs.filter(id => id !== 'croissance' || refreshDmeGrowthAvailability(window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey)));
                if (!permittedTabs.includes(subTabId)) subTabId = permittedTabs[0] || null;
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (subTabId === 'croissance' && currentTab && !refreshDmeGrowthAvailability(currentTab)) {
                    window.switchDmeSubTab('consultation');
                    return;
                }
                if (currentTab) currentTab.currentSubTab = subTabId;

                document.querySelectorAll('.dme-subtab-panel').forEach(p => p.classList.add('hidden'));

                document.querySelectorAll('.dme-subtab-btn').forEach(btn => {
                    btn.className = "dme-subtab-btn flex items-center gap-2 px-4 py-2.5 rounded-2xl text-xs font-bold transition cursor-pointer bg-white dark:bg-[#121c15] text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white border border-slate-200 dark:border-white/10";
                });

                const targetPanel = document.getElementById(`dme-panel-${subTabId}`);
                if (targetPanel) targetPanel.classList.remove('hidden');
                document.getElementById('dme-panel-croissance')?.classList.toggle('dme-growth-active', subTabId === 'croissance');

                const targetBtn = document.getElementById(`subtab-btn-${subTabId}`);
                if (targetBtn) {
                    targetBtn.className = "dme-subtab-btn flex items-center gap-2 px-4 py-2.5 rounded-2xl text-xs font-bold transition cursor-pointer bg-brand-neon text-black shadow-md shadow-brand-neon/20";
                }
                refreshDmeGrowthAvailability(currentTab);

                if (subTabId === 'ordonnance' && currentTab) {
                    loadMedsForPrescription();
                    renderActiveOrdLines();
                    loadPatientPrescriptionsHistory(currentTab.patientId, currentTab.passageId, currentTab.consultationId || currentTab.data?.dme?.id || 0);
                } else if (subTabId === 'bilans' && currentTab) {
                    window.loadDmeExamRequests();
                } else if (subTabId === 'consommations' && currentTab) {
                    loadDmeConsumptions();
                } else if (subTabId === 'echographie' && currentTab) {
                    renderEchoGallery();
                } else if (subTabId === 'croissance' && currentTab) {
                    window.renderDmeGrowthChart();
                }
                if (currentTab) applyDmeAccessMode(currentTab);
            };

            let dmeGrowthReferencePromise = null;
            function growthAgeMonths(birthIso, visitIso) {
                if (!birthIso || !visitIso) return null;
                const normalize = value => {
                    const raw = String(value).trim();
                    const iso = raw.match(/^(\\d{4})-(\\d{2})-(\\d{2})/);
                    if (iso) return `${iso[1]}-${iso[2]}-${iso[3]}T12:00:00`;
                    const fr = raw.match(/^(\\d{2})\\/(\\d{2})\\/(\\d{4})$/);
                    return fr ? `${fr[3]}-${fr[2]}-${fr[1]}T12:00:00` : raw;
                };
                const birth = new Date(normalize(birthIso)), visit = new Date(normalize(visitIso));
                if (Number.isNaN(birth.getTime()) || Number.isNaN(visit.getTime()) || visit < birth) return null;
                return (visit - birth) / (1000 * 60 * 60 * 24 * 30.4375);
            }
            function refreshDmeGrowthAvailability(tab) {
                const activeTab = tab || window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const patient = activeTab?.data?.patient || {};
                const dob = patient.date_naissance_iso || patient.date_naissance;
                const age = dob ? growthAgeMonths(dob, activeTab?.data?.passage?.date_passage_iso || new Date().toISOString()) : null;
                // En cas de date absente ou non reconnue, laisser l’onglet accessible au lieu de le masquer.
                const pediatricOrUnknown = age === null || age <= 228;
                document.getElementById('subtab-btn-croissance')?.classList.toggle('hidden', !pediatricOrUnknown);
                return pediatricOrUnknown;
            }
            function dmeGrowthSex(patient) {
                const sex = String(patient?.genre || '').trim().toLowerCase();
                if (['f', 'femme', 'female', 'féminin', 'feminin'].includes(sex)) return 'F';
                if (['m', 'homme', 'masculin', 'male'].includes(sex)) return 'M';
                return null;
            }
            window.renderDmeGrowthChart = async function() {
                const tab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!tab?.data) return;
                const p = tab.data.patient || {}, passage = tab.data.passage || {}, c = tab.data.dme?.constantes || {};
                const birthIso = p.date_naissance_iso || p.date_naissance;
                const currentAge = growthAgeMonths(birthIso, passage.date_passage_iso || new Date().toISOString());
                if (currentAge === null) {
                    const notice = document.getElementById('dmeGrowthNotice');
                    if (notice) { notice.textContent = 'La date de naissance ou la date du passage ne permet pas de calculer l’âge. Vérifiez la fiche patient.'; notice.classList.remove('hidden'); }
                    return;
                }
                if (currentAge > 228) return;

                const ageYears = Math.floor(currentAge / 12), ageMonths = Math.floor(currentAge % 12);
                const meta = document.getElementById('dmeGrowthPatientMeta');
                if (meta) meta.textContent = `${p.genre || 'Sexe non renseigné'} · ${ageYears} an(s) ${ageMonths} mois · références OMS par âge et sexe`;
                const sex = dmeGrowthSex(p), indicatorKey = document.getElementById('dmeGrowthIndicator')?.value || 'height_age';
                const chart = document.getElementById('dmeGrowthChart'), notice = document.getElementById('dmeGrowthNotice');
                if (!chart) return;
                if (!dmeGrowthReferencePromise) dmeGrowthReferencePromise = fetch('/api/dme/growth-reference').then(r => { if (!r.ok) throw new Error('Références OMS indisponibles'); return r.json(); });
                let reference;
                try { reference = await dmeGrowthReferencePromise; }
                catch (e) { if (notice) { notice.textContent = 'Les références OMS ne sont pas disponibles. Vérifiez que resources/who_growth/growth_reference.json est présent.'; notice.classList.remove('hidden'); } return; }
                if (tab.key !== window.currentActiveDmeKey) return;
                const indicator = reference.indicators?.[indicatorKey];
                if (!sex || !indicator?.[sex]?.length) {
                    if (notice) { notice.textContent = !sex ? 'Renseignez le sexe dans la fiche patient pour afficher la référence OMS correspondante.' : 'Aucune référence OMS disponible pour cet indicateur.'; notice.classList.remove('hidden'); }
                    chart.innerHTML = ''; return;
                }
                const minAge = Number(indicator.min_month || 0), maxAge = Number(indicator.max_month || 228);
                if (notice) {
                    const outside = currentAge < minAge || currentAge > maxAge;
                    notice.textContent = outside ? `La référence « ${indicator.label} » s’applique de ${Math.floor(minAge / 12)} à ${Math.floor(maxAge / 12)} ans. L’âge actuel est hors de cette plage.` : '';
                    notice.classList.toggle('hidden', !outside);
                }
                const valueKey = {height_age:'taille', weight_age:'poids', bmi_age:'imc', head_age:'perimetre_cranien', arm_age:'perimetre_brachial'}[indicatorKey];
                const history = (tab.data.historique || []).map(item => {
                    const values = item.constantes || {};
                    let value = Number(values[valueKey]);
                    if (indicatorKey === 'bmi_age' && !value && Number(values.poids) > 0 && Number(values.taille) > 0) value = Number(values.poids) / ((Number(values.taille) / 100) ** 2);
                    return {x:growthAgeMonths(birthIso, item.date_passage_iso), y:value, label:item.date_passage || ''};
                }).filter(point => Number.isFinite(point.x) && Number.isFinite(point.y) && point.y > 0 && point.x >= minAge && point.x <= maxAge);
                if (!tab.data.dme?.id || tab.data.dme?.verrouille === false) {
                    const inputValue = Number(document.getElementById(({height_age:'dme_growth_height',weight_age:'dme_growth_weight',bmi_age:'dme_imc',head_age:'dme_growth_head',arm_age:'dme_growth_arm'})[indicatorKey])?.value || 0);
                    let currentValue = inputValue;
                    if (indicatorKey === 'bmi_age' && !currentValue) {
                        const w = Number(document.getElementById('dme_growth_weight')?.value || 0), h = Number(document.getElementById('dme_growth_height')?.value || 0);
                        if (w > 0 && h > 0) currentValue = w / ((h / 100) ** 2);
                    }
                    if (currentAge >= minAge && currentAge <= maxAge && currentValue > 0) history.push({x:currentAge,y:currentValue,label:'Consultation en cours'});
                }
                const rows = indicator[sex].filter(row => row[0] >= minAge && row[0] <= maxAge);
                const measureValues = history.map(point => point.y);
                const curveValues = rows.flatMap(row => row.slice(1));
                const yMin = Math.min(...curveValues, ...measureValues), yMax = Math.max(...curveValues, ...measureValues);
                const xMin = minAge, xMax = maxAge, W = 900, H = 330, L = 58, R = 18, T = 18, B = 42;
                const sx = x => L + ((x-xMin)/(xMax-xMin || 1))*(W-L-R);
                const sy = y => H-B-((y-yMin)/(yMax-yMin || 1))*(H-T-B);
                const colors = ['#dc2626','#f97316','#f59e0b','#059669','#84cc16','#0ea5e9','#2563eb'];
                let svg = `<svg viewBox="0 0 ${W} ${H}" class="w-full min-w-[680px]" role="img" aria-label="${indicator.label}, courbes OMS et mesures du patient">`;
                for (let i=0;i<=4;i++) { const yy=T+i*(H-T-B)/4, value=yMax-i*(yMax-yMin)/4; svg+=`<line x1="${L}" y1="${yy}" x2="${W-R}" y2="${yy}" stroke="#cbd5e1" stroke-dasharray="3 5"/><text x="${L-8}" y="${yy+4}" text-anchor="end" font-size="10" fill="#64748b">${value.toFixed(1)}</text>`; }
                for (let i=0;i<=6;i++) { const x=xMin+i*(xMax-xMin)/6, xx=sx(x), years=(x/12).toFixed(0); svg+=`<line x1="${xx}" y1="${T}" x2="${xx}" y2="${H-B}" stroke="#e2e8f0"/><text x="${xx}" y="${H-14}" text-anchor="middle" font-size="10" fill="#64748b">${years} an${years==='1'?'':'s'}</text>`; }
                for(let z=0;z<7;z++){const points=rows.map(row=>`${sx(row[0]).toFixed(1)},${sy(row[z+1]).toFixed(1)}`).join(' ');svg+=`<polyline fill="none" stroke="${colors[z]}" stroke-width="${z===3?2.5:1.5}" opacity="${z===3?1:.78}" points="${points}"/>`;}
                if(history.length){ const points=history.sort((a,b)=>a.x-b.x).map(point=>`${sx(point.x).toFixed(1)},${sy(point.y).toFixed(1)}`).join(' '); if(history.length>1) svg+=`<polyline fill="none" stroke="#111827" stroke-width="2.5" points="${points}"/>`; history.forEach(point=>{svg+=`<circle cx="${sx(point.x)}" cy="${sy(point.y)}" r="5" fill="#111827"><title>${point.label}: ${point.y.toFixed(1)} ${indicator.unit}</title></circle>`;}); }
                svg+=`<text x="${(L+W-R)/2}" y="${H-1}" text-anchor="middle" font-size="11" fill="#475569">Âge</text><text transform="translate(14 ${(T+H-B)/2}) rotate(-90)" text-anchor="middle" font-size="11" fill="#475569">${indicator.label} (${indicator.unit})</text></svg>`;
                chart.innerHTML=svg;
                const legend=document.getElementById('dmeGrowthLegend');
                if(legend) legend.innerHTML=reference.z_levels.map((z,i)=>`<span class="inline-flex items-center gap-1"><i class="w-3 h-0.5" style="background:${colors[i]}"></i>${z==='0'?'Médiane':`${z} ET`}</span>`).join('')+'<span class="inline-flex items-center gap-1"><i class="w-3 h-0.5 bg-slate-900"></i>Mesures du patient</span>';
            };
            window.openDmeGrowthPrintView = function() {
                const tab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const chart = document.getElementById('dmeGrowthChart')?.querySelector('svg');
                if (!tab?.data || !chart) { alert('La courbe de croissance n’est pas encore prête à être imprimée.'); return; }
                const patient = tab.data.patient || {};
                const indicator = document.getElementById('dmeGrowthIndicator')?.selectedOptions?.[0]?.textContent || 'Courbe de croissance';
                const modal = document.getElementById('dmeGrowthPrintModal');
                if (!modal) return;
                document.getElementById('dmeGrowthPrintTitle').textContent = indicator;
                document.getElementById('dmeGrowthPrintPatient').textContent = `Patient : ${patient.nom || ''} ${patient.prenom || ''}`.trim();
                document.getElementById('dmeGrowthPrintBirth').textContent = `Date de naissance : ${patient.date_naissance || 'Non renseignée'} · Sexe : ${patient.genre || 'Non renseigné'}`;
                document.getElementById('dmeGrowthPrintDate').textContent = `Généré le ${new Date().toLocaleDateString('fr-FR')}`;
                document.getElementById('dmeGrowthPrintGraph').innerHTML = chart.outerHTML;
                document.getElementById('dmeGrowthPrintLegend').innerHTML = document.getElementById('dmeGrowthLegend')?.innerHTML || '';
                modal.classList.remove('hidden');
                modal.classList.add('flex');
            };
            window.closeDmeGrowthPrintView = function() {
                const modal = document.getElementById('dmeGrowthPrintModal');
                if (modal) { modal.classList.add('hidden'); modal.classList.remove('flex'); }
            };
            window.printDmeGrowthPdf = function() {
                const printTitle = document.getElementById('dmeGrowthPrintTitle')?.textContent || 'Courbe de croissance';
                const previousTitle = document.title;
                document.title = `${printTitle} - Cliniva`;
                window.onafterprint = function() { document.title = previousTitle; window.onafterprint = null; };
                window.print();
            };

            const DME_SPECIALTY_CHOICES = [
                {key:'reeducation_classique', label:'Rééducation classique', match:/reeducation classique/},
                {key:'physiotherapie', label:'Bilan de rééducation / physiothérapie', match:/physiotherap|bilan kine/},
                {key:'gastro', label:'Gastro-entérologie', match:/gastro|hepat|digest/},
                {key:'anesthesie', label:'Anesthésie', match:/anesth|reanim/},
                {key:'dermatologie', label:'Dermatologie', match:/dermat/},
                {key:'gynecologie', label:'Gynécologie', match:/gynec|obstet/},
                {key:'general', label:'Médecine générale', match:/medecine generale|generaliste/},
                {key:'medecine_physique', label:'Médecine physique et réadaptation', match:/medecine physique|readaptation|reeducation fonctionnelle/},
                {key:'medecine_sport', label:'Médecine du sport', match:/medecine du sport|medecine sportive|traumatologie du sport/}
            ];
            const DME_SPECIALTY_FIELD_LABELS = {
                symptomes_digestifs:'Symptômes digestifs, début, durée et évolution', transit_selles:'Transit et selles', symptomes_associes:'Symptômes associés et contexte', signes_alerte_digestifs:'Signes d’alerte recherchés', explorations_digestives:'Antécédents et explorations digestives', examen_abdominal:'Examen abdominal', plan_digestif:'Évaluation et plan digestif',
                intervention_prevue:'Intervention prévue, date et indication', antecedents_anesthesiques:'Antécédents anesthésiques', voies_aeriennes:'Voies aériennes et dentition', evaluation_cardiorespiratoire:'Évaluation cardio-respiratoire', capacite_fonctionnelle:'Capacité fonctionnelle et éléments de risque', classe_asa:'Classe ASA', examens_preoperatoires:'Examens préopératoires et consultations', consentement_anesthesique:'Consentement et information', plan_anesthesique:'Plan anesthésique et consignes',
                lesion_localisation:'Localisation de la lésion', lesion_evolution:'Apparition et évolution', lesion_description:'Description clinique', lesion_symptomes:'Symptômes et facteurs contextuels', examen_cutane:'Étendue de l’examen cutané', dermoscopie_histologie:'Antécédents, dermoscopie et histologie', photos_cliniques:'Photos cliniques / dermoscopie', plan_dermatologie:'Évaluation et suivi',
                dernieres_regles:'Dernières règles', cycles_menstruels:'Cycles et symptômes menstruels', antecedents_grossesse:'Antécédents de grossesse et d’accouchement', contraception:'Contraception et contexte hormonal', depistage_col:'Dépistage du col et antécédents gynécologiques', symptomes_gynecologiques:'Symptômes gynécologiques et ménopause', examen_gynecologique:'Examen gynécologique', plan_gynecologique:'Évaluation, plan et suivi',
                antecedents_familiaux:'Antécédents familiaux', revue_systemes:'Revue des symptômes par appareil', autonomie_fonctionnelle:'Autonomie et capacité fonctionnelle', prevention_depistage:'Prévention et dépistages', vaccinations:'Vaccinations', suivi_orientation:'Suivi et orientation',
                motif_reeducation:'Motif de rééducation et objectifs du patient', histoire_fonctionnelle:'Histoire fonctionnelle et évolution', douleur_reeducation:'Douleur', limitations_activites:'Limitations dans les activités quotidiennes', autonomie_deplacements:'Autonomie, transferts et déplacements', examen_articulaire_moteur:'Examen articulaire et moteur', examen_neurologique_fonctionnel:'Examen neurologique fonctionnel', equilibre_marche:'Équilibre et marche', aides_techniques:'Aides techniques, orthèses et appareillages', evaluations_fonctionnelles:'Échelles et mesures fonctionnelles', objectifs_reeducation:'Objectifs de réadaptation', plans_reeducation_json:'Plan de rééducation', type_reeducation:'Type de rééducation', seances_par_semaine:'Séances par semaine', nombre_total_seances:'Nombre total de séances', duree_seance_minutes:'Durée d’une séance (min)', kine_staff_id:'Kinésithérapeute responsable', kine_assigne_label:'Kinésithérapeute responsable', programme_reeducation:'Programme, précautions et consignes', suivi_reeducation:'Suivi et évolution',
                sport_pratique:'Sport pratiqué et rôle / poste', niveau_pratique_charge:'Niveau et charge d’entraînement', contexte_blessure_sport:'Contexte de la blessure ou du symptôme', antecedents_sportifs:'Antécédents sportifs et blessures', douleur_sport:'Douleur et symptômes à l’effort', examen_musculosquelettique_sport:'Examen musculosquelettique', tests_fonctionnels_sport:'Tests fonctionnels / de performance réalisés', aptitude_restrictions_sport:'Aptitude, restrictions et consignes sportives', plan_reprise_sport:'Plan de prise en charge et reprise', suivi_sport:'Suivi sportif'
            };


            function refreshPhysiotherapyMap() {
                const selected = (document.getElementById('physio-zones')?.value || '').split(';').map(v=>v.trim());
                document.querySelectorAll('[data-physio-zone]').forEach(zone=>zone.setAttribute('aria-pressed', String(selected.includes(zone.dataset.physioZone))));
            }
            function togglePhysiotherapyZone(zone) {
                const tab = window.activeDmeTabs.find(t=>t.key===window.currentActiveDmeKey);
                const input = document.getElementById('physio-zones');
                if (!input || input.disabled || tab?.mode === 'lecture') return;
                const selected = input.value.split(';').map(v=>v.trim()).filter(Boolean);
                const value=zone.dataset.physioZone, index=selected.indexOf(value);
                if(index>=0)selected.splice(index,1);else selected.push(value);
                input.value=selected.join('; '); input.dispatchEvent(new Event('input',{bubbles:true}));
                refreshPhysiotherapyMap();
            }
            document.querySelectorAll('[data-physio-zone]').forEach(zone=>{
                zone.addEventListener('click',()=>togglePhysiotherapyZone(zone));
                zone.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();togglePhysiotherapyZone(zone);}});
            });
            document.getElementById('physio-zones')?.addEventListener('input',refreshPhysiotherapyMap);
            window.printPhysiotherapyBilan = async function() {
                const tab=window.activeDmeTabs.find(t=>t.key===window.currentActiveDmeKey);
                if(!tab)return;
                const values={};
                document.querySelectorAll('[data-dme-specialty="physiotherapie"][data-dme-specialty-field]').forEach(field=>values[field.dataset.dmeSpecialtyField]=field.value);
                try {
                    const response=await fetch('/api/dme/rehabilitation/bilan-pdf',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({patient_id:tab.patientId,passage_id:tab.passageId,values})});
                    if(!response.ok){const error=await response.json().catch(()=>({}));throw new Error(error.detail||'Génération PDF impossible');}
                    const blob=await response.blob();
                    if(!blob.type.includes('pdf'))throw new Error('Réponse PDF invalide');
                    if(activeRehabPlanPdfUrl)URL.revokeObjectURL(activeRehabPlanPdfUrl);
                    activeRehabPlanPdfUrl=URL.createObjectURL(blob);
                    activeLivretDraftToken=null;setDmeCrPreviewMode('archived');
                    document.getElementById('dmeCrModalTitle').innerText='Bilan de rééducation / physiothérapie';
                    const modal=portalDmeModal('dmeCrPreviewModal');
                    document.getElementById('dmeCrIframe').src=activeRehabPlanPdfUrl;
                    modal.classList.remove('hidden');modal.classList.add('flex');document.body.style.overflow='hidden';
                }catch(error){alert(error.message);}
            };

            let dmeRehabilitationTherapistsCache = null;
            let dmeReeducationTypesCache = null;
            const escapeRehabHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
            function getDmeReeducationRows(tab = window.activeDmeTabs?.find(item => item.key === window.currentActiveDmeKey)) {
                const hidden = document.querySelector('[data-dme-specialty-field="plans_reeducation_json"]');
                try {
                    const parsed = JSON.parse(hidden?.value || '[]');
                    if (Array.isArray(parsed)) return parsed;
                } catch (_) {}
                const saved = tab?.data?.dme?.donnees_specialisees?.sections?.medecine_physique || {};
                if (saved.type_reeducation) return [{
                    type: saved.type_reeducation, seances_par_semaine: saved.seances_par_semaine || '',
                    nombre_total_seances: saved.nombre_total_seances || '', duree_seance_minutes: saved.duree_seance_minutes || '',
                    kine_id: saved.kine_staff_id || 'any', kine: saved.kine_assigne_label || "N’importe quelle kiné",
                }];
                return [];
            }
            window.syncDmeReeducationPlan = function() {
                const hidden = document.querySelector('[data-dme-specialty-field="plans_reeducation_json"]');
                const body = document.getElementById('dmeReeducationPlanRows');
                if (!hidden || !body) return;
                const rows = [...body.querySelectorAll('tr[data-plan-row]')].map(row => {
                    const field = name => row.querySelector(`[data-plan-field="${name}"]`);
                    const therapist = field('kine_id');
                    return {
                        type: field('type')?.value || '',
                        seances_par_semaine: field('seances_par_semaine')?.value || '',
                        nombre_total_seances: field('nombre_total_seances')?.value || '',
                        duree_seance_minutes: field('duree_seance_minutes')?.value || '',
                        kine_id: therapist?.value || 'any',
                        kine: therapist?.selectedOptions?.[0]?.dataset?.displayName || "N’importe quelle kiné",
                    };
                });
                hidden.value = JSON.stringify(rows);
            };
            window.renderDmeReeducationPlanRows = function(rows = getDmeReeducationRows()) {
                const body = document.getElementById('dmeReeducationPlanRows');
                if (!body) return;
                const typeOptions = dmeReeducationTypesCache || [];
                const therapists = dmeRehabilitationTherapistsCache || [];
                if (!rows.length) {
                    body.innerHTML = `<tr><td colspan="6" class="p-4 text-center text-slate-400">Aucune ligne. Cliquez sur « Ajouter une ligne » pour commencer le plan.</td></tr>`;
                    window.syncDmeReeducationPlan();
                    return;
                }
                body.innerHTML = rows.map((item, index) => {
                    const typeOptionsHtml = `<option value="">Choisir une rééducation</option>${typeOptions.map(type => `<option value="${escapeRehabHtml(type)}" ${String(item.type || '') === type ? 'selected' : ''}>${escapeRehabHtml(type)}</option>`).join('')}`;
                    const savedKineId = String(item.kine_id || 'any');
                    const savedKine = item.kine || "N’importe quelle kiné";
                    const therapistOptions = [`<option value="any" data-display-name="N’importe quelle kiné" ${savedKineId === 'any' ? 'selected' : ''}>N’importe quelle kiné</option>`, ...therapists.map(person => {
                        const name = `${person.nom || ''} ${person.prenom || ''}`.trim() || `Kiné #${person.id}`;
                        const extra = person.service_nom ? ` · ${person.service_nom}` : '';
                        return `<option value="${escapeRehabHtml(person.id)}" data-display-name="${escapeRehabHtml(name)}" ${String(person.id) === savedKineId ? 'selected' : ''}>${escapeRehabHtml(name + extra)}</option>`;
                    })];
                    if (savedKineId !== 'any' && !therapists.some(person => String(person.id) === savedKineId)) {
                        therapistOptions.push(`<option value="${escapeRehabHtml(savedKineId)}" data-display-name="${escapeRehabHtml(savedKine)}" selected>${escapeRehabHtml(savedKine)} · affectation enregistrée</option>`);
                    }
                    return `<tr data-plan-row="${index}" class="align-top">
                        <td class="min-w-56 p-2"><select data-plan-field="type" onchange="window.syncDmeReeducationPlan()" class="w-full rounded-lg border border-slate-200 bg-slate-50 px-2 py-2 text-[10px] font-semibold text-slate-700 dark:border-white/10 dark:bg-black/30 dark:text-white">${typeOptionsHtml}</select></td>
                        <td class="w-32 p-2"><input data-plan-field="seances_par_semaine" type="number" min="1" max="99" step="1" value="${escapeRehabHtml(item.seances_par_semaine || '')}" placeholder="Ex. 3" oninput="window.syncDmeReeducationPlan()" class="w-full rounded-lg border border-slate-200 bg-slate-50 px-2 py-2 text-[10px] dark:border-white/10 dark:bg-black/30 dark:text-white"></td>
                        <td class="w-28 p-2"><input data-plan-field="nombre_total_seances" type="number" min="1" max="999" step="1" value="${escapeRehabHtml(item.nombre_total_seances || '')}" placeholder="Ex. 12" oninput="window.syncDmeReeducationPlan()" class="w-full rounded-lg border border-slate-200 bg-slate-50 px-2 py-2 text-[10px] dark:border-white/10 dark:bg-black/30 dark:text-white"></td>
                        <td class="w-28 p-2"><input data-plan-field="duree_seance_minutes" type="number" min="1" max="600" step="5" value="${escapeRehabHtml(item.duree_seance_minutes || '')}" placeholder="Ex. 45" oninput="window.syncDmeReeducationPlan()" class="w-full rounded-lg border border-slate-200 bg-slate-50 px-2 py-2 text-[10px] dark:border-white/10 dark:bg-black/30 dark:text-white"></td>
                        <td class="min-w-52 p-2"><select data-plan-field="kine_id" onchange="window.syncDmeReeducationPlan()" class="w-full rounded-lg border border-slate-200 bg-slate-50 px-2 py-2 text-[10px] font-semibold text-slate-700 dark:border-white/10 dark:bg-black/30 dark:text-white">${therapistOptions.join('')}</select></td>
                        <td class="p-2 text-center"><button type="button" onclick="window.removeDmeReeducationPlanRow(${index})" aria-label="Supprimer cette ligne" class="rounded-lg p-2 text-rose-600 hover:bg-rose-50 dark:hover:bg-rose-500/10"><i class="ph-bold ph-trash"></i></button></td>
                    </tr>`;
                }).join('');
                window.syncDmeReeducationPlan();
            };
            window.addDmeReeducationPlanRow = function() {
                const rows = getDmeReeducationRows();
                rows.push({type:'', seances_par_semaine:'', nombre_total_seances:'', duree_seance_minutes:'', kine_id:'any', kine:"N’importe quelle kiné"});
                window.renderDmeReeducationPlanRows(rows);
            };
            window.removeDmeReeducationPlanRow = function(index) {
                const rows = getDmeReeducationRows();
                rows.splice(index, 1);
                window.renderDmeReeducationPlanRows(rows);
            };
            window.loadDmeRehabilitationTherapists = async function(tab) {
                const body = document.getElementById('dmeReeducationPlanRows');
                if (!body) return;
                try {
                    const [therapistResponse, typeResponse] = await Promise.all([
                        fetch('/api/dme/rehabilitation/kinesitherapeutes', {cache:'no-store'}),
                        fetch('/api/dme/rehabilitation/types', {cache:'no-store'})
                    ]);
                    const [therapistResult, typeResult] = await Promise.all([
                        therapistResponse.json(), typeResponse.json()
                    ]);
                    if (!therapistResponse.ok || !therapistResult.success) throw new Error(therapistResult.detail || 'Liste des kinésithérapeutes indisponible.');
                    if (!typeResponse.ok || !typeResult.success) throw new Error(typeResult.detail || 'Types de rééducation indisponibles.');
                    dmeRehabilitationTherapistsCache = Array.isArray(therapistResult.therapists) ? therapistResult.therapists : [];
                    dmeReeducationTypesCache = Array.isArray(typeResult.types) ? typeResult.types : [];
                    const hint = document.getElementById('dmeRehabilitationTherapistHint');
                    if (hint) hint.textContent = dmeReeducationTypesCache.length
                        ? `${dmeRehabilitationTherapistsCache.length} kiné(s) actif(s) · choix repérés dans les catégories, types et spécialités liés à la rééducation.`
                        : 'Aucun type de rééducation n’a été trouvé dans les catégories, types ou spécialités du catalogue.';
                    window.renderDmeReeducationPlanRows(getDmeReeducationRows(tab));
                } catch (error) {
                    body.innerHTML = `<tr><td colspan="6" class="p-4 text-center text-rose-600">${escapeRehabHtml(error.message || 'Impossible de charger le plan de rééducation.')}</td></tr>`;
                }
            };
            let activeRehabPlanPdfUrl = null;
            window.previewDmeReeducationPlan = async function() {
                const tab = window.activeDmeTabs?.find(item => item.key === window.currentActiveDmeKey);
                if (!tab?.patientId || !tab?.passageId) { alert('Aucun patient actif dans le DME.'); return; }
                window.syncDmeReeducationPlan();
                const plans = getDmeReeducationRows(tab).filter(row => String(row.type || '').trim());
                if (!plans.length) { alert('Ajoutez au moins une ligne et sélectionnez un type de rééducation.'); return; }
                const incomplete = plans.some(row => !Number(row.seances_par_semaine) || !Number(row.nombre_total_seances));
                if (incomplete) { alert('Renseignez les séances par semaine et le nombre total de séances pour chaque ligne.'); return; }
                try {
                    const section = tab.data?.dme?.donnees_specialisees?.sections?.medecine_physique || {};
                    const objectifs = document.querySelector('[data-dme-specialty-field="objectifs_reeducation"]')?.value || section.objectifs_reeducation || '';
                    const consignes = document.querySelector('[data-dme-specialty-field="programme_reeducation"]')?.value || section.programme_reeducation || '';
                    const response = await fetch('/api/dme/rehabilitation/plan-pdf', {
                        method:'POST', headers:{'Content-Type':'application/json'},
                        body:JSON.stringify({patient_id:tab.patientId, passage_id:tab.passageId, plans, objectifs, consignes})
                    });
                    if (!response.ok) {
                        let detail = 'Impossible de générer le PDF du plan de rééducation.';
                        try { const error = await response.json(); detail = error.detail || detail; } catch (_) {}
                        throw new Error(detail);
                    }
                    const blob = await response.blob();
                    if (!blob.size || !blob.type.toLowerCase().includes('pdf')) throw new Error('Le serveur n’a pas renvoyé un PDF valide.');
                    if (activeRehabPlanPdfUrl) URL.revokeObjectURL(activeRehabPlanPdfUrl);
                    activeRehabPlanPdfUrl = URL.createObjectURL(blob);
                    activeLivretDraftToken = null;
                    setDmeCrPreviewMode('archived');
                    const title = document.getElementById('dmeCrModalTitle');
                    if (title) title.innerText = 'Aperçu du plan de rééducation';
                    const modal = portalDmeModal('dmeCrPreviewModal');
                    const iframe = document.getElementById('dmeCrIframe');
                    if (iframe) iframe.src = activeRehabPlanPdfUrl;
                    if (modal) { modal.classList.remove('hidden'); modal.classList.add('flex'); document.body.style.overflow = 'hidden'; }
                } catch (error) {
                    alert(error.message || 'Erreur lors de la génération du plan PDF.');
                }
            };

            window.openDmeSpecialtyPicker = function() {
                const modal = portalDmeModal('dmeSpecialtyPickerModal');
                if (modal) { modal.classList.remove('hidden'); modal.classList.add('flex'); document.body.style.overflow = 'hidden'; }
            };
            window.closeDmeSpecialtyPicker = function() {
                const modal = document.getElementById('dmeSpecialtyPickerModal');
                if (modal) { modal.classList.add('hidden'); modal.classList.remove('flex'); }
                if (!document.querySelector('#dmeDicomPrintModal:not(.hidden), #echoManagerModal:not(.hidden), #echoCarouselModal:not(.hidden)')) document.body.style.overflow = '';
            };
            window.updateDmeSpecialtySection = function(specialtyKey) {
                const panel = document.querySelector(`[data-dme-specialty-panel="${specialtyKey}"]`);
                if (panel) { panel.classList.remove('hidden'); panel.style.display = 'grid'; }
            };
            window.addDmeSpecialtySection = async function() {
                const specialtyKey = document.getElementById('dmeSpecialtyAddSelect')?.value || '';
                const currentTab = window.activeDmeTabs.find(tab => tab.key === window.currentActiveDmeKey);
                if (!currentTab || currentTab.mode === 'lecture') {
                    alert('Cette consultation est en lecture seule. Les champs spécialisés ne peuvent pas être ajoutés.');
                    return;
                }
                window.updateDmeSpecialtySection(specialtyKey);
                window.closeDmeSpecialtyPicker();
                document.querySelector(`[data-dme-specialty-panel="${specialtyKey}"]`)?.scrollIntoView({behavior:'smooth', block:'center'});
                const visibleSections = [...document.querySelectorAll('[data-dme-specialty-panel]')]
                    .filter(panel => panel.style.display === 'grid')
                    .map(panel => panel.dataset.dmeSpecialtyPanel);
                try {
                    const response = await fetch('/api/dme/save', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            id_patient: currentTab.patientId,
                            id_passage: currentTab.passageId,
                            sections: visibleSections,
                            sections_only: true
                        })
                    });
                    const result = await response.json().catch(() => ({}));
                    if (!response.ok || !result.success) throw new Error(result.detail || result.message || 'Impossible de mémoriser les sections spécialisées.');
                    currentTab.data.dme = currentTab.data.dme || {};
                    currentTab.data.dme.donnees_specialisees = result.donnees_specialisees;
                } catch (error) {
                    alert(error.message || 'Erreur lors de l’enregistrement des sections spécialisées.');
                }
            };

            function initializeDmeSpecialtyFields(tab) {
                if (!tab) return;
                const saved = tab.data?.dme?.donnees_specialisees || {};
                const savedSections = saved.sections && typeof saved.sections === 'object' ? saved.sections : {};
                document.querySelectorAll('[data-dme-specialty-panel]').forEach(panel => { panel.classList.add('hidden'); panel.style.display = 'none'; });
                document.querySelectorAll('[data-dme-specialty-field]').forEach(field => {
                    field.value = savedSections[field.dataset.dmeSpecialty]?.[field.dataset.dmeSpecialtyField] || '';
                });
                const savedKeys = Object.keys(savedSections);
                if (saved.specialite && DME_SPECIALTY_CHOICES.some(choice => choice.key === saved.specialite)) savedKeys.push(saved.specialite);
                [...new Set(savedKeys)].forEach(window.updateDmeSpecialtySection);
                refreshPhysiotherapyMap();
                refreshClassicReeducation();
            }

            function collectDmeSpecialtyFields() {
                const sections = {};
                document.querySelectorAll('[data-dme-specialty-field]').forEach(field => {
                    if (field.closest('[data-dme-specialty-panel]')?.style.display !== 'grid') return;
                    const specialty = field.dataset.dmeSpecialty;
                    sections[specialty] ||= {};
                    sections[specialty][field.dataset.dmeSpecialtyField] = field.value || '';
                });
                const visible = [...document.querySelectorAll('[data-dme-specialty-panel]')].filter(panel => panel.style.display === 'grid').map(panel => panel.dataset.dmeSpecialtyPanel);
                return {specialite: visible[0] || '', sections};
            }

            // 4. INJECTION DANS LE DOM
            function displayActivePatientDme() {
                const container = document.getElementById('dmePatientContainer');
                const placeholder = document.getElementById('dmeEmptyPlaceholder');

                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab || !currentTab.data) {
                    if (container) container.classList.add('hidden');
                    if (placeholder) placeholder.classList.remove('hidden');
                    return;
                }

                if (placeholder) placeholder.classList.add('hidden');
                if (container) container.classList.remove('hidden');

                const p = currentTab.data.patient || {};
                const pass = currentTab.data.passage || {};
                const dme = currentTab.data.dme || {};
                const c = dme.constantes || {};
                initializeDmeSpecialtyFields(currentTab);
                window.loadDmeRehabilitationTherapists(currentTab);
                const histo = currentTab.data.historique || [];
                const crTemplates = currentTab.data.cr_templates || [];
                const radiologues = currentTab.data.radiologues || [];
                const archivedCrs = currentTab.data.archived_crs || [];

                // En-tête patient
                document.getElementById('dme_hdr_avatar').src = p.photo_face || p.avatar || '/static/images/avatar-placeholder.svg?w=120';
                document.getElementById('dme_hdr_fullname').innerText = `${p.nom || 'Patient'} ${p.prenom || ''}`;
                document.getElementById('dme_hdr_blood').innerText = p.groupe_sanguin || 'O+';
                document.getElementById('dme_hdr_dossier').innerText = `#DOS-${String(p.id || 0).padStart(4, '0')}`;
                document.getElementById('dme_hdr_dob').innerText = p.date_naissance || '-';
                document.getElementById('dme_hdr_tel').innerText = p.telephone_1 || '-';
                document.getElementById('dme_hdr_location').innerText = `${p.commune || ''} (${p.wilaya || ''})`;
                document.getElementById('dme_hdr_medecin').innerText = pass.medecin_nom ? `${pass.medecin_grade || 'Dr.'} ${pass.medecin_nom}` : 'Aucun';
                const handoffBanner = document.getElementById('dmeHandoffNoteBanner');
                const handoffNote = String(dme.note_reprise || '').trim();
                if (handoffBanner) {
                    handoffBanner.classList.toggle('hidden', !handoffNote);
                    document.getElementById('dmeHandoffNoteText').textContent = handoffNote;
                    const doctorText = String(dme.note_medecin || '').trim();
                    document.getElementById('dmeHandoffDoctorText').textContent = doctorText ? `Avis demandé à : ${doctorText}` : '';
                }
                renderDmePassagePrestations(Array.isArray(pass.actes) ? pass.actes : [], pass.type_passage);

                const questionnaire = pass.questionnaire || {};
                const observation = questionnaire.observation_medicale || questionnaire.observation || '';
                document.getElementById('dme_passage_observation').innerText = observation || 'Aucune observation médicale saisie pour ce passage.';
                const clinicalLabels = {
                    hospitalise: 'Hospitalisé', dossier_interne: 'Dossier interne', dossier_externe: 'Dossier externe',
                    urgence_vitale: 'Urgence vitale', grossesse: 'Grossesse', allergie: 'Allergie', diabete: 'Diabète',
                    hta: 'HTA', asthme: 'Asthme', insuffisance_renale: 'Insuffisance rénale', pace_maker: 'Pacemaker',
                    valve_cardiaque: 'Valve cardiaque', prothese_metallique: 'Prothèse métallique'
                };
                document.getElementById('dme_passage_clinical_info').innerHTML = Object.entries(clinicalLabels)
                    .filter(([key]) => questionnaire[key] === true || String(questionnaire[key]).trim().toUpperCase() === 'OUI')
                    .map(([, label]) => `<span class="px-2.5 py-1 rounded-lg bg-white dark:bg-black/30 border border-emerald-200 dark:border-emerald-900/40 text-slate-700 dark:text-slate-200"><b>${label} :</b> Oui</span>`).join('') || '<span class="text-[11px] text-slate-500">Aucun renseignement clinique coché « Oui ».</span>';

                const admissionDocs = pass.documents_scannes || [];
                const admissionDocsBox = document.getElementById('dmeAdmissionDocs');
                if (admissionDocsBox) {
                    const demandeButton = `<button type="button" onclick="openDmeDemandeExamen(${Number(currentTab.passageId)})" class="px-3 py-2 rounded-xl bg-slate-900 dark:bg-brand-neon text-white dark:text-black text-[10px] font-bold inline-flex items-center gap-1.5"><i class="ph-bold ph-file-pdf"></i>Demande d’examen · PDF / imprimer</button>`;
                    const docButtons = admissionDocs.map((doc, index) => `<button type="button" onclick="openDmeAdmissionDocument(${Number(currentTab.patientId)}, ${Number(currentTab.passageId)}, ${index})" class="px-3 py-2 rounded-xl bg-white dark:bg-black/20 border border-slate-200 dark:border-white/10 text-slate-700 dark:text-slate-200 text-[10px] font-bold inline-flex items-center gap-1.5"><i class="ph-bold ph-file-pdf text-rose-500"></i>${escapeDmeArchiveHtml(doc.type || `Document scanné ${index + 1}`)}${doc.heure ? ` · ${escapeDmeArchiveHtml(doc.heure)}` : ''}</button>`).join('');
                    admissionDocsBox.innerHTML = `<div class="flex flex-wrap items-center gap-2"><span class="text-[10px] font-extrabold uppercase tracking-wide text-slate-500 mr-1">Documents d’admission :</span>${demandeButton}${docButtons || '<span class="text-[10px] text-slate-400 italic">Aucun document scanné pour ce passage.</span>'}</div>`;
                }

                // Constantes
                document.getElementById('dme_ta').value = c.ta || '';
                document.getElementById('dme_pouls').value = c.pouls || '';
                document.getElementById('dme_temp').value = c.temp || '';
                document.getElementById('dme_poids').value = c.poids || '';
                document.getElementById('dme_taille').value = c.taille || '';
                document.getElementById('dme_imc').value = c.imc || '';
                document.getElementById('dme_glycemie').value = c.glycemie || '';
                document.getElementById('dme_growth_weight').value = c.poids || '';
                document.getElementById('dme_growth_height').value = c.taille || '';
                document.getElementById('dme_growth_head').value = c.perimetre_cranien || '';
                document.getElementById('dme_growth_arm').value = c.perimetre_brachial || '';

                // Les 8 champs ordonnés
                document.getElementById('dme_allergies').value = dme.allergies_intolerances || '';
                document.getElementById('dme_traitement').value = dme.traitement_en_cours || '';
                document.getElementById('dme_ant_med').value = dme.antecedents_medicaux || '';
                document.getElementById('dme_ant_chir').value = dme.antecedents_chirurgicaux || '';
                document.getElementById('dme_habitudes').value = dme.notes_habitudes || '';
                document.getElementById('dme_motif').value = dme.motif || '';
                document.getElementById('dme_diagnostic').value = dme.diagnostic || '';
                document.getElementById('dme_conduite').value = dme.conduite_a_tenir || '';

                // Actes
                currentDmeActs = Array.isArray(pass.actes) ? [...pass.actes] : [];
                renderDmeActsTable();
                autoDetectDmeTarifPeriod();
                calculateImc();
                window.renderDmeGrowthChart();

                // Modèles CR
                const tplSelect = document.getElementById('crTemplateSelect');
                if (tplSelect) {
                    tplSelect.value = '';
                }
                const tplSearch = document.getElementById('crTemplateSearchInput');
                if (tplSearch) tplSearch.value = '';
                const tplResults = document.getElementById('crTemplateSearchResults');
                if (tplResults) { tplResults.innerHTML = ''; tplResults.classList.add('hidden'); }

                // Prescripteur
                const prescInput = document.getElementById('crMedPrescripteurSelect');
                if (prescInput) {
                    prescInput.value = pass.medecin_nom ? `${pass.medecin_grade || 'Dr.'} ${pass.medecin_nom} ${pass.medecin_prenom || ''}`.trim() : 'Dr. Prescripteur';
                }

                // Signataires
                const sigSelect = document.getElementById('crMedSignataireSelect');
                if (sigSelect) {
                    sigSelect.innerHTML = radiologues.length > 0 
                        ? radiologues.map(r => `<option value="Dr. ${r.nom} ${r.prenom} (${r.specialite || 'Radiologie'})">Dr. ${r.nom} ${r.prenom} (${r.specialite || 'Radiologie'})</option>`).join('')
                        : '<option value="Dr. Praticien (Radiologie)">Dr. Praticien (Radiologie)</option>';
                }

                // Tableaux
                renderDmeArchivedCrTable(archivedCrs);
                renderEchoGallery();

                // Historique
                const histoBody = document.getElementById('dmeHistoTableBody');
                if (histoBody) {
                    histoBody.innerHTML = histo.length > 0 ? histo.map(h => `
                        <tr class="border-b border-slate-100 dark:border-white/5 hover:bg-slate-50 dark:hover:bg-white/[0.02]">
                            <td class="py-2.5 font-mono text-[11px]">#PASS-${String(h.id_passage).padStart(4, '0')}</td>
                            <td class="py-2.5">${h.date_passage} (${h.heure_passage})</td>
                            <td class="py-2.5 font-bold">${h.type_passage}</td>
                            <td class="py-2.5 italic">${h.diagnostic || 'Non renseigné'}</td>
                            <td class="py-2.5 font-semibold">${h.praticien || 'Dr'}</td>
                            <td class="py-2.5"><span class="px-2 py-0.5 rounded-lg text-[10px] font-bold bg-brand-neon/20 text-brand-neon">${h.statut}</span></td>
                            <td class="py-2.5 text-right"><button type="button" onclick="viewArchivedConsultation(${h.id_passage})" class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-slate-900 dark:bg-brand-neon text-white dark:text-black text-[10px] font-bold hover:opacity-90 transition"><i class="ph-bold ph-eye"></i><span>Voir l'archive</span></button></td>
                        </tr>
                    `).join('') : '<tr><td colspan="7" class="py-6 text-center text-slate-400 italic">Aucun passage antérieur.</td></tr>';
                }

                applyDmeDisplayPreferences(currentTab.data?.dme_display_permissions || dmeDisplayPermissions);
                switchDmeSubTab(currentTab.currentSubTab || 'consultation');
            }

            function escapeDmeArchiveHtml(value) {
                return String(value ?? '')
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;')
                    .replace(/'/g, '&#39;');
            }

            window.closeDmeArchiveDetailModal = function() {
                const modal = document.getElementById('dmeArchiveDetailModal');
                if (modal) { modal.classList.add('hidden'); modal.classList.remove('flex'); }
            };

            window.openArchivedDocumentPreview = function(recordId, format) {
                if (format === 'html') openCrPreviewModal(recordId);
                else openWordPdfPreview(recordId, 'Document archivé');
            };

            window.viewArchivedConsultation = async function(passageId) {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const modal = document.getElementById('dmeArchiveDetailModal');
                const body = document.getElementById('dmeArchiveModalBody');
                const title = document.getElementById('dmeArchiveModalTitle');
                const subtitle = document.getElementById('dmeArchiveModalSubtitle');
                if (!currentTab || !modal || !body) return;

                modal.classList.remove('hidden');
                modal.classList.add('flex');
                if (title) title.textContent = `Archive de consultation · #PASS-${String(passageId).padStart(4, '0')}`;
                if (subtitle) subtitle.textContent = 'Chargement des informations cliniques et documents associés...';
                body.innerHTML = `<div class="py-16 text-center text-xs text-slate-400"><i class="ph-bold ph-spinner animate-spin text-xl"></i><p class="mt-2">Chargement de l'archive...</p></div>`;

                try {
                    const response = await fetch(`/api/dme/archive/${currentTab.patientId}/${passageId}`);
                    const result = await response.json();
                    if (!response.ok) throw new Error(result.detail || 'Archive introuvable.');

                    const c = result.consultation || {};
                    const constants = c.constantes || {};
                    const safe = escapeDmeArchiveHtml;
                    const displayValue = value => value === null || value === undefined || String(value).trim() === '' ? '<span class="italic text-slate-400">Non renseigné</span>' : safe(value);
                    const field = (label, value) => `<div class="rounded-xl border border-slate-100 dark:border-white/5 bg-slate-50/70 dark:bg-black/15 p-3"><div class="text-[9px] font-bold uppercase tracking-wide text-slate-400">${safe(label)}</div><div class="mt-1 text-xs leading-5 text-slate-700 dark:text-slate-200 whitespace-pre-wrap break-words">${displayValue(value)}</div></div>`;
                    const section = (titleText, icon, content) => `<section class="space-y-3"><h4 class="flex items-center gap-2 text-xs font-extrabold text-slate-800 dark:text-white"><i class="ph-bold ${icon} text-brand-neon text-base"></i>${safe(titleText)}</h4>${content}</section>`;

                    const constantFields = [
                        ['Tension artérielle', constants.ta], ['Pouls (BPM)', constants.pouls],
                        ['Température (°C)', constants.temp], ['Poids (kg)', constants.poids],
                        ['Taille (cm)', constants.taille], ['IMC (kg/m²)', constants.imc], ['Glycémie (g/L)', constants.glycemie]
                    ].map(([labelText, value]) => field(labelText, value)).join('');
                    const clinicalFields = [
                        ['Observation médicale', c.questionnaire?.observation_medicale || c.questionnaire?.observation],
                        ['Allergies & intolérances', c.allergies_intolerances], ['Traitement en cours', c.traitement_en_cours],
                        ['Antécédents médicaux', c.antecedents_medicaux], ['Antécédents chirurgicaux', c.antecedents_chirurgicaux],
                        ['Notes & habitudes de vie', c.notes_habitudes], ['Motif de consultation', c.motif],
                        ['Diagnostic & hypothèse', c.diagnostic], ['Conduite à tenir', c.conduite_a_tenir]
                    ].map(([labelText, value]) => field(labelText, value)).join('');
                    const specialtyData = c.donnees_specialisees || {};
                    const specialtySections = specialtyData.sections && typeof specialtyData.sections === 'object' ? specialtyData.sections : {};
                    const specialtyFields = Object.entries(specialtySections).map(([sectionKey, values]) => {
                        const sectionChoice = DME_SPECIALTY_CHOICES.find(item => item.key === sectionKey);
                        const fields = Object.entries(values || {}).filter(([key, value]) => DME_SPECIALTY_FIELD_LABELS[key] && String(value || '').trim()).map(([key, value]) => {
                            if (key === 'plans_reeducation_json') {
                                try {
                                    const rows = JSON.parse(value);
                                    if (Array.isArray(rows) && rows.length) {
                                        const tableRows = rows.map(plan => `<tr class="border-t border-slate-200 dark:border-white/10"><td class="p-2">${safe(plan.type || '—')}</td><td class="p-2">${safe(plan.seances_par_semaine || '—')}</td><td class="p-2">${safe(plan.nombre_total_seances || '—')}</td><td class="p-2">${safe(plan.duree_seance_minutes || '—')}</td><td class="p-2">${safe(plan.kine || "N’importe quelle kiné")}</td></tr>`).join('');
                                        return `<div class="col-span-full overflow-x-auto rounded-xl border border-slate-200 dark:border-white/10"><div class="p-3 text-[9px] font-bold uppercase tracking-wide text-slate-400">${safe(DME_SPECIALTY_FIELD_LABELS[key])}</div><table class="w-full min-w-[680px] text-left text-[10px]"><thead class="bg-slate-100 dark:bg-white/5"><tr><th class="p-2">Type</th><th class="p-2">Séances / semaine</th><th class="p-2">Total</th><th class="p-2">Durée (min)</th><th class="p-2">Kinésithérapeute</th></tr></thead><tbody>${tableRows}</tbody></table></div>`;
                                    }
                                } catch (_) {}
                                return '';
                            }
                            return field(DME_SPECIALTY_FIELD_LABELS[key], value);
                        }).join('');
                        return fields && sectionChoice ? section(`Section ${sectionChoice.label}`, 'ph-first-aid-kit', `<div class="grid grid-cols-1 gap-2 sm:grid-cols-2">${fields}</div>`) : '';
                    }).join('');
                    const intakeLabels = {
                        hospitalise: 'Hospitalisé', dossier_interne: 'Dossier interne', dossier_externe: 'Dossier externe',
                        urgence_vitale: 'Urgence vitale', grossesse: 'Grossesse', allergie: 'Allergie', diabete: 'Diabète',
                        hta: 'HTA', asthme: 'Asthme', insuffisance_renale: 'Insuffisance rénale', pace_maker: 'Pacemaker',
                        valve_cardiaque: 'Valve cardiaque', prothese_metallique: 'Prothèse métallique'
                    };
                    const intakeFields = Object.entries(intakeLabels)
                        .map(([key, label]) => [label, c.questionnaire?.[key]])
                        .filter(([, value]) => value !== undefined && value !== null && value !== '')
                        .map(([label, value]) => field(label, value)).join('');

                    const acts = Array.isArray(c.actes) ? c.actes : [];
                    const amounts = [
                        ['Total brut', c.montant_total], ['Remise', c.remise],
                        ['Versement', c.versement], ['Reste', c.reste]
                    ].map(([labelText, value]) => field(labelText, value === null || value === undefined ? '' : `${Number(value).toLocaleString('fr-FR')} DA`)).join('');
                    const actsHtml = acts.length
                        ? `<div class="overflow-x-auto rounded-xl border border-slate-100 dark:border-white/5"><table class="w-full text-left text-[11px]"><thead class="bg-slate-50 dark:bg-white/5 text-slate-400"><tr><th class="p-2.5">Acte / prestation</th><th class="p-2.5">Période</th><th class="p-2.5 text-center">Qté</th><th class="p-2.5 text-right">Sous-total</th></tr></thead><tbody>${acts.map(act => `<tr class="border-t border-slate-100 dark:border-white/5"><td class="p-2.5 font-semibold">${safe(act.nom || 'Acte')}</td><td class="p-2.5">${safe(act.periode || '-')}</td><td class="p-2.5 text-center">${safe(act.quantite ?? '-')}</td><td class="p-2.5 text-right font-mono">${Number(act.sous_total || 0).toLocaleString('fr-FR')} DA</td></tr>`).join('')}</tbody></table></div>`
                        : '<p class="text-[11px] italic text-slate-400">Aucun acte rattaché à ce passage.</p>';

                    const prescriptions = Array.isArray(result.prescriptions) ? result.prescriptions : [];
                    const prescriptionsHtml = prescriptions.length
                        ? prescriptions.map(ord => `<article class="rounded-2xl border border-slate-200 dark:border-white/10 p-4"><div class="flex flex-wrap items-center justify-between gap-2"><div><div class="text-xs font-bold text-slate-800 dark:text-white">Ordonnance #ORD-${String(ord.id).padStart(4, '0')}</div><div class="mt-1 text-[10px] text-slate-500">${safe(ord.date_creation_str || '')} · ${safe(ord.medecin_nom || 'Praticien')} (${safe(ord.medecin_specialite || 'Médecine')}) · ${safe(ord.statut || 'Valide')}</div></div><button type="button" onclick="openPrescriptionModal(${Number(ord.id)}, ${Number(currentTab.patientId)}, ${Number(c.id_passage)}, ${Number(c.id_consultation || 0)})" class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-slate-900 dark:bg-brand-neon text-white dark:text-black text-[10px] font-bold"><i class="ph-bold ph-file-pdf"></i>Ouvrir le PDF</button></div><div class="mt-3 space-y-1.5">${(ord.lignes || []).map(line => `<div class="text-[11px] text-slate-600 dark:text-slate-300"><strong>${safe(line.medicament || 'Médicament')}</strong> · ${safe(line.posologie || 'Posologie non renseignée')} · ${safe(line.duree || '-')} · Qté ${safe(line.quantite ?? '-')}</div>`).join('') || '<span class="text-[11px] text-slate-400">Aucun médicament détaillé.</span>'}</div></article>`).join('')
                        : '<p class="text-[11px] italic text-slate-400">Aucune ordonnance liée à ce passage.</p>';

                    const documents = Array.isArray(result.documents) ? result.documents : [];
                    const documentsHtml = documents.length
                        ? documents.map(doc => {
                            const format = doc.type_modele === 'HTML' ? 'html' : 'pdf';
                            const titleText = doc.nom_fichier || doc.titre || 'Document archivé';
                            const labelText = doc.type_modele === 'Livret' ? 'Livret PDF' : `Compte rendu${doc.type_modele ? ` · ${doc.type_modele}` : ''}`;
                            return `<article class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 p-4 rounded-2xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/10"><div class="min-w-0"><div class="text-xs font-bold text-slate-800 dark:text-white break-words">${safe(titleText)}</div><div class="mt-1 text-[10px] text-slate-500">${safe(labelText)} · ${safe(doc.date_heure_str || '')} · ${safe(doc.medecin_signataire || 'Signataire non renseigné')}</div></div><button type="button" onclick="openArchivedDocumentPreview(${Number(doc.id)}, '${format}')" class="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-brand-neon text-black text-[10px] font-bold"><i class="ph-bold ph-file-pdf"></i>Ouvrir le document</button></article>`;
                        }).join('')
                        : '<p class="text-[11px] italic text-slate-400">Aucun compte rendu ou livret archivé pour ce passage.</p>';

                    if (title) title.textContent = `${currentTab.data.patient.nom || ''} ${currentTab.data.patient.prenom || ''}`.trim() || 'Archive de consultation';
                    if (subtitle) subtitle.textContent = `#PASS-${String(c.id_passage).padStart(4, '0')} · ${c.date_passage || '-'} à ${c.heure_passage || '-'} · ${c.type_passage || 'Consultation'} · ${c.statut || ''}`;
                    body.innerHTML = `
                        ${!c.id_consultation ? '<div class="p-3 rounded-xl border border-amber-200 bg-amber-50 dark:bg-amber-950/20 dark:border-amber-900/50 text-[11px] text-amber-800 dark:text-amber-200">Aucune fiche clinique DME n’a été enregistrée pour ce passage. Les archives et ordonnances disponibles restent consultables ci-dessous.</div>' : ''}
                        <div class="flex flex-wrap items-center justify-between gap-3 p-4 rounded-2xl bg-brand-neon/10 border border-brand-neon/20">
                            <div><div class="text-[10px] font-bold uppercase tracking-wider text-emerald-800 dark:text-brand-neon">Praticien</div><div class="mt-1 text-xs font-bold text-slate-800 dark:text-white">${displayValue(c.praticien)}</div></div>
                            <div class="px-3 py-1.5 rounded-full bg-white dark:bg-black/20 border border-slate-200 dark:border-white/10 text-[10px] font-bold text-slate-600 dark:text-slate-300">${safe(c.statut || 'Archive')}</div>
                        </div>
                        ${section('Constantes vitales', 'ph-heartbeat', `<div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2">${constantFields}</div>`)}
                        ${section('Dossier clinique', 'ph-notepad', `<div class="grid grid-cols-1 md:grid-cols-2 gap-2">${clinicalFields}</div>`)}
                        ${specialty ? section(`Consultation spécialisée · ${specialty.label}`, 'ph-first-aid-kit', `<div class="grid grid-cols-1 md:grid-cols-2 gap-2">${specialtyFields || field('Section spécialisée', 'Aucune information renseignée.')}</div>`) : ''}
                        ${section('Renseignements cliniques à l’admission', 'ph-clipboard-text', `<div class="grid grid-cols-1 sm:grid-cols-2 gap-2.5">${intakeFields || field('Questionnaire', 'Aucun renseignement disponible.')}</div>`)}
                        ${section('Actes & règlement du passage', 'ph-first-aid', `<div class="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-3">${amounts}</div>${actsHtml}`)}
                        ${section(`Ordonnances (${prescriptions.length})`, 'ph-pill', `<div class="space-y-3">${prescriptionsHtml}</div>`)}
                        ${section(`Comptes rendus & livrets (${documents.length})`, 'ph-files', `<div class="space-y-3">${documentsHtml}</div>`)}
                    `;
                } catch (error) {
                    if (subtitle) subtitle.textContent = 'Impossible de charger cette archive.';
                    body.innerHTML = `<div class="p-6 rounded-2xl bg-rose-50 dark:bg-rose-950/20 text-sm text-rose-700 dark:text-rose-300">${escapeDmeArchiveHtml(error.message || 'Erreur de chargement.')}</div>`;
                }
            };

            // =================================================================
            // 5. TABLEAU CR & NOUVEAU BOUTON « VISION PDF »
            // =================================================================
            function renderDmeArchivedCrTable(archivedCrs) {
                const tbody = document.getElementById('dmeArchivedCrTableBody');
                if (!tbody) return;

                if (!archivedCrs || archivedCrs.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" class="py-8 text-center text-slate-400 italic font-medium">Aucun compte rendu ou livret généré pour ce patient lors de ce passage.</td></tr>';
                    return;
                }

                tbody.innerHTML = archivedCrs.map(cr => {
                    const isLivret = cr.type_modele === 'Livret';
                    const isWord = cr.type_modele === 'Word';
                    const prestationLabel = isLivret ? String(cr.titre || '').replace(/^Livret\\s*-\\s*/i, '').trim() : '';

                    let badge = '';
                    let actionBtns = '';

                    if (isLivret) {
                        badge = '<span class="px-2.5 py-0.5 rounded-lg text-[10px] font-extrabold bg-brand-neon/20 text-emerald-800 dark:text-brand-neon border border-brand-neon/30">Livret PDF</span>';
                        actionBtns = `
                            <button type="button" onclick="openWordPdfPreview(${cr.id}, '${cr.nom_fichier || 'Livret'}')" class="px-3 py-1.5 rounded-xl bg-brand-neon hover:bg-brand-neonDark text-black font-extrabold text-[11px] inline-flex items-center gap-1 cursor-pointer shadow-sm">
                                <i class="ph-bold ph-book-open text-sm"></i>
                                <span>Voir Livret PDF</span>
                            </button>
                        `;
                    } else if (isWord) {
                        badge = '<span class="px-2.5 py-0.5 rounded-lg text-[10px] font-bold bg-sky-50 dark:bg-sky-950/40 text-sky-600 border border-sky-200">Word</span>';
                        actionBtns = `
                            <button type="button" onclick="openLivretModal(${cr.id})" class="px-2.5 py-1.5 rounded-xl bg-brand-neon hover:bg-brand-neonDark text-black font-extrabold text-[11px] inline-flex items-center gap-1 cursor-pointer shadow-sm" title="Créer un livret médical multipage">
                                <i class="ph-bold ph-book-open text-sm"></i>
                                <span>Création Livret</span>
                            </button>
                            <button type="button" onclick="openWordPdfPreview(${cr.id}, '${cr.nom_fichier || 'Compte_Rendu'}')" class="px-2.5 py-1.5 rounded-xl bg-[#0c1a10] hover:bg-[#1a3822] text-[#9fe870] font-bold text-[11px] inline-flex items-center gap-1 cursor-pointer border border-[#9fe870]/30">
                                <i class="ph-bold ph-file-pdf text-sm"></i>
                                <span>Vision Pdf</span>
                            </button>
                            <button type="button" onclick="openWordCrDocument(${cr.id})" class="px-2.5 py-1.5 rounded-xl bg-sky-500 hover:bg-sky-600 text-white font-bold text-[11px] inline-flex items-center gap-1 cursor-pointer">
                                <i class="ph-bold ph-microsoft-word-logo text-sm"></i>
                                <span>Ouvrir Word</span>
                            </button>
                        `;
                    } else {
                        badge = '<span class="px-2.5 py-0.5 rounded-lg text-[10px] font-bold bg-amber-50 dark:bg-amber-950/40 text-amber-600 border border-amber-200">HTML</span>';
                        actionBtns = `
                            <button type="button" onclick="openLivretModal(${cr.id})" class="px-2.5 py-1.5 rounded-xl bg-brand-neon hover:bg-brand-neonDark text-black font-extrabold text-[11px] inline-flex items-center gap-1 cursor-pointer shadow-sm">
                                <i class="ph-bold ph-book-open text-sm"></i>
                                <span>Création Livret</span>
                            </button>
                            <button type="button" onclick="openCrPreviewModal(${cr.id})" class="px-2.5 py-1.5 rounded-xl bg-slate-100 dark:bg-white/5 hover:bg-slate-200 text-slate-700 dark:text-slate-200 font-bold text-[11px] inline-flex items-center gap-1 cursor-pointer">
                                <i class="ph-bold ph-printer text-sm"></i>
                                <span>Aperçu A4</span>
                            </button>
                        `;
                    }

                    return `
                        <tr class="h-10 hover:bg-slate-50 dark:hover:bg-white/[0.02]">
                            <td class="py-1 pl-3 font-mono text-slate-400 whitespace-nowrap">${cr.date_heure_str || ''}</td>
                            <td class="py-0 font-bold text-slate-800 dark:text-slate-100 font-mono text-[11px]">
                                <div class="h-10 flex flex-col justify-center overflow-hidden pr-2 leading-none">
                                    <span class="block truncate leading-4" title="${cr.nom_fichier || cr.titre || ''}">${cr.nom_fichier || cr.titre || ''}</span>
                                    ${prestationLabel ? `<span class="block truncate text-[10px] font-semibold text-indigo-600 dark:text-indigo-300 leading-3" title="Prestation : ${prestationLabel}">Prestation : ${prestationLabel}</span>` : ''}
                                </div>
                            </td>
                            <td class="py-1 text-emerald-600 dark:text-brand-neon font-semibold">${cr.medecin_signataire || ''}</td>
                            <td class="py-1 text-slate-500">@${cr.proprietaire || 'admin'}</td>
                            <td class="py-1">${badge}</td>
                            <td class="py-1 text-right pr-4">
                                <div class="inline-flex items-center gap-1.5">
                                    ${actionBtns}
                                    <button type="button" onclick="deleteArchivedCr(${cr.id})" class="p-1.5 text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-500/10 rounded-lg transition cursor-pointer" title="Supprimer">
                                        <i class="ph-bold ph-trash text-sm"></i>
                                    </button>
                                </div>
                            </td>
                        </tr>
                    `;
                }).join('');
            }

            // Ouvre le PDF du Word dans le visionneur intégré
            let activeLivretDraftToken = null;
            function setDmeCrPreviewMode(mode) {
                const printButton = document.getElementById('btnPrintCrIframe');
                const validateButton = document.getElementById('btnValidateLivretDraft');
                if (printButton) printButton.classList.toggle('hidden', mode === 'draft');
                if (validateButton) validateButton.classList.toggle('hidden', mode !== 'draft');
            }
            window.openLivretDraftPreview = function(draftToken, nomFichier = 'Livret Médical') {
                if (!draftToken) { alert('Le brouillon du livret est introuvable.'); return; }
                const modal = portalDmeModal('dmeCrPreviewModal');
                const iframe = document.getElementById('dmeCrIframe');
                const titleEl = document.getElementById('dmeCrModalTitle');
                activeLivretDraftToken = draftToken;
                setDmeCrPreviewMode('draft');
                if (titleEl) titleEl.innerText = 'Aperçu du livret — ' + (nomFichier || 'Livret Médical');
                if (modal) { modal.classList.remove('hidden'); modal.classList.add('flex'); document.body.style.overflow = 'hidden'; }
                if (iframe) iframe.src = '/api/dme/livret/preview/' + encodeURIComponent(draftToken) + '?t=' + Date.now();
            };
            window.validateLivretDraft = async function() {
                const token = activeLivretDraftToken;
                const button = document.getElementById('btnValidateLivretDraft');
                if (!token || !button) return;
                const original = button.innerHTML;
                button.disabled = true;
                button.innerHTML = '<i class="ph-bold ph-spinner animate-spin"></i><span>Validation...</span>';
                try {
                    const response = await fetch('/api/dme/livret/validate', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({draft_token: token})
                    });
                    const result = await response.json();
                    if (!response.ok || !result.success) throw new Error(result.detail || result.message || 'Impossible de valider le livret.');
                    activeLivretDraftToken = null;
                    setDmeCrPreviewMode('archived');
                    const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                    if (currentTab) {
                        const refreshed = await fetch(`/api/dme/record/${currentTab.patientId}/${currentTab.passageId}`).then(r => r.json());
                        currentTab.data.archived_crs = refreshed.archived_crs || [];
                        renderDmeArchivedCrTable(currentTab.data.archived_crs);
                    }
                } catch (error) {
                    alert(error.message || 'Erreur lors de la validation du livret.');
                } finally {
                    button.disabled = false;
                    button.innerHTML = original;
                }
            };
            window.openWordPdfPreview = function(recordId, nomFichier) {
                const modal = portalDmeModal('dmeCrPreviewModal');
                const iframe = document.getElementById('dmeCrIframe');
                const titleEl = document.getElementById('dmeCrModalTitle');
                activeLivretDraftToken = null;
                setDmeCrPreviewMode('archived');

                if (titleEl) titleEl.innerText = "Vision Pdf : " + (nomFichier || "Compte Rendu");

                // 1. Écran de chargement esthétique immédiat pendant la conversion Word
                iframe.srcdoc = `
                    <!DOCTYPE html>
                    <html>
                    <body style="margin:0; background:#0c1a10; color:#8be140; font-family:sans-serif; display:flex; flex-direction:column; align-items:center; justify-content:center; height:100vh;">
                        <div style="font-size:32px; margin-bottom:12px; animation: spin 1s infinite linear;">⚙️</div>
                        <div style="font-size:14px; font-weight:bold;">Conversion du document Word en PDF en cours...</div>
                        <div style="font-size:11px; color:#94a3b8; margin-top:6px;">Veuillez patienter un instant.</div>
                    </body>
                    </html>
                `;
                if (modal) { modal.classList.remove('hidden'); modal.classList.add('flex'); document.body.style.overflow = 'hidden'; }

                // 2. Chargement du PDF dans l'iframe dès qu'il est prêt
                setTimeout(() => {
                    iframe.removeAttribute('srcdoc');
                    iframe.src = '/api/dme/cr/view-pdf/' + recordId + '?t=' + Date.now();
                }, 200);
            };

            // =================================================================
            // 6. DOSSIER ÉCHOGRAPHIE (PNG) & CARROUSEL INTERACTIF
            // =================================================================
            function echoImageTypeLabel(type) {
                return type === 'radiologique' ? 'Images de livret radiologique' : type === 'planche_trois' ? 'Images Planche A3' : 'Images échographiques';
            }
            function echoImageGroupId(image) {
                return image.groupe_id || `legacy_${image.type_image || 'echographique'}_${image.categorie || 'Général'}`;
            }
            function getEchoImageGroups(echos) {
                const groups = new Map();
                (echos || []).forEach(image => {
                    const imageType = image.type_image || 'echographique';
                    const category = image.categorie || 'Général';
                    const groupId = echoImageGroupId(image);
                    if (!groups.has(groupId)) groups.set(groupId, {id: groupId, category, items: []});
                    groups.get(groupId).items.push(image);
                });
                return [...groups.values()].map(group => {
                    group.items.sort((a, b) => (Number(a.ordre_affichage || 0) - Number(b.ordre_affichage || 0)) || ((Date.parse(a.created_at || '') || 0) - (Date.parse(b.created_at || '') || 0)) || (Number(a.id) - Number(b.id)));
                    group.types = [...new Set(group.items.map(image => image.type_image || 'echographique'))];
                    group.label = `${group.types.map(echoImageTypeLabel).join(' + ')} — ${group.category}`;
                    group.addedAt = Math.min(...group.items.map(image => Date.parse(image.created_at || '') || Number(image.id || 0)));
                    return group;
                }).sort((a, b) => a.addedAt - b.addedAt);
            }
            function renderEchoGallery() {
                const grid = document.getElementById('dmeEchoGalleryGrid');
                if (!grid) return;

                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const echos = (currentTab && currentTab.data && currentTab.data.echographies) ? currentTab.data.echographies : [];

                activeCarouselImages = echos;

                if (echos.length === 0) {
                    grid.innerHTML = `
                        <div class="col-span-full py-12 text-center text-slate-400">
                            <i class="ph-bold ph-image text-3xl opacity-30 mx-auto mb-2"></i>
                            <p class="text-xs">Aucun cliché échographique importé pour cette consultation.</p>
                            <p class="text-[11px] opacity-70 mt-0.5">Cliquez sur « Importer Clichés PNG » ci-dessus.</p>
                        </div>
                    `;
                    return;
                }

                const orderedGroups = getEchoImageGroups(echos);
                activeCarouselImages = orderedGroups.flatMap(group => group.items);
                let idx = 0;
                grid.innerHTML = orderedGroups.map(({id: groupId, label: typeLabel, category, items}) => {
                    const categoryArg = encodeURIComponent(category);
                    const groupIdArg = echoManagerEscape(groupId);
                    const cards = items.map(ec => {
                        const imageIndex = idx++;
                        return `
                    <div class="group relative bg-slate-50 dark:bg-black/30 rounded-2xl p-2 border border-slate-200 dark:border-white/10 shadow-xs flex flex-col justify-between overflow-hidden">
                        <div class="relative w-full aspect-video rounded-xl overflow-hidden bg-black flex items-center justify-center cursor-pointer" onclick="openEchoCarouselAtIndex(${imageIndex})">
                            <img src="/api/dme/echographies/file/${ec.id}" class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300">
                            <div class="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 flex items-center justify-center gap-2 transition-opacity">
                                <span class="p-2 rounded-xl bg-white/20 text-white text-sm"><i class="ph-bold ph-arrows-out-simple"></i></span>
                            </div>
                        </div>

                        <div class="pt-2 px-1 flex items-center justify-between text-[11px]">
                            <span class="font-bold text-slate-700 dark:text-slate-300 truncate max-w-[150px]" title="${ec.nom_fichier}">${ec.nom_fichier || 'Image'}</span>
                            <button type="button" onclick="deleteEchographie(${ec.id})" class="text-rose-500 hover:text-rose-700 p-1 rounded-lg" title="Supprimer">
                                <i class="ph-bold ph-trash"></i>
                            </button>
                        </div>
                    </div>
                `;
                    }).join('');
                    const managerButton = `<button type="button" onclick="openEchoManagerModal('${groupIdArg}')" class="shrink-0 inline-flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50 px-2.5 py-1.5 text-[10px] font-bold normal-case text-indigo-700 hover:bg-indigo-600 hover:text-white dark:border-indigo-400/20 dark:bg-indigo-950/30 dark:text-indigo-200"><i class="ph-bold ph-arrows-out-simple"></i>Gérer ce groupe</button>`;
                    const addImagesButton = `<button type="button" onclick="addDprintImagesToEchoGroup('${groupIdArg}')" class="shrink-0 inline-flex items-center gap-1.5 rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-1.5 text-[10px] font-bold normal-case text-emerald-700 hover:bg-emerald-600 hover:text-white dark:border-emerald-400/20 dark:bg-emerald-950/30 dark:text-emerald-200"><i class="ph-bold ph-plus-circle"></i>Ajouter des images</button>`;
                    const deleteGroupButton = `<button type="button" onclick="deleteEchoImageGroup('${groupIdArg}', decodeURIComponent('${categoryArg}'))" class="shrink-0 inline-flex items-center gap-1.5 rounded-lg border border-rose-200 bg-rose-50 px-2.5 py-1.5 text-[10px] font-bold normal-case text-rose-700 hover:bg-rose-600 hover:text-white dark:border-rose-400/20 dark:bg-rose-950/30 dark:text-rose-200"><i class="ph-bold ph-trash"></i>Supprimer le groupe</button>`;
                    return `<section class="col-span-full"><div class="flex flex-wrap items-center gap-2 mb-3 mt-2">${managerButton}${addImagesButton}${deleteGroupButton}<h4 class="text-xs font-extrabold text-slate-700 dark:text-slate-200 uppercase tracking-wide">${typeLabel} <span class="text-slate-400 normal-case">— ${category}</span></h4></div><div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-4">${cards}</div></section>`;
                }).join('');
            }

            let echoManagerSourceGroups = [];
            let echoManagerDragId = null;
            const echoManagerEscape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
            let echoManagerOriginalParent = null;
            let echoManagerOriginalNextSibling = null;
            let echoManagerOriginalStyle = null;
            window.openEchoManagerModal = function(groupId = '') {
                const modal = document.getElementById('echoManagerModal');
                if (!modal) return;
                if (modal.parentElement !== document.body) {
                    echoManagerOriginalParent = modal.parentElement;
                    echoManagerOriginalNextSibling = modal.nextSibling;
                    echoManagerOriginalStyle = modal.getAttribute('style');
                    document.body.appendChild(modal);
                }
                modal.style.position = 'fixed';
                modal.style.inset = '0';
                modal.style.width = '100vw';
                modal.style.height = '100vh';
                modal.style.zIndex = '2147483647';
                modal.classList.remove('hidden');
                window.renderEchoManager();
                const groupSelect = document.getElementById('echoManagerGroup');
                if ([...groupSelect.options].some(option => option.value === groupId)) {
                    groupSelect.value = groupId;
                    window.renderEchoManager();
                }
            };
            window.closeEchoManagerModal = function() {
                const modal = document.getElementById('echoManagerModal');
                if (!modal) return;
                modal.classList.add('hidden');
                if (echoManagerOriginalParent && echoManagerOriginalParent.isConnected && modal.parentElement === document.body) {
                    echoManagerOriginalParent.insertBefore(modal, echoManagerOriginalNextSibling?.parentNode === echoManagerOriginalParent ? echoManagerOriginalNextSibling : null);
                }
                if (echoManagerOriginalStyle === null) modal.removeAttribute('style');
                else if (echoManagerOriginalStyle !== null) modal.setAttribute('style', echoManagerOriginalStyle);
                echoManagerOriginalParent = null;
                echoManagerOriginalNextSibling = null;
                echoManagerOriginalStyle = null;
            };
            window.renderEchoManager = function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab) return;
                const echos = currentTab.data?.echographies || [];
                const groups = getEchoImageGroups(echos);
                const groupSelect = document.getElementById('echoManagerGroup');
                const currentGroupId = groupSelect.value;
                groupSelect.innerHTML = groups.map(group => `<option value="${echoManagerEscape(group.id)}">${echoManagerEscape(group.label)}</option>`).join('');
                const selectedGroup = groups.find(group => group.id === currentGroupId) || groups[0];
                if (selectedGroup) groupSelect.value = selectedGroup.id;
                const groupId = selectedGroup?.id || '';
                const category = selectedGroup?.category || '';
                const images = selectedGroup?.items || [];
                const grid = document.getElementById('echoManagerGrid');
                if (!images.length) {
                    grid.innerHTML = '<div class="col-span-full py-16 text-center text-slate-400"><i class="ph-bold ph-images text-4xl"></i><p class="mt-2 text-sm">Aucune image dans ce groupe.</p><p class="text-xs mt-1">Utilisez « Importer d’un autre groupe » pour y ajouter des images.</p></div>';
                } else {
                    grid.innerHTML = images.map((ec, index) => `
                        <article data-echo-id="${ec.id}" class="relative flex flex-col rounded-2xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/25 overflow-hidden shadow-sm transition">
                            <div class="relative w-full min-h-44 max-h-[48vh] bg-black/90 flex items-center justify-center overflow-hidden p-2">
                                <img src="/api/dme/echographies/file/${ec.id}" class="max-w-full max-h-[46vh] w-auto h-auto object-contain" alt="${echoManagerEscape(ec.nom_fichier)}">
                                <button type="button" data-echo-position="${index + 1}" onclick="openEchoManagerPositionMenu(event, ${ec.id})" class="absolute top-2 left-2 z-20 min-w-8 h-8 px-2 rounded-lg bg-black/75 hover:bg-indigo-600 text-white text-[10px] font-extrabold shadow-lg" title="Choisir la position de cette image">${index + 1}</button>
                                <button type="button" onpointerdown="echoManagerPointerDown(event, ${ec.id})" onclick="event.preventDefault();event.stopPropagation()" class="absolute top-2 right-2 z-20 w-9 h-9 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white touch-none select-none cursor-grab active:cursor-grabbing shadow-lg" title="Maintenir et glisser pour changer l’ordre"><i class="ph-bold ph-dots-six-vertical"></i></button>
                            </div>
                            <div class="min-w-0 px-2 pt-2"><div class="flex items-center justify-between gap-1"><p class="truncate text-[11px] font-bold text-slate-800 dark:text-white" title="${echoManagerEscape(ec.nom_fichier || 'Image')}">${echoManagerEscape(ec.nom_fichier || 'Image')}</p><span class="shrink-0 rounded-md bg-slate-200 px-1.5 py-0.5 text-[8px] font-bold text-slate-600 dark:bg-white/10 dark:text-slate-300">${(ec.type_image || 'echographique') === 'radiologique' ? 'Radiologique' : (ec.type_image || '') === 'planche_trois' ? 'Planche A3' : 'Échographique'}</span></div><p class="truncate text-[9px] text-slate-500" title="${echoManagerEscape(ec.date_heure_str || '')}">${echoManagerEscape(ec.date_heure_str || '')}</p></div>
                            <div class="flex items-center justify-between gap-1 p-2">
                                <span class="px-1 text-[9px] font-semibold text-slate-400">Position</span>
                                <div class="flex items-center gap-0.5">
                                    <button type="button" onclick="moveEchoManagerCard(${ec.id}, 'first')" class="w-7 h-7 rounded-md hover:bg-indigo-100 text-indigo-700" title="Déplacer en premier"><i class="ph-bold ph-caret-double-up"></i></button>
                                    <button type="button" onclick="moveEchoManagerCard(${ec.id}, 'up')" class="w-7 h-7 rounded-md hover:bg-indigo-100 text-indigo-700" title="Monter"><i class="ph-bold ph-caret-up"></i></button>
                                    <button type="button" onclick="moveEchoManagerCard(${ec.id}, 'down')" class="w-7 h-7 rounded-md hover:bg-indigo-100 text-indigo-700" title="Descendre"><i class="ph-bold ph-caret-down"></i></button>
                                    <button type="button" onclick="moveEchoManagerCard(${ec.id}, 'last')" class="w-7 h-7 rounded-md hover:bg-indigo-100 text-indigo-700" title="Déplacer en dernier"><i class="ph-bold ph-caret-double-down"></i></button>
                                </div>
                                <button type="button" onclick="deleteEchographie(${ec.id})" class="w-7 h-7 rounded-md text-rose-500 hover:bg-rose-50" title="Supprimer"><i class="ph-bold ph-trash"></i></button>
                            </div>
                        </article>`).join('');
                }
                const positionMenu = document.getElementById('echoManagerPositionMenu');
                if (positionMenu) positionMenu.classList.add('hidden');
                echoManagerSourceGroups = groups.filter(group => group.id !== groupId);
                const sourceSelect = document.getElementById('echoManagerSourceGroup');
                sourceSelect.innerHTML = echoManagerSourceGroups.map((group, i) => `<option value="${i}">${echoManagerEscape(group.label)}</option>`).join('');
                if (!echoManagerSourceGroups.length) sourceSelect.innerHTML = '<option value="">Aucun autre groupe disponible</option>';
                window.renderEchoManagerSources();
            };
            window.toggleEchoImportPanel = function() {
                const panel = document.getElementById('echoManagerImportPanel');
                panel.classList.toggle('hidden');
                window.renderEchoManagerSources();
            };
            window.renderEchoManagerSources = function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const grid = document.getElementById('echoManagerSourceGrid');
                if (!grid || !currentTab) return;
                const group = echoManagerSourceGroups[Number(document.getElementById('echoManagerSourceGroup').value)];
                const targetGroupId = document.getElementById('echoManagerGroup').value;
                if (!group) { grid.innerHTML = '<p class="text-xs text-slate-500">Aucun autre groupe à importer.</p>'; return; }
                const targetPaths = new Set((currentTab.data.echographies || []).filter(ec => echoImageGroupId(ec) === targetGroupId).map(ec => ec.chemin_image));
                const images = group.items.filter(ec => !targetPaths.has(ec.chemin_image));
                grid.innerHTML = images.length ? images.map(ec => `<label class="flex items-center gap-2 p-2 rounded-xl border border-indigo-100 dark:border-white/10 bg-white dark:bg-black/20 cursor-pointer"><input type="checkbox" class="echo-manager-import-check h-4 w-4" value="${ec.id}"><img src="/api/dme/echographies/file/${ec.id}" class="w-14 h-12 object-contain bg-black rounded-lg"><span class="min-w-0 text-[10px] font-semibold truncate">${echoManagerEscape(ec.nom_fichier)}</span></label>`).join('') : '<p class="text-xs text-slate-500">Aucune nouvelle image disponible dans ce groupe.</p>';
                document.getElementById('echoManagerImportHint').textContent = `${images.length} image(s) disponible(s)`;
            };
            window.importSelectedEchoManagerImages = async function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const imageIds = [...document.querySelectorAll('.echo-manager-import-check:checked')].map(input => Number(input.value));
                if (!currentTab || !imageIds.length) { alert('Sélectionnez au moins une image à importer.'); return; }
                const response = await fetch('/api/dme/echographies/import', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id_patient:currentTab.patientId, id_passage:currentTab.passageId, image_ids:imageIds, groupe_id:document.getElementById('echoManagerGroup').value})}).then(r => r.json());
                if (!response.success) { alert(response.detail || response.message || 'Impossible d’importer ces images.'); return; }
                await refreshEchoManagerData(currentTab);
                alert(response.message || 'Images importées.');
            };
            window.saveEchoManagerOrder = async function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab) return;
                const imageIds = [...document.querySelectorAll('#echoManagerGrid [data-echo-id]')].map(card => Number(card.dataset.echoId));
                const response = await fetch('/api/dme/echographies/reorder', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id_patient:currentTab.patientId, id_passage:currentTab.passageId, groupe_id:document.getElementById('echoManagerGroup').value, image_ids:imageIds})}).then(r => r.json());
                if (!response.success) { alert(response.detail || response.message || 'Impossible d’enregistrer cet ordre.'); return; }
                await refreshEchoManagerData(currentTab);
                alert(response.message || 'Ordre enregistré.');
            };
            async function refreshEchoManagerData(currentTab) {
                const ref = await fetch(`/api/dme/record/${currentTab.patientId}/${currentTab.passageId}`).then(r => r.json());
                currentTab.data.echographies = ref.echographies || [];
                renderEchoGallery();
                window.renderEchoManager();
            }
            window.echoManagerPointerDown = function(event, imageId) {
                event.preventDefault(); event.stopPropagation(); echoManagerDragId = String(imageId);
                document.addEventListener('pointermove', window.echoManagerPointerMove);
                document.addEventListener('pointerup', window.echoManagerPointerUp, {once:true});
                document.addEventListener('pointercancel', window.echoManagerPointerUp, {once:true});
            };
            window.echoManagerPointerMove = function(event) {
                if (!echoManagerDragId) return;
                event.preventDefault();
                const grid = document.getElementById('echoManagerGrid');
                const gridRect = grid.getBoundingClientRect();
                const edgeSize = 72;
                if (event.clientY < gridRect.top + edgeSize) grid.scrollTop -= 28;
                else if (event.clientY > gridRect.bottom - edgeSize) grid.scrollTop += 28;
                const cards = [...grid.querySelectorAll('[data-echo-id]')];
                const source = cards.find(card => card.dataset.echoId === echoManagerDragId);
                const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('#echoManagerGrid [data-echo-id]');
                if (!source || !target || source === target) return;
                const rect = target.getBoundingClientRect();
                const sourceRect = source.getBoundingClientRect();
                const sameRow = Math.abs(rect.top - sourceRect.top) < rect.height / 2;
                const after = sameRow ? event.clientX > rect.left + rect.width / 2 : event.clientY > rect.top + rect.height / 2;
                grid.insertBefore(source, after ? target.nextElementSibling : target);
            };
            window.moveEchoManagerCard = function(imageId, direction) {
                const grid = document.getElementById('echoManagerGrid');
                const cards = [...grid.querySelectorAll('[data-echo-id]')];
                const card = cards.find(item => item.dataset.echoId === String(imageId));
                if (!card || cards.length < 2) return;
                const index = cards.indexOf(card);
                if (direction === 'first') grid.insertBefore(card, cards[0]);
                else if (direction === 'last') grid.appendChild(card);
                else if (direction === 'up' && index > 0) grid.insertBefore(card, cards[index - 1]);
                else if (direction === 'down' && index < cards.length - 1) grid.insertBefore(cards[index + 1], card);
                window.refreshEchoManagerPositions();
            };
            window.refreshEchoManagerPositions = function() {
                document.querySelectorAll('#echoManagerGrid [data-echo-id]').forEach((card, index) => {
                    const badge = card.querySelector('[data-echo-position]');
                    if (badge) { badge.dataset.echoPosition = String(index + 1); badge.textContent = String(index + 1); }
                });
            };
            window.openEchoManagerPositionMenu = function(event, imageId) {
                event.preventDefault(); event.stopPropagation();
                const menu = document.getElementById('echoManagerPositionMenu');
                const cards = [...document.querySelectorAll('#echoManagerGrid [data-echo-id]')];
                if (!menu || !cards.length) return;
                menu.innerHTML = `<div class="sticky top-0 px-2 py-1.5 text-[10px] font-bold uppercase tracking-wide text-slate-500 bg-white dark:bg-slate-900">Déplacer vers</div>` + cards.map((card, index) => {
                    const selected = card.dataset.echoId === String(imageId);
                    return `<button type="button" onclick="setEchoManagerPosition(${Number(imageId)}, ${index + 1})" class="block w-full rounded-lg px-2 py-1.5 text-left text-xs font-semibold ${selected ? 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/50 dark:text-indigo-200' : 'text-slate-700 hover:bg-indigo-50 dark:text-slate-200 dark:hover:bg-white/10'}">Image ${index + 1}${selected ? ' · actuelle' : ''}</button>`;
                }).join('');
                menu.classList.remove('hidden');
                const rect = event.currentTarget.getBoundingClientRect();
                const width = 144, height = Math.min(256, cards.length * 36 + 40);
                menu.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`;
                menu.style.top = `${Math.max(8, Math.min(rect.bottom + 4, window.innerHeight - height - 8))}px`;
            };
            window.setEchoManagerPosition = function(imageId, position) {
                const grid = document.getElementById('echoManagerGrid');
                const cards = [...grid.querySelectorAll('[data-echo-id]')];
                const card = cards.find(item => item.dataset.echoId === String(imageId));
                const targetIndex = Math.max(0, Math.min(cards.length - 1, Number(position) - 1));
                if (card && cards.length) {
                    const withoutCard = cards.filter(item => item !== card);
                    const reference = withoutCard[targetIndex] || null;
                    grid.insertBefore(card, reference);
                    window.refreshEchoManagerPositions();
                }
                document.getElementById('echoManagerPositionMenu')?.classList.add('hidden');
            };
            document.addEventListener('click', function(event) {
                const menu = document.getElementById('echoManagerPositionMenu');
                if (menu && !menu.classList.contains('hidden') && !menu.contains(event.target) && !event.target.closest('[data-echo-position]')) menu.classList.add('hidden');
            });
            window.echoManagerPointerUp = function() {
                document.removeEventListener('pointermove', window.echoManagerPointerMove);
                document.removeEventListener('pointerup', window.echoManagerPointerUp);
                document.removeEventListener('pointercancel', window.echoManagerPointerUp);
                echoManagerDragId = null;
                window.refreshEchoManagerPositions();
            };

            window.handleEchoFilesUpload = async function(event) {
                const incomingFiles = Array.from(event.target.files || []);
                if (appendNextEchoUpload) pendingEchoUploadFiles = [...pendingEchoUploadFiles, ...incomingFiles];
                else {
                    pendingEchoUploadFiles = incomingFiles;
                    pendingEchoUploadCanAddDprint = false;
                }
                appendNextEchoUpload = false;
                if (!incomingFiles.length && !pendingEchoUploadFiles.length) return;
                pendingEchoUploadPreviewUrls.forEach(url => URL.revokeObjectURL(url));
                pendingEchoUploadPreviewUrls = pendingEchoUploadFiles.map(file => URL.createObjectURL(file));
                const list = document.getElementById('echoUploadConfirmFiles');
                list.innerHTML = pendingEchoUploadFiles.map((file, index) => `<label class="group relative flex cursor-pointer flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm transition hover:border-emerald-400 hover:shadow-md dark:border-white/10 dark:bg-[#121c15]"><span class="relative grid h-36 place-items-center overflow-hidden bg-slate-900 sm:h-40"><img src="${pendingEchoUploadPreviewUrls[index]}" alt="Aperçu de ${echoManagerEscape(file.name)}" loading="lazy" class="h-full w-full object-contain"><span class="absolute left-2 top-2"><input type="checkbox" data-echo-upload-index="${index}" checked class="h-5 w-5 cursor-pointer rounded accent-emerald-600" aria-label="Importer ${echoManagerEscape(file.name)}"></span></span><span class="min-w-0 p-2.5"><span class="block truncate text-[10px] font-bold text-slate-700 dark:text-slate-200" title="${echoManagerEscape(file.name)}">${echoManagerEscape(file.name)}</span><span class="mt-1 block text-[10px] text-slate-400">${(file.size / 1024).toFixed(0)} Ko</span></span></label>`).join('');
                list.onchange = window.updateEchoUploadSelection;
                window.updateEchoUploadSelection();
                document.getElementById('btnAddMoreDprintImages')?.classList.toggle('hidden', !pendingEchoUploadCanAddDprint);
                document.getElementById('echoUploadConfirmModal').classList.remove('hidden');
                document.getElementById('echoUploadConfirmModal').classList.add('flex');
                event.target.value = '';
            };

            window.updateEchoUploadSelection = function() {
                const list = document.getElementById('echoUploadConfirmFiles');
                if (!list) return;
                const selected = list.querySelectorAll('[data-echo-upload-index]:checked').length;
                const total = pendingEchoUploadFiles.length;
                const count = document.getElementById('echoUploadConfirmCount');
                if (count) count.textContent = `${selected} image(s) sélectionnée(s) sur ${total}. Cochez les images à importer et vérifiez le type et la catégorie.`;
                const button = document.getElementById('btnConfirmEchoUpload');
                if (button) button.disabled = selected === 0;
            };

            window.setEchoUploadSelection = function(checked) {
                document.querySelectorAll('#echoUploadConfirmFiles [data-echo-upload-index]').forEach(input => { input.checked = Boolean(checked); });
                window.updateEchoUploadSelection();
            };

            window._clinivaDmePrepareDprintAppend = function() {
                appendNextEchoUpload = true;
                pendingEchoUploadCanAddDprint = true;
            };

            window.addMoreDprintImagesToEchoUpload = function() {
                const selectedIndexes = [...document.querySelectorAll('#echoUploadConfirmFiles [data-echo-upload-index]:checked')].map(input => Number(input.dataset.echoUploadIndex));
                pendingEchoUploadFiles = selectedIndexes.map(index => pendingEchoUploadFiles[index]).filter(Boolean);
                pendingEchoUploadPreviewUrls.forEach(url => URL.revokeObjectURL(url));
                pendingEchoUploadPreviewUrls = [];
                appendNextEchoUpload = true;
                pendingEchoUploadCanAddDprint = true;
                const modal = document.getElementById('echoUploadConfirmModal');
                modal.classList.add('hidden');
                modal.classList.remove('flex');
                window.openDmeDicomPrint();
            };

            window.cancelEchoUploadConfirmation = function() {
                pendingEchoUploadFiles = [];
                pendingEchoUploadPreviewUrls.forEach(url => URL.revokeObjectURL(url));
                pendingEchoUploadPreviewUrls = [];
                appendNextEchoUpload = false;
                pendingEchoUploadCanAddDprint = false;
                pendingEchoUploadTargetGroupId = null;
                document.getElementById('btnAddMoreDprintImages')?.classList.add('hidden');
                const list = document.getElementById('echoUploadConfirmFiles');
                if (list) list.innerHTML = '';
                const modal = document.getElementById('echoUploadConfirmModal');
                modal.classList.add('hidden');
                modal.classList.remove('flex');
            };

            window.confirmEchoFilesUpload = async function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const selectedIndexes = [...document.querySelectorAll('#echoUploadConfirmFiles [data-echo-upload-index]:checked')].map(input => Number(input.dataset.echoUploadIndex));
                const selectedFiles = selectedIndexes.map(index => pendingEchoUploadFiles[index]).filter(Boolean);
                if (!currentTab || !selectedFiles.length) return;
                const button = document.getElementById('btnConfirmEchoUpload');
                const originalText = button.innerHTML;
                button.disabled = true;
                button.innerHTML = '<i class="ph-bold ph-spinner animate-spin mr-1"></i>Importation…';
                const formData = new FormData();
                formData.append('type_image', document.getElementById('echoUploadConfirmType').value);
                formData.append('categorie', document.getElementById('echoUploadConfirmCategory').value);
                if (pendingEchoUploadTargetGroupId) formData.append('groupe_id', pendingEchoUploadTargetGroupId);
                selectedFiles.forEach(file => formData.append('files', file));
                try {
                    const url = `/api/dme/echographies/upload?id_patient=${currentTab.patientId}&id_passage=${currentTab.passageId}`;
                    const response = await fetch(url, {method: 'POST', body: formData});
                    const result = await response.json();
                    if (!response.ok || !result.success) throw new Error(result.detail || result.message || 'Importation impossible.');
                    pendingEchoUploadFiles = [];
                    pendingEchoUploadTargetGroupId = null;
                    window.cancelEchoUploadConfirmation();
                    const ref = await fetch(`/api/dme/record/${currentTab.patientId}/${currentTab.passageId}`).then(r => r.json());
                    currentTab.data.echographies = ref.echographies || [];
                    renderEchoGallery();
                    if (!document.getElementById('echoManagerModal')?.classList.contains('hidden')) window.renderEchoManager();
                    alert('✅ ' + result.message);
                } catch (error) {
                    alert(error.message || 'Erreur lors de l’importation des images.');
                } finally {
                    button.disabled = false;
                    button.innerHTML = originalText;
                }
            };

            window.deleteEchographie = async function(echoId) {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!confirm("Voulez-vous supprimer ce cliché échographique ?")) return;

                try {
                    const res = await fetch('/api/dme/echographies/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id: echoId })
                    }).then(r => r.json());

                    if (res.success && currentTab) {
                        const ref = await fetch(`/api/dme/record/${currentTab.patientId}/${currentTab.passageId}`).then(r => r.json());
                        currentTab.data.echographies = ref.echographies || [];
                        renderEchoGallery();
                        if (!document.getElementById('echoManagerModal')?.classList.contains('hidden')) window.renderEchoManager();
                    }
                } catch(e) {
                    alert("Erreur de suppression.");
                }
            };

            window.deleteEchoImageGroup = async function(groupId, category) {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab || currentTab.mode !== 'execution' || currentTab.data?.dme?.verrouille) {
                    alert('La suppression est disponible uniquement dans une consultation ouverte en modification.');
                    return;
                }
                const groupImages = (currentTab.data?.echographies || []).filter(image =>
                    echoImageGroupId(image) === groupId
                );
                if (!groupImages.length) return;
                if (!confirm(`Supprimer le groupe « ${category} » et ses ${groupImages.length} image(s) de ce passage, quel que soit leur type ? Cette action est définitive.`)) return;

                try {
                    const response = await fetch('/api/dme/echographies/delete-group', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({id_patient: currentTab.patientId, id_passage: currentTab.passageId, groupe_id: groupId})
                    });
                    const result = await response.json();
                    if (!response.ok || !result.success) throw new Error(result.detail || result.message || 'La suppression du groupe a échoué.');
                    const refreshed = await fetch(`/api/dme/record/${currentTab.patientId}/${currentTab.passageId}`).then(r => r.json());
                    currentTab.data.echographies = refreshed.echographies || [];
                    renderEchoGallery();
                    if (!document.getElementById('echoManagerModal')?.classList.contains('hidden')) window.renderEchoManager();
                } catch (error) {
                    alert(error.message || 'Impossible de supprimer ce groupe.');
                }
            };

            // CARROUSEL INTERACTIF PLEIN ÉCRAN
            window.startEchoCarousel = function() {
                if (activeCarouselImages.length === 0) {
                    alert("Aucun cliché disponible pour le carrousel.");
                    return;
                }
                openEchoCarouselAtIndex(0);
            };

            window.openEchoCarouselAtIndex = function(idx) {
                if (!activeCarouselImages || activeCarouselImages.length === 0) return;
                currentCarouselIndex = (idx >= 0 && idx < activeCarouselImages.length) ? idx : 0;
                updateCarouselDisplay();

                const modal = document.getElementById('echoCarouselModal');
                if (modal) modal.classList.remove('hidden');

                // Écouteur clavier flèches gauche / droite
                window.onkeydown = function(e) {
                    if (e.key === "ArrowLeft") prevCarouselImage();
                    if (e.key === "ArrowRight") nextCarouselImage();
                    if (e.key === "Escape") closeEchoCarousel();
                };
            };

            window.printEchoCarouselImageA3 = async function() {
                const item = activeCarouselImages[currentCarouselIndex];
                const button = document.getElementById('carouselPrintA3Button');
                const status = document.getElementById('carouselPrintStatus');
                if (!item || !button || !status) return;
                button.disabled = true;
                status.className = 'w-full rounded-xl bg-white/10 px-3 py-2 text-center text-xs font-semibold text-white';
                status.textContent = 'Préparation de l’image et envoi à l’imprimante A3 du serveur…';
                try {
                    const imageResponse = await fetch(`/api/dme/echographies/file/${item.id}`);
                    if (!imageResponse.ok) throw new Error('Impossible de charger cette image pour l’impression.');
                    const imageBlob = await imageResponse.blob();
                    const imageData = await new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onload = () => resolve(reader.result);
                        reader.onerror = () => reject(new Error('Lecture de l’image impossible.'));
                        reader.readAsDataURL(imageBlob);
                    });
                    const response = await fetch('/api/serveur-impression/dicom-print/a3', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({image_base64: imageData, file_name: item.nom_fichier || 'Image_DME'})
                    });
                    const result = await response.json().catch(() => ({}));
                    if (!response.ok || !result.success) throw new Error(result.detail || 'Échec de l’impression A3.');
                    if (result.download_url) {
                        const link = document.createElement('a'); link.href = result.download_url; link.download = ''; document.body.appendChild(link); link.click(); link.remove();
                    }
                    status.textContent = result.message || `Image envoyée à ${result.printer || 'l’imprimante A3 du serveur'}.`;
                    status.className = 'w-full rounded-xl bg-emerald-600/90 px-3 py-2 text-center text-xs font-semibold text-white';
                } catch (error) {
                    status.textContent = error.message || 'Impossible d’imprimer cette image en A3.';
                    status.className = 'w-full rounded-xl bg-rose-600/90 px-3 py-2 text-center text-xs font-semibold text-white';
                } finally {
                    status.classList.remove('hidden');
                    button.disabled = false;
                }
            };

            function updateCarouselDisplay() {
                const item = activeCarouselImages[currentCarouselIndex];
                if (!item) return;

                document.getElementById('carouselMainImg').src = `/api/dme/echographies/file/${item.id}`;
                document.getElementById('carouselTitle').innerText = `${item.nom_fichier || 'Cliché'} (${item.date_heure_str || ''})`;
                document.getElementById('carouselCounter').innerText = `${currentCarouselIndex + 1} / ${activeCarouselImages.length}`;

                // Rendu du ruban miniature
                const ribbon = document.getElementById('carouselThumbnailsRibbon');
                if (ribbon) {
                    ribbon.innerHTML = activeCarouselImages.map((ec, i) => `
                        <img src="/api/dme/echographies/file/${ec.id}" 
                             onclick="openEchoCarouselAtIndex(${i})" 
                             class="h-12 w-16 object-cover rounded-lg cursor-pointer border-2 transition ${i === currentCarouselIndex ? 'border-brand-neon scale-105' : 'border-transparent opacity-60 hover:opacity-100'}">
                    `).join('');
                }
            }

            window.prevCarouselImage = function() {
                if (activeCarouselImages.length === 0) return;
                currentCarouselIndex = (currentCarouselIndex - 1 + activeCarouselImages.length) % activeCarouselImages.length;
                updateCarouselDisplay();
            };

            window.nextCarouselImage = function() {
                if (activeCarouselImages.length === 0) return;
                currentCarouselIndex = (currentCarouselIndex + 1) % activeCarouselImages.length;
                updateCarouselDisplay();
            };

            window.closeEchoCarousel = function() {
                const modal = document.getElementById('echoCarouselModal');
                if (modal) modal.classList.add('hidden');
                window.onkeydown = null;
            };

            // =================================================================
            // 7. FONCTIONS GÉNÉRALES DU DME
            // =================================================================
            window.calculateImc = function() {
                const poids = parseFloat(document.getElementById('dme_poids')?.value || 0);
                const taille = parseFloat(document.getElementById('dme_taille')?.value || 0);
                const imcDisplay = document.getElementById('dme_imc');

                if (poids > 0 && taille > 0) {
                    const tailleM = taille / 100;
                    const imc = (poids / (tailleM * tailleM)).toFixed(1);
                    if (imcDisplay) imcDisplay.value = imc;
                } else if (imcDisplay) {
                    imcDisplay.value = '';
                }
                const growthWeight = document.getElementById('dme_growth_weight');
                const growthHeight = document.getElementById('dme_growth_height');
                if (growthWeight && document.activeElement !== growthWeight) growthWeight.value = document.getElementById('dme_poids')?.value || '';
                if (growthHeight && document.activeElement !== growthHeight) growthHeight.value = document.getElementById('dme_taille')?.value || '';
                if (typeof window.renderDmeGrowthChart === 'function') window.renderDmeGrowthChart();
            };

            window.searchDmePrestationsLive = function() {
                const q = (document.getElementById('dmePrestationSearchInput')?.value || '').toLowerCase().trim();
                const dropdown = document.getElementById('dmePrestationSearchResultsTable');
                if (!dropdown) return;

                if (!q) {
                    dropdown.classList.add('hidden');
                    return;
                }

                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const consultations = (currentTab && currentTab.data && currentTab.data.consultations) ? currentTab.data.consultations : [];

                currentMatchingDmePrestations = consultations.filter(c => 
                    (c.designation && c.designation.toLowerCase().includes(q)) || 
                    (c.categorie && c.categorie.toLowerCase().includes(q)) ||
                    (c.specialite && c.specialite.toLowerCase().includes(q))
                ).map(c => ({
                    nom: c.designation,
                    categorie: c.categorie,
                    prix_jour: parseFloat(c.tarif || 0),
                    prix_nuit: parseFloat(c.tarif_nuit || c.tarif || 0)
                }));

                if (currentMatchingDmePrestations.length === 0) {
                    dropdown.innerHTML = '<div class="p-3 text-xs text-slate-400 italic text-center">Aucune prestation trouvée.</div>';
                } else {
                    dropdown.innerHTML = currentMatchingDmePrestations.map((r, i) => `
                        <div onclick="onSelectDmePrestationByIndex(${i})" class="p-2 hover:bg-brand-neon/10 flex items-center justify-between cursor-pointer text-xs">
                            <span class="font-bold text-slate-800 dark:text-slate-100">${r.nom}</span>
                            <div class="space-x-2 font-mono">
                                <span class="text-emerald-600 dark:text-brand-neon font-bold">☀️ ${r.prix_jour} DA</span>
                                <span class="text-purple-600 dark:text-purple-400 font-bold">🌙 ${r.prix_nuit} DA</span>
                            </div>
                        </div>
                    `).join('');
                }
                dropdown.classList.remove('hidden');
            };

            window.onSelectDmePrestationByIndex = function(index) {
                selectedDmePrestationTemp = currentMatchingDmePrestations[index];
                if (!selectedDmePrestationTemp) return;

                document.getElementById('dmePrestationSearchInput').value = selectedDmePrestationTemp.nom;
                document.getElementById('dmePrestationSearchResultsTable').classList.add('hidden');
            };

            window.autoDetectDmeTarifPeriod = function() {
                const now = new Date();
                const h = now.getHours();
                const isNight = (h >= 20 || h < 8);

                const selectPeriode = document.getElementById('dmeSelectAddPeriode');
                if (selectPeriode) selectPeriode.value = isNight ? 'Garde' : 'Jour';

                const badge = document.getElementById('dmeAutoTarifBadge');
                if (badge) {
                    badge.innerText = isNight ? "🌙 Période Garde/Nuit active" : "☀️ Période Jour active";
                    badge.className = isNight 
                        ? "text-[10px] font-bold px-2.5 py-0.5 rounded-full bg-purple-100 dark:bg-purple-950/40 text-purple-800 dark:text-purple-300" 
                        : "text-[10px] font-bold px-2.5 py-0.5 rounded-full bg-amber-100 dark:bg-amber-950/40 text-amber-800 dark:text-amber-300";
                }
            };

            window.addSelectedDmePrestationToTable = function() {
                if (!selectedDmePrestationTemp) {
                    alert("Veuillez d'abord rechercher et sélectionner une prestation.");
                    return;
                }

                const periode = document.getElementById('dmeSelectAddPeriode').value;
                const quantite = parseInt(document.getElementById('dmeInputAddQuantite').value) || 1;
                const tarif = (periode === 'Jour') ? selectedDmePrestationTemp.prix_jour : selectedDmePrestationTemp.prix_nuit;

                currentDmeActs.push({
                    nom: selectedDmePrestationTemp.nom,
                    periode: periode,
                    quantite: quantite,
                    tarif: tarif,
                    sous_total: tarif * quantite
                });

                renderDmeActsTable();
                document.getElementById('dmePrestationSearchInput').value = '';
                selectedDmePrestationTemp = null;
            };

            window.removeDmeActFromTable = function(index) {
                currentDmeActs.splice(index, 1);
                renderDmeActsTable();
            };

            function renderDmePassagePrestations(acts, fallbackType) {
                const container = document.getElementById('dme_hdr_prestations');
                if (!container) return;
                container.replaceChildren();
                const names = (Array.isArray(acts) ? acts : [])
                    .filter(act => act && typeof act === 'object')
                    .map(act => ({
                        name: String(act.nom || act.designation || act.name || '').trim(),
                        quantity: Number(act.quantite || act.quantity || 1)
                    }))
                    .filter(act => act.name);

                if (!names.length && fallbackType) names.push({ name: String(fallbackType).trim(), quantity: 1 });
                if (!names.length) {
                    const empty = document.createElement('span');
                    empty.className = 'text-[11px] text-slate-400 italic';
                    empty.textContent = 'Aucune prestation renseignée';
                    container.appendChild(empty);
                    return;
                }

                names.forEach(act => {
                    const badge = document.createElement('span');
                    badge.className = 'px-2.5 py-1 rounded-lg bg-brand-neon/15 border border-brand-neon/30 text-[11px] font-bold text-emerald-800 dark:text-brand-neon';
                    badge.textContent = `${act.name}${act.quantity > 1 ? ` × ${act.quantity}` : ''}`;
                    container.appendChild(badge);
                });
            }

            window.renderDmeActsTable = function() {
                const tbody = document.getElementById('dmeActsTableBody');
                if (!tbody) return;
                renderDmePassagePrestations(currentDmeActs);
                tbody.innerHTML = '';

                let grandTotal = 0;
                currentDmeActs.forEach((act, idx) => {
                    grandTotal += act.sous_total;
                    const tr = document.createElement('tr');
                    tr.className = "hover:bg-slate-50 dark:hover:bg-white/[0.02]";
                    tr.innerHTML = `
                        <td class="p-2.5 font-bold text-slate-800 dark:text-slate-100">${act.nom}</td>
                        <td class="p-2.5"><span class="px-2 py-0.5 rounded text-[10px] font-bold ${act.periode === 'Jour' ? 'bg-amber-100 text-amber-800' : 'bg-purple-100 text-purple-800'}">${act.periode}</span></td>
                        <td class="p-2.5 text-center font-bold">${act.quantite}</td>
                        <td class="p-2.5 font-mono">${Number(act.tarif).toLocaleString()} DA</td>
                        <td class="p-2.5 font-black text-emerald-600 dark:text-brand-neon font-mono">${Number(act.sous_total).toLocaleString()} DA</td>
                        <td class="p-2.5 text-right"><button type="button" onclick="removeDmeActFromTable(${idx})" class="text-rose-500 hover:text-rose-700 font-bold p-1">✕</button></td>
                    `;
                    tbody.appendChild(tr);
                });

                if (currentDmeActs.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" class="p-3 text-center text-slate-400 italic">Aucune prestation en cours de consultation.</td></tr>';
                }

                const totalDisplay = document.getElementById('dmeTotalActsDisplay');
                if (totalDisplay) totalDisplay.innerText = `${Number(grandTotal).toLocaleString()} DA`;
            };

            async function loadMedsForPrescription() {
                if (allMedicationsList.length === 0) {
                    try {
                        const res = await fetch('/api/medications');
                        if (!res.ok) throw new Error(`Erreur HTTP ${res.status}`);
                        const data = await res.json();
                        allMedicationsList = data.medications || [];
                    } catch(e) {
                        console.error("Erreur chargement médicaments :", e);
                    }
                }
            }

            window.openDmeMedicationModal = function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab || currentTab.mode !== 'execution' || currentTab.data?.dme?.verrouille) {
                    alert("L'ajout d'un médicament est disponible pendant l'exécution d'une consultation.");
                    return;
                }
                const form = document.getElementById('dmeMedicationForm');
                const error = document.getElementById('dmeMedicationError');
                const modal = document.getElementById('dmeAddMedicationModal');
                if (form) form.reset();
                if (error) { error.textContent = ''; error.classList.add('hidden'); }
                if (modal) { modal.classList.remove('hidden'); modal.classList.add('flex'); }
                document.getElementById('dmeMedicationName')?.focus();
            };

            window.closeDmeMedicationModal = function() {
                const modal = document.getElementById('dmeAddMedicationModal');
                if (modal) { modal.classList.add('hidden'); modal.classList.remove('flex'); }
            };

            window.saveDmeMedication = async function(event) {
                event.preventDefault();
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab || currentTab.mode !== 'execution' || currentTab.data?.dme?.verrouille) {
                    alert("La consultation est en lecture seule : aucun médicament ne peut être ajouté depuis ce dossier.");
                    closeDmeMedicationModal();
                    return;
                }

                const payload = {
                    id: null,
                    nom_commercial: document.getElementById('dmeMedicationName')?.value.trim() || '',
                    dci: document.getElementById('dmeMedicationDci')?.value.trim() || '',
                    forme: document.getElementById('dmeMedicationForm')?.value.trim() || '',
                    dosage: document.getElementById('dmeMedicationDosage')?.value.trim() || ''
                };
                const submitButton = document.getElementById('dmeMedicationSubmitButton');
                const errorBox = document.getElementById('dmeMedicationError');
                if (submitButton) submitButton.disabled = true;
                if (errorBox) { errorBox.textContent = ''; errorBox.classList.add('hidden'); }

                try {
                    const response = await fetch('/api/medications/save', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                    const result = await response.json();
                    if (!response.ok || !result.success) throw new Error(result.message || result.detail || 'Impossible d’enregistrer le médicament.');

                    let savedMedication = payload;
                    try {
                        const lookupResponse = await fetch(`/api/medications?q=${encodeURIComponent(payload.nom_commercial)}`);
                        if (lookupResponse.ok) {
                            const lookupData = await lookupResponse.json();
                            savedMedication = (lookupData.medications || []).find(medication =>
                                (medication.nom_commercial || '').trim().toLocaleUpperCase('fr') === payload.nom_commercial.toLocaleUpperCase('fr') &&
                                (medication.dci || '').trim().toLocaleLowerCase('fr') === payload.dci.toLocaleLowerCase('fr') &&
                                (medication.forme || '').trim().toLocaleLowerCase('fr') === payload.forme.toLocaleLowerCase('fr') &&
                                (medication.dosage || '').trim().toLocaleLowerCase('fr') === payload.dosage.toLocaleLowerCase('fr')
                            ) || payload;
                        }
                    } catch (lookupError) {
                        console.warn('Médicament enregistré; utilisation des données saisies pour la prescription :', lookupError);
                    }
                    const sameMedication = medication =>
                        (medication.nom_commercial || '').trim().toLocaleUpperCase('fr') === payload.nom_commercial.toLocaleUpperCase('fr') &&
                        (medication.dci || '').trim().toLocaleLowerCase('fr') === payload.dci.toLocaleLowerCase('fr') &&
                        (medication.forme || '').trim().toLocaleLowerCase('fr') === payload.forme.toLocaleLowerCase('fr') &&
                        (medication.dosage || '').trim().toLocaleLowerCase('fr') === payload.dosage.toLocaleLowerCase('fr');
                    const existingIndex = allMedicationsList.findIndex(sameMedication);
                    if (existingIndex >= 0) allMedicationsList[existingIndex] = savedMedication;
                    else allMedicationsList.push(savedMedication);

                    closeDmeMedicationModal();
                    window.selectOrdMedication(savedMedication.nom_commercial, savedMedication.dosage || '', savedMedication.forme || '');
                    if (typeof window.showToast === 'function') window.showToast('Médicament ajouté et sélectionné pour l’ordonnance.', 'success');
                } catch (error) {
                    if (errorBox) {
                        errorBox.textContent = error.message || 'Erreur lors de l’enregistrement du médicament.';
                        errorBox.classList.remove('hidden');
                    } else {
                        alert(error.message || 'Erreur lors de l’enregistrement du médicament.');
                    }
                } finally {
                    if (submitButton) submitButton.disabled = false;
                }
            };

            window.searchOrdMedicationsLive = function() {
                const q = (document.getElementById('ordMedSearchInput')?.value || '').toLowerCase().trim();
                const dropdown = document.getElementById('ordMedSearchResults');
                if (!dropdown) return;

                if (!q) {
                    dropdown.classList.add('hidden');
                    return;
                }

                const matches = allMedicationsList.filter(m => 
                    (m.nom_commercial && m.nom_commercial.toLowerCase().includes(q)) ||
                    (m.dci && m.dci.toLowerCase().includes(q))
                ).slice(0, 10);

                if (matches.length === 0) {
                    dropdown.innerHTML = '<div class="p-2.5 text-xs text-slate-400 italic">Aucun médicament trouvé.</div>';
                } else {
                    dropdown.innerHTML = matches.map(m => `
                        <div onclick="selectOrdMedication('${m.nom_commercial.replace(/'/g, "\\'")}', '${(m.dosage || '').replace(/'/g, "\\'")}', '${(m.forme || '').replace(/'/g, "\\'")}')" class="p-2.5 hover:bg-brand-neon/10 cursor-pointer text-xs flex justify-between items-center">
                            <span class="font-bold text-slate-800 dark:text-slate-100">${m.nom_commercial} <span class="text-slate-400 font-normal">(${m.forme || ''} ${m.dosage || ''})</span></span>
                            <span class="text-[10px] text-emerald-600 font-mono">${m.dci}</span>
                        </div>
                    `).join('');
                }
                dropdown.classList.remove('hidden');
            };

            window.selectOrdMedication = function(nom, dosage, forme) {
                const label = `${nom} ${dosage} ${forme}`.trim();
                document.getElementById('ordMedSearchInput').value = label;
                document.getElementById('ordMedSearchResults').classList.add('hidden');
                document.getElementById('ordPosologieInput').focus();
            };

            window.addMedicationToPrescription = function() {
                const med = document.getElementById('ordMedSearchInput')?.value.trim();
                const poso = document.getElementById('ordPosologieInput')?.value.trim();
                const duree = document.getElementById('ordDureeInput')?.value.trim();
                const qte = parseInt(document.getElementById('ordQteInput')?.value) || 1;

                if (!med) {
                    alert("Veuillez choisir un médicament.");
                    return;
                }

                currentOrdLines.push({
                    medicament: med,
                    posologie: poso || "Selon prescription",
                    duree: duree || "-",
                    quantite: qte
                });

                document.getElementById('ordMedSearchInput').value = '';
                document.getElementById('ordPosologieInput').value = '';
                document.getElementById('ordDureeInput').value = '';
                document.getElementById('ordQteInput').value = '1';

                renderActiveOrdLines();
            };

            window.removeOrdLine = function(idx) {
                currentOrdLines.splice(idx, 1);
                renderActiveOrdLines();
            };

            function renderActiveOrdLines() {
                const tbody = document.getElementById('ordActiveLinesTableBody');
                if (!tbody) return;
                tbody.innerHTML = '';

                if (currentOrdLines.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="5" class="py-6 text-center text-slate-400 italic">Aucun médicament ajouté à cette ordonnance.</td></tr>';
                    return;
                }

                currentOrdLines.forEach((l, idx) => {
                    const tr = document.createElement('tr');
                    tr.className = "hover:bg-slate-50 dark:hover:bg-white/[0.02]";
                    tr.innerHTML = `
                        <td class="py-2.5 font-bold text-slate-800 dark:text-slate-100">${l.medicament}</td>
                        <td class="py-2.5 text-slate-600 dark:text-slate-300">${l.posologie}</td>
                        <td class="py-2.5 text-slate-600 dark:text-slate-300">${l.duree}</td>
                        <td class="py-2.5 text-center font-black text-emerald-600 dark:text-brand-neon font-mono">${l.quantite}</td>
                        <td class="py-2.5 text-right pr-2">
                            <button type="button" onclick="removeOrdLine(${idx})" class="text-rose-500 hover:text-rose-700 font-bold p-1">✕</button>
                        </td>
                    `;
                    tbody.appendChild(tr);
                });
            }

            window.saveActivePrescriptionOnly = async function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab || currentOrdLines.length === 0) {
                    alert("Veuillez ajouter au moins un médicament dans l'ordonnance.");
                    return;
                }

                const payload = {
                    id_patient: currentTab.patientId,
                    id_passage: currentTab.passageId,
                    id_consultation: currentTab.consultationId || currentTab.data?.dme?.id,
                    lignes: currentOrdLines
                };

                try {
                    const res = await fetch('/api/dme/prescriptions/save', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    }).then(r => r.json());

                    if (res.success && res.ord_id) {
                        alert("✅ " + res.message);
                        currentOrdLines = [];
                        renderActiveOrdLines();
                        await loadPatientPrescriptionsHistory(currentTab.patientId, currentTab.passageId, currentTab.consultationId || currentTab.data?.dme?.id || 0);
                    } else {
                        alert(res.message);
                    }
                } catch(e) {
                    alert("Erreur lors de l'enregistrement de l'ordonnance.");
                }
            };

            window.saveAndPrintActivePrescription = async function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab || currentOrdLines.length === 0) {
                    alert("Veuillez ajouter au moins un médicament avant de générer l'ordonnance.");
                    return;
                }

                const payload = {
                    id_patient: currentTab.patientId,
                    id_passage: currentTab.passageId,
                    id_consultation: currentTab.consultationId || currentTab.data?.dme?.id,
                    lignes: currentOrdLines
                };

                try {
                    const res = await fetch('/api/dme/prescriptions/save', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    }).then(r => r.json());

                    if (res.success && res.ord_id) {
                        currentOrdLines = [];
                        renderActiveOrdLines();
                        await loadPatientPrescriptionsHistory(currentTab.patientId, currentTab.passageId, currentTab.consultationId || currentTab.data?.dme?.id || 0);
                        openPrescriptionModal(res.ord_id, currentTab.patientId, currentTab.passageId, currentTab.consultationId || currentTab.data?.dme?.id || 0);
                    } else {
                        alert(res.message);
                    }
                } catch(e) {
                    alert("Erreur lors de la création de l'ordonnance.");
                }
            };

            window.openPrescriptionModal = function(ordId, patientId = null, passageId = null, consultationId = null) {
                document.getElementById('dmePrescriptionModalTitle').textContent = 'Ordonnance Médicale Officielle (Fond KACEL ORD.png)';
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const scopedPatientId = patientId ?? currentTab?.patientId;
                const scopedPassageId = passageId ?? currentTab?.passageId;
                const scopedConsultationId = consultationId ?? currentTab?.consultationId ?? currentTab?.data?.dme?.id ?? 0;
                if (!scopedPatientId || !scopedPassageId) {
                    alert('Impossible d’ouvrir cette ordonnance sans son patient et son passage.');
                    return;
                }
                const modal = document.getElementById('prescriptionModal');
                const iframe = document.getElementById('prescriptionIframe');
                if (iframe) iframe.src = `/api/dme/prescriptions/print/${scopedPatientId}/${scopedPassageId}/${Number(scopedConsultationId) || 0}/${ordId}`;
                if (modal) modal.classList.remove('hidden');
            };

            window.closePrescriptionModal = function() {
                const modal = document.getElementById('prescriptionModal');
                const iframe = document.getElementById('prescriptionIframe');
                if (iframe) iframe.src = 'about:blank';
                if (modal) modal.classList.add('hidden');
            };

            window.printPrescriptionIframe = function() {
                const iframe = document.getElementById('prescriptionIframe');
                if (iframe && iframe.contentWindow) {
                    iframe.contentWindow.focus();
                    iframe.contentWindow.print();
                }
            };

            async function loadPatientPrescriptionsHistory(patientId, passageId, consultationId) {
                const container = document.getElementById('ordHistoryCardsList');
                if (!container) return;

                try {
                    const res = await fetch(`/api/dme/prescriptions/${patientId}/${passageId}/${Number(consultationId) || 0}`).then(r => r.json());
                    const ords = res.prescriptions || [];

                    if (ords.length === 0) {
                        container.innerHTML = '<div class="text-xs text-slate-400 italic p-3">Aucune ordonnance antérieure pour ce patient.</div>';
                        return;
                    }

                    container.innerHTML = ords.map(o => {
                        const isSupprimee = o.statut === 'Supprimée';
                        const badgeStatut = isSupprimee
                            ? `<span class="px-2 py-0.5 rounded text-[10px] font-extrabold bg-rose-50 text-rose-600 border border-rose-200">SUPPRIMÉE</span> <span class="text-[10px] text-slate-400">(@${o.supprime_par || 'admin'} le ${o.date_suppression_str || ''})</span>`
                            : `<span class="px-2 py-0.5 rounded text-[10px] font-extrabold bg-emerald-50 text-emerald-800 border border-emerald-200">VALIDE</span>`;

                        const resumeMedicaments = (o.lignes || []).map(l => `<strong>${l.medicament}</strong> (${l.quantite} boîte)`).join(' • ');

                        return `
                        <div class="p-3.5 rounded-2xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                            <div class="space-y-1">
                                <div class="flex items-center gap-2 text-xs font-bold text-slate-800 dark:text-white">
                                    <span>#ORD-${String(o.id).padStart(4, '0')} du ${o.date_creation_str} — ${o.medecin_nom} (${o.medecin_specialite})</span>
                                    ${badgeStatut}
                                </div>
                                <div class="text-xs text-slate-600 dark:text-slate-300 leading-snug">
                                    ${resumeMedicaments || 'Aucun détail'}
                                </div>
                            </div>

                            <div class="flex items-center gap-2 flex-shrink-0">
                                <button type="button" onclick="openPrescriptionModal(${o.id}, ${patientId}, ${passageId}, ${Number(consultationId) || 0})" class="px-3.5 py-1.5 rounded-xl bg-[#0c1a10] hover:bg-[#1a3822] text-[#9fe870] font-bold text-xs inline-flex items-center gap-1.5 transition cursor-pointer">
                                    <i class="ph-bold ph-printer"></i>
                                    <span>Visualiser / Imprimer</span>
                                </button>
                                ${!isSupprimee ? `
                                <button type="button" onclick="deletePrescription(${o.id}, ${patientId}, ${passageId}, ${Number(consultationId) || 0})" class="p-2 text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-500/10 rounded-xl transition cursor-pointer" title="Annuler cette ordonnance">
                                    <i class="ph-bold ph-trash text-sm"></i>
                                </button>
                                ` : ''}
                            </div>
                        </div>
                        `;
                    }).join('');
                    const activeTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                    if (activeTab) applyDmeAccessMode(activeTab);
                } catch(e) {
                    console.error("Erreur historique ordonnances :", e);
                }
            }

            window.deletePrescription = async function(ordId, patientId, passageId, consultationId) {
                if (confirm("Voulez-vous marquer cette ordonnance comme SUPPRIMÉE ? Un filigrane d'annulation sera apposé.")) {
                    const response = await fetch('/api/dme/prescriptions/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id: ordId, id_patient: patientId, id_passage: passageId, id_consultation: Number(consultationId) || null })
                    });
                    const res = await response.json();

                    if (res.success) {
                        loadPatientPrescriptionsHistory(patientId, passageId, consultationId);
                    } else {
                        alert(res.message);
                    }
                }
            };

            // MODÈLES CR
            function normalizeDmeModalite(value) {
                return String(value || '').trim().normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase();
            }

            window.searchDmeCrTemplatesLive = function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const input = document.getElementById('crTemplateSearchInput');
                const results = document.getElementById('crTemplateSearchResults');
                if (!currentTab || !input || !results) return;

                const modalities = new Set((currentDmeActs || currentTab.data.passage?.actes || [])
                    .map(act => normalizeDmeModalite(act?.modalite))
                    .filter(Boolean));
                const selectedTemplate = (currentTab.data.cr_templates || []).find(item => Number(item.id) === Number(document.getElementById('crTemplateSelect')?.value));
                const selectedLabel = selectedTemplate ? `${selectedTemplate.designation} · ${selectedTemplate.modalite}` : '';
                const query = input.value.trim() === selectedLabel ? '' : input.value.trim().toLocaleLowerCase();
                const matchingTemplates = (currentTab.data.cr_templates || []).filter(template => {
                    const sameModalite = modalities.has(normalizeDmeModalite(template.modalite));
                    const matchesName = !query || String(template.designation || '').toLocaleLowerCase().includes(query);
                    return sameModalite && matchesName;
                });

                if (!modalities.size) {
                    results.innerHTML = '<div class="p-3 text-xs text-amber-700 dark:text-amber-300">Aucune modalité n’est renseignée sur les prestations de ce passage.</div>';
                } else if (!matchingTemplates.length) {
                    results.innerHTML = '<div class="p-3 text-xs text-slate-500">Aucun modèle ne correspond à cette recherche pour la modalité de la prestation.</div>';
                } else {
                    results.innerHTML = `
                        <table class="w-full text-left text-xs">
                            <thead class="sticky top-0 bg-slate-50 dark:bg-black/40 text-[10px] uppercase tracking-wide text-slate-500">
                                <tr><th class="px-3 py-2">Compte rendu</th><th class="px-3 py-2">Modalité</th><th class="px-3 py-2">Format</th></tr>
                            </thead>
                            <tbody class="divide-y divide-slate-100 dark:divide-white/5">
                                ${matchingTemplates.map(template => `
                                    <tr class="hover:bg-brand-neon/10 cursor-pointer" onclick="selectDmeCrTemplate(${Number(template.id)})">
                                        <td class="px-3 py-2.5 font-bold text-slate-800 dark:text-slate-100">${escapeDmeArchiveHtml(template.designation || 'Modèle sans nom')}</td>
                                        <td class="px-3 py-2.5 text-slate-600 dark:text-slate-300">${escapeDmeArchiveHtml(template.modalite || '')}</td>
                                        <td class="px-3 py-2.5 text-slate-500">${escapeDmeArchiveHtml(template.type_modele || '')}</td>
                                    </tr>
                                `).join('')}
                            </tbody>
                        </table>`;
                }
                results.classList.remove('hidden');
            };

            window.handleDmeCrTemplateSearchInput = function() {
                const selected = document.getElementById('crTemplateSelect');
                if (selected) selected.value = '';
                window.onCrTemplateChange();
                window.searchDmeCrTemplatesLive();
            };

            window.selectDmeCrTemplate = function(templateId) {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const template = (currentTab?.data.cr_templates || []).find(item => Number(item.id) === Number(templateId));
                if (!template) return;
                const select = document.getElementById('crTemplateSelect');
                const input = document.getElementById('crTemplateSearchInput');
                const results = document.getElementById('crTemplateSearchResults');
                if (select) select.value = String(template.id);
                if (input) input.value = `${template.designation} · ${template.modalite}`;
                if (results) results.classList.add('hidden');
                window.onCrTemplateChange();
            };

            window.onCrTemplateChange = function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                const select = document.getElementById('crTemplateSelect');
                const btnText = document.getElementById('crGenerateBtnText');
                if (!select || !btnText) return;

                const selectedTemplate = (currentTab?.data.cr_templates || []).find(item => Number(item.id) === Number(select.value));
                const format = selectedTemplate?.type_modele || 'Word';

                btnText.innerText = format === 'Word' ? "Générer Word" : "Générer HTML A4";
            };

            window.generateCrFromDme = async function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab) return;

                const templateSelect = document.getElementById('crTemplateSelect');
                const templateId = templateSelect ? parseInt(templateSelect.value) : null;
                const medPrescripteur = document.getElementById('crMedPrescripteurSelect')?.value || '';
                const medSignataire = document.getElementById('crMedSignataireSelect')?.value || '';

                if (!templateId) {
                    alert("Veuillez sélectionner un modèle de compte rendu.");
                    return;
                }

                const btn = document.getElementById('btnGenerateCr');
                const originalHtml = btn ? btn.innerHTML : '';
                if (btn) {
                    btn.disabled = true;
                    btn.innerHTML = '<i class="ph-bold ph-spinner animate-spin text-sm"></i> Génération...';
                }

                try {
                    const payload = {
                        id_patient: currentTab.patientId,
                        id_passage: currentTab.passageId,
                        template_id: templateId,
                        medecin_prescripteur: medPrescripteur,
                        medecin_signataire: medSignataire
                    };

                    const res = await fetch('/api/dme/cr/generate', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    }).then(r => r.json());

                    if (res.success) {
                        const ref = await fetch(`/api/dme/record/${currentTab.patientId}/${currentTab.passageId}`).then(r => r.json());
                        currentTab.data.archived_crs = ref.archived_crs || [];
                        renderDmeArchivedCrTable(currentTab.data.archived_crs);

                        if (res.type_modele === 'HTML' && res.record_id) {
                            openCrPreviewModal(res.record_id);
                        } else if (res.type_modele === 'Word') {
                            const opened = await openWordCrDocument(res.record_id);
                            if (opened) alert(`✅ Compte rendu Word généré : ${res.nom_fichier}\\nOuvert sur ce poste dans Microsoft Word.`);
                        }
                    } else {
                        alert(res.message);
                    }
                } catch(err) {
                    alert("Erreur lors de la génération du compte rendu : " + err.message);
                } finally {
                    if (btn) {
                        btn.disabled = false;
                        btn.innerHTML = originalHtml;
                    }
                }
            };

            window.openWordCrDocument = async function(recordId) {
                try {
                    const bridge = window.pywebview?.api;
                    if (bridge?.get_deployment_mode && await bridge.get_deployment_mode() === 'local') {
                        const serverResponse = await fetch('/api/dme/cr/open-word-server', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            credentials: 'same-origin',
                            body: JSON.stringify({ id: Number(recordId) })
                        });
                        const serverResult = await serverResponse.json().catch(() => ({}));
                        if (!serverResponse.ok || !serverResult.success) {
                            throw new Error(serverResult.detail || serverResult.message || 'Impossible d’ouvrir le compte rendu sur le serveur.');
                        }
                        if (typeof window.showToast === 'function') window.showToast('Le compte rendu original est ouvert sur le serveur.', 'success');
                        return true;
                    }

                    const response = await fetch(`/api/dme/cr/download-word/${Number(recordId)}`, {cache: 'no-store'});
                    if (!response.ok) {
                        const detail = await response.json().catch(() => ({}));
                        throw new Error(detail.detail || 'Le fichier Word est introuvable sur le serveur.');
                    }
                    if (!bridge?.client_open_word_document) {
                        const link = document.createElement('a');
                        link.href = URL.createObjectURL(await response.blob());
                        link.download = `Compte_rendu_${Number(recordId)}.docx`;
                        link.click();
                        setTimeout(() => URL.revokeObjectURL(link.href), 30000);
                        return true;
                    }
                    const buffer = await response.arrayBuffer();
                    if (!buffer.byteLength || buffer.byteLength > 100 * 1024 * 1024) throw new Error('Le document est vide ou dépasse 100 Mo.');
                    let binary = '';
                    const bytes = new Uint8Array(buffer);
                    const chunkSize = 0x8000;
                    for (let offset = 0; offset < bytes.length; offset += chunkSize) binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
                    const dataUrl = 'data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64,' + btoa(binary);
                    const result = bridge.get_deployment_mode
                        ? await bridge.client_open_word_document(dataUrl, `Compte_rendu_${Number(recordId)}.docx`)
                        : await bridge.client_open_word_document(dataUrl, `Compte_rendu_${Number(recordId)}.docx`, Number(recordId), 'dme_cr');
                    if (!result?.ok) throw new Error(result?.error || 'Microsoft Word n’a pas pu ouvrir le compte rendu.');
                    if (result.sync_started && typeof window.showToast === 'function') {
                        window.showToast('Après Ctrl+S, le compte rendu patient sera synchronisé automatiquement sur le serveur.', 'success');
                    }
                    return true;
                } catch(e) {
                    alert("Erreur lors de l'ouverture du document Word : " + (e.message || 'erreur inconnue'));
                    return false;
                }
            };

            window.openCrPreviewModal = function(recordId) {
                const modal = portalDmeModal('dmeCrPreviewModal');
                const iframe = document.getElementById('dmeCrIframe');
                const titleEl = document.getElementById('dmeCrModalTitle');
                activeLivretDraftToken = null;
                setDmeCrPreviewMode('archived');
                if (titleEl) titleEl.innerText = "Aperçu Compte Rendu Médical A4";
                if (iframe) iframe.src = '/api/dme/cr/print/' + recordId;
                if (modal) { modal.classList.remove('hidden'); modal.classList.add('flex'); document.body.style.overflow = 'hidden'; }
            };

            window.closeCrPreviewModal = function() {
                const modal = document.getElementById('dmeCrPreviewModal');
                const iframe = document.getElementById('dmeCrIframe');
                const draftToken = activeLivretDraftToken;
                activeLivretDraftToken = null;
                if (iframe) iframe.src = 'about:blank';
                if (activeRehabPlanPdfUrl) {
                    const previousUrl = activeRehabPlanPdfUrl;
                    activeRehabPlanPdfUrl = null;
                    setTimeout(() => URL.revokeObjectURL(previousUrl), 1000);
                }
                if (modal) { modal.classList.add('hidden'); modal.classList.remove('flex'); }
                document.body.style.overflow = '';
                setDmeCrPreviewMode('archived');
                if (draftToken) {
                    fetch('/api/dme/livret/discard', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({draft_token:draftToken})}).catch(() => {});
                }
            };

            window.printCrIframe = function() {
                const iframe = document.getElementById('dmeCrIframe');
                if (iframe && iframe.contentWindow) {
                    iframe.contentWindow.focus();
                    iframe.contentWindow.print();
                }
            };

            window.deleteArchivedCr = async function(recordId) {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!confirm("Voulez-vous supprimer ce compte rendu archivé ?")) return;

                try {
                    const res = await fetch('/api/dme/cr/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id: recordId })
                    }).then(r => r.json());

                    if (res.success && currentTab) {
                        const ref = await fetch(`/api/dme/record/${currentTab.patientId}/${currentTab.passageId}`).then(r => r.json());
                        currentTab.data.archived_crs = ref.archived_crs || [];
                        renderDmeArchivedCrTable(currentTab.data.archived_crs);
                    }
                } catch(e) {
                    alert("Erreur lors de la suppression.");
                }
            };

            // ENREGISTREMENT GLOBAL DE LA CONSULTATION
            window.resetActiveDmeConsultation = async function() {
                const tab = window.activeDmeTabs.find(item => item.key === window.currentActiveDmeKey);
                if (!tab || tab.mode !== 'execution' || tab.data?.dme?.verrouille || getDmeResetPermission(tab) !== true || !(tab.consultationId || tab.data?.dme?.id)) return;
                if (tab.resetting) return;
                if (tab.examRequestState?.saving) return;
                if (!confirm('Remettre TOUTE cette consultation en cours à zéro, comme au démarrage ?\\n\\nLes champs cliniques, fiches spécialisées, ordonnances (y compris celle en préparation), produits consommés, comptes rendus, livrets et images ajoutés pendant cette consultation seront retirés.\\n\\nL’identité du patient, les anciens dossiers, les documents d’accueil et la caisse seront conservés.')) return;
                const button = document.getElementById('dmeResetConsultationButton');
                const saveButton = document.getElementById('dmeSaveConsultationButton');
                const controls = [...document.querySelectorAll('.dme-subtab-panel input, .dme-subtab-panel textarea, .dme-subtab-panel select, .dme-subtab-panel button')].map(control => [control, control.disabled]);
                controls.forEach(([control]) => control.disabled = true);
                let completed = false;
                tab.resetting = true;
                if (button) button.disabled = true;
                if (saveButton) saveButton.disabled = true;
                try {
                    const response = await fetch('/api/dme/consultation/reset', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({id_patient: Number(tab.patientId), id_passage: Number(tab.passageId), id_consultation: Number(tab.consultationId || tab.data?.dme?.id)})
                    });
                    const result = await response.json();
                    if (!response.ok || !result.success) throw new Error(result.detail || result.message || 'Réinitialisation impossible.');
                    completed = true;
                    delete tab.examRequestState;
                    tab.data = result.data;
                    tab.consultationId = result.data.dme?.id || tab.consultationId;
                    tab.mode = result.data.dme?.verrouille ? 'lecture' : 'execution';
                    tab.currentSubTab = 'consultation';
                    if (window.currentActiveDmeKey === tab.key) {
                        document.querySelectorAll('.dme-subtab-panel input, .dme-subtab-panel textarea, .dme-subtab-panel select').forEach(field => {
                            if (field.tagName === 'SELECT') field.selectedIndex = 0;
                            else if (field.type === 'checkbox' || field.type === 'radio') field.checked = field.defaultChecked;
                            else field.value = field.defaultValue || '';
                        });
                        currentOrdLines = [];
                        activeLivretDraftToken = null;
                        activeCarouselImages = [];
                        currentCarouselIndex = 0;
                        pendingEchoUploadPreviewUrls.forEach(url => URL.revokeObjectURL(url));
                        pendingEchoUploadPreviewUrls = [];
                        pendingEchoUploadFiles = [];
                        pendingEchoUploadTargetGroupId = null;
                        pendingEchoUploadCanAddDprint = false;
                        appendNextEchoUpload = false;
                        if (activeRehabPlanPdfUrl) URL.revokeObjectURL(activeRehabPlanPdfUrl);
                        activeRehabPlanPdfUrl = null;
                        document.getElementById('ordMedSearchResults')?.classList.add('hidden');
                        renderActiveOrdLines();
                        displayActivePatientDme();
                        window.renderDmeReeducationPlanRows([]);
                        loadPatientPrescriptionsHistory(tab.patientId, tab.passageId, tab.consultationId);
                        loadDmeConsumptions();
                        applyDmeAccessMode(tab);
                    }
                    alert(result.message);
                } catch (error) {
                    alert(error.message || 'Erreur lors de la réinitialisation.');
                } finally {
                    tab.resetting = false;
                    if (!completed && window.currentActiveDmeKey === tab.key) controls.forEach(([control, disabled]) => control.disabled = disabled);
                    if (window.currentActiveDmeKey === tab.key) {
                        if (saveButton) saveButton.disabled = false;
                        applyDmeAccessMode(tab);
                    }
                }
            };

            window.saveActiveDmeConsultation = async function(options = {}) {
                if (window.activeDmeTabs.find(item => item.key === window.currentActiveDmeKey)?.resetting) return false;
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab) return false;
                if (currentTab.mode !== 'execution' || currentTab.data?.dme?.verrouille) {
                    alert("Cette consultation est en lecture seule.");
                    return false;
                }

                // Le tableau de rééducation stocke ses lignes dans un champ JSON caché.
                // Synchroniser avant la collecte pour ne pas perdre la dernière saisie.
                window.syncDmeReeducationPlan();
                const payload = {
                    id_patient: currentTab.patientId,
                    id_passage: currentTab.passageId,
                    id_consultation: currentTab.consultationId || currentTab.data?.dme?.id,
                    constantes: {
                        ta: document.getElementById('dme_ta')?.value || '',
                        pouls: document.getElementById('dme_pouls')?.value || '',
                        temp: document.getElementById('dme_temp')?.value || '',
                        poids: document.getElementById('dme_poids')?.value || '',
                        taille: document.getElementById('dme_taille')?.value || '',
                        imc: document.getElementById('dme_imc')?.value || '',
                        glycemie: document.getElementById('dme_glycemie')?.value || '',
                        perimetre_cranien: document.getElementById('dme_growth_head')?.value || '',
                        perimetre_brachial: document.getElementById('dme_growth_arm')?.value || '',
                    },
                    allergies_intolerances: document.getElementById('dme_allergies')?.value || '',
                    traitement_en_cours: document.getElementById('dme_traitement')?.value || '',
                    antecedents_medicaux: document.getElementById('dme_ant_med')?.value || '',
                    antecedents_chirurgicaux: document.getElementById('dme_ant_chir')?.value || '',
                    notes_habitudes: document.getElementById('dme_habitudes')?.value || '',
                    motif: document.getElementById('dme_motif')?.value || '',
                    diagnostic: document.getElementById('dme_diagnostic')?.value || '',
                    conduite_a_tenir: document.getElementById('dme_conduite')?.value || '',
                    donnees_specialisees: collectDmeSpecialtyFields(),
                    actes: currentDmeActs
                };
                if (options.keepOpen) {
                    payload.note_reprise = options.note;
                    payload.note_medecin_id = Number(options.doctorId);
                }

                try {
                    const saveUrl = options.cancelStart
                        ? '/api/dme/consultation/cancel-start'
                        : (options.keepOpen ? '/api/dme/consultation/keep' : '/api/dme/save');
                    const res = await fetch(saveUrl, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    }).then(async response => {
                        const body = await response.json().catch(() => ({}));
                        if (!response.ok) throw new Error(body.detail || body.message || 'Enregistrement impossible.');
                        return body;
                    });

                    if (res.success) {
                        if (!options.closeAfter) alert("✅ " + res.message);
                        currentTab.data.dme = {
                            ...currentTab.data.dme,
                            ...payload,
                            id: res.id_consultation || currentTab.consultationId || currentTab.data.dme?.id,
                            constantes: payload.constantes,
                            praticien: res.praticien || currentTab.data.dme?.praticien,
                            verrouille: (options.keepOpen || options.cancelStart) ? false : true,
                            note_reprise: options.keepOpen ? res.note_reprise : currentTab.data.dme?.note_reprise,
                            note_medecin: options.keepOpen ? res.note_medecin : currentTab.data.dme?.note_medecin
                        };
                        currentTab.consultationId = currentTab.data.dme.id;
                        currentTab.data.passage.actes = currentDmeActs;
                        currentTab.data.passage.statut = res.statut || (options.cancelStart ? 'En attente' : (options.keepOpen ? 'En cours' : 'Terminé'));
                        currentTab.data.historique = (currentTab.data.historique || []).map(item =>
                            Number(item.id_passage) === Number(currentTab.passageId)
                                ? { ...item, statut: currentTab.data.passage.statut, diagnostic: payload.diagnostic, praticien: currentTab.data.dme.praticien, constantes: payload.constantes }
                                : item
                        );
                        currentTab.mode = (options.keepOpen || options.cancelStart) ? 'execution' : 'lecture';
                        if (options.closeAfter) {
                            const closeModal = document.getElementById('dmeCloseConsultationModal');
                            closeModal?.classList.add('hidden');
                            closeModal?.classList.remove('flex');
                            document.body.style.overflow = '';
                            pendingDmeCloseTabKey = null;
                            removeDmeTabNow(currentTab.key);
                        } else displayActivePatientDme();
                        if (typeof window.loadReceptionPassagesModule === 'function') window.loadReceptionPassagesModule();
                        return true;
                    } else {
                        alert("❌ " + res.message);
                        return false;
                    }
                } catch (err) {
                    alert(err.message || "Erreur de sauvegarde du DME.");
                    return false;
                }
            };

            renderDmeTabsBar();
            displayActivePatientDme();
        })();
    </script>
    """


def render_html(user=None) -> str:
    from modules.dme_display import DME_TABS, display_permissions
    from modules.physiotherapy_bilan import render_bilan_html, LABELS
    from modules.reeducation_classique import render_classic_html, CLASSIC_SCRIPT, LABELS as CLASSIC_LABELS
    from modules.exam_requests_ui import render_exam_requests_panel, exam_requests_script
    permissions = display_permissions(user)
    anchors = {
        'observation': 'dme_passage_observation', 'constantes': 'dme_ta',
        'allergies': 'dme_allergies', 'traitement': 'dme_traitement',
        'ant_med': 'dme_ant_med', 'ant_chir': 'dme_ant_chir',
        'habitudes': 'dme_habitudes', 'motif': 'dme_motif',
        'diagnostic': 'dme_diagnostic', 'conduite': 'dme_conduite',
        'specialites': 'dmeSpecialtyPanel-gastro', 'actes': 'dmeActsTableBody',
        'physiotherapie': 'dmeSpecialtyPanel-physiotherapie',
        'reeducation_classique': 'dmeSpecialtyPanel-reeducation_classique',
    }
    setup = "const dmeTabIds = " + json.dumps(list(DME_TABS)) + ";"
    can_reset_at_render = None if user is None else bool(
        user.get('status', 'Actif') == 'Actif' and
        (user.get('role') == 'Administrateur' or permissions.get('dme_reset') is True))
    setup += 'const dmeResetPermissionAtRender = ' + json.dumps(can_reset_at_render) + ';'
    setup += "let dmeDisplayPermissions = " + json.dumps(permissions) + ";"
    setup += "let dmeVisibleTabs = []; const dmeGroupAnchors = " + json.dumps(anchors) + ";"
    setup += r"""
        function getDmeResetPermission(tab) {
            const permission = tab?.data?.dme_can_reset;
            return typeof permission === 'boolean' ? permission : dmeResetPermissionAtRender;
        }
        function applyDmeDisplayPreferences(permissions) {
            dmeDisplayPermissions = permissions || {};
            dmeVisibleTabs = dmeTabIds.filter(key => dmeDisplayPermissions['dme_display_tab_' + key] !== false);
            const hiddenSelectors = [];
            dmeTabIds.forEach(key => {
                if (!dmeVisibleTabs.includes(key)) hiddenSelectors.push('#subtab-btn-' + key, '#dme-panel-' + key);
            });
            const consultation = document.getElementById('dme-panel-consultation');
            Object.entries(dmeGroupAnchors).forEach(([key, anchorId]) => {
                let group = document.getElementById(anchorId);
                if (!['physiotherapie','reeducation_classique'].includes(key)) {
                    while (group && group.parentElement !== consultation) group = group.parentElement;
                }
                // Les champs communs sont dans une grille : masquer uniquement leur carte.
                if (['allergies','traitement','ant_med','ant_chir','habitudes','motif','diagnostic','conduite'].includes(key)) {
                    group = document.getElementById(anchorId)?.parentElement;
                }
                if (group) {
                    group.dataset.dmeDisplayGroup = key;
                    if (dmeDisplayPermissions['dme_display_group_' + key] === false) {
                        hiddenSelectors.push('[data-dme-display-group="' + key + '"]');
                    }
                }
            });
            let style = document.getElementById('dmeDisplayPreferencesStyle');
            if (!style) {
                style = document.createElement('style'); style.id = 'dmeDisplayPreferencesStyle';
                document.getElementById('dmeSubTabsBar').appendChild(style);
            }
            style.textContent = hiddenSelectors.length ? hiddenSelectors.join(',') + '{display:none!important}' : '';
            let message = document.getElementById('dmeDisplayEmptyMessage');
            if (!message) {
                message = document.createElement('p'); message.id = 'dmeDisplayEmptyMessage';
                message.className = 'p-4 text-sm text-slate-500';
                message.textContent = 'Aucun onglet du dossier médical n’est affiché pour votre profil.';
                document.getElementById('dmeSubTabsBar').after(message);
            }
            message.hidden = dmeVisibleTabs.length > 0;
        }
        applyDmeDisplayPreferences(dmeDisplayPermissions);
    """
    html = _render_html().replace('<div id="dmeSpecialtyPanel-gastro"', render_bilan_html() + render_classic_html() + '<div id="dmeSpecialtyPanel-gastro"', 1)
    html = html.replace('<button type="button" onclick="switchDmeSubTab(\'compterendu\')"', '<button type="button" onclick="switchDmeSubTab(\'bilans\')" id="subtab-btn-bilans" class="dme-subtab-btn flex items-center gap-2 px-4 py-2.5 rounded-2xl text-xs font-bold bg-white text-slate-500 border border-slate-200"><i class="ph-bold ph-flask"></i>Demandes de bilans</button><button type="button" onclick="switchDmeSubTab(\'compterendu\')"', 1)
    html = html.replace('<!-- PANNEAU : CONSOMMATIONS RÉELLES -->', render_exam_requests_panel() + '<!-- PANNEAU : CONSOMMATIONS RÉELLES -->', 1)
    html = html.replace('<option value="physiotherapie">', '<option value="reeducation_classique">Rééducation classique</option><option value="physiotherapie">', 1)
    html = html.replace('const DME_SPECIALTY_FIELD_LABELS = {', 'const DME_SPECIALTY_FIELD_LABELS = {' + ','.join(json.dumps(key) + ':' + json.dumps(label) for key, label in {**LABELS, **CLASSIC_LABELS}.items()) + ',', 1)
    return html.replace('(function() {', '(function() {' + setup + CLASSIC_SCRIPT + exam_requests_script(), 1)
