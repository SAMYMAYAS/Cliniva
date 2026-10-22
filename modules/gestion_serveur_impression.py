"""Serveur d'impression centralise des livrets médicaux validés."""

import json
import base64
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import subprocess
from datetime import datetime
import time
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.auth import get_current_user_optional
from database import get_db_cursor


print_server_router = APIRouter(prefix="/api/serveur-impression", tags=["Serveur d'impression"])
logger = logging.getLogger("ClinivaPrintServer")
BASE_DIR = Path(__file__).resolve().parent.parent


def _load_print_config() -> Dict[str, Any]:
    from main import load_app_configuration
    return load_app_configuration()


def _save_print_config(config: Dict[str, Any]) -> None:
    from main import CONFIG_FILE
    temporary_path = f"{CONFIG_FILE}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as config_file:
        json.dump(config, config_file, ensure_ascii=False, indent=4)
    os.replace(temporary_path, CONFIG_FILE)


def _enumerate_server_printers():
    if sys.platform != "win32":
        raise HTTPException(status_code=503, detail="La détection des imprimantes du serveur est disponible uniquement sous Windows.")
    try:
        import win32print
        import win32con
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Le composant Windows pywin32 manque sur le serveur. Installez-le puis redémarrez Cliniva.") from exc

    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    printers = win32print.EnumPrinters(flags, None, 2)
    detected = []
    for printer in printers:
        name = str(printer.get("pPrinterName") or "").strip()
        if not name:
            continue
        paper_codes = set()
        try:
            codes = win32print.DeviceCapabilities(name, printer.get("pPortName"), win32con.DC_PAPERS)
            paper_codes = {int(code) for code in (codes or [])}
        except Exception:
            logger.debug("Formats papier indisponibles pour l’imprimante serveur %s", name, exc_info=True)
        detected.append({"name": name, "supports_a4": 9 in paper_codes, "supports_a3": 8 in paper_codes, "paper_sizes_known": bool(paper_codes)})
    return detected


def _pdf_paper_format(pdf_path: str) -> str:
    """Détermine A3/A4 à partir du MediaBox de la première page PDF."""
    try:
        with open(pdf_path, "rb") as pdf_file:
            pdf_head = pdf_file.read(2_000_000)
        match = re.search(rb"/MediaBox\s*\[\s*(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)", pdf_head)
        if match:
            x0, y0, x1, y1 = (float(value) for value in match.groups())
            long_edge = max(abs(x1 - x0), abs(y1 - y0))
            return "a3" if long_edge >= 1000 else "a4"
    except (OSError, ValueError):
        logger.warning("Impossible de lire le format de la page PDF %s", pdf_path, exc_info=True)
    return "a4"


def _available_pdf_viewers():
    """Retourne une liste fixe de lecteurs connus et réellement installés sur le serveur."""
    viewers = [{"id": "system_default", "label": "Lecteur PDF par défaut de Windows"}]
    if sys.platform != "win32":
        return viewers
    program_dirs = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
                    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
                    Path(os.environ.get("LOCALAPPDATA", ""))]
    candidates = [
        ("sumatra", "SumatraPDF", "SumatraPDF.exe", ["SumatraPDF\\SumatraPDF.exe", "Programs\\SumatraPDF\\SumatraPDF.exe"]),
        ("adobe", "Adobe Acrobat Reader", "AcroRd32.exe", ["Adobe\\Acrobat Reader DC\\Reader\\AcroRd32.exe", "Adobe\\Acrobat DC\\Acrobat\\Acrobat.exe", "Adobe\\Acrobat\\Acrobat.exe"]),
        ("foxit", "Foxit PDF Reader", "FoxitPDFReader.exe", ["Foxit Software\\Foxit PDF Reader\\FoxitPDFReader.exe"]),
        ("pdfxchange", "PDF-XChange Editor", "PDFXEdit.exe", ["Tracker Software\\PDF Editor\\PDFXEdit.exe"]),
        ("edge", "Microsoft Edge (visionneur PDF)", "msedge.exe", ["Microsoft\\Edge\\Application\\msedge.exe", "Microsoft\\Edge\\Application\\msedge.exe"]),
        ("chrome", "Google Chrome (visionneur PDF)", "chrome.exe", ["Google\\Chrome\\Application\\chrome.exe", "Google\\Chrome\\Application\\chrome.exe"]),
    ]
    seen = set()
    for viewer_id, label, executable, relative_paths in candidates:
        found = shutil.which(executable)
        if not found:
            for root in program_dirs:
                for relative in relative_paths:
                    candidate = root / relative
                    if candidate.is_file():
                        found = str(candidate)
                        break
                if found:
                    break
        if found and Path(found).is_file() and str(Path(found).resolve()).casefold() not in seen:
            seen.add(str(Path(found).resolve()).casefold())
            viewers.append({"id": viewer_id, "label": label, "path": str(Path(found).resolve())})
    return viewers


def _require_print_server_admin(request: Request) -> Dict[str, Any]:
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    if user.get("status") != "Actif":
        raise HTTPException(status_code=403, detail="Ce compte utilisateur est inactif.")
    if user.get("role") != "Administrateur":
        raise HTTPException(status_code=403, detail="La gestion du spooler est réservée à l’administrateur.")
    return user


def _spooler_service_status(win32service, service_handle) -> Dict[str, Any]:
    current = win32service.QueryServiceStatus(service_handle)[1]
    states = {
        win32service.SERVICE_STOPPED: "Arrêté",
        win32service.SERVICE_START_PENDING: "Démarrage en cours",
        win32service.SERVICE_STOP_PENDING: "Arrêt en cours",
        win32service.SERVICE_RUNNING: "En cours d’exécution",
        win32service.SERVICE_CONTINUE_PENDING: "Reprise en cours",
        win32service.SERVICE_PAUSE_PENDING: "Mise en pause en cours",
        win32service.SERVICE_PAUSED: "En pause",
    }
    return {"code": current, "label": states.get(current, f"État {current}"), "running": current == win32service.SERVICE_RUNNING}


def _wait_for_spooler_state(win32service, service_handle, expected_state: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if win32service.QueryServiceStatus(service_handle)[1] == expected_state:
            return
        time.sleep(0.4)
    raise TimeoutError("Le spooler n’a pas atteint l’état demandé dans le délai prévu.")


def _is_windows_access_denied(exc: Exception) -> bool:
    return getattr(exc, "winerror", None) == 5 or bool(getattr(exc, "args", ())) and exc.args[0] == 5


def _spooler_permission_message() -> str:
    return (
        "Windows a refusé l’accès au spooler. Pour gérer le service ou ses travaux, "
        "démarrez Cliniva sur le serveur avec un compte Windows autorisé (par exemple « Exécuter en tant qu’administrateur »), "
        "ou accordez à son compte de service les droits nécessaires."
    )


def _control_spooler_service(action: str) -> Dict[str, Any]:
    if sys.platform != "win32":
        raise HTTPException(status_code=503, detail="Le spooler du serveur est disponible uniquement sous Windows.")
    try:
        import win32service
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Le composant Windows pywin32 manque sur le serveur.") from exc

    manager = service = None
    try:
        manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        # La lecture de l’état doit rester possible pour un compte qui ne peut
        # pas administrer le service Windows.
        service_access = win32service.SERVICE_QUERY_STATUS
        if action != "status":
            service_access |= win32service.SERVICE_START | win32service.SERVICE_STOP
        service = win32service.OpenService(manager, "Spooler", service_access)
        state = _spooler_service_status(win32service, service)
        if action == "status":
            pass
        elif action == "stop":
            if state["code"] == win32service.SERVICE_STOP_PENDING:
                _wait_for_spooler_state(win32service, service, win32service.SERVICE_STOPPED)
            elif state["code"] != win32service.SERVICE_STOPPED:
                win32service.ControlService(service, win32service.SERVICE_CONTROL_STOP)
                _wait_for_spooler_state(win32service, service, win32service.SERVICE_STOPPED)
        elif action == "start":
            if state["code"] != win32service.SERVICE_RUNNING:
                if state["code"] == win32service.SERVICE_PAUSED:
                    win32service.ControlService(service, win32service.SERVICE_CONTROL_CONTINUE)
                    _wait_for_spooler_state(win32service, service, win32service.SERVICE_RUNNING)
                elif state["code"] == win32service.SERVICE_START_PENDING:
                    _wait_for_spooler_state(win32service, service, win32service.SERVICE_RUNNING)
                elif state["code"] != win32service.SERVICE_STOPPED:
                    _wait_for_spooler_state(win32service, service, win32service.SERVICE_STOPPED)
                if state["code"] not in (win32service.SERVICE_PAUSED, win32service.SERVICE_START_PENDING):
                    win32service.StartService(service, None)
                    _wait_for_spooler_state(win32service, service, win32service.SERVICE_RUNNING)
        elif action == "restart":
            if state["code"] == win32service.SERVICE_STOP_PENDING:
                _wait_for_spooler_state(win32service, service, win32service.SERVICE_STOPPED)
            elif state["code"] == win32service.SERVICE_START_PENDING:
                _wait_for_spooler_state(win32service, service, win32service.SERVICE_RUNNING)
                win32service.ControlService(service, win32service.SERVICE_CONTROL_STOP)
                _wait_for_spooler_state(win32service, service, win32service.SERVICE_STOPPED)
            elif state["code"] != win32service.SERVICE_STOPPED:
                win32service.ControlService(service, win32service.SERVICE_CONTROL_STOP)
                _wait_for_spooler_state(win32service, service, win32service.SERVICE_STOPPED)
            if _spooler_service_status(win32service, service)["code"] != win32service.SERVICE_RUNNING:
                win32service.StartService(service, None)
                _wait_for_spooler_state(win32service, service, win32service.SERVICE_RUNNING)
        else:
            raise HTTPException(status_code=404, detail="Commande spooler inconnue.")
        return _spooler_service_status(win32service, service)
    except HTTPException:
        raise
    except Exception as exc:
        if _is_windows_access_denied(exc):
            logger.warning("Windows refuse la commande %s sur le spooler : %s", action, exc)
            raise HTTPException(status_code=403, detail=_spooler_permission_message()) from exc
        logger.exception("Échec de la commande %s sur le spooler Windows", action)
        raise HTTPException(status_code=503, detail=f"La commande spooler a échoué : {exc}") from exc
    finally:
        if service:
            win32service.CloseServiceHandle(service)
        if manager:
            win32service.CloseServiceHandle(manager)


def _list_server_spool_jobs() -> list[Dict[str, Any]]:
    if sys.platform != "win32":
        raise HTTPException(status_code=503, detail="La gestion du spooler est disponible uniquement sur le serveur Windows.")
    try:
        import win32print
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Le composant Windows pywin32 manque sur le serveur.") from exc

    jobs = []
    for printer in _enumerate_server_printers():
        printer_name = printer["name"]
        handle = None
        try:
            handle = win32print.OpenPrinter(printer_name)
            for job in win32print.EnumJobs(handle, 0, 999, 2):
                submitted = job.get("Submitted")
                jobs.append({
                    "printer": printer_name,
                    "job_id": int(job.get("JobId", 0)),
                    "document": str(job.get("pDocument") or "Document sans titre"),
                    "owner": str(job.get("pUserName") or "—"),
                    "machine": str(job.get("pMachineName") or ""),
                    "status": int(job.get("Status") or 0),
                    "status_text": str(job.get("pStatus") or "En attente"),
                    "pages": int(job.get("TotalPages") or 0),
                    "pages_printed": int(job.get("PagesPrinted") or 0),
                    "size": int(job.get("Size") or 0),
                    "submitted": submitted.isoformat(sep=" ", timespec="seconds") if hasattr(submitted, "isoformat") else str(submitted or ""),
                    "paused": bool(int(job.get("Status") or 0) & 1),
                })
        except Exception as exc:
            logger.warning("Impossible de lire la file de l’imprimante %s : %s", printer_name, exc)
        finally:
            if handle:
                win32print.ClosePrinter(handle)
    jobs.sort(key=lambda job: (job["printer"].casefold(), job["job_id"]))
    return jobs


@print_server_router.get("/spooler")
async def get_spooler_control_panel(request: Request):
    _require_print_server_admin(request)
    return JSONResponse({"success": True, "service": _control_spooler_service("status"), "jobs": _list_server_spool_jobs()})


@print_server_router.post("/spooler/service/{action}")
async def control_spooler(action: str, request: Request):
    _require_print_server_admin(request)
    if action not in {"start", "stop", "restart"}:
        raise HTTPException(status_code=404, detail="Commande spooler inconnue.")
    return JSONResponse({"success": True, "service": _control_spooler_service(action)})


@print_server_router.post("/spooler/jobs/{action}")
async def control_spool_job(action: str, request: Request):
    _require_print_server_admin(request)
    actions = {"pause": "JOB_CONTROL_PAUSE", "resume": "JOB_CONTROL_RESUME", "delete": "JOB_CONTROL_DELETE"}
    if action not in actions:
        raise HTTPException(status_code=404, detail="Commande de job inconnue.")
    payload = await request.json()
    printer_name = str(payload.get("printer") or "").strip()
    try:
        job_id = int(payload.get("job_id"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Identifiant de job invalide.") from exc
    if not printer_name or job_id <= 0:
        raise HTTPException(status_code=400, detail="Imprimante ou identifiant de job manquant.")
    available_names = {printer["name"] for printer in _enumerate_server_printers()}
    if printer_name not in available_names:
        raise HTTPException(status_code=404, detail="Cette imprimante n’est plus installée sur le serveur.")
    try:
        import win32print
        control_code = getattr(win32print, actions[action])
        handle = win32print.OpenPrinter(printer_name)
        try:
            win32print.SetJob(handle, job_id, 0, None, control_code)
        finally:
            win32print.ClosePrinter(handle)
    except Exception as exc:
        if _is_windows_access_denied(exc):
            logger.warning("Windows refuse la commande %s sur le job %s de %s : %s", action, job_id, printer_name, exc)
            raise HTTPException(status_code=403, detail=_spooler_permission_message()) from exc
        logger.exception("Échec de la commande %s sur le job %s de %s", action, job_id, printer_name)
        raise HTTPException(status_code=409, detail=f"Impossible d’appliquer l’action au job {job_id} : {exc}") from exc
    return JSONResponse({"success": True, "message": f"Job {job_id} : action {action} envoyée."})


@print_server_router.post("/spooler/jobs/delete-all")
async def delete_all_spool_jobs(request: Request):
    _require_print_server_admin(request)
    try:
        import win32print
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Le composant Windows pywin32 manque sur le serveur.") from exc
    removed = 0
    errors = []
    for job in _list_server_spool_jobs():
        handle = None
        try:
            handle = win32print.OpenPrinter(job["printer"])
            win32print.SetJob(handle, job["job_id"], 0, None, win32print.JOB_CONTROL_DELETE)
            removed += 1
        except Exception as exc:
            errors.append(
                f"{job['printer']} · #{job['job_id']} : "
                f"{_spooler_permission_message() if _is_windows_access_denied(exc) else exc}"
            )
        finally:
            if handle:
                win32print.ClosePrinter(handle)
    return JSONResponse({"success": True, "removed": removed, "errors": errors, "message": f"{removed} job(s) supprimé(s)."})


@print_server_router.get("/livrets")
async def list_validated_booklets(request: Request):
    if not get_current_user_optional(request):
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")

    # Assure les migrations des colonnes d’archives avant la lecture globale.
    from modules.gestion_dme import dme_manager
    dme_manager.ensure_dme_schema()

    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT cr.id, cr.id_patient, cr.id_passage, cr.titre, cr.modalite,
                   cr.medecin_prescripteur, cr.medecin_signataire, cr.proprietaire,
                   cr.nom_fichier,
                   COALESCE(NULLIF(cr.date_heure_str, ''), TO_CHAR(cr.created_at, 'DD/MM/YYYY HH24:MI')) AS date_creation,
                   COALESCE(NULLIF(BTRIM(CONCAT_WS(' ', p.prenom, p.nom)), ''), 'Patient #' || cr.id_patient::text) AS patient
            FROM patient_cr_records cr
            LEFT JOIN patients p ON p.id = cr.id_patient
            WHERE LOWER(BTRIM(COALESCE(cr.type_modele, ''))) LIKE 'livret%'
               OR (
                    (
                        LOWER(BTRIM(COALESCE(cr.titre, ''))) LIKE 'livret%'
                        OR LOWER(BTRIM(COALESCE(cr.nom_fichier, ''))) LIKE 'livret%'
                    )
                    AND LOWER(COALESCE(cr.chemin_docx, '')) LIKE '%.pdf'
               )
            ORDER BY cr.id DESC;
        """)
        booklets = [dict(row) for row in cursor.fetchall()]

    return JSONResponse({"success": True, "livrets": booklets})


@print_server_router.get("/visionneurs-pdf")
async def list_pdf_viewers(request: Request):
    if not get_current_user_optional(request):
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    viewers = [{"id": item["id"], "label": item["label"]} for item in _available_pdf_viewers()]
    return JSONResponse({"success": True, "viewers": viewers})


@print_server_router.post("/livrets/{record_id}/ouvrir")
async def open_booklet_in_pdf_viewer(record_id: int, request: Request):
    if not get_current_user_optional(request):
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    if sys.platform != "win32":
        raise HTTPException(status_code=503, detail="L’ouverture dans un lecteur externe nécessite le serveur Cliniva sous Windows.")
    payload = await request.json()
    viewer_id = str(payload.get("viewer_id") or "system_default").strip()
    with get_db_cursor() as cursor:
        cursor.execute("SELECT id, type_modele, chemin_docx FROM patient_cr_records WHERE id = %s;", (record_id,))
        record = cursor.fetchone()
    if not record or not str(record.get("type_modele") or "").strip().casefold().startswith("livret"):
        raise HTTPException(status_code=404, detail="Livret validé introuvable.")
    patient_root = (BASE_DIR / "uploads" / "dossiers_patients").resolve()
    pdf_path = Path(str(record.get("chemin_docx") or "")).resolve()
    try:
        if os.path.commonpath([str(patient_root), str(pdf_path)]) != str(patient_root):
            raise HTTPException(status_code=403, detail="Le fichier du livret n’est pas dans le dossier documentaire autorisé.")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Chemin de livret invalide.") from exc
    if not pdf_path.is_file() or pdf_path.suffix.casefold() != ".pdf":
        raise HTTPException(status_code=404, detail="Le fichier PDF du livret est introuvable sur le serveur.")
    try:
        if viewer_id == "system_default":
            os.startfile(str(pdf_path))
            label = "Lecteur PDF par défaut de Windows"
        else:
            viewer = next((item for item in _available_pdf_viewers() if item["id"] == viewer_id), None)
            if not viewer:
                raise HTTPException(status_code=404, detail="Ce lecteur PDF n’est pas installé ou n’est plus disponible sur le serveur.")
            subprocess.Popen([viewer["path"], str(pdf_path)], close_fds=True)
            label = viewer["label"]
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Impossible d’ouvrir le livret %s dans le lecteur PDF sélectionné", record_id)
        raise HTTPException(status_code=503, detail="Windows n’a pas pu ouvrir le livret dans le lecteur sélectionné.") from exc
    return JSONResponse({"success": True, "message": f"Livret ouvert avec {label}."})


@print_server_router.get("/imprimantes")
async def get_server_printers(request: Request):
    if not get_current_user_optional(request):
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    config = _load_print_config()
    return JSONResponse({
        "success": True,
        "server": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "Serveur Cliniva",
        "printers": _enumerate_server_printers(),
        "defaults": {
            "livret": config.get("livret_printer_livret", ""),
            "a3_planche": config.get("livret_printer_a3_planche", config.get("livret_printer_a3", "")),
            "a4": config.get("livret_printer_a4", ""),
        },
    })


@print_server_router.post("/imprimantes/defaults")
async def save_server_printer_defaults(request: Request):
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    if user.get("role") != "Administrateur":
        raise HTTPException(status_code=403, detail="Seul un administrateur peut définir les imprimantes par défaut.")

    payload = await request.json()
    printer_livret = str(payload.get("livret") or "").strip()
    printer_a3_planche = str(payload.get("a3_planche") or "").strip()
    printer_a4 = str(payload.get("a4") or "").strip()
    printers = _enumerate_server_printers()
    available_names = {printer["name"] for printer in printers}
    for format_name, printer_name in (("Livret", printer_livret), ("A3 Planche", printer_a3_planche), ("A4", printer_a4)):
        if printer_name and printer_name not in available_names:
            raise HTTPException(status_code=400, detail=f"L’imprimante choisie pour {format_name} n’est plus disponible sur le serveur.")
    for format_name, printer_name in (("a4", printer_a4), ("a3", printer_a3_planche)):
        selected_printer = next((printer for printer in printers if printer["name"] == printer_name), None)
        if selected_printer and selected_printer["paper_sizes_known"] and not selected_printer[f"supports_{format_name}"]:
            raise HTTPException(status_code=400, detail=f"L’imprimante choisie ne déclare pas prendre en charge le format {format_name.upper()}.")
    config = _load_print_config()
    config["livret_printer_livret"] = printer_livret
    config["livret_printer_a3_planche"] = printer_a3_planche
    config["livret_printer_a3"] = printer_a3_planche
    config["livret_printer_a4"] = printer_a4
    try:
        _save_print_config(config)
    except OSError as exc:
        logger.exception("Échec de sauvegarde des imprimantes de livrets.")
        raise HTTPException(status_code=500, detail="Impossible d’enregistrer les imprimantes par défaut du serveur.") from exc
    return JSONResponse({"success": True, "defaults": {"livret": printer_livret, "a3_planche": printer_a3_planche, "a4": printer_a4}})


@print_server_router.post("/livrets/{record_id}/imprimer")
async def print_server_booklet(record_id: int, request: Request):
    if not get_current_user_optional(request):
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    if sys.platform != "win32":
        raise HTTPException(status_code=503, detail="L’impression directe nécessite le serveur Cliniva sous Windows.")
    try:
        import win32api
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Le composant Windows pywin32 manque sur le serveur. Installez-le puis redémarrez Cliniva.") from exc

    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT id, id_patient, type_modele, titre, chemin_docx, nom_fichier
            FROM patient_cr_records WHERE id = %s;
        """, (record_id,))
        record = cursor.fetchone()
    if not record or not str(record.get("type_modele") or "").strip().casefold().startswith("livret"):
        raise HTTPException(status_code=404, detail="Livret validé introuvable.")

    patient_root = (BASE_DIR / "uploads" / "dossiers_patients").resolve()
    pdf_path = Path(str(record.get("chemin_docx") or "")).resolve()
    try:
        if os.path.commonpath([str(patient_root), str(pdf_path)]) != str(patient_root):
            raise HTTPException(status_code=403, detail="Le fichier du livret n’est pas dans le dossier documentaire autorisé.")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Chemin de livret invalide.") from exc
    if not pdf_path.is_file() or pdf_path.suffix.casefold() != ".pdf":
        raise HTTPException(status_code=404, detail="Le fichier PDF du livret est introuvable sur le serveur.")

    paper_format = _pdf_paper_format(str(pdf_path))
    config = _load_print_config()
    printer_name = str(config.get("livret_printer_livret") or "").strip()
    if not printer_name:
        raise HTTPException(status_code=409, detail="Aucune imprimante serveur n’est définie pour l’impression des livrets.")
    available_printers = {printer["name"]: printer for printer in _enumerate_server_printers()}
    if printer_name not in available_printers:
        raise HTTPException(status_code=409, detail=f"L’imprimante serveur {printer_name} n’est plus disponible. Détectez à nouveau les imprimantes et choisissez-en une autre.")
    if printer_name.casefold() == "microsoft print to pdf":
        output_dir = BASE_DIR / "uploads" / "print_server_output"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_name = f"livret_{record_id}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.pdf"
        output_path = output_dir / output_name
        try:
            shutil.copy2(pdf_path, output_path)
        except OSError as exc:
            logger.exception("Impossible de créer le PDF de sortie du livret %s", record_id)
            raise HTTPException(status_code=500, detail="Impossible d’enregistrer le PDF généré par l’imprimante virtuelle.") from exc
        return JSONResponse({
            "success": True,
            "printer": printer_name,
            "paper_format": paper_format.upper(),
            "download_url": f"/api/serveur-impression/impressions/{output_name}",
            "message": "Microsoft Print to PDF est une imprimante virtuelle : le livret a été enregistré côté serveur sans fenêtre de dialogue.",
        })
    try:
        result = win32api.ShellExecute(0, "printto", str(pdf_path), f'"{printer_name}"', str(pdf_path.parent), 0)
    except Exception as exc:
        logger.exception("Échec d’envoi du livret %s vers l’imprimante serveur %s", record_id, printer_name)
        raise HTTPException(status_code=503, detail=f"Impossible d’envoyer le livret à l’imprimante {printer_name}.") from exc
    if int(result) <= 32:
        raise HTTPException(status_code=503, detail=f"Windows n’a pas accepté l’impression du livret (code {result}). Vérifiez l’application PDF configurée sur le serveur.")
    return JSONResponse({"success": True, "printer": printer_name, "paper_format": paper_format.upper(), "message": f"Livret envoyé à {printer_name} ({paper_format.upper()})."})


@print_server_router.post("/dicom-print/a3")
async def print_dicom_image_a3(request: Request):
    """Envoie un PDF A3 ou convertit une image en page A3 puis l’imprime côté serveur."""
    if not get_current_user_optional(request):
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    if sys.platform != "win32":
        raise HTTPException(status_code=503, detail="L’impression directe nécessite le serveur Cliniva sous Windows.")
    try:
        import win32api
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Le composant Windows pywin32 manque sur le serveur.") from exc

    try:
        payload = await request.json()
        encoded_pdf = str(payload.get("pdf_base64") or "")
        encoded_image = str(payload.get("image_base64") or "")
        if not encoded_pdf and not encoded_image:
            raise HTTPException(status_code=400, detail="Aucun PDF ni aucune image n’a été transmis.")
        encoded = encoded_pdf or encoded_image
        if encoded.startswith("data:"):
            encoded = encoded.split(",", 1)[1]
        if len(encoded) > 45 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Le document dépasse la taille autorisée pour l’impression.")
        source_bytes = base64.b64decode(encoded, validate=True)
        if encoded_image:
            import io
            from PIL import Image

            with Image.open(io.BytesIO(source_bytes)) as source:
                page = Image.new("RGB", (3508, 4961), "white")
                image = source.convert("RGB")
                image.thumbnail((3390, 4843), Image.Resampling.LANCZOS)
                page.paste(image, ((3508 - image.width) // 2, (4961 - image.height) // 2))
                output = io.BytesIO()
                page.save(output, format="PDF", resolution=300.0)
                pdf_bytes = output.getvalue()
        else:
            pdf_bytes = source_bytes
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Le document ou l’image transmis est invalide.") from exc
    if not pdf_bytes.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="Le document transmis n’est pas un PDF valide.")

    config = _load_print_config()
    printer_name = str(config.get("livret_printer_a3_planche") or config.get("livret_printer_a3") or "").strip()
    if not printer_name:
        raise HTTPException(status_code=409, detail="Aucune imprimante n’est configurée pour le format A3 Planche.")
    available = {printer["name"]: printer for printer in _enumerate_server_printers()}
    printer = available.get(printer_name)
    if not printer:
        raise HTTPException(status_code=409, detail=f"L’imprimante A3 {printer_name} n’est plus disponible sur le serveur.")
    if printer.get("paper_sizes_known") and not printer.get("supports_a3"):
        raise HTTPException(status_code=409, detail=f"L’imprimante {printer_name} ne déclare pas prendre en charge le format A3.")

    output_dir = BASE_DIR / "uploads" / "print_server_output"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_name = f"dicom_print_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.pdf"
    output_path = output_dir / output_name
    try:
        output_path.write_bytes(pdf_bytes)
        if printer_name.casefold() == "microsoft print to pdf":
            return JSONResponse({"success": True, "printer": printer_name,
                                 "download_url": f"/api/serveur-impression/impressions/{output_name}",
                                 "message": "PDF A3 enregistré sur le serveur."})
        result = win32api.ShellExecute(0, "printto", str(output_path), f'"{printer_name}"', str(output_dir), 0)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Échec d’impression A3 DICOM vers %s", printer_name)
        raise HTTPException(status_code=503, detail=f"Impossible d’envoyer l’image à l’imprimante {printer_name}.") from exc
    if int(result) <= 32:
        raise HTTPException(status_code=503, detail=f"Windows n’a pas accepté l’impression A3 (code {result}).")
    return JSONResponse({"success": True, "printer": printer_name,
                         "message": f"Image envoyée à {printer_name} au format A3."})


@print_server_router.get("/impressions/{file_name}")
async def download_server_print_output(file_name: str, request: Request):
    if not get_current_user_optional(request):
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    if not re.fullmatch(r"(?:livret_\d+|dicom_print)_\d{8}_\d{6}_\d{6}\.pdf", file_name, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Nom de fichier d’impression invalide.")
    output_path = (BASE_DIR / "uploads" / "print_server_output" / file_name).resolve()
    output_root = (BASE_DIR / "uploads" / "print_server_output").resolve()
    if os.path.commonpath([str(output_root), str(output_path)]) != str(output_root) or not output_path.is_file():
        raise HTTPException(status_code=404, detail="Le fichier PDF d’impression est introuvable.")
    return FileResponse(output_path, media_type="application/pdf", filename=file_name)


def render_html() -> str:
    return r'''<section class="space-y-6">
        <header class="flex flex-wrap items-center justify-between gap-4 rounded-[2rem] bg-gradient-to-r from-[#101b13] to-[#183b20] px-7 py-6 text-white shadow-lg">
            <div><p class="text-xs font-bold uppercase tracking-[.18em] text-lime-300">Documents médicaux</p><h1 class="mt-1 text-2xl font-extrabold">Serveur d’impression</h1><p class="mt-1 text-sm text-slate-300">Livrets validés disponibles pour consultation et impression.</p></div>
            <div class="flex flex-wrap gap-2">
                <button type="button" onclick="openPrintServerSpoolerModal()" class="inline-flex items-center gap-2 rounded-xl border border-white/20 bg-white/10 px-4 py-2.5 text-sm font-bold text-white hover:bg-white/20"><i class="ph-bold ph-queue"></i>Gestion du spooler</button>
                <button type="button" onclick="openPrintServerConfigModal()" class="inline-flex items-center gap-2 rounded-xl bg-lime-300 px-4 py-2.5 text-sm font-extrabold text-slate-900 shadow-md hover:bg-lime-200"><i class="ph-bold ph-gear-six"></i>Configuration d’impression</button>
                <button type="button" onclick="loadPrintServerBooklets()" class="inline-flex items-center gap-2 rounded-xl border border-white/20 bg-white/10 px-4 py-2.5 text-sm font-bold text-white hover:bg-white/20"><i class="ph-bold ph-arrows-clockwise"></i>Actualiser</button>
            </div>
        </header>

        <div class="rounded-[1.75rem] border border-slate-200 bg-white p-4 shadow-sm sm:p-5">
            <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
                <div><h2 class="text-lg font-extrabold text-slate-800">Livrets validés</h2><p id="printServerCount" class="text-xs text-slate-500">Chargement…</p><p class="mt-1 text-[10px] text-slate-400">Les livrets du jour s’affichent d’abord. La recherche retrouve aussi les anciens livrets.</p></div>
                <div class="flex w-full flex-wrap gap-2 sm:w-auto">
                    <input id="printServerSearch" type="search" placeholder="Rechercher un patient ou un ancien livret…" oninput="filterPrintServerBooklets()" class="min-w-56 flex-1 rounded-xl border border-slate-200 px-4 py-2.5 text-sm outline-none focus:border-emerald-500">
                    <label class="flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-500"><i class="ph-bold ph-calendar-blank text-emerald-600"></i><input id="printServerDateFilter" type="date" aria-label="Filtrer les livrets par date de création" onchange="filterPrintServerBooklets()" class="bg-transparent text-sm text-slate-700 outline-none"></label>
                </div>
            </div>
            <div class="overflow-x-auto">
                <table class="w-full min-w-[1050px] border-separate border-spacing-y-2 text-left text-sm">
                    <thead><tr class="text-[11px] uppercase tracking-wider text-slate-500">
                        <th class="px-3 py-2">Date et heure de création</th><th class="px-3 py-2">Patient</th><th class="px-3 py-2">Livret / prestation</th><th class="px-3 py-2">Modalité</th><th class="px-3 py-2">Médecin signataire</th><th class="px-3 py-2">Créé par</th><th class="px-3 py-2 text-right">Actions</th>
                    </tr></thead>
                    <tbody id="printServerBookletsBody"><tr><td colspan="7" class="rounded-2xl bg-slate-50 py-10 text-center text-sm text-slate-400">Chargement des livrets…</td></tr></tbody>
                </table>
            </div>
        </div>

        <div id="printServerConfigModal" class="fixed inset-0 z-[125] hidden items-center justify-center bg-slate-950/70 p-3 backdrop-blur-sm sm:p-6" onclick="if(event.target===this)closePrintServerConfigModal()">
            <div class="w-full max-w-3xl overflow-hidden rounded-[2rem] bg-white shadow-2xl">
                <div class="relative overflow-hidden bg-gradient-to-br from-[#101b13] via-[#183b20] to-[#24653a] px-6 py-6 text-white sm:px-8">
                    <div class="absolute -right-8 -top-12 h-48 w-48 rounded-full border-[24px] border-white/5"></div>
                    <div class="relative flex items-start justify-between gap-4">
                        <div class="flex items-start gap-4"><div class="flex h-14 w-14 shrink-0 items-center justify-center rounded-2xl bg-lime-300 text-emerald-950 shadow-lg"><i class="ph-bold ph-printer text-3xl"></i></div><div><p class="text-[10px] font-extrabold uppercase tracking-[.2em] text-lime-300">Configuration du serveur</p><h2 class="mt-1 text-xl font-extrabold sm:text-2xl">Configuration d’impression</h2><p id="printServerHost" class="mt-1 text-xs text-slate-300">Détection des imprimantes installées sur le serveur…</p></div></div>
                        <button type="button" onclick="closePrintServerConfigModal()" class="rounded-xl p-2 text-white/70 hover:bg-white/10 hover:text-white" aria-label="Fermer"><i class="ph-bold ph-x text-xl"></i></button>
                    </div>
                </div>
                <div class="space-y-4 p-5 sm:p-8">
                    <div class="grid grid-cols-1 gap-3 md:grid-cols-3">
                        <label class="group rounded-2xl border border-slate-200 bg-slate-50 p-4 text-xs font-bold text-slate-700 transition hover:border-emerald-300 hover:bg-emerald-50/40"><span class="mb-3 flex items-center gap-2 text-emerald-700"><span class="flex h-9 w-9 items-center justify-center rounded-xl bg-emerald-100"><i class="ph-bold ph-book-open text-lg"></i></span>Imprimante Livret</span><select id="printServerPrinterLivret" class="w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm font-medium text-slate-800"><option value="">Choisir une imprimante…</option></select></label>
                        <label class="group rounded-2xl border border-slate-200 bg-slate-50 p-4 text-xs font-bold text-slate-700 transition hover:border-indigo-300 hover:bg-indigo-50/40"><span class="mb-3 flex items-center gap-2 text-indigo-700"><span class="flex h-9 w-9 items-center justify-center rounded-xl bg-indigo-100"><i class="ph-bold ph-image-square text-lg"></i></span>Imprimante A3 Planche</span><select id="printServerPrinterA3" class="w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm font-medium text-slate-800"><option value="">Choisir une imprimante…</option></select></label>
                        <label class="group rounded-2xl border border-slate-200 bg-slate-50 p-4 text-xs font-bold text-slate-700 transition hover:border-sky-300 hover:bg-sky-50/40"><span class="mb-3 flex items-center gap-2 text-sky-700"><span class="flex h-9 w-9 items-center justify-center rounded-xl bg-sky-100"><i class="ph-bold ph-file text-lg"></i></span>Imprimante A4</span><select id="printServerPrinterA4" class="w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm font-medium text-slate-800"><option value="">Choisir une imprimante…</option></select></label>
                    </div>
                    <div class="flex flex-wrap items-center justify-between gap-3 border-t border-slate-100 pt-4">
                        <div class="flex items-start gap-2"><i class="ph-bold ph-info mt-0.5 text-emerald-600"></i><p id="printServerPrinterMessage" class="min-h-5 text-xs text-slate-500">Le bouton Imprimer utilise la file Livret côté serveur. Les imprimantes A3 Planche et A4 restent configurables pour leurs usages respectifs.</p></div>
                        <div class="flex gap-2"><button type="button" onclick="loadPrintServerPrinters()" class="rounded-xl border border-slate-200 px-3 py-2.5 text-xs font-bold text-slate-700 hover:bg-slate-50"><i class="ph-bold ph-arrows-clockwise mr-1"></i>Détecter</button><button type="button" onclick="savePrintServerDefaults()" class="rounded-xl bg-emerald-600 px-4 py-2.5 text-xs font-bold text-white shadow-sm hover:bg-emerald-700"><i class="ph-bold ph-floppy-disk mr-1"></i>Enregistrer</button></div>
                    </div>
                </div>
            </div>
        </div>

        <div id="printServerSpoolerModal" class="fixed inset-0 z-[130] hidden items-center justify-center bg-slate-950/75 p-3 backdrop-blur-sm sm:p-6" onclick="if(event.target===this)closePrintServerSpoolerModal()">
            <div class="flex max-h-[94vh] w-full max-w-7xl flex-col overflow-hidden rounded-[2rem] bg-white shadow-2xl">
                <div class="relative overflow-hidden bg-gradient-to-br from-[#101b13] via-[#183b20] to-[#24653a] px-6 py-5 text-white sm:px-8">
                    <div class="absolute -right-8 -top-12 h-48 w-48 rounded-full border-[24px] border-white/5"></div>
                    <div class="relative flex flex-wrap items-center justify-between gap-4">
                        <div class="flex items-center gap-4"><div class="flex h-14 w-14 items-center justify-center rounded-2xl bg-lime-300 text-emerald-950 shadow-lg"><i class="ph-bold ph-queue text-3xl"></i></div><div><p class="text-[10px] font-extrabold uppercase tracking-[.2em] text-lime-300">Administration Windows</p><h2 class="text-xl font-extrabold sm:text-2xl">Gestion du spooler serveur</h2><p id="printSpoolerHost" class="mt-1 text-xs text-slate-300">Service d’impression et files d’attente du serveur.</p></div></div>
                        <button type="button" onclick="closePrintServerSpoolerModal()" class="rounded-xl p-2 text-white/70 hover:bg-white/10 hover:text-white" aria-label="Fermer"><i class="ph-bold ph-x text-xl"></i></button>
                    </div>
                </div>
                <div class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 bg-slate-50 px-5 py-4 sm:px-7">
                    <div class="flex flex-wrap items-center gap-2"><span id="printSpoolerStatus" class="rounded-full bg-slate-200 px-3 py-2 text-xs font-extrabold text-slate-700">État inconnu</span><button type="button" onclick="controlPrintSpoolerService('start')" class="rounded-lg bg-emerald-600 px-3 py-2 text-xs font-bold text-white hover:bg-emerald-700"><i class="ph-bold ph-play mr-1"></i>Démarrer</button><button type="button" onclick="controlPrintSpoolerService('stop')" class="rounded-lg bg-amber-500 px-3 py-2 text-xs font-bold text-white hover:bg-amber-600"><i class="ph-bold ph-stop mr-1"></i>Arrêter</button><button type="button" onclick="controlPrintSpoolerService('restart')" class="rounded-lg bg-indigo-600 px-3 py-2 text-xs font-bold text-white hover:bg-indigo-700"><i class="ph-bold ph-arrows-clockwise mr-1"></i>Redémarrer</button></div>
                    <div class="flex flex-wrap gap-2"><button type="button" onclick="refreshPrintServerSpooler()" class="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-bold text-slate-700 hover:bg-slate-100"><i class="ph-bold ph-arrow-clockwise mr-1"></i>Actualiser</button><button type="button" onclick="deleteAllPrintServerJobs()" class="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs font-bold text-rose-700 hover:bg-rose-100"><i class="ph-bold ph-trash mr-1"></i>Supprimer tous les jobs</button></div>
                </div>
                <p id="printSpoolerMessage" class="min-h-5 px-5 pt-3 text-xs text-slate-500 sm:px-7"></p>
                <div class="min-h-0 flex-1 overflow-auto px-4 pb-5 sm:px-7">
                    <table class="w-full min-w-[900px] border-separate border-spacing-y-2 text-left text-sm"><thead><tr class="text-[10px] uppercase tracking-wider text-slate-500"><th class="px-3 py-2">Imprimante serveur</th><th class="px-3 py-2">Document</th><th class="px-3 py-2">Utilisateur</th><th class="px-3 py-2">État</th><th class="px-3 py-2">Pages</th><th class="px-3 py-2">Soumis</th><th class="px-3 py-2 text-right">Actions</th></tr></thead><tbody id="printSpoolerJobs"><tr><td colspan="7" class="rounded-xl bg-slate-50 p-8 text-center text-slate-400">Chargement des travaux…</td></tr></tbody></table>
                </div>
            </div>
        </div>

        <div id="printServerPreview" class="fixed inset-0 z-[120] hidden items-center justify-center bg-black/80 p-3 backdrop-blur-sm sm:p-6">
            <div class="flex h-[94vh] w-full max-w-6xl flex-col overflow-hidden rounded-3xl bg-white shadow-2xl">
                <div class="flex items-center justify-between gap-3 border-b border-slate-100 px-5 py-3">
                    <h3 id="printServerPreviewTitle" class="truncate font-bold text-slate-800">Aperçu du livret</h3>
                    <div class="flex shrink-0 flex-wrap items-center justify-end gap-2">
                        <button type="button" onclick="downloadPrintServerPreviewPdf()" class="inline-flex items-center gap-2 rounded-xl border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs font-bold text-indigo-800 hover:bg-indigo-100"><i class="ph-bold ph-download-simple"></i>Télécharger PDF</button>
                        <select id="printServerPdfViewerSelect" aria-label="Choisir un lecteur PDF externe" class="max-w-48 rounded-xl border border-slate-200 bg-white px-2.5 py-2 text-xs font-semibold text-slate-700"><option value="system_default">Lecteur PDF par défaut Windows</option></select>
                        <button type="button" onclick="openPrintServerPreviewExternal()" class="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs font-bold text-slate-700 hover:bg-slate-50"><i class="ph-bold ph-arrow-square-out"></i>Ouvrir avec</button>
                        <button type="button" onclick="printServerPreviewNow()" class="inline-flex items-center gap-2 rounded-xl bg-emerald-600 px-3 py-2 text-xs font-bold text-white hover:bg-emerald-700"><i class="ph-bold ph-printer"></i>Imprimer serveur</button>
                        <button type="button" onclick="closePrintServerPreview()" class="rounded-xl bg-slate-100 px-3 py-2 text-xs font-bold text-slate-700 hover:bg-slate-200">Fermer</button>
                    </div>
                </div>
                <iframe id="printServerPreviewFrame" title="Aperçu PDF du livret" class="min-h-0 flex-1 border-0 bg-slate-100"></iframe>
            </div>
        </div>
    </section>

    <script>
    (() => {
        let printServerRows = [];
        let activePrintServerRecordId = null;
        let printSpoolerRows = [];
        const htmlEscape = (value) => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
        const pdfUrl = id => `/api/dme/cr/view-pdf/${encodeURIComponent(id)}`;

        window.filterPrintServerBooklets = function() {
            const query = (document.getElementById('printServerSearch')?.value || '').trim().toLocaleLowerCase();
            const selectedDate = document.getElementById('printServerDateFilter')?.value || '';
            const now = new Date();
            const today = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;
            const visible = printServerRows.filter(row => {
                const textMatch = [row.date_creation, row.patient, row.titre, row.nom_fichier, row.modalite, row.medecin_signataire, row.proprietaire].some(value => String(value || '').toLocaleLowerCase().includes(query));
                const rowDate = (() => {
                    const match = String(row.date_creation || '').match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})/);
                    return match ? `${match[3]}-${match[2].padStart(2, '0')}-${match[1].padStart(2, '0')}` : '';
                })();
                // Sans filtre explicite, l’écran montre seulement aujourd’hui.
                // Dès qu’une recherche est saisie, elle porte sur tout l’historique.
                const dateMatch = selectedDate ? rowDate === selectedDate : query ? true : rowDate === today;
                return textMatch && dateMatch;
            });
            const body = document.getElementById('printServerBookletsBody');
            document.getElementById('printServerCount').textContent = selectedDate
                ? `${visible.length} livret(s) · ${selectedDate}`
                : query ? `${visible.length} résultat(s) dans l’historique` : `${visible.length} livret(s) aujourd’hui`;
            if (!visible.length) {
                const emptyMessage = query || selectedDate
                    ? 'Aucun livret ne correspond à cette recherche.'
                    : printServerRows.length ? 'Aucun livret validé aujourd’hui. Recherchez un patient ou un livret pour retrouver les anciens.' : 'Aucun livret validé aujourd’hui.';
                body.innerHTML = `<tr><td colspan="7" class="rounded-2xl bg-slate-50 py-10 text-center text-sm text-slate-400">${emptyMessage}</td></tr>`;
                return;
            }
            body.innerHTML = visible.map(row => {
                const prestation = String(row.titre || '').replace(/^Livret\s*-\s*/i, '');
                return `<tr class="bg-slate-50 text-sm text-slate-700 hover:bg-emerald-50/70">
                    <td class="rounded-l-xl px-3 py-3 font-mono text-xs text-slate-500">${htmlEscape(row.date_creation)}</td>
                    <td class="px-3 py-3 font-bold text-slate-800">${htmlEscape(row.patient)}</td>
                    <td class="px-3 py-3"><div class="font-bold">${htmlEscape(row.nom_fichier || row.titre || 'Livret')}</div><div class="text-xs text-indigo-600">${htmlEscape(prestation)}</div></td>
                    <td class="px-3 py-3">${htmlEscape(row.modalite || '—')}</td>
                    <td class="px-3 py-3">${htmlEscape(row.medecin_signataire || '—')}</td>
                    <td class="px-3 py-3">${htmlEscape(row.proprietaire || '—')}</td>
                    <td class="rounded-r-xl px-3 py-3"><div class="flex justify-end gap-2">
                        <button type="button" onclick="showPrintServerPreview(${Number(row.id)})" class="inline-flex items-center gap-2 rounded-lg bg-slate-800 px-3 py-2 text-xs font-bold text-white hover:bg-slate-700"><i class="ph-bold ph-eye"></i>Afficher</button>
                        <button type="button" onclick="printServerBookletDirect(${Number(row.id)})" class="inline-flex items-center gap-2 rounded-lg bg-emerald-600 px-3 py-2 text-xs font-bold text-white hover:bg-emerald-700"><i class="ph-bold ph-printer"></i>Imprimer</button>
                    </div></td>
                </tr>`;
            }).join('');
        };

        window.loadPrintServerBooklets = async function() {
            const body = document.getElementById('printServerBookletsBody');
            try {
                const response = await fetch('/api/serveur-impression/livrets', {cache:'no-store'});
                const result = await response.json();
                if (!response.ok || !result.success) throw new Error(result.detail || 'Impossible de charger les livrets.');
                printServerRows = result.livrets || [];
                window.filterPrintServerBooklets();
            } catch (error) {
                body.innerHTML = `<tr><td colspan="7" class="rounded-2xl bg-rose-50 py-10 text-center text-sm font-semibold text-rose-600">${htmlEscape(error.message)}</td></tr>`;
                document.getElementById('printServerCount').textContent = 'Erreur de chargement';
            }
        };

        window.loadPrintServerPrinters = async function() {
            const message = document.getElementById('printServerPrinterMessage');
            const host = document.getElementById('printServerHost');
            try {
                const response = await fetch('/api/serveur-impression/imprimantes', {cache:'no-store'});
                const result = await response.json();
                if (!response.ok || !result.success) throw new Error(result.detail || 'La détection des imprimantes a échoué.');
                host.textContent = `Imprimantes détectées sur le serveur : ${result.server}`;
                const printers = result.printers || [];
                [['livret', 'Livret'], ['a3_planche', 'A3'], ['a4', 'A4']].forEach(([format, suffix]) => {
                    const select = document.getElementById(`printServerPrinter${suffix}`);
                    const selected = select.value || result.defaults?.[format] || '';
                    select.innerHTML = '<option value="">Choisir une imprimante serveur…</option>' + printers.map(printer => {
                        const paperKey = format === 'a3_planche' ? 'a3' : format;
                        const capability = paperKey !== 'livret' && printer.paper_sizes_known ? (printer[`supports_${paperKey}`] ? ` · ${paperKey.toUpperCase()} disponible` : ` · ${paperKey.toUpperCase()} non confirmé`) : '';
                        return `<option value="${htmlEscape(printer.name)}">${htmlEscape(printer.name + capability)}</option>`;
                    }).join('');
                    select.value = selected;
                });
                message.textContent = printers.length ? `${printers.length} imprimante(s) du serveur détectée(s).` : 'Aucune imprimante installée sur le serveur.';
                message.className = `min-h-5 text-xs ${printers.length ? 'text-slate-500' : 'font-semibold text-amber-600'}`;
            } catch (error) {
                host.textContent = 'Détection effectuée sur le serveur Cliniva, jamais sur le poste client.';
                message.textContent = error.message;
                message.className = 'min-h-5 text-xs font-semibold text-rose-600';
            }
        };

        window.savePrintServerDefaults = async function() {
            const message = document.getElementById('printServerPrinterMessage');
            try {
                const response = await fetch('/api/serveur-impression/imprimantes/defaults', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({livret:document.getElementById('printServerPrinterLivret').value, a3_planche:document.getElementById('printServerPrinterA3').value, a4:document.getElementById('printServerPrinterA4').value})
                });
                const result = await response.json();
                if (!response.ok || !result.success) throw new Error(result.detail || 'Impossible d’enregistrer les imprimantes.');
                message.textContent = 'Les trois imprimantes par défaut sont enregistrées sur le serveur.';
                message.className = 'min-h-5 text-xs font-semibold text-emerald-700';
            } catch (error) {
                message.textContent = error.message;
                message.className = 'min-h-5 text-xs font-semibold text-rose-600';
            }
        };

        window.submitServerPrint = async function(recordId) {
            const message = document.getElementById('printServerPrinterMessage');
            try {
                const response = await fetch(`/api/serveur-impression/livrets/${encodeURIComponent(recordId)}/imprimer`, {method:'POST'});
                const result = await response.json();
                if (!response.ok || !result.success) throw new Error(result.detail || 'L’impression serveur a échoué.');
                message.textContent = result.message;
                message.className = 'min-h-5 text-xs font-semibold text-emerald-700';
                if (result.download_url) {
                    const link = document.createElement('a');
                    link.href = result.download_url;
                    link.download = '';
                    link.textContent = 'Télécharger le PDF créé';
                    link.className = 'ml-2 inline-flex font-extrabold text-emerald-800 underline';
                    message.appendChild(link);
                }
            } catch (error) {
                message.textContent = error.message;
                message.className = 'min-h-5 text-xs font-semibold text-rose-600';
                alert(error.message);
            }
        };

        window.showPrintServerPreview = function(id) {
            const row = printServerRows.find(item => Number(item.id) === Number(id));
            activePrintServerRecordId = Number(id);
            document.getElementById('printServerPreviewTitle').textContent = row?.nom_fichier || row?.titre || 'Aperçu du livret';
            const frame = document.getElementById('printServerPreviewFrame');
            frame.onload = null;
            frame.src = `${pdfUrl(id)}?t=${Date.now()}`;
            const modal = document.getElementById('printServerPreview');
            modal.classList.remove('hidden'); modal.classList.add('flex');
            window.loadPrintServerPdfViewers();
        };
        window.loadPrintServerPdfViewers = async function() {
            const select = document.getElementById('printServerPdfViewerSelect');
            if (!select) return;
            try {
                const response = await fetch('/api/serveur-impression/visionneurs-pdf', {cache:'no-store'});
                const result = await response.json();
                if (!response.ok || !result.success) throw new Error(result.detail || 'Liste des lecteurs PDF indisponible.');
                select.innerHTML = (result.viewers || []).map(viewer => `<option value="${htmlEscape(viewer.id)}">${htmlEscape(viewer.label)}</option>`).join('') || '<option value="system_default">Lecteur PDF par défaut Windows</option>';
            } catch (error) {
                select.innerHTML = '<option value="system_default">Lecteur PDF par défaut Windows</option>';
                select.title = error.message || 'Lecteurs externes indisponibles';
            }
        };
        window.downloadPrintServerPreviewPdf = async function() {
            if (!activePrintServerRecordId) return;
            try {
                const response = await fetch(`${pdfUrl(activePrintServerRecordId)}?t=${Date.now()}`, {cache:'no-store'});
                if (!response.ok) throw new Error(`Le PDF du livret ne peut pas être téléchargé (HTTP ${response.status}).`);
                const blob = await response.blob();
                if (!blob.size || (blob.type && !blob.type.toLowerCase().includes('pdf'))) throw new Error('Le serveur n’a pas renvoyé un fichier PDF valide.');
                const row = printServerRows.find(item => Number(item.id) === activePrintServerRecordId);
                const baseName = String(row?.nom_fichier || row?.titre || `Livret_${activePrintServerRecordId}`).replace(/[\\/:*?"<>|]+/g, '_').replace(/\.pdf$/i, '');
                const filename = `${baseName || 'Livret'}.pdf`;
                const bridge = window.pywebview?.api;
                if (bridge?.print_server_save_pdf) {
                    const pdfDataUrl = await new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onload = () => resolve(reader.result);
                        reader.onerror = () => reject(new Error('Impossible de lire le PDF reçu.'));
                        reader.readAsDataURL(blob);
                    });
                    const result = await bridge.print_server_save_pdf(pdfDataUrl, filename);
                    if (!result?.ok) throw new Error(result?.error || 'La fenêtre de sauvegarde PDF a échoué.');
                    if (!result.cancelled) alert(`PDF enregistré : ${result.path}`);
                    return;
                }
                const url = URL.createObjectURL(blob);
                const link = document.createElement('a'); link.href = url; link.download = filename; link.style.display = 'none'; document.body.appendChild(link); link.click(); link.remove();
                setTimeout(() => URL.revokeObjectURL(url), 10000);
            } catch (error) { alert(error.message || 'Échec du téléchargement du PDF.'); }
        };
        window.openPrintServerPreviewExternal = async function() {
            if (!activePrintServerRecordId) return;
            const viewerId = document.getElementById('printServerPdfViewerSelect')?.value || 'system_default';
            try {
                const row = printServerRows.find(item => Number(item.id) === activePrintServerRecordId);
                const filename = String(row?.nom_fichier || row?.titre || `Livret_${activePrintServerRecordId}`).replace(/[\\/:*?"<>|]+/g, '_').replace(/\.pdf$/i, '') + '.pdf';
                if (viewerId === 'system_default' && window.pywebview?.api?.print_server_open_pdf) {
                    const response = await fetch(`${pdfUrl(activePrintServerRecordId)}?t=${Date.now()}`, {cache:'no-store'});
                    if (!response.ok) throw new Error(`Le PDF du livret ne peut pas être ouvert (HTTP ${response.status}).`);
                    const blob = await response.blob();
                    if (!blob.size || (blob.type && !blob.type.toLowerCase().includes('pdf'))) throw new Error('Le serveur n’a pas renvoyé un fichier PDF valide.');
                    const pdfDataUrl = await new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onload = () => resolve(reader.result);
                        reader.onerror = () => reject(new Error('Impossible de lire le PDF reçu.'));
                        reader.readAsDataURL(blob);
                    });
                    const result = await window.pywebview.api.print_server_open_pdf(pdfDataUrl, filename);
                    if (!result?.ok) throw new Error(result?.error || 'Windows n’a pas pu ouvrir le lecteur PDF par défaut.');
                    return;
                }
                const response = await fetch(`/api/serveur-impression/livrets/${activePrintServerRecordId}/ouvrir`, {
                    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({viewer_id:viewerId})
                });
                const result = await response.json();
                if (!response.ok || !result.success) throw new Error(result.detail || 'Impossible d’ouvrir le PDF dans le lecteur sélectionné.');
                alert(result.message || 'Le livret a été ouvert dans le lecteur PDF sélectionné sur le serveur.');
            } catch (error) { alert(error.message || 'Échec de l’ouverture du PDF.'); }
        };
        window.printServerPreviewNow = function() {
            if (activePrintServerRecordId) window.submitServerPrint(activePrintServerRecordId);
        };
        window.closePrintServerPreview = function() {
            const modal = document.getElementById('printServerPreview');
            document.getElementById('printServerPreviewFrame').src = 'about:blank';
            activePrintServerRecordId = null;
            modal.classList.add('hidden'); modal.classList.remove('flex');
        };
        window.printServerBookletDirect = function(id) {
            window.submitServerPrint(id);
        };
        window.openPrintServerSpoolerModal = function() {
            const modal = document.getElementById('printServerSpoolerModal');
            modal.classList.remove('hidden'); modal.classList.add('flex');
            window.refreshPrintServerSpooler();
        };
        window.closePrintServerSpoolerModal = function() {
            const modal = document.getElementById('printServerSpoolerModal');
            modal.classList.add('hidden'); modal.classList.remove('flex');
        };
        window.refreshPrintServerSpooler = async function() {
            const body = document.getElementById('printSpoolerJobs');
            const message = document.getElementById('printSpoolerMessage');
            body.innerHTML = '<tr><td colspan="7" class="rounded-xl bg-slate-50 p-8 text-center text-slate-400"><i class="ph-bold ph-spinner animate-spin mr-2"></i>Lecture des files serveur…</td></tr>';
            try {
                const response = await fetch('/api/serveur-impression/spooler', {cache:'no-store'});
                const result = await response.json();
                if (!response.ok || !result.success) throw new Error(result.detail || 'Impossible de lire le spooler.');
                printSpoolerRows = result.jobs || [];
                const status = document.getElementById('printSpoolerStatus');
                status.textContent = `Spooler : ${result.service?.label || 'État inconnu'}`;
                status.className = `rounded-full px-3 py-2 text-xs font-extrabold ${result.service?.running ? 'bg-emerald-100 text-emerald-800' : 'bg-amber-100 text-amber-800'}`;
                document.getElementById('printSpoolerHost').textContent = `${printSpoolerRows.length} job(s) sur les imprimantes du serveur.`;
                body.innerHTML = printSpoolerRows.length ? printSpoolerRows.map((job, index) => `<tr class="bg-slate-50 text-slate-700 hover:bg-emerald-50/70"><td class="rounded-l-xl px-3 py-3 font-semibold">${htmlEscape(job.printer)}</td><td class="px-3 py-3"><div class="font-bold">${htmlEscape(job.document)}</div><div class="text-[10px] text-slate-500">Job #${Number(job.job_id)}${job.machine ? ` · ${htmlEscape(job.machine)}` : ''}</div></td><td class="px-3 py-3">${htmlEscape(job.owner)}</td><td class="px-3 py-3">${htmlEscape(job.status_text || (job.paused ? 'En pause' : 'En attente'))}</td><td class="px-3 py-3">${Number(job.pages_printed || 0)} / ${Number(job.pages || 0)}</td><td class="px-3 py-3 text-xs text-slate-500">${htmlEscape(job.submitted || '—')}</td><td class="rounded-r-xl px-3 py-3"><div class="flex justify-end gap-1"><button type="button" onclick="controlPrintServerJob(${index}, '${job.paused ? 'resume' : 'pause'}')" title="${job.paused ? 'Reprendre' : 'Mettre en pause'}" class="rounded-lg p-2 text-indigo-700 hover:bg-indigo-100"><i class="ph-bold ${job.paused ? 'ph-play' : 'ph-pause'}"></i></button><button type="button" onclick="controlPrintServerJob(${index}, 'delete')" title="Supprimer ce job" class="rounded-lg p-2 text-rose-600 hover:bg-rose-100"><i class="ph-bold ph-trash"></i></button></div></td></tr>`).join('') : '<tr><td colspan="7" class="rounded-xl bg-slate-50 p-10 text-center text-slate-400">Aucun travail dans le spooler.</td></tr>';
                message.textContent = '';
                message.className = 'min-h-5 px-5 pt-3 text-xs text-slate-500 sm:px-7';
            } catch (error) {
                body.innerHTML = `<tr><td colspan="7" class="rounded-xl bg-rose-50 p-8 text-center font-semibold text-rose-600">${htmlEscape(error.message)}</td></tr>`;
                message.textContent = error.message;
                message.className = 'min-h-5 px-5 pt-3 text-xs font-semibold text-rose-600 sm:px-7';
            }
        };
        window.controlPrintSpoolerService = async function(action) {
            const actionLabel = {start:'démarrer', stop:'arrêter', restart:'redémarrer'}[action] || action;
            if (action !== 'start' && !confirm(`Confirmer la commande « ${actionLabel} » du spooler serveur ?`)) return;
            const message = document.getElementById('printSpoolerMessage');
            try {
                const response = await fetch(`/api/serveur-impression/spooler/service/${action}`, {method:'POST'});
                const result = await response.json();
                if (!response.ok || !result.success) throw new Error(result.detail || `Impossible de ${actionLabel} le spooler.`);
                message.textContent = `Spooler : ${result.service?.label || 'Commande effectuée'}.`;
                message.className = 'min-h-5 px-5 pt-3 text-xs font-semibold text-emerald-700 sm:px-7';
                await window.refreshPrintServerSpooler();
            } catch (error) {
                message.textContent = error.message;
                message.className = 'min-h-5 px-5 pt-3 text-xs font-semibold text-rose-600 sm:px-7';
                alert(error.message);
            }
        };
        window.controlPrintServerJob = async function(index, action) {
            const job = printSpoolerRows[index];
            if (!job) return;
            if (action === 'delete' && !confirm(`Supprimer le job #${job.job_id} de l’imprimante « ${job.printer} » ?`)) return;
            const message = document.getElementById('printSpoolerMessage');
            try {
                const response = await fetch(`/api/serveur-impression/spooler/jobs/${action}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({printer:job.printer, job_id:job.job_id})});
                const result = await response.json();
                if (!response.ok || !result.success) throw new Error(result.detail || 'Action sur le job impossible.');
                message.textContent = result.message;
                message.className = 'min-h-5 px-5 pt-3 text-xs font-semibold text-emerald-700 sm:px-7';
                await window.refreshPrintServerSpooler();
            } catch (error) {
                message.textContent = error.message;
                message.className = 'min-h-5 px-5 pt-3 text-xs font-semibold text-rose-600 sm:px-7';
                alert(error.message);
            }
        };
        window.deleteAllPrintServerJobs = async function() {
            if (!printSpoolerRows.length) { alert('Aucun job à supprimer.'); return; }
            if (!confirm(`Supprimer les ${printSpoolerRows.length} job(s) de toutes les files d’attente du serveur ?`)) return;
            const message = document.getElementById('printSpoolerMessage');
            try {
                const response = await fetch('/api/serveur-impression/spooler/jobs/delete-all', {method:'POST'});
                const result = await response.json();
                if (!response.ok || !result.success) throw new Error(result.detail || 'La suppression des jobs a échoué.');
                message.textContent = result.errors?.length ? `${result.message} ${result.errors.length} job(s) n’ont pas pu être supprimés.` : result.message;
                message.className = `min-h-5 px-5 pt-3 text-xs font-semibold ${result.errors?.length ? 'text-amber-700' : 'text-emerald-700'} sm:px-7`;
                await window.refreshPrintServerSpooler();
            } catch (error) {
                message.textContent = error.message;
                message.className = 'min-h-5 px-5 pt-3 text-xs font-semibold text-rose-600 sm:px-7';
                alert(error.message);
            }
        };

        window.openPrintServerConfigModal = function() {
            const modal = document.getElementById('printServerConfigModal');
            modal.classList.remove('hidden'); modal.classList.add('flex');
            window.loadPrintServerPrinters();
        };
        window.closePrintServerConfigModal = function() {
            const modal = document.getElementById('printServerConfigModal');
            modal.classList.add('hidden'); modal.classList.remove('flex');
        };
        window.loadPrintServerPrinters();
        window.loadPrintServerBooklets();
    })();
    </script>'''
