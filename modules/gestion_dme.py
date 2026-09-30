from datetime import date, datetime
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
from typing import Dict, Any, List, Optional
import zipfile

from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib import colors


from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse, Response
from database import get_db_cursor
from app.auth import get_current_user_optional, track_action

logger = logging.getLogger("DmeModule")

dme_router = APIRouter(prefix="/api/dme", tags=["Dossier Médical"])

BASE_DIR = Path(__file__).resolve().parent.parent


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
    selected_echo_paths: List[str],
    cover_page_path: Optional[str] = None,
) -> bool:
    """Assemble le livret A4 en insérant le compte rendu original exact via pypdf."""
    import tempfile
    from reportlab.lib.pagesizes import A4
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

    # --- C. Génération des pages Clichés Échographiques (2 GRANDES IMAGES PAR PAGE A4) ---
    temp_echo_pdf = os.path.join(temp_dir, "echos.pdf")
    valid_echo_paths = [p for p in selected_echo_paths if os.path.exists(p)]

    if valid_echo_paths:
        c_echo = canvas.Canvas(temp_echo_pdf, pagesize=A4)
        images_per_page = 2  # 2 grandes images par feuille A4 pour maximiser l'espace

        for i in range(0, len(valid_echo_paths), images_per_page):
            batch = valid_echo_paths[i:i + images_per_page]

            # En-tête discret
            c_echo.setFillColor(colors.HexColor("#0f172a"))
            c_echo.setFont("Helvetica-Bold", 10.5)
            c_echo.drawString(30, height - 28, f"CLICHÉS ÉCHOGRAPHIQUES — {patient_info.get('nom_prenom', '')}")

            # Dimensions maximales pour chaque cliché (~19 cm de large x 13 cm de haut)
            img_w = width - 60  # 535 points (largeur quasi pleine page)
            img_h = 370  # 370 points de hauteur par cliché

            # Coordonnées des 2 emplacements (Haut et Bas)
            positions = [
                (30, height - 35 - img_h),  # Image 1 (Haut)
                (30, 30)  # Image 2 (Bas)
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
        if valid_echo_paths and os.path.exists(temp_echo_pdf):
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
    cover_dir = LIVRET_COVER_DIR
    if not cover_dir.is_dir():
        return {"covers": []}
    files = [p.name for p in cover_dir.iterdir() if p.is_file() and p.suffix.lower() in LIVRET_COVER_EXTENSIONS]
    files.sort(key=lambda name: [(0, int(part)) if part.isdigit() else (1, part.casefold()) for part in re.split(r"(\d+)", name)])
    return {
        "covers": [
            {"filename": name, "url": f"/api/dme/livret/covers/{name}"}
            for name in files
        ],
        "default": "1.png" if "1.png" in files else (files[0] if files else ""),
    }


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
        cursor.execute("SELECT 1 FROM patient_passages WHERE id = %s AND id_patient = %s;", (passage_id, patient_id))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Le passage ne correspond pas au patient sélectionné.")
    cr_id = payload.get("cr_id")
    try:
        selected_echo_ids = sorted({int(value) for value in (payload.get("selected_echo_ids") or [])})
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Sélection de clichés invalide.")
    cover_name = str(payload.get("page_de_garde") or "").strip()
    if not cover_name:
        raise HTTPException(status_code=400, detail="Choisissez une page de garde pour le livret.")
    cover_path = _resolve_livret_cover(cover_name)

    nom_prenom = str(payload.get("nom_prenom") or "").strip().upper()
    date_examen = str(payload.get("date_examen") or "").strip()
    type_examen = str(payload.get("type_examen") or "").strip().upper()


    cr = dme_manager.get_cr_record_by_id(cr_id) if cr_id else {}
    if not cr:
        raise HTTPException(status_code=404, detail="Compte rendu de référence introuvable.")
    if int(cr.get("id_patient") or 0) != int(patient_id) or int(cr.get("id_passage") or 0) != int(passage_id):
        raise HTTPException(status_code=404, detail="Le compte rendu ne correspond pas au patient et au passage sélectionnés.")

    # Récupération des chemins des clichés sélectionnés
    echo_paths = []
    if selected_echo_ids:
        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT chemin_image FROM patient_echographies WHERE id = ANY(%s) AND id_patient = %s AND id_passage = %s;",
                (selected_echo_ids, patient_id, passage_id)
            )
            echo_paths = [r["chemin_image"] for r in cursor.fetchall()]
            if len(echo_paths) != len(selected_echo_ids):
                raise HTTPException(status_code=400, detail="Un ou plusieurs clichés ne correspondent pas à ce passage.")

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

    success = generate_livret_pdf_file(output_pdf, patient_info, cr, echo_paths, str(cover_path))
    if not success:
        raise HTTPException(status_code=500, detail="Échec lors de l'assemblage du livret PDF.")

    now_str = datetime.now().strftime("%d/%m/%Y à %H:%M")
    user = get_current_user_optional(request)
    user_nom = f"{user['prenom']} {user['nom']}" if user else "Dr. Praticien"

    # Archivage automatique sous type 'Livret'
    with get_db_cursor() as cursor:
        cursor.execute("""
                       INSERT INTO patient_cr_records
                       (id_patient, id_passage, template_id, titre, type_modele, modalite,
                        medecin_prescripteur, medecin_signataire, proprietaire, contenu_html,
                        chemin_docx, nom_fichier, date_heure_str)
                       VALUES (%s, %s, %s, %s, 'Livret', %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
                       """, (
                           patient_id, passage_id, cr.get("template_id"), f"Livret - {type_examen}",
                           cr.get("modalite", "Échographie"), cr.get("medecin_prescripteur", ""),
                           cr.get("medecin_signataire", ""), user_nom, "",
                           output_pdf, filename, now_str
                       ))
        new_livret_id = cursor.fetchone()["id"]

    return {
        "success": True,
        "message": f"Livret '{filename}' créé et archivé avec succès !",
        "livret_id": new_livret_id
    }

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
        except Exception:
            pass

        # Migration des liens d'ordonnance vers la consultation clinique précise.
        # Le défaut TRUE conserve le comportement verrouillé des anciennes fiches DME;
        # le démarrage d'une nouvelle consultation crée explicitement un brouillon non verrouillé.
        try:
            with get_db_cursor() as cursor:
                cursor.execute("ALTER TABLE patient_dme_records ADD COLUMN IF NOT EXISTS verrouille BOOLEAN NOT NULL DEFAULT TRUE;")
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
                                  diagnostic,
                                  conduite_a_tenir,
                                  praticien,
                                  verrouille
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
                                  d.praticien
                           FROM patient_passages p
                                    LEFT JOIN LATERAL (
                                        SELECT diagnostic, praticien
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
                h_dict["date_passage"] = format_date_display(h_dict.get("date_passage"))
                historique.append(h_dict)

            # 5. Prestations
            try:
                cursor.execute(
                    "SELECT id, designation, categorie, tarif, tarif_nuit, specialite FROM consultation_types ORDER BY designation ASC;")
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
                               WHERE type_personnel IN ('Docteur', 'Médecin', 'Professeur')
                                  OR specialite ILIKE '%%radio%%'
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
                               SELECT id, chemin_image, nom_fichier, titre, date_heure_str
                               FROM patient_echographies
                               WHERE id_patient = %s
                                 AND id_passage = %s
                               ORDER BY id DESC;
                               """, (patient_id, passage_id))
                echographies = [dict(r) for r in cursor.fetchall()]
            except Exception:
                echographies = []

        if patient:
            patient_data = dict(patient)
            patient_data["date_naissance"] = format_date_display(patient_data.get("date_naissance"))
        else:
            patient_data = {
                "id": patient_id, "nom": "Patient", "prenom": f"#{patient_id}",
                "date_naissance": "-", "genre": "Homme", "telephone_1": "-",
                "wilaya": "", "commune": "", "groupe_sanguin": "O+", "avatar": ""
            }

        if passage:
            passage_data = dict(passage)
            passage_data["date_passage"] = format_date_display(passage_data.get("date_passage"))
            passage_data.update(medecin_info)
            actes = passage_data.get("actes_json")
            passage_data["actes"] = actes if isinstance(actes, list) else json.loads(actes or "[]")
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
                       d.diagnostic,
                       d.conduite_a_tenir,
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

    def save_consultation(self, data: Dict[str, Any], user_fullname: str) -> Dict[str, Any]:
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

        actes_list = data.get("actes", [])
        actes_json = json.dumps(actes_list)

        if not patient_id or not passage_id:
            return {"success": False, "message": "Identifiants manquants."}

        with get_db_cursor() as cursor:
            cursor.execute("SELECT id_patient, statut FROM patient_passages WHERE id = %s FOR UPDATE;", (passage_id,))
            passage_row = cursor.fetchone()
            if not passage_row or passage_row.get("statut") != "En cours":
                return {"success": False, "message": "Le passage doit être en cours pour enregistrer la consultation."}
            if int(passage_row.get("id_patient")) != int(patient_id):
                return {"success": False, "message": "Ce passage ne correspond pas au patient sélectionné."}

            if requested_consultation_id:
                cursor.execute("""
                    SELECT id, verrouille FROM patient_dme_records
                    WHERE id = %s AND id_patient = %s AND id_passage = %s
                    FOR UPDATE;
                """, (requested_consultation_id, patient_id, passage_id))
            else:
                cursor.execute("""
                    SELECT id, verrouille FROM patient_dme_records
                    WHERE id_patient = %s AND id_passage = %s
                    ORDER BY id DESC LIMIT 1 FOR UPDATE;
                """, (patient_id, passage_id))
            existing = cursor.fetchone()

            if requested_consultation_id and not existing:
                return {"success": False, "message": "Cette consultation ne correspond pas au patient et au passage sélectionnés."}

            if existing and existing.get("verrouille", True):
                return {"success": False, "message": "Cette consultation est déjà enregistrée et verrouillée en lecture seule."}

            consultation_values = (
                user_fullname, constantes, allergies, traitement, ant_med, ant_chir,
                habitudes, motif, diagnostic, conduite
            )
            if existing:
                consultation_id = existing["id"]
                cursor.execute("""
                    UPDATE patient_dme_records
                    SET praticien = %s, constantes_json = %s::jsonb,
                        allergies_intolerances = %s, traitement_en_cours = %s,
                        antecedents_medicaux = %s, antecedents_chirurgicaux = %s,
                        notes_habitudes = %s, motif = %s, diagnostic = %s,
                        conduite_a_tenir = %s, verrouille = TRUE,
                        date_consultation = CURRENT_TIMESTAMP
                    WHERE id = %s AND id_patient = %s AND id_passage = %s;
                """, (*consultation_values, consultation_id, patient_id, passage_id))
            else:
                cursor.execute("""
                    INSERT INTO patient_dme_records
                    (id_patient, id_passage, praticien, constantes_json, allergies_intolerances,
                     traitement_en_cours, antecedents_medicaux, antecedents_chirurgicaux,
                     notes_habitudes, motif, diagnostic, conduite_a_tenir, verrouille)
                    VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                    RETURNING id;
                """, (patient_id, passage_id, *consultation_values))
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

            cursor.execute("UPDATE patient_passages SET statut = 'Terminé' WHERE id = %s;", (passage_id,))

        return {"success": True, "message": "Consultation enregistrée et verrouillée en lecture seule !", "statut": "Terminé", "id_consultation": consultation_id}

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
                if os.path.exists(master_path):
                    shutil.copy2(master_path, chemin_final_docx)
                else:
                    with open(chemin_final_docx, "wb") as f:
                        f.write(b"")

            # Remplacement garanti des 8 variables dans le Word
            replace_docx_variables_robust(chemin_final_docx, replacements)

            try:
                if platform.system() == "Windows":
                    os.startfile(chemin_final_docx)
            except Exception:
                pass

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
async def api_get_dme(patient_id: int, passage_id: int):
    try:
        return dme_manager.get_patient_dme_data(patient_id, passage_id)
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
    dme_manager.ensure_dme_schema()
    patient_id = payload.get("id_patient")
    passage_id = payload.get("id_passage")
    if not patient_id or not passage_id:
        raise HTTPException(status_code=400, detail="Identifiants patient et passage obligatoires.")
    with get_db_cursor() as cursor:
        cursor.execute("SELECT id_patient FROM patient_passages WHERE id = %s FOR UPDATE;", (passage_id,))
        passage = cursor.fetchone()
        if not passage:
            raise HTTPException(status_code=404, detail="Passage introuvable.")
        if int(passage.get("id_patient")) != int(patient_id):
            raise HTTPException(status_code=403, detail="Ce passage ne correspond pas à ce patient.")
        cursor.execute("""
            SELECT id, verrouille FROM patient_dme_records
            WHERE id_patient = %s AND id_passage = %s
            ORDER BY id DESC LIMIT 1 FOR UPDATE;
        """, (patient_id, passage_id))
        consultation = cursor.fetchone()
        if consultation and consultation.get("verrouille", True):
            raise HTTPException(status_code=409, detail="Cette consultation est déjà enregistrée en lecture seule.")
        if consultation:
            consultation_id = consultation["id"]
        else:
            cursor.execute("""
                INSERT INTO patient_dme_records (id_patient, id_passage, praticien, verrouille)
                VALUES (%s, %s, 'Dr', FALSE)
                RETURNING id;
            """, (patient_id, passage_id))
            consultation_id = cursor.fetchone()["id"]
        cursor.execute("""
            UPDATE patient_prescriptions
            SET id_consultation = %s
            WHERE id_patient = %s AND id_passage = %s AND id_consultation IS NULL;
        """, (consultation_id, patient_id, passage_id))
        cursor.execute("UPDATE patient_passages SET statut = 'En cours' WHERE id = %s;", (passage_id,))
    track_action(request, "CONSULTATION_DEMARREE", f"Consultation démarrée pour le passage #{passage_id}")
    return {"success": True, "statut": "En cours", "id_consultation": consultation_id}


@dme_router.post("/save")
async def api_save_dme(payload: Dict[str, Any], request: Request):
    user = get_current_user_optional(request)
    praticien = f"{user['prenom']} {user['nom']}" if user else "Dr Praticien"
    res = dme_manager.save_consultation(payload, praticien)
    if res.get("success"):
        track_action(request, "EDITION_DME", f"Mise à jour consultation #{payload.get('id_passage')}")
        res["praticien"] = praticien
    return res


@dme_router.get("/prescriptions/{patient_id}/{passage_id}/{consultation_id}")
async def api_get_patient_prescriptions(patient_id: int, passage_id: int, consultation_id: int):
    return {"prescriptions": dme_manager.get_prescriptions_patient(patient_id, passage_id, consultation_id or None)}


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
    if cr:
        require_consultation_unlocked(cr.get("id_passage"))
    if not cr or not cr.get("chemin_docx") or not os.path.exists(cr["chemin_docx"]):
        raise HTTPException(status_code=404, detail="Fichier Word introuvable sur le disque.")

    try:
        if platform.system() == "Windows":
            os.startfile(cr["chemin_docx"])
        elif platform.system() == "Darwin":
            subprocess.call(["open", cr["chemin_docx"]])
        else:
            subprocess.call(["xdg-open", cr["chemin_docx"]])
        return {"success": True, "message": "Ouvert dans Microsoft Word."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur d'ouverture : {str(e)}")


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
        files: List[UploadFile] = File(...)
):
    """Enregistre les images échographiques PNG/JPG pour le patient et le passage en cours."""
    dme_manager.ensure_dme_schema()
    require_consultation_unlocked(id_passage)

    if not files:
        raise HTTPException(status_code=400, detail="Aucun fichier image reçu.")

    patient_folder = os.path.join(str(BASE_DIR), "uploads", "dossiers_patients", str(id_patient), "echographies")
    os.makedirs(patient_folder, exist_ok=True)

    now_str = datetime.now().strftime("%d/%m/%Y (%H:%M)")
    saved_count = 0

    for file in files:
        ext = file.filename.split(".")[-1].lower() if "." in file.filename else "png"
        clean_base = f"ECHO_{id_patient}_{id_passage}_{int(time.time() * 1000)}_{random.randint(100, 999)}.{ext}"
        dest_path = os.path.join(patient_folder, clean_base)

        content = await file.read()
        with open(dest_path, "wb") as f_out:
            f_out.write(content)

        # Enregistrement dans PostgreSQL
        with get_db_cursor() as cursor:
            cursor.execute("""
                           INSERT INTO patient_echographies
                           (id_patient, id_passage, chemin_image, nom_fichier, titre, date_heure_str)
                           VALUES (%s, %s, %s, %s, %s, %s);
                           """,
                           (id_patient, id_passage, dest_path, file.filename, f"Cliché Écho #{clean_base}", now_str))
            saved_count += 1

    return {"success": True, "message": f"{saved_count} cliché(s) échographique(s) importé(s) avec succès !"}


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
        if os.path.exists(row["chemin_image"]):
            try:
                os.remove(row["chemin_image"])
            except Exception:
                pass
    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM patient_echographies WHERE id = %s;", (echo_id,))

    return {"success": True, "message": "Cliché supprimé."}


# ============================================================================
# RENDU DU COMPOSANT HTML ET JAVASCRIPT COMPLET
# ============================================================================
def render_html() -> str:
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
                    <img id="dme_hdr_avatar" src="https://images.unsplash.com/photo-1535713875002-d1d0cf377fde?w=120" class="w-16 h-16 rounded-2xl object-cover border-2 border-brand-neon shadow-md">
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
                    </div>
                </div>

                <div class="flex items-center gap-3">
                <span id="dmeAccessModeBadge" class="px-3 py-2 rounded-xl text-[10px] font-bold bg-slate-100 text-slate-600 dark:bg-white/5 dark:text-slate-300">Lecture seule</span>
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
                        <div class="flex-1">
                            <label class="block text-[11px] font-bold text-slate-600 dark:text-slate-300 mb-1.5 uppercase">
                                1. MODÈLE DE COMPTE RENDU *
                            </label>
                            <select id="crTemplateSelect" onchange="onCrTemplateChange()" class="w-full text-xs p-3 rounded-2xl border border-slate-200 dark:border-white/10 bg-white dark:bg-[#121c15] font-bold text-slate-800 dark:text-white outline-none focus:border-brand-neon shadow-xs">
                                <option value="" disabled selected>-- Sélectionnez un modèle --</option>
                            </select>
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

                        <div class="flex items-center gap-2.5">
                            <!-- Input Upload Clichés PNG -->
                            <label for="dmeEchoUploadInput" class="px-4 py-2 bg-brand-neon hover:bg-brand-neonDark text-black text-xs font-bold rounded-2xl shadow-md inline-flex items-center gap-2 transition cursor-pointer">
                                <i class="ph-bold ph-upload-simple text-base"></i>
                                <span>Importer Clichés PNG</span>
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
    <div id="livretCreationModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center hidden z-[105] p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-2xl max-h-[92vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
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
                    <label class="block text-[11px] font-bold text-slate-600 dark:text-slate-300 mb-1">Type d'Examen (Prestation par défaut) *</label>
                    <input type="text" id="livretTypeExamen" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white outline-none focus:border-brand-neon">
                </div>

                <div class="grid grid-cols-1 sm:grid-cols-[1fr_150px] gap-3 items-start pt-2 border-t border-slate-100 dark:border-white/5">
                    <div>
                        <label for="livretPageGardeSelect" class="block text-[11px] font-bold text-slate-600 dark:text-slate-300 mb-1">Page de garde *</label>
                        <select id="livretPageGardeSelect" required onchange="previewLivretPageGarde()" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-semibold text-slate-800 dark:text-white outline-none focus:border-brand-neon">
                            <option value="">Chargement des pages de garde…</option>
                        </select>
                        <p id="livretPageGardeHint" class="mt-1 text-[10px] text-slate-400">Choisissez une page enregistrée dans model_templates/Page_De_Garde.</p>
                    </div>
                    <div class="h-40 rounded-xl overflow-hidden border border-slate-200 dark:border-white/10 bg-slate-100 dark:bg-black/30 flex items-center justify-center">
                        <img id="livretPageGardePreview" class="hidden w-full h-full object-contain" alt="Aperçu de la page de garde">
                        <span id="livretPageGardePlaceholder" class="px-3 text-center text-[10px] text-slate-400">Aperçu de la page sélectionnée</span>
                    </div>
                </div>

              <!-- 3. Sélection des clichés échographiques (GRAND FORMAT) -->
                <div class="space-y-2 pt-2 border-t border-slate-100 dark:border-white/5">
                    <div class="flex items-center justify-between">
                        <label class="text-xs font-bold text-slate-700 dark:text-slate-300">
                            Sélectionner les clichés à intégrer (Grille 2×2) :
                        </label>
                        <div class="space-x-2 text-[11px]">
                            <button type="button" onclick="toggleAllLivretEchos(true)" class="text-emerald-500 font-bold hover:underline cursor-pointer">Tout cocher</button>
                            <span class="text-slate-400">|</span>
                            <button type="button" onclick="toggleAllLivretEchos(false)" class="text-slate-400 font-bold hover:underline cursor-pointer">Tout décocher</button>
                        </div>
                    </div>
                    
                    <!-- GRILLE EN GRAND FORMAT (Cartes spacieuses et images agrandies) -->
                    <div id="livretEchosCheckboxGrid" class="grid grid-cols-1 sm:grid-cols-2 gap-3 max-h-72 overflow-y-auto p-3 bg-slate-50 dark:bg-black/30 rounded-2xl border border-slate-200 dark:border-white/10">
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
                    <span>Ordonnance Médicale Officielle (Fond KACEL ORD.png)</span>
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
    <div id="dmeCrPreviewModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center hidden z-[100] p-2 sm:p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-4xl h-[95vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-5 py-3 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50 dark:bg-black/20 flex-shrink-0">
                <h3 class="text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5">
                    <i class="ph-bold ph-file-text text-brand-neon"></i>
                    <span id="dmeCrModalTitle">Visionneur de Compte Rendu (Aperçu Intégré)</span>
                </h3>
                <div class="flex items-center space-x-2">
                    <button type="button" onclick="printCrIframe()" class="px-3.5 py-1.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-sm inline-flex items-center gap-1 cursor-pointer">
                        <i class="ph-bold ph-printer"></i>
                        <span>Imprimer</span>
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
                    <button type="button" onclick="closeEchoCarousel()" class="text-slate-400 hover:text-white p-1 rounded-lg text-lg cursor-pointer">✕</button>
                </div>
            </div>

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

    <!-- SCRIPT DU MOTEUR DME MULTI-ONGLETS -->
    <script>
        (function() {
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

            function askDmeOpenMode(isLocked) {
                const modal = document.getElementById('dmeOpenModeModal');
                const message = document.getElementById('dmeOpenModeMessage');
                const executeButton = document.getElementById('dmeOpenExecuteButton');
                if (!modal) return Promise.resolve('lecture');
                if (isLocked) {
                    if (message) message.textContent = 'Cette consultation est déjà enregistrée et verrouillée. Vous pouvez consulter les informations et ouvrir les documents en lecture seule.';
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
                    const openMode = await askDmeOpenMode(isLocked);
                    if (!openMode) {
                        if (loadingNotice) loadingNotice.classList.add('hidden');
                        return;
                    }

                    if (openMode === 'execution' && !isLocked) {
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
                    mode: isLocked ? 'lecture' : openMode,
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
                    if (hint) hint.textContent = 'Ajoutez une image PNG ou JPG dans uploads/model_templates/Page_De_Garde.';
                    document.getElementById('btnSubmitLivret').disabled = true;
                    previewLivretPageGarde();
                    return;
                }
                select.disabled = false;
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
            document.getElementById('livretTypeExamen').value = (pass.type_passage || cr?.titre || 'Échographie').toUpperCase();
            await loadLivretPageGardeOptions();


            // Grille des images agrandies (cartes larges avec aperçu haute visibilité)
            const grid = document.getElementById('livretEchosCheckboxGrid');
            if (grid) {
                if (echos.length === 0) {
                    grid.innerHTML = '<span class="text-xs text-slate-400 italic col-span-2 py-6 text-center">Aucun cliché échographique importé pour ce passage.</span>';
                } else {
                    grid.innerHTML = echos.map(ec => `
                        <label class="group relative flex items-start gap-3 p-3 rounded-2xl border border-slate-200 dark:border-white/10 bg-white dark:bg-[#121c15] hover:border-brand-neon cursor-pointer shadow-xs transition">
                            <input type="checkbox" value="${ec.id}" checked class="livret-echo-check mt-1 rounded text-brand-neon focus:ring-brand-neon h-4 w-4">
                            <div class="w-28 h-20 rounded-xl overflow-hidden bg-black shrink-0 border border-slate-200 dark:border-white/10">
                                <img src="/api/dme/echographies/file/${ec.id}" class="w-full h-full object-cover group-hover:scale-105 transition duration-200">
                            </div>
                            <div class="flex-1 min-w-0 pr-1">
                                <span class="text-xs font-bold text-slate-800 dark:text-slate-100 block truncate" title="${ec.nom_fichier}">${ec.nom_fichier || 'Cliché Échographique'}</span>
                                <span class="text-[10px] text-slate-400 block mt-1 font-mono">${ec.date_heure_str || ''}</span>
                                <span class="mt-2 inline-block px-2 py-0.5 rounded text-[9px] font-bold bg-brand-neon/20 text-emerald-800 dark:text-brand-neon">PNG HD</span>
                            </div>
                        </label>
                    `).join('');
                }
            }

            document.getElementById('livretCreationModal').classList.remove('hidden');
        };
        window.closeLivretModal = function() {
            document.getElementById('livretCreationModal').classList.add('hidden');
        };

        window.toggleAllLivretEchos = function(check) {
            document.querySelectorAll('.livret-echo-check').forEach(cb => cb.checked = check);
        };

        window.handleConfirmLivret = async function(e) {
            e.preventDefault();
            const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
            if (!currentTab) return;

            const selectedEchoIds = [];
            document.querySelectorAll('.livret-echo-check:checked').forEach(cb => {
                selectedEchoIds.push(parseInt(cb.value));
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
                type_examen: document.getElementById('livretTypeExamen').value,
                page_de_garde: document.getElementById('livretPageGardeSelect').value,
                selected_echo_ids: selectedEchoIds
            };

            try {
                const res = await fetch('/api/dme/livret/create', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                }).then(r => r.json());

                if (res.success && res.livret_id) {
                    closeLivretModal();
                    // Rafraîchissement des comptes rendus
                    const ref = await fetch(`/api/dme/record/${currentTab.patientId}/${currentTab.passageId}`).then(r => r.json());
                    currentTab.data.archived_crs = ref.archived_crs || [];
                    renderDmeArchivedCrTable(currentTab.data.archived_crs);

                    // Affichage direct du livret PDF généré
                    openWordPdfPreview(res.livret_id, "Livret Médical");
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

            window.closeDmeTab = function(key, e) {
                e.stopPropagation();
                window.activeDmeTabs = window.activeDmeTabs.filter(t => t.key !== key);
                if (window.currentActiveDmeKey === key) {
                    window.currentActiveDmeKey = window.activeDmeTabs.length > 0 ? window.activeDmeTabs[window.activeDmeTabs.length - 1].key : null;
                }
                renderDmeTabsBar();
                displayActivePatientDme();
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

                const viewOnlyActions = /viewArchivedConsultation|openWordPdfPreview|openCrPreviewModal|openPrescriptionModal|startEchoCarousel|openEchoCarouselAtIndex|openDmeAdmissionDocument|openDmeDemandeExamen/;
                document.querySelectorAll('.dme-subtab-panel').forEach(panel => {
                    panel.querySelectorAll('input, textarea, select, button').forEach(control => {
                        if (canEdit) {
                            control.disabled = false;
                            control.classList.remove('opacity-50', 'cursor-not-allowed');
                            return;
                        }
                        const inlineAction = control.getAttribute('onclick') || '';
                        const isReadAction = control.tagName === 'BUTTON' && viewOnlyActions.test(inlineAction);
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

            // 3. NAVIGATION ENTRE LES 5 SOUS-ONGLETS
            window.switchDmeSubTab = function(subTabId) {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (currentTab) currentTab.currentSubTab = subTabId;

                document.querySelectorAll('.dme-subtab-panel').forEach(p => p.classList.add('hidden'));

                document.querySelectorAll('.dme-subtab-btn').forEach(btn => {
                    btn.className = "dme-subtab-btn flex items-center gap-2 px-4 py-2.5 rounded-2xl text-xs font-bold transition cursor-pointer bg-white dark:bg-[#121c15] text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white border border-slate-200 dark:border-white/10";
                });

                const targetPanel = document.getElementById(`dme-panel-${subTabId}`);
                if (targetPanel) targetPanel.classList.remove('hidden');

                const targetBtn = document.getElementById(`subtab-btn-${subTabId}`);
                if (targetBtn) {
                    targetBtn.className = "dme-subtab-btn flex items-center gap-2 px-4 py-2.5 rounded-2xl text-xs font-bold transition cursor-pointer bg-brand-neon text-black shadow-md shadow-brand-neon/20";
                }

                if (subTabId === 'ordonnance' && currentTab) {
                    loadMedsForPrescription();
                    renderActiveOrdLines();
                    loadPatientPrescriptionsHistory(currentTab.patientId, currentTab.passageId, currentTab.consultationId || currentTab.data?.dme?.id || 0);
                } else if (subTabId === 'echographie' && currentTab) {
                    renderEchoGallery();
                }
                if (currentTab) applyDmeAccessMode(currentTab);
            };

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
                const histo = currentTab.data.historique || [];
                const crTemplates = currentTab.data.cr_templates || [];
                const radiologues = currentTab.data.radiologues || [];
                const archivedCrs = currentTab.data.archived_crs || [];

                // En-tête patient
                document.getElementById('dme_hdr_avatar').src = p.photo_face || p.avatar || 'https://images.unsplash.com/photo-1535713875002-d1d0cf377fde?w=120';
                document.getElementById('dme_hdr_fullname').innerText = `${p.nom || 'Patient'} ${p.prenom || ''}`;
                document.getElementById('dme_hdr_blood').innerText = p.groupe_sanguin || 'O+';
                document.getElementById('dme_hdr_dossier').innerText = `#DOS-${String(p.id || 0).padStart(4, '0')}`;
                document.getElementById('dme_hdr_dob').innerText = p.date_naissance || '-';
                document.getElementById('dme_hdr_tel').innerText = p.telephone_1 || '-';
                document.getElementById('dme_hdr_location').innerText = `${p.commune || ''} (${p.wilaya || ''})`;
                document.getElementById('dme_hdr_medecin').innerText = pass.medecin_nom ? `${pass.medecin_grade || 'Dr.'} ${pass.medecin_nom}` : 'Aucun';

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

                // Modèles CR
                const tplSelect = document.getElementById('crTemplateSelect');
                if (tplSelect) {
                    tplSelect.innerHTML = '<option value="" disabled selected>-- Sélectionnez un modèle --</option>' +
                        crTemplates.map(t => `<option value="${t.id}" data-format="${t.type_modele}">[${t.type_modele}] ${t.designation} (${t.modalite})</option>`).join('');
                }

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
                        <tr class="hover:bg-slate-50 dark:hover:bg-white/[0.02]">
                            <td class="py-3 pl-3 font-mono text-slate-400">${cr.date_heure_str || ''}</td>
                            <td class="py-3 font-bold text-slate-800 dark:text-slate-100 font-mono text-[11px]">${cr.nom_fichier || cr.titre || ''}</td>
                            <td class="py-3 text-emerald-600 dark:text-brand-neon font-semibold">${cr.medecin_signataire || ''}</td>
                            <td class="py-3 text-slate-500">@${cr.proprietaire || 'admin'}</td>
                            <td class="py-3">${badge}</td>
                            <td class="py-3 text-right pr-4">
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
            window.openWordPdfPreview = function(recordId, nomFichier) {
                const modal = document.getElementById('dmeCrPreviewModal');
                const iframe = document.getElementById('dmeCrIframe');
                const titleEl = document.getElementById('dmeCrModalTitle');

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
                if (modal) modal.classList.remove('hidden');

                // 2. Chargement du PDF dans l'iframe dès qu'il est prêt
                setTimeout(() => {
                    iframe.removeAttribute('srcdoc');
                    iframe.src = '/api/dme/cr/view-pdf/' + recordId + '?t=' + Date.now();
                }, 200);
            };

            // =================================================================
            // 6. DOSSIER ÉCHOGRAPHIE (PNG) & CARROUSEL INTERACTIF
            // =================================================================
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

                grid.innerHTML = echos.map((ec, idx) => `
                    <div class="group relative bg-slate-50 dark:bg-black/30 rounded-2xl p-2 border border-slate-200 dark:border-white/10 shadow-xs flex flex-col justify-between overflow-hidden">
                        <div class="relative w-full aspect-video rounded-xl overflow-hidden bg-black flex items-center justify-center cursor-pointer" onclick="openEchoCarouselAtIndex(${idx})">
                            <img src="/api/dme/echographies/file/${ec.id}" class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300">
                            <div class="absolute inset-0 bg-black/40 opacity-0 group-hover:opacity-100 flex items-center justify-center gap-2 transition-opacity">
                                <span class="p-2 rounded-xl bg-white/20 text-white text-sm"><i class="ph-bold ph-arrows-out-simple"></i></span>
                            </div>
                        </div>

                        <div class="pt-2 px-1 flex items-center justify-between text-[11px]">
                            <span class="font-bold text-slate-700 dark:text-slate-300 truncate max-w-[110px]" title="${ec.nom_fichier}">${ec.nom_fichier || 'Cliché Écho'}</span>
                            <button type="button" onclick="deleteEchographie(${ec.id})" class="text-rose-500 hover:text-rose-700 p-1 rounded-lg" title="Supprimer">
                                <i class="ph-bold ph-trash"></i>
                            </button>
                        </div>
                    </div>
                `).join('');
            }

            window.handleEchoFilesUpload = async function(event) {
                const files = event.target.files;
                if (!files || files.length === 0) return;

                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab) return;

                const formData = new FormData();
                for (let i = 0; i < files.length; i++) {
                    formData.append('files', files[i]);
                }

                try {
                    const url = `/api/dme/echographies/upload?id_patient=${currentTab.patientId}&id_passage=${currentTab.passageId}`;
                    const res = await fetch(url, {
                        method: 'POST',
                        body: formData
                    }).then(r => r.json());

                    if (res.success) {
                        alert("✅ " + res.message);
                        // Rechargement immédiat des clichés
                        const ref = await fetch(`/api/dme/record/${currentTab.patientId}/${currentTab.passageId}`).then(r => r.json());
                        currentTab.data.echographies = ref.echographies || [];
                        renderEchoGallery();
                    } else {
                        alert(res.message);
                    }
                } catch(e) {
                    alert("Erreur lors de l'envoi des images échographiques.");
                }
                event.target.value = '';
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
                    }
                } catch(e) {
                    alert("Erreur de suppression.");
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

            window.renderDmeActsTable = function() {
                const tbody = document.getElementById('dmeActsTableBody');
                if (!tbody) return;
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
            window.onCrTemplateChange = function() {
                const select = document.getElementById('crTemplateSelect');
                const btnText = document.getElementById('crGenerateBtnText');
                if (!select || !btnText) return;

                const selectedOption = select.options[select.selectedIndex];
                const format = selectedOption ? selectedOption.getAttribute('data-format') : 'Word';

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
                            alert(`✅ Compte rendu Word généré : ${res.nom_fichier}\\nOuvert automatiquement dans Microsoft Word.`);
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
                    const res = await fetch('/api/dme/cr/open-word', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id: recordId })
                    }).then(r => r.json());

                    if (!res.success) {
                        alert("❌ " + res.message);
                    }
                } catch(e) {
                    alert("Erreur lors de l'ouverture du document Word.");
                }
            };

            window.openCrPreviewModal = function(recordId) {
                const modal = document.getElementById('dmeCrPreviewModal');
                const iframe = document.getElementById('dmeCrIframe');
                const titleEl = document.getElementById('dmeCrModalTitle');
                if (titleEl) titleEl.innerText = "Aperçu Compte Rendu Médical A4";
                if (iframe) iframe.src = '/api/dme/cr/print/' + recordId;
                if (modal) modal.classList.remove('hidden');
            };

            window.closeCrPreviewModal = function() {
                const modal = document.getElementById('dmeCrPreviewModal');
                const iframe = document.getElementById('dmeCrIframe');
                if (iframe) iframe.src = 'about:blank';
                if (modal) modal.classList.add('hidden');
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
            window.saveActiveDmeConsultation = async function() {
                const currentTab = window.activeDmeTabs.find(t => t.key === window.currentActiveDmeKey);
                if (!currentTab) return;
                if (currentTab.mode !== 'execution' || currentTab.data?.dme?.id || currentTab.data?.dme?.verrouille) {
                    alert("Cette consultation est en lecture seule.");
                    return;
                }

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
                    },
                    allergies_intolerances: document.getElementById('dme_allergies')?.value || '',
                    traitement_en_cours: document.getElementById('dme_traitement')?.value || '',
                    antecedents_medicaux: document.getElementById('dme_ant_med')?.value || '',
                    antecedents_chirurgicaux: document.getElementById('dme_ant_chir')?.value || '',
                    notes_habitudes: document.getElementById('dme_habitudes')?.value || '',
                    motif: document.getElementById('dme_motif')?.value || '',
                    diagnostic: document.getElementById('dme_diagnostic')?.value || '',
                    conduite_a_tenir: document.getElementById('dme_conduite')?.value || '',
                    actes: currentDmeActs
                };

                try {
                    const res = await fetch('/api/dme/save', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    }).then(r => r.json());

                    if (res.success) {
                        alert("✅ " + res.message);
                        currentTab.data.dme = {
                            ...currentTab.data.dme,
                            ...payload,
                            id: res.id_consultation || currentTab.consultationId || currentTab.data.dme?.id,
                            constantes: payload.constantes,
                            praticien: res.praticien || currentTab.data.dme?.praticien,
                            verrouille: true
                        };
                        currentTab.consultationId = currentTab.data.dme.id;
                        currentTab.data.passage.actes = currentDmeActs;
                        currentTab.data.passage.statut = res.statut || 'Terminé';
                        currentTab.data.historique = (currentTab.data.historique || []).map(item =>
                            Number(item.id_passage) === Number(currentTab.passageId)
                                ? { ...item, statut: currentTab.data.passage.statut, diagnostic: payload.diagnostic, praticien: currentTab.data.dme.praticien }
                                : item
                        );
                        currentTab.mode = 'lecture';
                        displayActivePatientDme();
                        if (typeof window.loadReceptionPassagesModule === 'function') window.loadReceptionPassagesModule();
                    } else {
                        alert("❌ " + res.message);
                    }
                } catch (err) {
                    alert("Erreur de sauvegarde du DME.");
                }
            };

            renderDmeTabsBar();
            displayActivePatientDme();
        })();
    </script>
    """
