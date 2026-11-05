"""Inventaire, maintenance et demandes d'intervention du service technique."""

from datetime import date
import asyncio
import base64
import json
from html import escape
import logging
from pathlib import Path
import sys
import unicodedata
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from app.auth import get_current_user, track_action
from app.smtp_mailer import build_cliniva_email_html, get_smtp_configuration, send_smtp_email
from database import DOCS_DIR, get_db_cursor

service_tech_router = APIRouter(prefix="/api/service-technique", tags=["Service technique"])
logger = logging.getLogger("ClinivaServiceTechnique")

EQUIPMENT_STATUSES = {"En service", "En maintenance", "Hors service", "Réformé"}
TICKET_TYPES = {
    "Incident", "Maintenance préventive", "Maintenance corrective",
    "Demande non définie", "Vérification de routine", "Calibration",
}
TICKET_STATUSES = {"Nouveau", "Planifié", "En cours", "En attente", "Terminé", "Annulé"}
PRIORITIES = {"Basse", "Normale", "Haute", "Urgente"}
MAINTENANCE_TYPES = {"Préventive", "Curative"}
MAINTENANCE_REPORT_EXTENSIONS = {".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png"}
MAX_MAINTENANCE_REPORT_BYTES = 25 * 1024 * 1024


def _allowed(user: Dict[str, Any], permission: str) -> bool:
    if user.get("role") == "Administrateur":
        return True
    return bool((user.get("permissions") or {}).get(permission))


def _require(user: Dict[str, Any], permission: str) -> None:
    if not _allowed(user, permission):
        raise HTTPException(status_code=403, detail="Vous n'avez pas l'autorisation nécessaire pour le Service Technique.")


def _text(data: Dict[str, Any], key: str, label: str, required: bool = False) -> str:
    value = str(data.get(key) or "").strip()
    if required and not value:
        raise HTTPException(status_code=400, detail=f"Le champ {label} est obligatoire.")
    return value


def _date_value(data: Dict[str, Any], key: str, label: str) -> Optional[str]:
    value = _text(data, key, label)
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"La date {label} est invalide.") from exc


def _time_value(data: Dict[str, Any], key: str, label: str) -> Optional[str]:
    value = _text(data, key, label)
    if not value:
        return None
    from datetime import datetime
    try:
        return datetime.strptime(value, "%H:%M").strftime("%H:%M")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"L'heure {label} est invalide.") from exc


def _is_technical_assignee(staff_type: str) -> bool:
    normalized = " ".join(
        "".join(char for char in unicodedata.normalize("NFKD", staff_type.casefold())
                if not unicodedata.combining(char)).split()
    )
    return (
        "ingenieur" in normalized
        or "directeur technique" in normalized
        or "responsable resultat" in normalized
        or "responsable des resultats" in normalized
        or "responsable de resultat" in normalized
    )


def _protect_equipment_password(password: str) -> str:
    """Chiffre le mot de passe avec DPAPI Windows avant son stockage."""
    if sys.platform != "win32":
        raise HTTPException(status_code=500, detail="Le chiffrement sécurisé du mot de passe nécessite Windows.")
    try:
        import win32crypt
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="pywin32 est nécessaire pour chiffrer le mot de passe de l'équipement.") from exc
    encrypted = win32crypt.CryptProtectData(
        password.encode("utf-8"), "Cliniva Service Technique", None, None, None, 0
    )
    return base64.b64encode(encrypted).decode("ascii")


@service_tech_router.get("/data")
def service_tech_data(request: Request):
    user = get_current_user(request)
    _require(user, "service_tech_view")
    with get_db_cursor() as cursor:
        cursor.execute("""SELECT id, code, designation, categorie, marque_modele, numero_serie,
            adresse_ip, utilisateur, (mot_de_passe_ciphertext <> '') AS password_configured, aet, port,
            localisation, fournisseur, date_acquisition, statut, systeme_exploitation, derniere_maintenance,
            prochaine_maintenance, notes FROM service_tech_equipment
            ORDER BY designation, id""")
        equipment = [dict(row) for row in cursor.fetchall()]
        cursor.execute("""SELECT t.id, t.equipment_id, e.designation AS equipment_name, t.type,
            t.titre, t.description, t.priorite, t.statut, t.demandeur, t.responsable,
            t.service_concerne, t.lieu_panne, t.arret_travail,
            t.date_echeance, t.heure_echeance, t.date_cloture, t.created_at,
            r.intervenant AS report_intervenant, r.solution_trouvee AS report_solution,
            r.date_execution AS report_date, r.heure_debut_travail AS report_start,
            r.heure_fin_travail AS report_end, r.etapes_correction AS report_steps,
            r.correction_definitive AS report_definitive, r.scan_pages AS report_pages
            FROM service_tech_tickets t LEFT JOIN service_tech_equipment e ON e.id=t.equipment_id
            LEFT JOIN service_tech_closure_reports r ON r.ticket_id=t.id
            ORDER BY CASE t.priorite WHEN 'Urgente' THEN 0 WHEN 'Haute' THEN 1 WHEN 'Normale' THEN 2 ELSE 3 END,
            t.created_at DESC LIMIT 500""")
        tickets = [dict(row) for row in cursor.fetchall()]
        cursor.execute("""SELECT m.id, m.site_designation, m.site, m.equipment_name, m.equipment_designation,
            m.type, m.maintenance_date, m.heure_debut, m.heure_fin, m.ingenieur_interne, m.ingenieur_externe,
            m.notes, m.rapport_nom, (m.rapport_path <> '') AS has_report, m.statut,
            r.intervenant AS report_intervenant, r.solution_trouvee AS report_solution,
            r.date_execution AS report_date, r.heure_debut_travail AS report_start,
            r.heure_fin_travail AS report_end, r.etapes_correction AS report_steps,
            r.correction_definitive AS report_definitive, r.scan_pages AS report_pages
            FROM service_tech_maintenance m
            LEFT JOIN service_tech_closure_reports r ON r.maintenance_id=m.id
            ORDER BY m.maintenance_date DESC, m.heure_debut DESC, m.id DESC""")
        maintenances = [dict(row) for row in cursor.fetchall()]
        cursor.execute("""SELECT prenom, nom, login FROM users
            ORDER BY nom, prenom, login""")
        user_requesters = [
            {"value": f"{row['prenom']} {row['nom']} — Utilisateur (@{row['login']})",
             "label": f"{row['prenom']} {row['nom']} — Utilisateur (@{row['login']})"}
            for row in cursor.fetchall()
        ]
        cursor.execute("""SELECT prenom, nom, type_personnel FROM clinic_staff
            ORDER BY nom, prenom""")
        staff = [dict(row) for row in cursor.fetchall()]
        staff_requesters = [
            {"value": f"{row['prenom']} {row['nom']} — {row['type_personnel']}",
             "label": f"{row['prenom']} {row['nom']} — {row['type_personnel']}"}
            for row in staff
        ]
        engineers = [
            {"value": f"{row['prenom']} {row['nom']} — {row['type_personnel']}",
             "label": f"{row['prenom']} {row['nom']} — {row['type_personnel']}"}
            for row in staff
            if _is_technical_assignee(row.get("type_personnel") or "")
        ]
    today = date.today()
    overdue = sum(1 for item in equipment if item.get("prochaine_maintenance") and item["prochaine_maintenance"] < today and item["statut"] != "Réformé")
    return {"equipment": equipment, "tickets": tickets, "maintenances": maintenances,
            "requesters": user_requesters + staff_requesters, "engineers": engineers,
            "can_manage": _allowed(user, "service_tech_manage"),
            "kpi": {"equipment": len(equipment), "open": sum(1 for item in tickets if item["statut"] not in {"Terminé", "Annulé"}),
                    "urgent": sum(1 for item in tickets if item["priorite"] == "Urgente" and item["statut"] not in {"Terminé", "Annulé"}),
                    "overdue": overdue}}


@service_tech_router.get("/reporting-options")
def service_tech_reporting_options(request: Request):
    """Listes de choix du formulaire de panne, accessibles à tout utilisateur connecté."""
    get_current_user(request)
    with get_db_cursor() as cursor:
        cursor.execute("SELECT id, designation FROM service_tech_equipment ORDER BY designation, id")
        equipment = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT DISTINCT nom_service FROM clinic_services WHERE BTRIM(nom_service) <> '' ORDER BY nom_service")
        services = [row["nom_service"] for row in cursor.fetchall()]
    return {"equipment": equipment, "services": services}


@service_tech_router.post("/maintenance/save")
async def service_tech_maintenance_save(request: Request):
    user = get_current_user(request)
    _require(user, "service_tech_manage")
    data = await request.json()
    site_designation = _text(data, "site_designation", "désignation du site", True)
    equipment_designation = _text(data, "equipment_designation", "désignation de l'équipement", True)
    maintenance_type = _text(data, "type", "type de maintenance") or "Préventive"
    if maintenance_type not in MAINTENANCE_TYPES:
        raise HTTPException(status_code=400, detail="Le type doit être Préventive ou Curative.")
    maintenance_date = _date_value(data, "maintenance_date", "de maintenance")
    if not maintenance_date:
        raise HTTPException(status_code=400, detail="La date de maintenance est obligatoire.")
    start_time = _text(data, "heure_debut", "heure de début", True)
    end_time = _text(data, "heure_fin", "heure de fin", True)
    try:
        if end_time <= start_time:
            raise ValueError
        # Validate HH:MM values before passing them to PostgreSQL.
        from datetime import datetime
        datetime.strptime(start_time, "%H:%M")
        datetime.strptime(end_time, "%H:%M")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="L'heure de fin doit être postérieure à l'heure de début.") from exc
    maintenance_id = data.get("id")
    values = (site_designation, _text(data, "site", "site"), _text(data, "equipment_name", "nom de l'équipement"),
              equipment_designation, maintenance_type, maintenance_date, start_time, end_time,
              _text(data, "ingenieur_interne", "ingénieur interne"),
              _text(data, "ingenieur_externe", "ingénieur externe"), _text(data, "notes", "notes"))
    with get_db_cursor() as cursor:
        if maintenance_id:
            cursor.execute("""UPDATE service_tech_maintenance SET site_designation=%s, site=%s,
                equipment_name=%s, equipment_designation=%s, type=%s, maintenance_date=%s,
                heure_debut=%s, heure_fin=%s, ingenieur_interne=%s, ingenieur_externe=%s,
                notes=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING id""",
                           (*values, int(maintenance_id)))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Maintenance introuvable.")
            saved_id = row["id"]
        else:
            cursor.execute("""INSERT INTO service_tech_maintenance
                (site_designation, site, equipment_name, equipment_designation, type, maintenance_date,
                 heure_debut, heure_fin, ingenieur_interne, ingenieur_externe, notes, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                           (*values, user["id"]))
            saved_id = cursor.fetchone()["id"]
    track_action(request, "SERVICE_TECH_MAINTENANCE_SAVE", f"Maintenance {equipment_designation} planifiée le {maintenance_date}")
    return {"success": True, "id": saved_id}


@service_tech_router.post("/maintenance/{maintenance_id}/report")
async def service_tech_maintenance_upload_report(
    maintenance_id: int, request: Request, file: UploadFile = File(...)
):
    user = get_current_user(request)
    _require(user, "service_tech_manage")
    original_name = Path(file.filename or "rapport").name
    extension = Path(original_name).suffix.lower()
    if extension not in MAINTENANCE_REPORT_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Formats autorisés : PDF, Word ou image JPG/PNG.")
    content = await file.read(MAX_MAINTENANCE_REPORT_BYTES + 1)
    if not content or len(content) > MAX_MAINTENANCE_REPORT_BYTES:
        raise HTTPException(status_code=400, detail="Le rapport est vide ou dépasse 25 Mo.")
    storage_dir = Path(DOCS_DIR) / "service_technique" / "rapports_maintenance"
    storage_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"maintenance_{maintenance_id}_{uuid4().hex}{extension}"
    stored_path = storage_dir / stored_name
    stored_path.write_bytes(content)
    relative_path = str(Path("service_technique") / "rapports_maintenance" / stored_name)
    with get_db_cursor() as cursor:
        cursor.execute("""UPDATE service_tech_maintenance SET rapport_path=%s, rapport_nom=%s,
            updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING id""",
                       (relative_path, original_name, maintenance_id))
        row = cursor.fetchone()
        if not row:
            stored_path.unlink(missing_ok=True)
            raise HTTPException(status_code=404, detail="Maintenance introuvable.")
    track_action(request, "SERVICE_TECH_MAINTENANCE_REPORT", f"Rapport ajouté à la maintenance #{maintenance_id}")
    return {"success": True, "name": original_name}


@service_tech_router.get("/maintenance/{maintenance_id}/report")
def service_tech_maintenance_report(maintenance_id: int, request: Request):
    user = get_current_user(request)
    _require(user, "service_tech_view")
    with get_db_cursor() as cursor:
        cursor.execute("SELECT rapport_path, rapport_nom FROM service_tech_maintenance WHERE id=%s", (maintenance_id,))
        row = cursor.fetchone()
    if not row or not row["rapport_path"]:
        raise HTTPException(status_code=404, detail="Aucun rapport n'est enregistré pour cette maintenance.")
    root = Path(DOCS_DIR).resolve()
    report_path = (root / row["rapport_path"]).resolve()
    if root not in report_path.parents or not report_path.is_file():
        raise HTTPException(status_code=404, detail="Le fichier du rapport est introuvable.")
    return FileResponse(report_path, filename=row["rapport_nom"] or report_path.name)


@service_tech_router.post("/closure-report/{entity_type}/{entity_id}")
async def service_tech_save_closure_report(
    entity_type: str,
    entity_id: int,
    request: Request,
    metadata: str = Form(...),
    files: list[UploadFile] = File(default=[]),
):
    user = get_current_user(request)
    _require(user, "service_tech_manage")
    if entity_type not in {"ticket", "maintenance"}:
        raise HTTPException(status_code=400, detail="Type de clôture invalide.")
    try:
        data = json.loads(metadata)
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Le formulaire du rapport est invalide.") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Le formulaire du rapport est invalide.")
    intervenant = _text(data, "intervenant", "intervenant", True)
    solution = _text(data, "solution_trouvee", "solution trouvée", True)
    execution_date = _date_value(data, "date_execution", "d'exécution")
    start_time = _time_value(data, "heure_debut_travail", "de début")
    end_time = _time_value(data, "heure_fin_travail", "de fin")
    steps = _text(data, "etapes_correction", "étapes de correction", True)
    if not execution_date or not start_time or not end_time:
        raise HTTPException(status_code=400, detail="La date d’exécution et les deux heures de travail sont obligatoires.")
    if end_time <= start_time:
        raise HTTPException(status_code=400, detail="L’heure de fin doit être postérieure à l’heure de début.")
    definitive_value = str(data.get("correction_definitive") or "").lower()
    if definitive_value not in {"true", "false"}:
        raise HTTPException(status_code=400, detail="Indiquez si la correction est définitive ou nécessite une révision.")
    correction_definitive = definitive_value == "true"
    if len(files) > 30:
        raise HTTPException(status_code=400, detail="Le rapport ne peut pas contenir plus de 30 pages numérisées.")

    target_column = "ticket_id" if entity_type == "ticket" else "maintenance_id"
    target_table = "service_tech_tickets" if entity_type == "ticket" else "service_tech_maintenance"
    target_label = "Intervention" if entity_type == "ticket" else "Maintenance"
    storage_dir = Path(DOCS_DIR) / "service_technique" / "rapports_cloture"
    saved_files: list[Path] = []
    page_records: list[dict[str, str]] = []
    try:
        with get_db_cursor() as cursor:
            cursor.execute(f"SELECT id FROM {target_table} WHERE id=%s FOR UPDATE", (entity_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail=f"{target_label} introuvable.")
        total_size = 0
        if files:
            storage_dir.mkdir(parents=True, exist_ok=True)
        for index, upload in enumerate(files, start=1):
            original_name = Path(upload.filename or f"page_{index}.jpg").name
            extension = Path(original_name).suffix.lower()
            if extension not in {".jpg", ".jpeg", ".png", ".pdf"}:
                raise HTTPException(status_code=400, detail="Les pages du rapport doivent être en PDF, JPG ou PNG.")
            content = await upload.read(12 * 1024 * 1024 + 1)
            if not content or len(content) > 12 * 1024 * 1024:
                raise HTTPException(status_code=400, detail=f"La page « {original_name} » est vide ou dépasse 12 Mo.")
            total_size += len(content)
            if total_size > 60 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="La taille totale du rapport numérisé dépasse 60 Mo.")
            stored_name = f"{entity_type}_{entity_id}_page_{index}_{uuid4().hex}{extension}"
            stored_path = storage_dir / stored_name
            stored_path.write_bytes(content)
            saved_files.append(stored_path)
            relative_path = str(Path("service_technique") / "rapports_cloture" / stored_name)
            page_records.append({"path": relative_path, "name": original_name})

        with get_db_cursor() as cursor:
            cursor.execute(f"""INSERT INTO service_tech_closure_reports
                ({target_column}, intervenant, solution_trouvee, date_execution,
                 heure_debut_travail, heure_fin_travail, etapes_correction,
                 correction_definitive, scan_pages, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT ({target_column}) DO UPDATE SET intervenant=EXCLUDED.intervenant,
                    solution_trouvee=EXCLUDED.solution_trouvee, date_execution=EXCLUDED.date_execution,
                    heure_debut_travail=EXCLUDED.heure_debut_travail,
                    heure_fin_travail=EXCLUDED.heure_fin_travail,
                    etapes_correction=EXCLUDED.etapes_correction,
                    correction_definitive=EXCLUDED.correction_definitive,
                    scan_pages=EXCLUDED.scan_pages, created_by=EXCLUDED.created_by,
                    created_at=CURRENT_TIMESTAMP""",
                (entity_id, intervenant, solution, execution_date, start_time, end_time, steps,
                 correction_definitive, json.dumps(page_records, ensure_ascii=False), user["id"]))
            if entity_type == "ticket":
                cursor.execute("UPDATE service_tech_tickets SET statut='Terminé', date_cloture=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=%s", (entity_id,))
            else:
                cursor.execute("UPDATE service_tech_maintenance SET statut='Terminée', updated_at=CURRENT_TIMESTAMP WHERE id=%s", (entity_id,))
    except Exception:
        for saved_path in saved_files:
            saved_path.unlink(missing_ok=True)
        raise
    track_action(request, "SERVICE_TECH_CLOSURE_REPORT", f"Rapport de clôture créé pour {target_label.lower()} #{entity_id}")
    return {"success": True, "pages": len(page_records)}


@service_tech_router.get("/closure-report/{entity_type}/{entity_id}/page/{page_index}")
def service_tech_closure_report_page(entity_type: str, entity_id: int, page_index: int, request: Request):
    user = get_current_user(request)
    _require(user, "service_tech_view")
    if entity_type not in {"ticket", "maintenance"} or page_index < 0:
        raise HTTPException(status_code=400, detail="Page de rapport invalide.")
    target_column = "ticket_id" if entity_type == "ticket" else "maintenance_id"
    with get_db_cursor() as cursor:
        cursor.execute(f"SELECT scan_pages FROM service_tech_closure_reports WHERE {target_column}=%s", (entity_id,))
        row = cursor.fetchone()
    pages = row["scan_pages"] if row else []
    if isinstance(pages, str):
        try:
            pages = json.loads(pages)
        except json.JSONDecodeError:
            pages = []
    if page_index >= len(pages):
        raise HTTPException(status_code=404, detail="Page de rapport introuvable.")
    root = Path(DOCS_DIR).resolve()
    report_path = (root / pages[page_index].get("path", "")).resolve()
    if root not in report_path.parents or not report_path.is_file():
        raise HTTPException(status_code=404, detail="Le fichier numérisé est introuvable.")
    return FileResponse(report_path, filename=pages[page_index].get("name") or report_path.name)


@service_tech_router.post("/equipment/save")
async def service_tech_equipment_save(request: Request):
    user = get_current_user(request)
    _require(user, "service_tech_manage")
    data = await request.json()
    designation = _text(data, "designation", "désignation", True)
    status = _text(data, "statut", "statut") or "En service"
    if status not in EQUIPMENT_STATUSES:
        raise HTTPException(status_code=400, detail="Le statut de l'équipement est invalide.")
    operating_system = _text(data, "systeme_exploitation", "système d'exploitation")
    if operating_system == "Non défini":
        operating_system = ""
    if operating_system not in {"", "Windows", "Linux"}:
        raise HTTPException(status_code=400, detail="Le système d'exploitation doit être Windows ou Linux.")
    equipment_id = data.get("id")
    raw_port = _text(data, "port", "port")
    try:
        port = int(raw_port) if raw_port else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Le port doit être un nombre entre 1 et 65535.") from exc
    if port is not None and not 1 <= port <= 65535:
        raise HTTPException(status_code=400, detail="Le port doit être compris entre 1 et 65535.")
    password = _text(data, "mot_de_passe", "mot de passe")
    password_ciphertext = _protect_equipment_password(password) if password else None
    values = (_text(data, "code", "code"), designation, _text(data, "categorie", "catégorie"),
              _text(data, "marque_modele", "marque et modèle"), _text(data, "numero_serie", "numéro de série"),
              _text(data, "adresse_ip", "adresse IP"), _text(data, "utilisateur", "utilisateur"),
              _text(data, "aet", "AET"), port,
              _text(data, "localisation", "localisation"), _text(data, "fournisseur", "fournisseur"),
              _date_value(data, "date_acquisition", "d'acquisition"), status, operating_system,
              _date_value(data, "derniere_maintenance", "de dernière maintenance"),
              _date_value(data, "prochaine_maintenance", "de prochaine maintenance"),
              _text(data, "notes", "notes"))
    with get_db_cursor() as cursor:
        if equipment_id:
            cursor.execute("""UPDATE service_tech_equipment SET code=%s, designation=%s, categorie=%s,
                marque_modele=%s, numero_serie=%s, adresse_ip=%s, utilisateur=%s,
                aet=%s, port=%s, localisation=%s, fournisseur=%s, date_acquisition=%s,
                statut=%s, systeme_exploitation=%s,
                derniere_maintenance=COALESCE(%s, derniere_maintenance),
                prochaine_maintenance=COALESCE(%s, prochaine_maintenance), notes=%s,
                mot_de_passe_ciphertext=COALESCE(%s,mot_de_passe_ciphertext),
                updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING id""",
                           (*values, password_ciphertext, int(equipment_id)))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Équipement introuvable.")
            saved_id = row["id"]
        else:
            cursor.execute("""INSERT INTO service_tech_equipment
                (code, designation, categorie, marque_modele, numero_serie, adresse_ip, utilisateur,
                 mot_de_passe_ciphertext, aet, port, localisation, fournisseur, date_acquisition,
                 statut, systeme_exploitation, derniere_maintenance, prochaine_maintenance, notes, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                           (*values[:7], password_ciphertext or "", *values[7:], user["id"]))
            saved_id = cursor.fetchone()["id"]
    track_action(request, "SERVICE_TECH_EQUIPMENT_SAVE", f"Équipement {designation} enregistré")
    return {"success": True, "id": saved_id}


@service_tech_router.post("/equipment/delete")
async def service_tech_equipment_delete(request: Request):
    user = get_current_user(request)
    _require(user, "service_tech_manage")
    data = await request.json()
    try:
        equipment_id = int(data.get("id"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Équipement invalide.") from exc
    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM service_tech_equipment WHERE id=%s RETURNING designation", (equipment_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Équipement introuvable.")
    track_action(request, "SERVICE_TECH_EQUIPMENT_DELETE", f"Équipement {row['designation']} supprimé")
    return {"success": True}


@service_tech_router.post("/tickets/save")
async def service_tech_ticket_save(request: Request):
    user = get_current_user(request)
    _require(user, "service_tech_manage")
    data = await request.json()
    title = _text(data, "titre", "titre", True)
    ticket_type = _text(data, "type", "type") or "Incident"
    priority = _text(data, "priorite", "priorité") or "Normale"
    status = _text(data, "statut", "statut") or "Nouveau"
    if ticket_type not in TICKET_TYPES or priority not in PRIORITIES or status not in TICKET_STATUSES:
        raise HTTPException(status_code=400, detail="Le type, la priorité ou le statut de l'intervention est invalide.")
    if status == "Terminé":
        raise HTTPException(status_code=400, detail="Clôturez l’intervention avec le bouton « Clôturer » et enregistrez son rapport.")
    equipment_id = data.get("equipment_id") or None
    try:
        equipment_id = int(equipment_id) if equipment_id else None
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Équipement associé invalide.") from exc
    ticket_id = data.get("id")
    values = (equipment_id, ticket_type, title, _text(data, "description", "description"), priority, status,
              _text(data, "demandeur", "demandeur"), _text(data, "responsable", "responsable"),
              _date_value(data, "date_echeance", "prévue"),
              _time_value(data, "heure_echeance", "prévue"))
    with get_db_cursor() as cursor:
        if ticket_id:
            cursor.execute("""UPDATE service_tech_tickets SET equipment_id=%s, type=%s, titre=%s,
                description=%s, priorite=%s, statut=%s, demandeur=%s, responsable=%s, date_echeance=%s,
                heure_echeance=%s,
                date_cloture=CASE WHEN %s='Terminé' THEN COALESCE(date_cloture,CURRENT_TIMESTAMP) ELSE NULL END,
                updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING id""", (*values, status, int(ticket_id)))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Demande d'intervention introuvable.")
            saved_id = row["id"]
        else:
            cursor.execute("""INSERT INTO service_tech_tickets
                (equipment_id, type, titre, description, priorite, statut, demandeur, responsable,
                 date_echeance, heure_echeance, created_by, date_cloture)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s='Terminé' THEN CURRENT_TIMESTAMP ELSE NULL END)
                RETURNING id""", (*values, user["id"], status))
            saved_id = cursor.fetchone()["id"]
    track_action(request, "SERVICE_TECH_TICKET_SAVE", f"Intervention {title} enregistrée")
    return {"success": True, "id": saved_id}


@service_tech_router.post("/report-failure")
async def service_tech_report_failure(request: Request):
    """Permet à tout utilisateur connecté de signaler une panne au service technique."""
    user = get_current_user(request)
    data = await request.json()
    subject = _text(data, "titre", "titre de la panne", True)
    location = _text(data, "lieu", "lieu", True)
    service = _text(data, "service", "service", True)
    details = _text(data, "description", "description", True)
    equipment_id = data.get("equipment_id") or None
    try:
        equipment_id = int(equipment_id) if equipment_id else None
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Équipement sélectionné invalide.") from exc
    priority = _text(data, "priorite", "priorité") or "Normale"
    if priority not in {"Normale", "Haute", "Urgente"}:
        raise HTTPException(status_code=400, detail="Le niveau de priorité est invalide.")
    if len(subject) > 180 or len(location) > 180 or len(service) > 180 or len(details) > 5000:
        raise HTTPException(status_code=400, detail="La déclaration dépasse la longueur autorisée.")
    stopped_work = data.get("arret_travail") is True or str(data.get("arret_travail") or "").strip().lower() in {"true", "on", "1"}
    requester = f"{user['prenom']} {user['nom']} (@{user['login']})".strip()
    with get_db_cursor() as cursor:
        cursor.execute("SELECT 1 FROM clinic_services WHERE nom_service=%s LIMIT 1", (service,))
        if not cursor.fetchone():
            raise HTTPException(status_code=400, detail="Le service sélectionné n’existe plus.")
        if equipment_id is not None:
            cursor.execute("SELECT 1 FROM service_tech_equipment WHERE id=%s", (equipment_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=400, detail="L’équipement sélectionné n’existe plus.")
        cursor.execute("""INSERT INTO service_tech_tickets
            (equipment_id, type, titre, description, priorite, statut, demandeur,
             responsable, date_echeance, heure_echeance, created_by,
             service_concerne, lieu_panne, arret_travail)
            VALUES (%s, 'Incident', %s, %s, %s, 'Nouveau', %s, '', NULL, NULL, %s, %s, %s, %s)
            RETURNING id""",
            (equipment_id, subject, details, priority, requester, user["id"], service, location, stopped_work))
        ticket_id = cursor.fetchone()["id"]
        cursor.execute("""SELECT id, prenom, nom, role, permissions FROM users
            WHERE status='Actif' ORDER BY id""")
        technical_users = [
            dict(row) for row in cursor.fetchall()
            if row.get("role") == "Administrateur"
            or bool((row.get("permissions") or {}).get("service_tech_view"))
            or bool((row.get("permissions") or {}).get("service_tech_manage"))
        ]
        alert_title = f"Nouvelle panne · {subject}"
        alert_message = f"{service} · {location} · priorité {priority}" + (" · arrêt du travail" if stopped_work else "")
        for recipient in technical_users:
            cursor.execute("""INSERT INTO cliniva_notifications
                (user_id, categorie, titre, message, ticket_id)
                VALUES (%s, 'service_technique', %s, %s, %s)""",
                (recipient["id"], alert_title, alert_message, ticket_id))
        cursor.execute("""SELECT email, type_personnel FROM clinic_staff
            WHERE actif=TRUE AND COALESCE(email, '') <> ''""")
        technical_emails = sorted({
            str(row["email"]).strip() for row in cursor.fetchall()
            if row.get("email") and _is_technical_assignee(str(row.get("type_personnel") or ""))
        })
    track_action(request, "SERVICE_TECH_FAILURE_REPORTED", f"Panne signalée #{ticket_id} : {subject} — {service}")
    email_status = "aucun_destinataire"
    if technical_emails:
        try:
            email_status = await asyncio.to_thread(
                _send_failure_email, technical_emails, ticket_id, subject, service,
                location, priority, details, requester, stopped_work,
            )
        except Exception:
            logger.exception("Échec de l'envoi du courriel pour la panne #%s", ticket_id)
            email_status = "echec"
    return {
        "success": True, "id": ticket_id, "arret_travail": stopped_work,
        "alertes": len(technical_users), "courriels": email_status,
    }


def _send_failure_email(recipients, ticket_id, subject, service, location, priority,
                        details, requester, stopped_work):
    """Envoie la panne aux adresses des collaborateurs techniques du Staff."""
    configuration = get_smtp_configuration()
    if not configuration.get("enabled"):
        logger.info("Courriel panne #%s non envoyé : configuration SMTP désactivée ou absente.", ticket_id)
        return "non_configure"
    priority_color = {"Urgente": "#be123c", "Haute": "#c2410c", "Normale": "#0369a1"}.get(priority, "#475569")
    rows = [
        ("Référence", f"Panne #{ticket_id}"), ("Service", service), ("Lieu", location),
        ("Priorité", priority), ("Arrêt de travail", "Oui" if stopped_work else "Non"),
        ("Déclarée par", requester),
    ]
    rows_html_parts = []
    for label, value in rows:
        safe_value = escape(str(value))
        if label == "Priorité":
            safe_value = (
                f'<span style="display:inline-block;border-radius:999px;padding:4px 10px;'
                f'background:{priority_color};color:#fff;font-size:11px">{safe_value}</span>'
            )
        rows_html_parts.append(
            f'<tr><td style="padding:10px 12px;border-bottom:1px solid #e7eee9;width:36%;font-size:12px;font-weight:700;color:#68786d">{escape(label)}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e7eee9;font-size:13px;font-weight:700;color:#17231b">{safe_value}</td></tr>'
        )
    rows_html = "".join(rows_html_parts)
    html_content = (
        '<p style="margin:0 0 18px;font-size:14px;line-height:1.65;color:#435249">Une nouvelle panne vient d’être déclarée. Voici les informations à traiter :</p>'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="width:100%;border:1px solid #e2eae4;border-collapse:collapse;border-radius:10px;overflow:hidden">'
        f'{rows_html}</table>'
        '<div style="margin-top:18px;padding:16px;border:1px solid #e2eae4;border-left:4px solid #4d8b42;border-radius:10px;background:#f8faf8">'
        '<div style="margin-bottom:8px;font-size:11px;font-weight:800;letter-spacing:.8px;text-transform:uppercase;color:#52705a">Description de la panne</div>'
        f'<div style="font-size:13px;line-height:1.65;color:#26352a;white-space:pre-wrap;overflow-wrap:anywhere">{escape(details)}</div></div>'
        '<p style="margin:20px 0 0;font-size:12px;line-height:1.6;color:#68786d">Ouvre <strong>Service Technique → Incidents et interventions</strong> dans Cliniva pour consulter et prendre en charge le dossier.</p>'
    )
    plain_body = (
        f"Nouvelle déclaration de panne Cliniva · dossier #{ticket_id}\n\nTitre : {subject}\n"
        f"Service : {service}\nLieu : {location}\nPriorité : {priority}\n"
        f"Arrêt de travail : {'Oui' if stopped_work else 'Non'}\nDéclarée par : {requester}\n\n"
        f"Description :\n{details}\n\nConsulte le dossier dans Service Technique → Incidents et interventions.\n"
    )
    html_body = build_cliniva_email_html(
        "Nouvelle déclaration de panne", f"Dossier #{ticket_id} · {subject}", html_content,
        f"Panne {priority.lower()} déclarée au service {service}.",
    )
    send_smtp_email(configuration, recipients, f"[Cliniva] Panne #{ticket_id} · {priority} · {subject}", plain_body, html_body)
    logger.info("Courriel de panne #%s envoyé à %d destinataire(s).", ticket_id, len(recipients))
    return "envoye"


@service_tech_router.get("/notifications")
def service_tech_notifications(request: Request):
    user = get_current_user(request)
    if not (_allowed(user, "service_tech_view") or _allowed(user, "service_tech_manage")):
        return {"unread_count": 0, "notifications": []}
    with get_db_cursor() as cursor:
        cursor.execute("""SELECT id, titre, message, ticket_id, created_at, read_at
            FROM cliniva_notifications WHERE user_id=%s
            ORDER BY created_at DESC LIMIT 30""", (user["id"],))
        rows = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT COUNT(*) AS count FROM cliniva_notifications WHERE user_id=%s AND read_at IS NULL", (user["id"],))
        unread_count = cursor.fetchone()["count"]
    return {"unread_count": unread_count, "notifications": rows}


@service_tech_router.post("/notifications/read")
async def service_tech_notifications_read(request: Request):
    user = get_current_user(request)
    if not (_allowed(user, "service_tech_view") or _allowed(user, "service_tech_manage")):
        raise HTTPException(status_code=403, detail="Vous n'avez pas l'autorisation nécessaire pour consulter ces alertes.")
    data = await request.json()
    notification_id = data.get("id")
    with get_db_cursor() as cursor:
        if notification_id in (None, "all"):
            cursor.execute("UPDATE cliniva_notifications SET read_at=CURRENT_TIMESTAMP WHERE user_id=%s AND read_at IS NULL", (user["id"],))
        else:
            try:
                notification_id = int(notification_id)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Alerte invalide.") from exc
            cursor.execute("UPDATE cliniva_notifications SET read_at=CURRENT_TIMESTAMP WHERE id=%s AND user_id=%s AND read_at IS NULL", (notification_id, user["id"]))
    return {"success": True}


@service_tech_router.post("/tickets/delete")
async def service_tech_ticket_delete(request: Request):
    user = get_current_user(request)
    _require(user, "service_tech_manage")
    data = await request.json()
    try:
        ticket_id = int(data.get("id"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Intervention invalide.") from exc
    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM service_tech_tickets WHERE id=%s RETURNING titre", (ticket_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Demande d'intervention introuvable.")
    track_action(request, "SERVICE_TECH_TICKET_DELETE", f"Intervention {row['titre']} supprimée")
    return {"success": True}


def render_html(user: Optional[Dict[str, Any]] = None) -> str:
    if not user or not _allowed(user, "service_tech_view"):
        raise HTTPException(status_code=403, detail="Accès au module Service Technique refusé.")
    can_manage = _allowed(user, "service_tech_manage")
    manage_controls = """
      <div class="flex flex-wrap gap-2"><button onclick="techOpenEquipment()" class="flex items-center gap-2 rounded-2xl bg-brand-neon px-4 py-2.5 text-xs font-bold text-black shadow-md shadow-brand-neon/20 transition hover:bg-brand-neonDark"><i class="ph-bold ph-plus-circle text-base"></i>Ajouter un équipement</button>
      <button onclick="techOpenTicket()" class="flex items-center gap-2 rounded-2xl border border-white/10 bg-white/10 px-4 py-2.5 text-xs font-bold text-white transition hover:bg-white/15"><i class="ph-bold ph-wrench text-base"></i>Nouvelle intervention</button>
      <button onclick="techOpenMaintenance()" class="flex items-center gap-2 rounded-2xl border border-white/10 bg-white/10 px-4 py-2.5 text-xs font-bold text-white transition hover:bg-white/15"><i class="ph-bold ph-calendar-plus text-base"></i>Planifier une maintenance</button></div>
    """ if can_manage else ""
    page = r'''
<div id="view-service-tech" class="space-y-5 text-slate-800">
 <header class="flex flex-wrap items-center justify-between gap-4 rounded-[2rem] bg-gradient-to-r from-[#101b13] to-[#183b20] px-7 py-6 text-white shadow-lg">
  <div><p class="text-xs font-bold uppercase tracking-[.18em] text-lime-300">Maintenance · Équipements</p><h1 class="mt-1 text-2xl font-extrabold">Service Technique</h1><p class="mt-1 text-sm text-slate-300">Suivi des équipements, incidents et interventions techniques.</p></div>
  __ACTIONS__
 </header>
 <section class="grid grid-cols-2 gap-3 xl:grid-cols-4"><article class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Équipements</span><i class="ph-bold ph-wrench text-xl text-brand-neon"></i></div><div id="techKpiEquipment" class="mt-2 text-2xl font-extrabold text-slate-800 dark:text-white">—</div></article><article class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Interventions ouvertes</span><i class="ph-bold ph-warning-circle text-xl text-amber-500"></i></div><div id="techKpiOpen" class="mt-2 text-2xl font-extrabold text-slate-800 dark:text-white">—</div></article><article class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Urgentes</span><i class="ph-bold ph-siren text-xl text-rose-500"></i></div><div id="techKpiUrgent" class="mt-2 text-2xl font-extrabold text-slate-800 dark:text-white">—</div></article><article class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Maintenances en retard</span><i class="ph-bold ph-calendar-x text-xl text-sky-500"></i></div><div id="techKpiOverdue" class="mt-2 text-2xl font-extrabold text-slate-800 dark:text-white">—</div></article></section>
 <nav class="flex w-fit max-w-full flex-wrap items-center gap-1 rounded-2xl border border-slate-200/80 bg-white p-1.5 shadow-sm dark:border-white/10 dark:bg-[#121c15]" aria-label="Sections du Service Technique">
  <button type="button" data-tech-tab="equipment" aria-selected="true" onclick="techSetTab('equipment')" class="flex items-center gap-2 rounded-xl bg-brand-neon px-4 py-2.5 text-xs font-bold text-black shadow-md shadow-brand-neon/20 transition"><i class="ph-bold ph-wrench text-base"></i>Parc des équipements</button>
  <button type="button" data-tech-tab="maintenance" aria-selected="false" onclick="techSetTab('maintenance')" class="flex items-center gap-2 rounded-xl px-4 py-2.5 text-xs font-bold text-slate-500 transition hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-white/5 dark:hover:text-white"><i class="ph-bold ph-calendar-check text-base"></i>Maintenances</button>
  <button type="button" data-tech-tab="tickets" aria-selected="false" onclick="techSetTab('tickets')" class="flex items-center gap-2 rounded-xl px-4 py-2.5 text-xs font-bold text-slate-500 transition hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-white/5 dark:hover:text-white"><i class="ph-bold ph-wrench text-base"></i>Incidents et interventions</button>
 </nav>
 <section id="techTabEquipment" data-tech-panel="equipment" class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15] sm:p-5"><div class="mb-4 flex flex-wrap items-center justify-between gap-3"><div><h2 class="font-extrabold text-slate-800 dark:text-white">Parc des équipements</h2><p class="text-xs text-slate-500">Suivi de l’état et des informations du parc.</p></div><input id="techEquipmentSearch" oninput="techRenderEquipment()" class="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-800 outline-none focus:border-brand-neon dark:border-white/10 dark:bg-black/30 dark:text-white" placeholder="Rechercher un équipement"></div><div class="overflow-x-auto"><table class="w-full min-w-[900px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600 dark:bg-black/20 dark:text-slate-400"><tr><th class="rounded-l-xl px-3 py-3">Équipement / système</th><th class="px-3 py-3">Catégorie</th><th class="px-3 py-3">Fournisseur / marque</th><th class="px-3 py-3">Localisation</th><th class="px-3 py-3">État</th><th class="rounded-r-xl px-3 py-3 text-right">Actions</th></tr></thead><tbody id="techEquipmentRows"><tr><td colspan="6" class="p-8 text-center text-slate-400">Chargement…</td></tr></tbody></table></div></section>
 <section id="techTabMaintenance" data-tech-panel="maintenance" class="hidden rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15] sm:p-5"><div class="mb-4"><h2 class="font-extrabold text-slate-800 dark:text-white">Planning des maintenances</h2><p class="text-xs text-slate-500">Créneaux, sites, ingénieurs affectés et rapports de maintenance.</p></div><div class="overflow-x-auto"><table class="w-full min-w-[1100px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600 dark:bg-black/20 dark:text-slate-400"><tr><th class="rounded-l-xl px-3 py-3">Date / horaire</th><th class="px-3 py-3">Site</th><th class="px-3 py-3">Équipement</th><th class="px-3 py-3">Type</th><th class="px-3 py-3">Ingénieurs affectés</th><th class="px-3 py-3">Rapport / notes</th><th class="rounded-r-xl px-3 py-3 text-right">Actions</th></tr></thead><tbody id="techMaintenanceRows"><tr><td colspan="7" class="p-8 text-center text-slate-400">Chargement…</td></tr></tbody></table></div></section>
 <section id="techTabTickets" data-tech-panel="tickets" class="hidden rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15] sm:p-5"><div class="mb-4 flex flex-wrap items-center justify-between gap-3"><div><h2 class="font-extrabold text-slate-800 dark:text-white">Incidents et interventions</h2><p class="text-xs text-slate-500">Demandes correctives, préventives et incidents signalés.</p></div><select id="techTicketFilter" onchange="techRenderTickets()" class="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-800 dark:border-white/10 dark:bg-black/30 dark:text-white"><option value="">Tous les statuts</option><option>Nouveau</option><option>Planifié</option><option>En cours</option><option>En attente</option><option>Terminé</option><option>Annulé</option></select></div><div class="overflow-x-auto"><table class="w-full min-w-[900px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600 dark:bg-black/20 dark:text-slate-400"><tr><th class="rounded-l-xl px-3 py-3">Demande</th><th class="px-3 py-3">Équipement</th><th class="px-3 py-3">Priorité</th><th class="px-3 py-3">Responsable</th><th class="px-3 py-3">Échéance</th><th class="px-3 py-3">Statut</th><th class="rounded-r-xl px-3 py-3 text-right">Actions</th></tr></thead><tbody id="techTicketRows"><tr><td colspan="7" class="p-8 text-center text-slate-400">Chargement…</td></tr></tbody></table></div></section>
 <div id="techModal" class="fixed inset-0 z-[100000] hidden items-center justify-center bg-slate-950/60 p-3 backdrop-blur-sm"><div class="max-h-[94vh] w-full max-w-2xl overflow-y-auto rounded-3xl bg-white shadow-2xl dark:bg-[#121c15]"><div class="sticky top-0 z-10 flex items-center justify-between bg-gradient-to-r from-[#101b13] to-[#183b20] px-5 py-4 text-white"><div><div id="techModalEyebrow" class="text-[10px] font-bold uppercase tracking-widest text-brand-neon">Service Technique</div><h3 id="techModalTitle" class="text-lg font-black">Formulaire</h3></div><button onclick="techCloseModal()" class="grid h-9 w-9 place-items-center rounded-xl bg-white/10 hover:bg-white/20" aria-label="Fermer"><i class="ph-bold ph-x"></i></button></div><form id="techForm" onsubmit="techSave(event)" class="space-y-4 p-5"><input id="techEntityType" type="hidden"><input id="techEntityId" type="hidden"><div id="techFormFields" class="grid gap-3 sm:grid-cols-2"></div><div class="flex justify-end gap-2 border-t border-slate-100 pt-4 dark:border-white/10"><button type="button" onclick="techCloseModal()" class="rounded-xl border border-slate-200 px-4 py-2.5 text-sm font-bold text-slate-700 dark:border-white/10 dark:text-slate-200">Fermer</button><button id="techFormSubmit" class="rounded-2xl bg-brand-neon px-5 py-2.5 text-xs font-bold text-black shadow-md shadow-brand-neon/20 transition hover:bg-brand-neonDark"><i class="ph-bold ph-floppy-disk mr-1"></i>Enregistrer</button></div></form></div></div>
 <div id="techToast" class="fixed bottom-5 right-5 z-[100001] hidden max-w-sm rounded-xl bg-slate-900 px-4 py-3 text-sm font-semibold text-white shadow-xl"></div>
 <script>
 (()=>{const $=id=>document.getElementById(id);let state={equipment:[],tickets:[],maintenances:[],can_manage:false};const canManage=__CAN_MANAGE__;const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const notify=(message,bad=false)=>{const el=$('techToast');el.textContent=message;el.className='fixed bottom-5 right-5 z-[100001] max-w-sm rounded-xl px-4 py-3 text-sm font-semibold text-white shadow-xl '+(bad?'bg-rose-700':'bg-slate-900');el.classList.remove('hidden');setTimeout(()=>el.classList.add('hidden'),3500)};const api=async(path,options={})=>{const response=await fetch('/api/service-technique'+path,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(options.headers||{})},...options});const body=await response.json().catch(()=>({}));if(!response.ok)throw new Error(body.detail||'Une erreur est survenue.');return body};
 window.techSetTab=(tab)=>{const allowed=['equipment','maintenance','tickets'];if(!allowed.includes(tab))tab='equipment';document.querySelectorAll('#view-service-tech [data-tech-panel]').forEach(panel=>panel.classList.toggle('hidden',panel.dataset.techPanel!==tab));document.querySelectorAll('#view-service-tech [data-tech-tab]').forEach(button=>{const active=button.dataset.techTab===tab;button.setAttribute('aria-selected',active?'true':'false');button.className=active?'flex items-center gap-2 rounded-xl bg-brand-neon px-4 py-2.5 text-xs font-bold text-black shadow-md shadow-brand-neon/20 transition':'flex items-center gap-2 rounded-xl px-4 py-2.5 text-xs font-bold text-slate-500 transition hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-white/5 dark:hover:text-white'});try{localStorage.setItem('cliniva_service_tech_tab',tab)}catch(_){}};
 window.techRefresh=async()=>{try{state=await api('/data');$('techKpiEquipment').textContent=state.kpi.equipment;$('techKpiOpen').textContent=state.kpi.open;$('techKpiUrgent').textContent=state.kpi.urgent;$('techKpiOverdue').textContent=state.kpi.overdue;techRenderEquipment();techRenderMaintenances();techRenderTickets();let savedTab='equipment';try{savedTab=localStorage.getItem('cliniva_service_tech_tab')||'equipment'}catch(_){}techSetTab(savedTab)}catch(e){notify(e.message,true)}};
 window.techRenderEquipment=()=>{const search=($('techEquipmentSearch')?.value||'').trim().toLowerCase();const rows=state.equipment.filter(x=>[x.code,x.designation,x.categorie,x.localisation,x.numero_serie,x.fournisseur,x.marque_modele,x.systeme_exploitation].join(' ').toLowerCase().includes(search));$('techEquipmentRows').innerHTML=rows.length?rows.map(x=>{const isWindows=x.systeme_exploitation==='Windows',isLinux=x.systeme_exploitation==='Linux';const osBadge=isWindows?'<span class="inline-flex items-center gap-1 rounded-md bg-sky-50 px-1.5 py-0 text-sky-700 dark:bg-sky-400/10 dark:text-sky-300"><i class="ph-bold ph-windows-logo"></i><span>Windows</span></span>':isLinux?'<span class="inline-flex items-center gap-1 rounded-md bg-amber-50 px-1.5 py-0 text-amber-800 dark:bg-amber-400/10 dark:text-amber-300"><i class="ph-bold ph-linux-logo"></i><span>Linux</span></span>':'<span class="text-slate-400">Système non défini</span>';const remoteButton=x.adresse_ip&&isWindows||x.adresse_ip&&isLinux?`<button onclick="techLaunchRemote(${x.id})" class="mt-1 inline-flex items-center gap-1 rounded-lg bg-emerald-50 px-2 py-1 text-[10px] font-bold text-emerald-800 transition hover:bg-emerald-100 dark:bg-emerald-400/10 dark:text-emerald-300" title="${isWindows?'Lancer le Bureau à distance':'Lancer SSH'}"><i class="ph-bold ${isWindows?'ph-desktop':'ph-terminal-window'}"></i>${isWindows?'RDP':'SSH'}</button>`:'';return `<tr class="border-b border-slate-100 text-slate-700 transition hover:bg-sky-50 dark:border-white/5 dark:text-slate-200 dark:hover:bg-slate-700/50"><td class="px-3 py-1.5"><div class="flex items-center gap-2"><span class="grid h-8 w-8 shrink-0 place-items-center rounded-xl ${isWindows?'bg-sky-50 text-sky-700 ring-1 ring-sky-100 dark:bg-sky-400/10 dark:text-sky-300 dark:ring-sky-300/10':isLinux?'bg-amber-50 text-amber-800 ring-1 ring-amber-100 dark:bg-amber-400/10 dark:text-amber-300 dark:ring-amber-300/10':'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-100 dark:bg-emerald-400/10 dark:text-emerald-300 dark:ring-emerald-300/10'}"><i class="ph-bold ${isWindows?'ph-windows-logo':isLinux?'ph-linux-logo':'ph-hard-drives'} text-base"></i></span><div class="min-w-0"><div class="truncate font-bold">${esc(x.designation)}</div><div class="text-[11px] leading-tight text-slate-500">${esc(x.code||x.numero_serie||'Sans référence')}</div><div class="mt-0.5 flex flex-wrap items-center gap-1 text-[10px] font-semibold">${osBadge}${remoteButton}</div></div></div></td><td class="px-3 py-1.5">${esc(x.categorie||'—')}</td><td class="px-3 py-1.5"><div class="font-semibold">${esc(x.fournisseur||'—')}</div><div class="text-xs text-slate-500">${esc(x.marque_modele||'')}</div></td><td class="px-3 py-1.5">${esc(x.localisation||'—')}</td><td class="px-3 py-1.5"><span class="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-bold text-slate-700 dark:bg-white/10 dark:text-slate-200">${esc(x.statut)}</span></td><td class="px-3 py-1.5 text-right">${canManage?`<button onclick="techOpenEquipment(${x.id})" class="rounded-lg p-1.5 text-sky-700 hover:bg-sky-50 dark:text-sky-300 dark:hover:bg-white/5" title="Modifier"><i class="ph-bold ph-pencil-simple"></i></button><button onclick="techDelete('equipment',${x.id})" class="rounded-lg p-1.5 text-rose-700 hover:bg-rose-50 dark:text-rose-300 dark:hover:bg-white/5" title="Supprimer"><i class="ph-bold ph-trash"></i></button>`:'—'}</td></tr>`}).join(''):'<tr><td colspan="6" class="p-8 text-center text-slate-400">Aucun équipement trouvé.</td></tr>'};
 window.techLaunchRemote=async(id)=>{const equipment=state.equipment.find(item=>Number(item.id)===Number(id));if(!equipment)return;const mode=equipment.systeme_exploitation==='Windows'?'Windows':'Linux';try{if(!window.pywebview?.api?.service_tech_remote_access)throw new Error('Le lancement distant est disponible depuis l’application Cliniva de bureau.');const result=await window.pywebview.api.service_tech_remote_access(mode,equipment.adresse_ip,equipment.utilisateur||'');if(!result?.ok)throw new Error(result?.error||'Impossible de lancer l’accès distant.');notify(mode==='Windows'?'Bureau à distance lancé.':'Terminal SSH lancé.')}catch(error){notify(error.message||'Impossible de lancer l’accès distant.',true)}};
 window.techRenderMaintenances=()=>{$('techMaintenanceRows').innerHTML=state.maintenances.length?state.maintenances.map(x=>`<tr class="border-b border-slate-100 text-slate-700 transition hover:bg-lime-50/50 dark:border-white/5 dark:text-slate-200 dark:hover:bg-white/5"><td class="px-3 py-3"><div class="font-bold">${esc(x.maintenance_date)}</div><div class="text-xs text-slate-500">${esc(String(x.heure_debut||'').slice(0,5))} – ${esc(String(x.heure_fin||'').slice(0,5))}</div></td><td class="px-3 py-3"><div class="font-semibold">${esc(x.site_designation)}</div><div class="text-xs text-slate-500">${esc(x.site||'')}</div></td><td class="px-3 py-3"><div class="font-semibold">${esc(x.equipment_name||'')}</div><div class="text-xs text-slate-500">${esc(x.equipment_designation)}</div></td><td class="px-3 py-3"><span class="rounded-full px-2.5 py-1 text-xs font-bold ${x.type==='Préventive'?'bg-emerald-50 text-emerald-800 dark:bg-emerald-400/10 dark:text-emerald-300':'bg-amber-50 text-amber-800 dark:bg-amber-400/10 dark:text-amber-300'}">${esc(x.type)}</span><div class="mt-1 text-[10px] font-bold ${x.statut==='Terminée'?'text-emerald-600':'text-slate-500'}">${esc(x.statut||'Planifiée')}</div></td><td class="px-3 py-3 text-xs"><div>${esc(x.ingenieur_interne||'Interne : —')}</div><div class="mt-1 text-slate-500">${esc(x.ingenieur_externe||'Externe : —')}</div></td><td class="px-3 py-3 text-xs"><div>${x.report_intervenant?`<button onclick="techOpenClosure('maintenance',${x.id},true)" class="font-bold text-sky-700 hover:underline dark:text-sky-300">Rapport · ${Number((x.report_pages||[]).length)} page(s)</button>`:x.has_report?`<a class="font-bold text-sky-700 hover:underline dark:text-sky-300" href="/api/service-technique/maintenance/${x.id}/report" target="_blank">${esc(x.rapport_nom||'Rapport')}</a>`:'<span class="text-slate-400">Rapport non ajouté</span>'}</div><div class="mt-1 max-w-48 truncate text-slate-500" title="${esc(x.notes||'')}">${esc(x.notes||'')}</div></td><td class="px-3 py-3 text-right"><button onclick="techOpenMaintenance(${x.id})" class="rounded-lg p-2 text-sky-700 hover:bg-sky-50 dark:text-sky-300 dark:hover:bg-white/5" title="Modifier"><i class="ph-bold ph-pencil-simple"></i></button>${canManage&&x.statut!=='Terminée'?`<button onclick="techOpenClosure('maintenance',${x.id})" class="inline-flex items-center gap-1 rounded-lg bg-emerald-50 px-2 py-1.5 text-[10px] font-bold text-emerald-800 dark:bg-emerald-400/10 dark:text-emerald-300" title="Clôturer et rédiger le rapport"><i class="ph-bold ph-check-circle"></i>Clôturer</button>`:''}</td></tr>`).join(''):'<tr><td colspan="7" class="p-8 text-center text-slate-400">Aucune maintenance planifiée.</td></tr>'};
 window.techRenderTickets=()=>{const filter=$('techTicketFilter')?.value||'';const rows=state.tickets.filter(x=>!filter||x.statut===filter);$('techTicketRows').innerHTML=rows.length?rows.map(x=>`<tr class="border-b border-slate-100 text-slate-700 transition hover:bg-lime-50/50 dark:border-white/5 dark:text-slate-200 dark:hover:bg-white/5"><td class="px-3 py-3"><div class="font-bold">${esc(x.titre)}</div><div class="text-xs text-slate-500">${esc(x.type)} · ${esc(x.demandeur||'Demandeur non renseigné')}</div>${x.service_concerne||x.lieu_panne?`<div class="mt-1 text-[11px] text-slate-500">${esc(x.service_concerne||'Service non renseigné')} · ${esc(x.lieu_panne||'Lieu non renseigné')}</div>`:''}${x.arret_travail?'<span class="mt-1 inline-flex items-center gap-1 rounded-full bg-rose-100 px-2 py-0.5 text-[10px] font-bold text-rose-800 dark:bg-rose-400/10 dark:text-rose-300"><i class="ph-bold ph-prohibit-inset"></i>Travail interrompu</span>':''}</td><td class="px-3 py-3">${esc(x.equipment_name||'—')}</td><td class="px-3 py-3"><span class="rounded-full px-2.5 py-1 text-xs font-bold ${x.priorite==='Urgente'?'bg-rose-100 text-rose-800 dark:bg-rose-400/10 dark:text-rose-300':x.priorite==='Haute'?'bg-amber-100 text-amber-800 dark:bg-amber-400/10 dark:text-amber-300':'bg-slate-100 text-slate-700 dark:bg-white/10 dark:text-slate-300'}">${esc(x.priorite)}</span></td><td class="px-3 py-3">${esc(x.responsable||'—')}</td><td class="px-3 py-3"><div>${esc(x.date_echeance||'—')}</div><div class="text-xs text-slate-500">${esc(String(x.heure_echeance||'').slice(0,5))}</div></td><td class="px-3 py-3"><span class="rounded-full bg-sky-50 px-2.5 py-1 text-xs font-bold text-sky-800 dark:bg-sky-400/10 dark:text-sky-300">${esc(x.statut)}</span></td><td class="px-3 py-3 text-right">${x.report_intervenant?`<button onclick="techOpenClosure('ticket',${x.id},true)" class="rounded-lg p-1.5 text-emerald-700 hover:bg-emerald-50 dark:text-emerald-300" title="Voir le rapport de clôture"><i class="ph-bold ph-file-text"></i></button>`:''}${canManage&&x.statut==='Terminé'&&!x.report_intervenant?`<button onclick="techOpenClosure('ticket',${x.id})" class="inline-flex items-center gap-1 rounded-lg bg-emerald-50 px-2 py-1.5 text-[10px] font-bold text-emerald-800 dark:bg-emerald-400/10 dark:text-emerald-300" title="Ajouter le rapport de clôture"><i class="ph-bold ph-file-plus"></i>Rapport</button>`:''}${canManage&&x.statut!=='Terminé'&&x.statut!=='Annulé'?`<button onclick="techOpenTicket(${x.id})" class="rounded-lg p-1.5 text-sky-700 hover:bg-sky-50 dark:text-sky-300 dark:hover:bg-white/5" title="Modifier"><i class="ph-bold ph-pencil-simple"></i></button><button onclick="techOpenClosure('ticket',${x.id})" class="inline-flex items-center gap-1 rounded-lg bg-emerald-50 px-2 py-1.5 text-[10px] font-bold text-emerald-800 dark:bg-emerald-400/10 dark:text-emerald-300" title="Clôturer et rédiger le rapport"><i class="ph-bold ph-check-circle"></i>Clôturer</button><button onclick="techDelete('ticket',${x.id})" class="rounded-lg p-1.5 text-rose-700 hover:bg-rose-50 dark:text-rose-300 dark:hover:bg-white/5" title="Supprimer"><i class="ph-bold ph-trash"></i></button>`:''}</td></tr>`).join(''):'<tr><td colspan="7" class="p-8 text-center text-slate-400">Aucune intervention pour ce filtre.</td></tr>'};
 const field=(name,label,type='text',value='',extra='')=>`<label class="block text-xs font-bold text-slate-600 dark:text-slate-300">${label}<input name="${name}" type="${type}" value="${esc(value)}" ${extra} class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm font-medium text-slate-800 outline-none focus:border-brand-neon focus:bg-white dark:border-white/10 dark:bg-black/30 dark:text-white"></label>`;const select=(name,label,items,value)=>`<label class="block text-xs font-bold text-slate-600 dark:text-slate-300">${label}<select name="${name}" class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm font-medium text-slate-800 outline-none focus:border-brand-neon dark:border-white/10 dark:bg-black/30 dark:text-white">${items.map(x=>`<option ${x===value?'selected':''}>${esc(x)}</option>`).join('')}</select></label>`;const selectChoices=(name,label,choices,value)=>{const items=[...choices];if(value&&!items.some(item=>item.value===value))items.unshift({value,label:value});return `<label class="block text-xs font-bold text-slate-600 dark:text-slate-300">${label}<select name="${name}" class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm font-medium text-slate-800 outline-none focus:border-brand-neon dark:border-white/10 dark:bg-black/30 dark:text-white"><option value="">— Sélectionner —</option>${items.map(item=>`<option value="${esc(item.value)}" ${item.value===value?'selected':''}>${esc(item.label)}</option>`).join('')}</select></label>`};
 let closureFiles=[];const localDate=()=>{const d=new Date();return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,10)};window.techRenderClosureFiles=()=>{const box=$('techClosurePages');if(!box)return;box.innerHTML=closureFiles.map((file,index)=>`<div class="flex items-center justify-between gap-2 rounded-lg bg-white px-3 py-2 text-xs dark:bg-black/20"><span class="truncate"><i class="ph-bold ph-file-image mr-1 text-sky-600"></i>Page ${index+1} · ${esc(file.name)}</span><button type="button" onclick="techRemoveClosurePage(${index})" class="rounded p-1 text-rose-600 hover:bg-rose-50" aria-label="Retirer cette page"><i class="ph-bold ph-x"></i></button></div>`).join('')||'<span class="text-xs text-slate-400">Aucune page ajoutée.</span>'};window.techRemoveClosurePage=index=>{closureFiles.splice(index,1);techRenderClosureFiles()};window.techAddClosureFiles=event=>{const incoming=Array.from(event.target.files||[]);if(closureFiles.length+incoming.length>30){notify('Le rapport est limité à 30 pages.',true)}else{closureFiles.push(...incoming);techRenderClosureFiles()}event.target.value=''};window.techDetectClosureScanner=async()=>{const status=$('techScannerStatus'),selectBox=$('techScannerSelect');try{const bridge=window.pywebview?.api;if(!bridge?.client_scanner_detect)throw new Error('Le scan nécessite Cliniva ouvert sur le PC qui possède le scanner.');const result=await bridge.client_scanner_detect();if(!result?.success)throw new Error(result?.message||'Aucun scanner détecté.');const scanners=result.scanners||[];selectBox.innerHTML=scanners.map(scanner=>`<option value="${esc(scanner.id)}">${esc(scanner.name)}</option>`).join('');if(!scanners.length)throw new Error(result.message||'Aucun scanner WIA détecté.');status.textContent=`${scanners.length} scanner(s) disponible(s) sur ce poste.`;$('techScanPageBtn').disabled=false}catch(error){status.textContent=error.message||'Détection impossible.';notify(status.textContent,true)}};window.techScanClosurePage=async()=>{const status=$('techScannerStatus'),button=$('techScanPageBtn');if(closureFiles.length>=30){notify('Le rapport est limité à 30 pages.',true);return}try{button.disabled=true;status.textContent='Numérisation de la page en cours…';const bridge=window.pywebview?.api;if(!bridge?.client_scanner_acquire)throw new Error('Le scanner de ce poste n’est pas accessible.');const result=await bridge.client_scanner_acquire($('techScannerSelect').value||'');if(!result?.success)throw new Error(result?.message||'La page n’a pas pu être numérisée.');const blob=await(await fetch(result.image_data)).blob();closureFiles.push(new File([blob],result.name||`rapport_page_${closureFiles.length+1}.jpg`,{type:blob.type||'image/jpeg'}));techRenderClosureFiles();status.textContent=`${closureFiles.length} page(s) numérisée(s). Cliquez à nouveau pour la page suivante.`}catch(error){status.textContent=error.message||'Échec de numérisation.';notify(status.textContent,true)}finally{button.disabled=false}};window.techOpenClosure=(kind,id,readonly=false)=>{const record=(kind==='ticket'?state.tickets:state.maintenances).find(row=>Number(row.id)===Number(id));if(!record)return;closureFiles=[];$('techEntityType').value=`closure_${kind}`;$('techEntityId').value=id;$('techModalEyebrow').textContent=kind==='ticket'?'Clôture d’intervention':'Clôture de maintenance';$('techModalTitle').textContent=readonly?'Rapport de clôture':'Clôturer et créer le rapport';$('techFormSubmit').classList.toggle('hidden',readonly);const disabled=readonly?'disabled':'';const value=(key,fallback='')=>record[key]??fallback;const dateValue=value('report_date',localDate());const timeField=(name,label,val)=>field(name,label,'time',String(val||'').slice(0,5),`required ${disabled}`);const textArea=(name,label,val)=>`<label class="block text-xs font-bold text-slate-600 dark:text-slate-300 sm:col-span-2">${label}<textarea name="${name}" rows="2" required ${disabled} class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm font-medium text-slate-800 outline-none focus:border-brand-neon dark:border-white/10 dark:bg-black/30 dark:text-white">${esc(val)}</textarea></label>`;const selected=readonly?Boolean(record.report_definitive):false;const pages=record.report_pages||[];const savedPages=pages.map((page,index)=>`<a class="inline-flex items-center gap-1 rounded-lg bg-white px-2.5 py-1.5 text-xs font-bold text-sky-700 hover:underline dark:bg-black/20 dark:text-sky-300" href="/api/service-technique/closure-report/${kind}/${id}/page/${index}" target="_blank"><i class="ph-bold ph-file-image"></i>Page ${index+1} · ${esc(page.name||'Scan')}</a>`).join('');const scanSection=readonly?`<div class="sm:col-span-2 rounded-xl border border-slate-200 p-3 dark:border-white/10"><div class="mb-2 text-xs font-bold">Pages numérisées (${pages.length})</div><div class="flex flex-wrap gap-2">${savedPages||'<span class="text-xs text-slate-400">Aucune page numérisée.</span>'}</div></div>`:`<div class="sm:col-span-2 rounded-2xl border border-sky-100 bg-sky-50/70 p-4 dark:border-sky-300/10 dark:bg-sky-400/5"><div class="font-bold text-slate-800 dark:text-white"><i class="ph-bold ph-scan mr-1 text-sky-600"></i>Scanner le rapport manuscrit · plusieurs pages</div><p class="mt-1 text-xs text-slate-500">Numérisez une page à la fois et répétez l’action pour les pages suivantes, ou sélectionnez des images/PDF déjà numérisés.</p><div class="mt-3 flex flex-wrap items-center gap-2"><button type="button" onclick="techDetectClosureScanner()" class="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-bold dark:border-white/10 dark:bg-black/20">Détecter le scanner</button><select id="techScannerSelect" class="min-w-40 flex-1 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs dark:border-white/10 dark:bg-black/20"></select><button id="techScanPageBtn" type="button" disabled onclick="techScanClosurePage()" class="rounded-lg bg-sky-600 px-3 py-2 text-xs font-bold text-white disabled:opacity-40"><i class="ph-bold ph-scan mr-1"></i>Scanner une page</button><label class="cursor-pointer rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-bold dark:border-white/10 dark:bg-black/20"><i class="ph-bold ph-upload-simple mr-1"></i>Ajouter des pages<input type="file" multiple accept="image/jpeg,image/png,application/pdf" class="hidden" onchange="techAddClosureFiles(event)"></label></div><div id="techScannerStatus" class="mt-2 text-xs text-slate-500">Scanner non détecté.</div><div id="techClosurePages" class="mt-2 space-y-1.5"></div></div>`;$('techFormFields').innerHTML=field('intervenant','Intervenant *','text',value('report_intervenant',kind==='ticket'?value('responsable'):value('ingenieur_interne')) ,`required ${disabled}`)+field('date_execution','Date d’exécution *','date',dateValue,`required ${disabled}`)+field('heure_debut_travail','Heure de début du travail *','time',String(value('report_start',kind==='maintenance'?value('heure_debut'):'' )).slice(0,5),`required ${disabled}`)+field('heure_fin_travail','Heure de fin du travail *','time',String(value('report_end',kind==='maintenance'?value('heure_fin'):'' )).slice(0,5),`required ${disabled}`)+textArea('solution_trouvee','Solution trouvée *',value('report_solution'))+textArea('etapes_correction','Étapes de correction *',value('report_steps'))+`<fieldset class="sm:col-span-2"><legend class="text-xs font-bold text-slate-600 dark:text-slate-300">Résultat de la correction *</legend><div class="mt-2 flex flex-wrap gap-4 text-sm"><label class="inline-flex items-center gap-2"><input type="radio" name="correction_definitive" value="true" ${selected?'checked':''} ${readonly?'disabled':'required'}>Correction définitive</label><label class="inline-flex items-center gap-2"><input type="radio" name="correction_definitive" value="false" ${readonly&&!selected?'checked':''} ${readonly?'disabled':'required'}>Une révision sera nécessaire</label></div></fieldset>`+scanSection;if(!readonly)techRenderClosureFiles();techShowModal()};
 window.techOpenEquipment=(id)=>{if(!canManage)return;const x=state.equipment.find(row=>row.id===id)||{};$('techEntityType').value='equipment';$('techEntityId').value=x.id||'';$('techModalEyebrow').textContent='Parc technique';$('techModalTitle').textContent=x.id?'Modifier un équipement':'Ajouter un équipement';const passwordHint=x.password_configured?'Mot de passe enregistré · laisser vide pour le conserver':'Mot de passe de connexion';$('techFormFields').innerHTML=field('designation','Désignation *','text',x.designation||'','required')+field('code','Code inventaire','text',x.code||'')+field('categorie','Catégorie','text',x.categorie||'')+field('marque_modele','Marque / modèle','text',x.marque_modele||'')+field('numero_serie','Numéro de série','text',x.numero_serie||'')+field('adresse_ip','Adresse IP','text',x.adresse_ip||'','placeholder="192.168.1.10"')+field('utilisateur','Utilisateur','text',x.utilisateur||'')+field('mot_de_passe','Mot de passe','password','',`autocomplete="new-password" placeholder="${passwordHint}"`)+field('aet','AET','text',x.aet||'','placeholder="AE Title"')+field('port','Port','number',x.port||'','min="1" max="65535"')+field('localisation','Localisation / service','text',x.localisation||'')+field('fournisseur','Fournisseur','text',x.fournisseur||'')+field('date_acquisition','Date d’acquisition','date',x.date_acquisition||'')+select('statut','État',['En service','En maintenance','Hors service','Réformé'],x.statut||'En service')+select('systeme_exploitation','Système d’exploitation',['Non défini','Windows','Linux'],x.systeme_exploitation||'Non défini')+`<label class="block text-xs font-bold text-slate-600 dark:text-slate-300 sm:col-span-2">Notes<textarea name="notes" rows="2" class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm font-medium outline-none focus:border-brand-neon dark:border-white/10 dark:bg-black/30 dark:text-white">${esc(x.notes||'')}</textarea></label>`;techShowModal()};
 window.techOpenMaintenance=(id)=>{if(!canManage)return;const x=state.maintenances.find(row=>row.id===id)||{};$('techEntityType').value='maintenance';$('techEntityId').value=x.id||'';$('techModalEyebrow').textContent='Maintenance préventive et curative';$('techModalTitle').textContent=x.id?'Modifier la maintenance':'Planifier une maintenance';$('techFormFields').innerHTML=field('site_designation','Désignation du site *','text',x.site_designation||'','required')+field('site','DZ Site','text',x.site||'')+field('equipment_name','Nom de l’équipement','text',x.equipment_name||'')+field('equipment_designation','Désignation de l’équipement *','text',x.equipment_designation||'','required')+select('type','Type de maintenance',['Préventive','Curative'],x.type||'Préventive')+field('maintenance_date','Date de la maintenance *','date',x.maintenance_date||'','required')+field('heure_debut','Heure de début *','time',String(x.heure_debut||'').slice(0,5),'required')+field('heure_fin','Heure de fin *','time',String(x.heure_fin||'').slice(0,5),'required')+field('ingenieur_interne','Ingénieur interne affecté','text',x.ingenieur_interne||'')+field('ingenieur_externe','Ingénieur externe affecté','text',x.ingenieur_externe||'')+`<label class="block text-xs font-bold text-slate-600 dark:text-slate-300 sm:col-span-2">Notes<textarea name="notes" rows="3" class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm outline-none focus:border-brand-neon dark:border-white/10 dark:bg-black/30 dark:text-white">${esc(x.notes||'')}</textarea></label>`;techShowModal()};
 window.techOpenTicket=(id)=>{if(!canManage)return;const x=state.tickets.find(row=>row.id===id)||{};const equipmentOptions=['<option value="">Aucun équipement associé</option>',...state.equipment.map(e=>`<option value="${e.id}" ${Number(x.equipment_id)===Number(e.id)?'selected':''}>${esc(e.designation)}</option>`)].join('');const requestTypes=['Incident','Maintenance préventive','Maintenance corrective','Demande non définie','Vérification de routine','Calibration'];const oldType=x.type==='Demande'?'Demande non définie':x.type||'Incident';$('techEntityType').value='ticket';$('techEntityId').value=x.id||'';$('techModalEyebrow').textContent='Incidents et maintenance';$('techModalTitle').textContent=x.id?'Modifier une demande':'Nouvelle intervention';$('techFormFields').innerHTML=select('type','Type de demande',requestTypes,oldType)+select('priorite','Priorité',['Basse','Normale','Haute','Urgente'],x.priorite||'Normale')+field('titre','Titre *','text',x.titre||'','required')+`<label class="block text-xs font-bold text-slate-600 dark:text-slate-300">Équipement<select name="equipment_id" class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm dark:border-white/10 dark:bg-black/30 dark:text-white">${equipmentOptions}</select></label>`+selectChoices('demandeur','Demandeur',state.requesters||[],x.demandeur||'')+selectChoices('responsable','Responsable / technicien',state.engineers||[],x.responsable||'')+field('date_echeance','Date prévue','date',x.date_echeance||'')+field('heure_echeance','Heure prévue','time',String(x.heure_echeance||'').slice(0,5))+select('statut','Statut',['Nouveau','Planifié','En cours','En attente','Annulé'],x.statut||'Nouveau')+`<label class="block text-xs font-bold text-slate-600 dark:text-slate-300 sm:col-span-2">Description<textarea name="description" rows="3" class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm outline-none focus:border-brand-neon dark:border-white/10 dark:bg-black/30 dark:text-white">${esc(x.description||'')}</textarea></label>`;techShowModal()};
 function techShowModal(){$('techModal').classList.remove('hidden');$('techModal').classList.add('flex');document.body.style.overflow='hidden'}window.techCloseModal=()=>{$('techModal').classList.add('hidden');$('techModal').classList.remove('flex');$('techFormSubmit').classList.remove('hidden');document.body.style.overflow=''};
 window.techSave=async event=>{event.preventDefault();const form=event.currentTarget,type=$('techEntityType').value,id=$('techEntityId').value;if(type.startsWith('closure_')){const kind=type.slice('closure_'.length),body=new FormData(),metadata=Object.fromEntries(new FormData(form).entries());body.append('metadata',JSON.stringify(metadata));closureFiles.forEach(file=>body.append('files',file,file.name));try{const response=await fetch(`/api/service-technique/closure-report/${kind}/${id}`,{method:'POST',credentials:'same-origin',body});const result=await response.json().catch(()=>({}));if(!response.ok)throw new Error(result.detail||'Le rapport de clôture n’a pas pu être enregistré.');techCloseModal();notify(`Rapport de clôture enregistré · ${result.pages} page(s) numérisée(s).`);await techRefresh()}catch(error){notify(error.message,true)}return}const payload=Object.fromEntries(new FormData(form).entries());if(id)payload.id=Number(id);const endpoint=type==='equipment'?'/equipment/save':type==='maintenance'?'/maintenance/save':'/tickets/save';try{await api(endpoint,{method:'POST',body:JSON.stringify(payload)});techCloseModal();notify(type==='equipment'?'Équipement enregistré.':type==='maintenance'?'Maintenance planifiée.':'Intervention enregistrée.');await techRefresh()}catch(e){notify(e.message,true)}};
 window.techSubmitReport=async(event,id)=>{const file=event.target.files?.[0];if(!file)return;const body=new FormData();body.append('file',file);try{const response=await fetch(`/api/service-technique/maintenance/${id}/report`,{method:'POST',credentials:'same-origin',body});const result=await response.json().catch(()=>({}));if(!response.ok)throw new Error(result.detail||'Le rapport n’a pas pu être envoyé.');notify(`Rapport « ${result.name} » enregistré.`);await techRefresh()}catch(e){notify(e.message,true)}finally{event.target.value=''}};
 window.techDelete=async(type,id)=>{if(!canManage||!await window.showConfirm(type==='equipment'?'Supprimer cet équipement ? Les interventions associées resteront dans le registre.':'Supprimer cette demande d’intervention ?'))return;try{await api(type==='equipment'?'/equipment/delete':'/tickets/delete',{method:'POST',body:JSON.stringify({id})});notify('Suppression effectuée.');await techRefresh()}catch(e){notify(e.message,true)}};
 $('techModal').addEventListener('click',e=>{if(e.target===$('techModal'))techCloseModal()});window.techRefresh()})();
 </script>
</div>
'''
    return page.replace("__ACTIONS__", manage_controls).replace("__CAN_MANAGE__", "true" if can_manage else "false")
