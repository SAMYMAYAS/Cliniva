"""Module principal et point d'entrée de l'application de bureau Cliniva.

Architecture logicielle complète :
- Serveur FastAPI asynchrone embarqué
- Interface logicielle native Windows (Microsoft Edge WebView2)
- Connexion et synchronisation avec base de données PostgreSQL
- Système d'authentification, gestion des sessions et contrôle d'accès
- Moteur de traçabilité et journal d'audit en temps réel
- Inclusion de l'ensemble des routeurs métiers (Patients, Staff, Médicaments, etc.)
- Exécuteur dynamique de modules avec rechargement à chaud
- Conforme strictement aux normes PEP 8
"""

import html
import importlib
import json
import logging
import multiprocessing
import os
import platform
import re
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional

import uvicorn
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
import webview

# Services et Persistance
from app.services import DashboardService
from app.voip_server import voip_server
from database import init_database, get_db_cursor, record_audit

# Moteur d'authentification et de session
from app.auth import (
    SESSION_COOKIE_NAME,
    hash_password,
    create_user_session,
    delete_user_session,
    get_current_user_optional,
    track_action,
)

# Routeurs des modules métiers
from modules.clinique_param import clinic_router
from modules.gestion_specialites import specialites_router
from modules.gestion_salles_soin import rooms_router
from modules.gestion_staff import staff_router
from modules.gestion_medicaments import medications_router
from modules.gestion_patients import patients_router
from modules.gestion_medecins_traitants import treating_doctors_router
from modules.gestion_utilisateur import users_router
from modules.gestion_audit import audit_router
from modules.gestion_modeles_cr import cr_templates_router
from modules.gestion_passages import reception_router
from modules.gestion_dme import dme_router
from modules.gestion_demandes_bilans import exam_requests_router
from modules.gestion_caisse import caisse_router
from modules.gestion_rendezvous import appointments_router
from modules.gestion_worklist import worklist_router
from modules.gestion_telephonie_ip import telephony_router
from modules.gestion_serveur_impression import print_server_router
from modules import gestion_dicom_print
from modules.gestion_backup import backup_router, start_backup_scheduler, stop_backup_scheduler
from modules.gestion_kacel_job import kacel_job_router
from app.mobile_scan import mobile_scan_router


# Configuration centralisée de la journalisation
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
)
logger = logging.getLogger("ClinivaDesktop")

BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
CONFIG_FILE = "config.json"


# ============================================================================
# CONFIGURATION CLIENT / SERVEUR
# ============================================================================
def load_app_configuration() -> Dict[str, Any]:
    """Charge ou génère la configuration réseau et de la fenêtre logicielle.

    Returns:
        Dict[str, Any]: Paramètres de configuration du système.
    """
    default_config = {
        "server_mode": "local",  # 'local' (autonome) ou 'remote' (poste client réseau)
        "remote_server_url": "http://127.0.0.1:8000",
        "local_host": "0.0.0.0",
        "local_port": 8000,
        "window_title": "Cliniva - Système de Gestion de Clinique",
        "window_width": 1400,
        "window_height": 900,
        "mwl_enabled": True,
        "mwl_bind_host": "0.0.0.0",
        "mwl_port": 11112,
        "mwl_ae_title": "CLINIVA_MWL",
        "mwl_station_ae_title": "",
        "pacs_auto_start": False,
    }

    if not os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as file_handler:
                json.dump(default_config, file_handler, indent=4)
                logger.info("Fichier config.json initialisé avec succès.")
        except IOError as err:
            logger.error("Impossible de créer config.json : %s", err)
        return default_config
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as file_handler:
            config = json.load(file_handler)
            if not isinstance(config, dict):
                return default_config
            # Preserve the previous PACS startup preference for existing installs.
            config.setdefault("pacs_auto_start", bool(config.get("pacs_enabled", False)))
            return config
    except Exception as exc:
        logger.warning(
            "Erreur lors de la lecture de config.json (%s), chargement des valeurs par défaut.",
            exc,
        )
        return default_config


def _persist_config_updates(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Enregistre de petits réglages applicatifs sans écraser les autres clés."""
    config_path = os.path.abspath(CONFIG_FILE)
    current = load_app_configuration()
    current.update(updates)
    temporary_path = config_path + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as destination:
        json.dump(current, destination, ensure_ascii=False, indent=4)
    os.replace(temporary_path, config_path)
    return current


class ClientDicomServicesBridge:
    """Pont pywebview vers les services DICOM exécutés sur le poste courant."""

    def interface_zoom(self, action="status", value=None):
        """Active les raccourcis et règle le zoom sur le thread UI Windows."""
        from app.interface_zoom import update_native_zoom
        if action not in {"status", "in", "out", "reset", "set"}:
            return {"ok": False, "error": "Commande de zoom invalide."}
        if platform.system() != "Windows" or not webview.windows:
            return {"ok": False, "error": "Utilisez les raccourcis de zoom du navigateur."}
        result = {}
        try:
            native = webview.windows[0].native
            def update():
                result.update(update_native_zoom(native, action, value))
            if native.InvokeRequired:
                from System import Action
                native.Invoke(Action(update))
            else:
                update()
            return result
        except Exception as exc:
            logger.warning("Zoom de l'interface indisponible : %s", exc)
            return {"ok": False, "error": "Le navigateur intégré n'est pas prêt pour le zoom."}

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._print_server = None
        self._pacs_server = None
        self._pacs_datetime_jobs: Dict[str, Dict[str, Any]] = {}
        self._pacs_datetime_jobs_lock = threading.Lock()
        self._pacs_send_jobs: Dict[str, Dict[str, Any]] = {}
        self._pacs_send_jobs_lock = threading.Lock()
        self._pacs_send_processes: Dict[str, Any] = {}

    def client_scanner_detect(self) -> Dict[str, Any]:
        """Liste les scanners WIA connectés au poste qui affiche Cliniva."""
        if sys.platform != "win32":
            return {"success": False, "scanners": [], "message": "Le scan matériel est disponible sur Windows."}
        pythoncom = None
        try:
            import pythoncom as _pythoncom
            import win32com.client
            pythoncom = _pythoncom
            pythoncom.CoInitialize()
            manager = win32com.client.Dispatch("WIA.DeviceManager")
            scanners = []
            for info in manager.DeviceInfos:
                try:
                    name = str(info.Properties("Name").Value or "Scanner WIA")
                    scanners.append({"id": str(info.DeviceID), "name": name})
                except Exception:
                    continue
            return {"success": True, "scanners": scanners,
                    "message": "Scanner(s) détecté(s) sur ce poste." if scanners else "Aucun scanner WIA détecté sur ce poste."}
        except Exception as exc:
            logger.info("Détection du scanner client indisponible : %s", exc)
            return {"success": False, "scanners": [], "message": "WIA est indisponible sur ce poste. Vérifiez le pilote du scanner et pywin32."}
        finally:
            if pythoncom:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    def client_scanner_acquire(self, device_id: str = "") -> Dict[str, Any]:
        """Acquiert une page sur le scanner du poste et la renvoie au formulaire web."""
        if sys.platform != "win32":
            return {"success": False, "message": "Le scan matériel est disponible sur Windows."}
        pythoncom = None
        temporary_path = None
        try:
            import base64
            import tempfile
            import pythoncom as _pythoncom
            import win32com.client
            pythoncom = _pythoncom
            pythoncom.CoInitialize()
            manager = win32com.client.Dispatch("WIA.DeviceManager")
            device_info = next((item for item in manager.DeviceInfos
                                if str(item.DeviceID) == str(device_id)), None) if device_id else None
            if device_info is None:
                return {"success": False, "message": "Le scanner sélectionné n'est plus disponible."}
            device = device_info.Connect()
            if device.Items.Count < 1:
                return {"success": False, "message": "Le scanner ne contient aucun élément de numérisation."}
            image = device.Items.Item(1).Transfer("{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}")
            fd, temporary_path = tempfile.mkstemp(prefix="cliniva_scan_", suffix=".jpg")
            os.close(fd)
            image.SaveFile(temporary_path)
            with open(temporary_path, "rb") as source:
                content = source.read(25 * 1024 * 1024 + 1)
            if not content or len(content) > 12 * 1024 * 1024:
                return {"success": False, "message": "Le scan est vide ou dépasse la limite de 12 Mo. Réduisez la résolution du scanner."}
            encoded = base64.b64encode(content).decode("ascii")
            return {"success": True, "image_data": "data:image/jpeg;base64," + encoded,
                    "name": Path(temporary_path).name}
        except Exception as exc:
            logger.warning("Échec de numérisation sur le poste client : %s", exc)
            return {"success": False, "message": f"Le scanner n'a pas pu numériser le document : {exc}"}
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
            if pythoncom:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    def client_open_word_document(self, docx_data_url: str, suggested_name: str = "Compte_rendu.docx") -> Dict[str, Any]:
        """Télécharge une copie locale du document reçu et l'ouvre avec Office/Word."""
        try:
            import base64
            if sys.platform != "win32":
                return {"ok": False, "error": "L'ouverture dans Microsoft Word nécessite un poste Windows."}
            header, separator, encoded = str(docx_data_url or "").partition(",")
            expected = "data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64"
            if not separator or header.strip().lower() != expected:
                return {"ok": False, "error": "Le document Word reçu est invalide."}
            content = base64.b64decode(encoded, validate=True)
            if not content or len(content) > 100 * 1024 * 1024:
                return {"ok": False, "error": "Le document Word est vide ou dépasse 100 Mo."}
            name = os.path.basename(str(suggested_name or "Compte_rendu.docx"))
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "Compte_rendu.docx"
            if not name.lower().endswith(".docx"):
                name += ".docx"
            target_dir = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "Cliniva" / "Documents"
            target_dir.mkdir(parents=True, exist_ok=True)
            output_path = target_dir / name
            if output_path.exists():
                output_path = target_dir / f"{output_path.stem}_{int(time.time())}.docx"
            output_path.write_bytes(content)
            os.startfile(str(output_path))
            return {"ok": True, "path": str(output_path)}
        except Exception as exc:
            logger.exception("Impossible d'ouvrir le document Word sur le poste client")
            return {"ok": False, "error": str(exc)}

    def _service(self):
        if self._print_server is None:
            from app.dicom_print_service import DicomPrintServer
            self._print_server = DicomPrintServer()
            saved = self.config.get("dicom_print_config")
            if isinstance(saved, dict):
                self._print_server._config.update(saved)
        return self._print_server

    def _save_config(self) -> None:
        config_path = os.path.abspath(CONFIG_FILE)
        try:
            existing = {}
            if os.path.isfile(config_path):
                with open(config_path, "r", encoding="utf-8") as source:
                    existing = json.load(source)
            if not isinstance(existing, dict):
                existing = {}
            existing.update({key: value for key, value in self.config.items()
                             if key.startswith(("dicom_print_", "pacs_"))})
            temporary_path = config_path + ".tmp"
            with open(temporary_path, "w", encoding="utf-8") as destination:
                json.dump(existing, destination, ensure_ascii=False, indent=4)
            os.replace(temporary_path, config_path)
        except Exception:
            logger.exception("Impossible d’enregistrer la configuration DICOM Print locale")

    def dicom_print_status(self) -> Dict[str, Any]:
        with self._lock:
            status_data = self._service().status()
            return {**status_data, "machine": socket.gethostname()}

    def dicom_print_start(self, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            with self._lock:
                status_data = self._service().start(settings or {})
                self.config["dicom_print_config"] = status_data["config"]
                self.config["dicom_print_enabled"] = True
                self._save_config()
            return {"ok": True, **status_data, "machine": socket.gethostname()}
        except Exception as exc:
            logger.warning("Impossible de démarrer DICOM Print sur cette machine : %s", exc)
            return {"ok": False, "error": str(exc)}

    def dicom_print_stop(self) -> Dict[str, Any]:
        try:
            with self._lock:
                status_data = self._service().stop()
                self.config["dicom_print_enabled"] = False
                self._save_config()
            return {"ok": True, **status_data}
        except Exception as exc:
            logger.exception("Impossible d’arrêter DICOM Print sur ce poste")
            return {"ok": False, "error": str(exc)}

    def dicom_services_shutdown(self) -> None:
        """Arrête les listeners à la fermeture, sans désactiver l'auto-démarrage."""
        try:
            with self._lock:
                if self._print_server is not None:
                    self._print_server.stop()
                if self._pacs_server is not None:
                    self._pacs_server.stop()
        except Exception:
            logger.exception("Erreur pendant l’arrêt des services DICOM locaux")

    def dicom_print_recent_images(self) -> list[Dict[str, Any]]:
        try:
            with self._lock:
                return self._service().recent_images()
        except Exception:
            logger.exception("Impossible de lister les images DICOM Print locales")
            return []

    def dicom_print_group_images(self, group_id: str) -> list[Dict[str, Any]]:
        with self._lock:
            return self._service().group_images(group_id)

    def dicom_print_reorder_group_images(self, group_id: str, image_names: list[str]) -> Dict[str, Any]:
        try:
            with self._lock:
                return self._service().reorder_group_images(group_id, image_names)
        except Exception as exc:
            logger.warning("Impossible de réordonner les images DICOM Print du dossier %s : %s", group_id, exc)
            return {"ok": False, "error": str(exc)}

    def dicom_print_import_group_images(self, target_group_id: str, source_group_id: str,
                                        image_names: list[str]) -> Dict[str, Any]:
        try:
            with self._lock:
                return self._service().import_group_images(target_group_id, source_group_id, image_names)
        except Exception as exc:
            logger.warning("Impossible d’importer des images DICOM Print dans %s depuis %s : %s",
                           target_group_id, source_group_id, exc)
            return {"ok": False, "error": str(exc)}

    def dicom_print_group_image(self, group_id: str, index: int) -> Dict[str, Any]:
        with self._lock:
            return self._service().group_image(group_id, index)

    def dicom_print_group_thumbnail(self, group_id: str, index: int) -> Dict[str, Any]:
        with self._lock:
            return self._service().group_image_thumbnail(group_id, index)

    def dicom_print_delete_group(self, group_id: str) -> Dict[str, Any]:
        try:
            with self._lock:
                return self._service().delete_group(group_id)
        except Exception as exc:
            logger.warning("Impossible de supprimer le dossier DICOM Print %s : %s", group_id, exc)
            return {"ok": False, "error": str(exc)}

    def dicom_print_group_image_pdf(self, group_id: str, index: int) -> Dict[str, Any]:
        """Construit un PDF A3 contenant l’image DICOM courante, prêt à télécharger/imprimer."""
        import base64
        import io
        from PIL import Image

        image_data = self.dicom_print_group_image(group_id, index)
        raw = base64.b64decode(image_data["data_url"].split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as source:
            page = Image.new("RGB", (3508, 4961), "white")
            image = source.convert("RGB")
            image.thumbnail((3388, 4841), Image.Resampling.LANCZOS)
            page.paste(image, ((3508 - image.width) // 2, (4961 - image.height) // 2))
            output = io.BytesIO()
            page.save(output, format="PDF", resolution=300.0)
        group = next((item for item in self._service().recent_images()
                      if item.get("id") == group_id), {})
        return {"name": image_data["name"],
                "patient_name": group.get("patient_name", ""),
                "pdf_data_url": "data:application/pdf;base64," + base64.b64encode(output.getvalue()).decode("ascii")}

    def dicom_print_save_pdf(self, pdf_data_url: str, suggested_name: str = "Image_DICOM_A3.pdf") -> Dict[str, Any]:
        """Demande un emplacement natif puis enregistre le PDF A3 sur le poste client."""
        import base64

        try:
            if not webview.windows:
                return {"ok": False, "error": "La fenêtre Cliniva n’est pas prête à enregistrer le PDF."}
            header, separator, encoded = str(pdf_data_url or "").partition(",")
            if not separator or header.strip().lower() != "data:application/pdf;base64":
                return {"ok": False, "error": "Le contenu PDF à enregistrer est invalide."}
            content = base64.b64decode(encoded, validate=True)
            name = os.path.basename(str(suggested_name or "Image_DICOM_A3.pdf"))
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "Image_DICOM_A3.pdf"
            if not name.lower().endswith(".pdf"):
                name += ".pdf"
            dialog_api = getattr(webview, "FileDialog", None)
            dialog_type = dialog_api.SAVE if dialog_api else webview.SAVE_DIALOG
            selected = webview.windows[0].create_file_dialog(
                dialog_type, save_filename=name, file_types=("Fichiers PDF (*.pdf)",)
            )
            if not selected:
                return {"ok": True, "cancelled": True}
            output_path = selected[0] if isinstance(selected, (list, tuple)) else selected
            output_path = os.fspath(output_path)
            if not output_path.lower().endswith(".pdf"):
                output_path += ".pdf"
            with open(output_path, "wb") as destination:
                destination.write(content)
            return {"ok": True, "path": output_path}
        except Exception as exc:
            logger.exception("Impossible d’enregistrer le PDF A3 DICOM Print")
            return {"ok": False, "error": str(exc)}

    def kacel_job_save_and_open_docx(self, docx_data_url: str, suggested_name: str = "Convention.docx") -> Dict[str, Any]:
        """Enregistre un DOCX via le dialogue Windows puis l’ouvre dans Word."""
        import base64

        try:
            if sys.platform != "win32":
                return {"ok": False, "error": "L’ouverture automatique du document nécessite un poste Windows."}
            if not webview.windows:
                return {"ok": False, "error": "La fenêtre Cliniva n’est pas prête à enregistrer le document."}
            header, separator, encoded = str(docx_data_url or "").partition(",")
            expected = "data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64"
            if not separator or header.strip().lower() != expected:
                return {"ok": False, "error": "Le document Word à enregistrer est invalide."}
            content = base64.b64decode(encoded, validate=True)
            if not content or len(content) > 100 * 1024 * 1024:
                return {"ok": False, "error": "Le document Word est vide ou dépasse 100 Mo."}
            name = os.path.basename(str(suggested_name or "Convention.docx"))
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "Convention.docx"
            if not name.lower().endswith(".docx"):
                name += ".docx"
            dialog_api = getattr(webview, "FileDialog", None)
            dialog_type = dialog_api.SAVE if dialog_api else webview.SAVE_DIALOG
            selected = webview.windows[0].create_file_dialog(
                dialog_type, save_filename=name, file_types=("Document Word (*.docx)",)
            )
            if not selected:
                return {"ok": True, "cancelled": True}
            output_path = selected[0] if isinstance(selected, (list, tuple)) else selected
            output_path = os.fspath(output_path)
            if not output_path.lower().endswith(".docx"):
                output_path += ".docx"
            with open(output_path, "wb") as destination:
                destination.write(content)
            os.startfile(output_path)
            return {"ok": True, "path": output_path}
        except Exception as exc:
            logger.exception("Impossible d’enregistrer ou d’ouvrir le document Word Kacel Job")
            return {"ok": False, "error": str(exc)}

    def print_server_save_pdf(self, pdf_data_url: str, suggested_name: str = "Livret.pdf") -> Dict[str, Any]:
        """Enregistre un PDF du serveur d’impression via le dialogue natif du poste Cliniva."""
        import base64

        try:
            header, separator, encoded = str(pdf_data_url or "").partition(",")
            if not separator or header.strip().lower() != "data:application/pdf;base64":
                return {"ok": False, "error": "Le contenu PDF à enregistrer est invalide."}
            content = base64.b64decode(encoded, validate=True)
            if len(content) > 200 * 1024 * 1024:
                return {"ok": False, "error": "Le PDF dépasse la taille maximale autorisée (200 Mo)."}
            name = os.path.basename(str(suggested_name or "Livret.pdf"))
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "Livret.pdf"
            if not name.lower().endswith(".pdf"):
                name += ".pdf"
            if not webview.windows:
                return {"ok": False, "error": "La fenêtre Cliniva n’est pas prête à enregistrer le PDF."}
            dialog_api = getattr(webview, "FileDialog", None)
            dialog_type = dialog_api.SAVE if dialog_api else webview.SAVE_DIALOG
            selected = webview.windows[0].create_file_dialog(
                dialog_type, save_filename=name, file_types=("Fichiers PDF (*.pdf)",)
            )
            if not selected:
                return {"ok": True, "cancelled": True}
            output_path = selected[0] if isinstance(selected, (list, tuple)) else selected
            output_path = os.fspath(output_path)
            if not output_path.lower().endswith(".pdf"):
                output_path += ".pdf"
            with open(output_path, "wb") as destination:
                destination.write(content)
            return {"ok": True, "path": output_path}
        except Exception as exc:
            logger.exception("Impossible d’enregistrer le PDF du serveur d’impression sur le poste client")
            return {"ok": False, "error": str(exc)}

    def print_server_open_pdf(self, pdf_data_url: str, suggested_name: str = "Livret.pdf") -> Dict[str, Any]:
        """Ouvre le PDF téléchargé depuis le serveur avec le lecteur par défaut du poste Cliniva."""
        import base64
        import tempfile

        try:
            if sys.platform != "win32":
                return {"ok": False, "error": "L’ouverture dans le lecteur PDF Windows nécessite un poste Windows."}
            header, separator, encoded = str(pdf_data_url or "").partition(",")
            if not separator or header.strip().lower() != "data:application/pdf;base64":
                return {"ok": False, "error": "Le contenu PDF à ouvrir est invalide."}
            content = base64.b64decode(encoded, validate=True)
            if len(content) > 200 * 1024 * 1024:
                return {"ok": False, "error": "Le PDF dépasse la taille maximale autorisée (200 Mo)."}
            stem = Path(str(suggested_name or "Livret.pdf")).stem
            stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .") or "Livret"
            with tempfile.NamedTemporaryFile(prefix=f"{stem}_", suffix=".pdf", delete=False) as destination:
                destination.write(content)
                pdf_path = destination.name
            os.startfile(pdf_path)
            return {"ok": True, "path": pdf_path}
        except Exception as exc:
            logger.exception("Impossible d’ouvrir le PDF du serveur d’impression sur le poste client")
            return {"ok": False, "error": str(exc)}

    def dicom_print_open_folder(self) -> Dict[str, Any]:
        try:
            with self._lock:
                folder = self._service().status()["config"]["storage_dir"]
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": f"Impossible d’ouvrir le dossier local : {exc}"}

    def _pacs(self):
        if self._pacs_server is None:
            from app.pacs_server import PacsServer
            self._pacs_server = PacsServer(self.config.get("pacs_config"))
        return self._pacs_server

    def pacs_status(self) -> Dict[str, Any]:
        with self._lock:
            return {**self._pacs().status(), "machine": socket.gethostname(),
                    "deployment_mode": self.config.get("server_mode", "local"),
                    "auto_start": bool(self.config.get(
                        "pacs_auto_start", self.config.get("pacs_enabled", False)))}

    def pacs_set_auto_start(self, enabled: bool) -> Dict[str, Any]:
        """Mémorise le démarrage PACS au lancement, sans modifier son état courant."""
        with self._lock:
            self.config["pacs_auto_start"] = bool(enabled)
            self._save_config()
            return {"ok": True, "auto_start": self.config["pacs_auto_start"]}

    def pacs_start(self, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            with self._lock:
                result = self._pacs().start(settings or {})
                self.config["pacs_config"] = result["config"]
                self.config["pacs_enabled"] = True
                self._save_config()
            return {"ok": True, **result, "machine": socket.gethostname(),
                    "deployment_mode": self.config.get("server_mode", "local")}
        except Exception as exc:
            logger.warning("Impossible de démarrer le PACS sur ce poste (%s) : %s",
                           self.config.get("server_mode", "local"), exc)
            return {"ok": False, "error": str(exc)}

    def pacs_stop(self) -> Dict[str, Any]:
        try:
            with self._lock:
                result = self._pacs().stop()
                self.config["pacs_enabled"] = False
                self._save_config()
            return {"ok": True, **result}
        except Exception as exc:
            logger.exception("Impossible d’arrêter le PACS sur ce poste")
            return {"ok": False, "error": str(exc)}

    def pacs_stats(self) -> Dict[str, int]:
        with self._lock:
            return self._pacs().stats()

    def pacs_studies(self, query: str = "", limit: int = 15,
                     filters: Optional[Dict[str, Any]] = None,
                     offset: int = 0) -> Dict[str, Any]:
        with self._lock:
            return self._pacs().studies(query, limit, filters, offset)

    def pacs_study_detail(self, study_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._pacs().study_detail(study_id)

    def pacs_nodes(self) -> list[Dict[str, Any]]:
        with self._lock:
            return self._pacs().nodes()

    def pacs_node_save(self, node: Dict[str, Any]) -> Dict[str, Any]:
        try:
            with self._lock:
                return self._pacs().save_node(node)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def pacs_node_delete(self, node_id: int) -> Dict[str, Any]:
        try:
            with self._lock:
                return self._pacs().delete_node(node_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def pacs_node_echo(self, node_id: int) -> Dict[str, Any]:
        try:
            with self._lock:
                return self._pacs().echo_node(node_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def pacs_send(self, node_id: int, kind: str, record_id: int) -> Dict[str, Any]:
        job_id = f"pacs-send-{threading.get_ident()}-{time.time_ns()}"
        job = {"job_id": job_id, "status": "running", "progress": 0, "processed": 0,
               "sent": 0, "failed": 0, "total": 0, "message": "Préparation de l’envoi…",
               "result": None, "error": None}
        with self._pacs_send_jobs_lock:
            self._pacs_send_jobs[job_id] = job

        def run_send() -> None:
            progress_queue = None
            process = None
            try:
                with self._lock:
                    service_config = dict(self._pacs().status().get("config") or {})
                # L'envoi et les éventuelles conversions DICOM s'exécutent hors du
                # processus de l'interface : ils ne peuvent donc pas monopoliser
                # son thread principal ni le GIL pendant les transferts lourds.
                context = multiprocessing.get_context("spawn")
                progress_queue = context.Queue()
                from app.pacs_transfer_worker import run_pacs_transfer
                process = context.Process(
                    target=run_pacs_transfer,
                    args=(service_config, int(node_id), str(kind), int(record_id), progress_queue),
                    name="ClinivaPacsSendProcess",
                    daemon=True,
                )
                process.start()
                with self._pacs_send_jobs_lock:
                    self._pacs_send_processes[job_id] = process
                    job.update(message="Envoi démarré dans un processus indépendant")

                terminal = False
                while not terminal:
                    try:
                        event = progress_queue.get(timeout=0.4)
                    except Exception:
                        if process.is_alive():
                            continue
                        # Laisser le pipe vider son dernier message avant de conclure.
                        try:
                            event = progress_queue.get(timeout=0.2)
                        except Exception:
                            break
                    event_type = event.get("type")
                    if event_type == "progress":
                        state = event.get("state") or {}
                        total = int(state.get("total") or 0)
                        processed = int(state.get("processed") or 0)
                        with self._pacs_send_jobs_lock:
                            current = self._pacs_send_jobs.get(job_id)
                            if current:
                                current.update(state, progress=100 if total == 0 else
                                               max(0, min(100, int(processed * 100 / total))))
                    elif event_type == "complete":
                        result = event.get("result") or {}
                        with self._pacs_send_jobs_lock:
                            job.update(status="complete", progress=100,
                                       message="Envoi terminé", result=result)
                        terminal = True
                    elif event_type == "error":
                        with self._pacs_send_jobs_lock:
                            job.update(status="error", message="L’envoi a échoué",
                                       error=str(event.get("error") or "Erreur du processus de transfert."))
                        terminal = True
                if not terminal:
                    exit_code = process.exitcode
                    with self._pacs_send_jobs_lock:
                        job.update(status="error", message="Le processus d’envoi s’est arrêté",
                                   error=f"Le processus de transfert DICOM s’est arrêté sans résultat (code {exit_code}).")
            except Exception as exc:
                logger.exception("Échec d’envoi DICOM PACS")
                with self._pacs_send_jobs_lock:
                    job.update(status="error", message="L’envoi a échoué", error=str(exc))
            finally:
                if process is not None:
                    try:
                        process.join(timeout=1)
                    except Exception:
                        pass
                if progress_queue is not None:
                    try:
                        progress_queue.close()
                    except Exception:
                        pass
                with self._pacs_send_jobs_lock:
                    self._pacs_send_processes.pop(job_id, None)

        threading.Thread(target=run_send, name="ClinivaPacsSend", daemon=True).start()
        return {"ok": True, "job_id": job_id}

    # Keep transfer state in this bridge instance: pywebview polls this method
    # repeatedly while the worker thread sends each C-STORE object.
    def pacs_send(self, node_id: int, kind: str, record_id: int) -> Dict[str, Any]:
        job_id = f"pacs-send-{threading.get_ident()}-{time.time_ns()}"
        job = {"job_id": job_id, "status": "running", "progress": 0, "processed": 0,
               "sent": 0, "failed": 0, "total": 0, "message": "Préparation de l’envoi…",
               "result": None, "error": None}
        with self._pacs_send_jobs_lock:
            self._pacs_send_jobs[job_id] = job

        def run_send() -> None:
            try:
                with self._lock:
                    service = self._pacs()

                def update_progress(state: Dict[str, Any]) -> None:
                    total = int(state.get("total") or 0)
                    processed = int(state.get("processed") or 0)
                    with self._pacs_send_jobs_lock:
                        current = self._pacs_send_jobs.get(job_id)
                        if current is not None:
                            current.update(state, progress=100 if total == 0 else
                                           max(0, min(100, int(processed * 100 / total))))

                result = service.send_to_node(node_id, kind, record_id, update_progress)
                with self._pacs_send_jobs_lock:
                    job.update(status="complete", progress=100,
                               message="Envoi terminé", result=result)
            except Exception as exc:
                logger.exception("Échec d’envoi DICOM PACS")
                with self._pacs_send_jobs_lock:
                    job.update(status="error", message="L’envoi a échoué", error=str(exc))

        threading.Thread(target=run_send, name="ClinivaPacsSend", daemon=True).start()
        return {"ok": True, "job_id": job_id}

    def pacs_send_progress(self, job_id: str) -> Dict[str, Any]:
        with self._pacs_send_jobs_lock:
            job = self._pacs_send_jobs.get(str(job_id))
            if not job:
                return {"ok": False, "error": "Progression du transfert introuvable."}
            return {"ok": True, **job}

    def pacs_transfer_history(self) -> list[Dict[str, Any]]:
        with self._lock:
            return self._pacs().transfer_history()

    def pacs_patient_update(self, study_id: int, values: Dict[str, Any]) -> Dict[str, Any]:
        try:
            with self._lock:
                return self._pacs().update_patient(study_id, values)
        except Exception as exc:
            logger.exception("Échec de modification des informations patient PACS")
            return {"ok": False, "error": str(exc)}

    def pacs_study_datetime_update(self, study_id: int, values: Dict[str, Any]) -> Dict[str, Any]:
        job_id = f"pacs-datetime-{threading.get_ident()}-{time.time_ns()}"
        job = {"job_id": job_id, "status": "running", "progress": 0,
               "message": "Démarrage de la modification…", "result": None, "error": None}
        with self._pacs_datetime_jobs_lock:
            self._pacs_datetime_jobs[job_id] = job

        def run_update() -> None:
            try:
                with self._lock:
                    service = self._pacs()

                def update_progress(percent: int, message: str) -> None:
                    with self._pacs_datetime_jobs_lock:
                        current = self._pacs_datetime_jobs.get(job_id)
                        if current:
                            current.update(progress=max(0, min(100, int(percent))), message=message)

                result = service.update_study_datetime(study_id, values, update_progress)
                with self._pacs_datetime_jobs_lock:
                    job.update(status="complete", progress=100, message="Modification terminée", result=result)
            except Exception as exc:
                logger.exception("Échec de modification de la date/heure de l’examen PACS")
                with self._pacs_datetime_jobs_lock:
                    job.update(status="error", message="La modification a échoué", error=str(exc))

        threading.Thread(target=run_update, name="ClinivaPacsDateTimeUpdate", daemon=True).start()
        return {"ok": True, "job_id": job_id}

    def pacs_study_datetime_progress(self, job_id: str) -> Dict[str, Any]:
        with self._pacs_datetime_jobs_lock:
            job = self._pacs_datetime_jobs.get(str(job_id))
            if not job:
                return {"ok": False, "error": "Progression de modification introuvable."}
            return {**job}

    def pacs_study_delete(self, study_id: int) -> Dict[str, Any]:
        try:
            with self._lock:
                return self._pacs().delete_study(study_id)
        except Exception as exc:
            logger.exception("Échec de suppression d’un examen PACS")
            return {"ok": False, "error": str(exc)}

    def pacs_series_instances(self, study_id: int, series_id: int) -> list[Dict[str, Any]]:
        with self._lock:
            return self._pacs().series_instances(study_id, series_id)

    def pacs_image_preview(self, study_id: int, instance_id: int,
                           window_center: Optional[float] = None,
                           window_width: Optional[float] = None) -> Dict[str, Any]:
        with self._lock:
            return self._pacs().image_preview(study_id, instance_id, window_center, window_width)

    def pacs_import_source(self, source_type: str) -> Dict[str, Any]:
        """Ouvre le sélecteur natif sur le poste courant et importe dossier ou ZIP."""
        try:
            if not webview.windows:
                return {"ok": False, "error": "La fenêtre Cliniva n’est pas prête pour choisir des fichiers."}
            file_dialog = getattr(webview, "FileDialog", None)
            if source_type == "folder":
                dialog_type = file_dialog.FOLDER if file_dialog else webview.FOLDER_DIALOG
                selected = webview.windows[0].create_file_dialog(dialog_type)
            elif source_type == "zip":
                dialog_type = file_dialog.OPEN if file_dialog else webview.OPEN_DIALOG
                selected = webview.windows[0].create_file_dialog(
                    dialog_type, allow_multiple=False,
                    file_types=("Archives ZIP (*.zip)",),
                )
            else:
                return {"ok": False, "error": "Choisissez un dossier ou une archive ZIP."}
            if not selected:
                return {"ok": True, "cancelled": True}
            source_path = selected[0] if isinstance(selected, (list, tuple)) else selected
            with self._lock:
                report = self._pacs().import_source(str(source_path), source_type)
            return report
        except Exception as exc:
            logger.exception("Échec de l’importation DICOM PACS")
            return {"ok": False, "error": str(exc)}

    def pacs_open_folder(self) -> Dict[str, Any]:
        try:
            with self._lock:
                folder = self._pacs().status()["config"]["data_dir"]
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": f"Impossible d’ouvrir le dossier PACS local : {exc}"}

    def pacs_study_open_folder(self, study_id: int) -> Dict[str, Any]:
        try:
            with self._lock:
                folder = self._pacs().study_folder(study_id)
            os.startfile(str(folder))
            return {"ok": True, "path": str(folder)}
        except Exception as exc:
            logger.exception("Impossible d’ouvrir le dossier DICOM de l’examen %s", study_id)
            return {"ok": False, "error": str(exc)}

# ============================================================================
# APPLICATION FASTAPI & ATTACHEMENT DES ROUTEURS
# ============================================================================
_runtime_state = {"server": None, "thread": None, "restarting": False}
_runtime_restart_lock = threading.Lock()


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    """Gère le démarrage et l'arrêt des services de l'application."""
    await start_worklist_service()
    backup_scheduler_started = False
    voip_schema_ready = False
    try:
        voip_server.ensure_schema()
        voip_schema_ready = True
        if load_app_configuration().get("server_mode") == "local":
            try:
                from app.client_update_service import publish_client_release

                publish_client_release(BASE_DIR, STATIC_DIR)
            except Exception:
                logger.exception("Impossible de publier la mise à jour des postes clients")
            voip_server.autostart()
        if load_app_configuration().get("server_mode") == "local":
            start_backup_scheduler()
            backup_scheduler_started = True
    except Exception:
        logger.exception("Impossible d'initialiser le planificateur des sauvegardes Cliniva")
    _runtime_state["restarting"] = False
    try:
        yield
    finally:
        if backup_scheduler_started:
            stop_backup_scheduler()
        if voip_schema_ready:
            voip_server.stop()
        await stop_worklist_service()


app = FastAPI(
    title="Cliniva Management Backend",
    description="Système d'exploitation clinique médicale haute performance",
    version="1.0.0",
    lifespan=app_lifespan,
)

STATIC_DIR = os.path.join(BASE_DIR, "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Attachement de tous les modules métiers de la clinique
app.include_router(clinic_router)
app.include_router(specialites_router)
app.include_router(rooms_router)
app.include_router(staff_router)
app.include_router(medications_router)
app.include_router(patients_router)
app.include_router(treating_doctors_router)
app.include_router(users_router)
app.include_router(audit_router)

app.include_router(cr_templates_router)

app.include_router(reception_router)

app.include_router(dme_router)
app.include_router(exam_requests_router)

app.include_router(caisse_router)
app.include_router(appointments_router)
app.include_router(worklist_router)
app.include_router(telephony_router)
app.include_router(print_server_router)
app.include_router(backup_router)
app.include_router(kacel_job_router)
app.include_router(mobile_scan_router)


async def start_worklist_service() -> None:
    """Démarre le serveur DICOM MWL avec le backend local Cliniva."""
    config = load_app_configuration()
    from app.worklist_service import configure_mwl_server, start_mwl_server

    configure_mwl_server(
        host=str(config.get("mwl_bind_host", "0.0.0.0")),
        port=int(config.get("mwl_port", 11112)),
        ae_title=str(config.get("mwl_ae_title", "CLINIVA_MWL")),
        station_ae_title=str(config.get("mwl_station_ae_title", "")),
    )
    if config.get("server_mode") != "local" or not config.get("mwl_enabled", True):
        logger.info("Serveur DICOM MWL désactivé pour ce mode d'exécution.")
        return
    try:
        start_mwl_server()
    except Exception:
        logger.exception("Impossible de démarrer le serveur DICOM MWL.")


async def stop_worklist_service() -> None:
    """Arrête proprement le listener DICOM MWL à la fermeture de Cliniva."""
    try:
        from app.worklist_service import stop_mwl_server

        stop_mwl_server()
    except Exception:
        logger.exception("Erreur pendant l'arrêt du serveur DICOM MWL.")

templates = Jinja2Templates(directory=TEMPLATES_DIR)


class LoginPayload(BaseModel):
    """Schéma de validation des identifiants de connexion."""

    login: str
    password: str


class DashboardTargetsPayload(BaseModel):
    passages: int = Field(default=0, ge=0, le=1000000)
    consultations: int = Field(default=0, ge=0, le=1000000)
    patients: int = Field(default=0, ge=0, le=1000000)


# ============================================================================
# ROUTES D'AUTHENTIFICATION & NAVIGATION
# ============================================================================
@app.get("/login", response_class=HTMLResponse, summary="Page de connexion")
async def login_page(request: Request) -> Any:
    """Affiche l'écran de connexion si non connecté, sinon redirige vers l'accueil."""
    if get_current_user_optional(request):
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/api/auth/login", summary="Traitement de l'authentification")
async def api_login(payload: LoginPayload, request: Request) -> JSONResponse:
    """Vérifie le compte, génère le token de session et trace la connexion."""
    pwd_hash = hash_password(payload.password)
    login_clean = payload.login.strip().lower()

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT id, prenom, nom, role, status 
            FROM users 
            WHERE login = %s AND password_hash = %s;
            """,
            (login_clean, pwd_hash),
        )
        user = cursor.fetchone()

    ip_client = request.client.host if request.client else "127.0.0.1"

    if not user:
        track_action(
            request,
            "ECHEC_CONNEXION",
            f"Tentative infructueuse avec le compte : {payload.login}",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Identifiant ou mot de passe incorrect.",
        )

    if user["status"] != "Actif":
        track_action(
            request,
            "COMPTE_BLOQUE",
            f"Tentative d'accès sur compte inactif : {payload.login}",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Ce compte utilisateur est désactivé. Veuillez contacter la direction.",
        )

    # Création du jeton de session dans PostgreSQL
    session_token = create_user_session(user["id"])

    # Traçabilité de l'action de connexion réussie
    record_audit(
        user_id=user["id"],
        user_fullname=f"{user['prenom']} {user['nom']}",
        user_role=user["role"],
        action_type="CONNEXION",
        description="Connexion réussie à l'application Cliniva",
        ip_address=ip_client,
    )

    response = JSONResponse(
        content={
            "success": True,
            "message": "Connexion autorisée.",
            "user": {
                "id": user["id"],
                "nom_complet": f"{user['prenom']} {user['nom']}",
                "role": user["role"],
            },
        }
    )
    # Pose du cookie de session HTTP-Only sécurisé
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        max_age=86400,
        samesite="lax",
    )
    return response


@app.get("/api/auth/logout", summary="Déconnexion utilisateur")
async def api_logout(request: Request) -> RedirectResponse:
    """Détruit la session active, trace l'événement et redirige vers /login."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        track_action(
            request,
            "DECONNEXION",
            "Déconnexion volontaire de l'application",
        )
        delete_user_session(token)

    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/", response_class=HTMLResponse, summary="Vue racine du tableau de bord")
async def serve_dashboard(request: Request) -> Any:
    """Affiche le tableau de bord avec injection des données et de l'utilisateur connecté."""
    user = get_current_user_optional(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    period = request.query_params.get("periode", "today")
    data = DashboardService.get_dashboard_metrics(
        user,
        period,
        load_app_configuration(),
        request.query_params.get("date_debut"),
        request.query_params.get("date_fin"),
        request.query_params.get("mois"),
    )

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "data": data,
            "user": user,
            "app_restart_allowed": (
                load_app_configuration().get("server_mode") == "local"
                and user.get("role") == "Administrateur"
            ),
        },
    )


@app.get("/api/dashboard/metrics", summary="Indicateurs du tableau de bord")
async def api_dashboard_metrics(
    request: Request,
    periode: str = "today",
    date_debut: Optional[str] = None,
    date_fin: Optional[str] = None,
    mois: Optional[str] = None,
) -> JSONResponse:
    """Retourne les statistiques seulement aux utilisateurs autorisés à la dashboard."""
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expirée.")
    metrics = DashboardService.get_dashboard_metrics(
        user, periode, load_app_configuration(), date_debut, date_fin, mois
    )
    if not metrics["can_dashboard"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Accès au tableau de bord non autorisé.")
    return JSONResponse(content=metrics)


@app.put("/api/dashboard/objectifs", summary="Configurer les objectifs mensuels")
async def api_dashboard_targets(payload: DashboardTargetsPayload, request: Request) -> JSONResponse:
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée.")
    if user.get("role") != "Administrateur":
        raise HTTPException(status_code=403, detail="Seul un administrateur peut définir les objectifs.")
    targets = {
        "passages": payload.passages,
        "consultations": payload.consultations,
        "patients": payload.patients,
    }
    try:
        _persist_config_updates({"dashboard_targets": targets})
    except OSError as exc:
        logger.exception("Impossible d’enregistrer les objectifs dashboard")
        raise HTTPException(status_code=500, detail="Impossible d’enregistrer les objectifs.") from exc
    return JSONResponse({"ok": True, "targets": targets})


@app.get("/api/app/health", summary="État du serveur local")
async def api_app_health() -> JSONResponse:
    if _runtime_state["restarting"]:
        return JSONResponse({"success": False, "restarting": True}, status_code=503)
    return JSONResponse({"success": True, "restarting": False})


@app.post("/api/app/restart", summary="Redémarrage du serveur Cliniva")
async def api_restart_application(request: Request) -> JSONResponse:
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    if user.get("role") != "Administrateur":
        raise HTTPException(status_code=403, detail="Seul un administrateur peut redémarrer Cliniva.")
    if load_app_configuration().get("server_mode") != "local":
        raise HTTPException(status_code=409, detail="Le redémarrage intégré est disponible en mode local uniquement.")
    if not _runtime_restart_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Le redémarrage de Cliniva est déjà en cours.")

    server = _runtime_state.get("server")
    current_thread = _runtime_state.get("thread")
    if server is None or current_thread is None or not current_thread.is_alive():
        _runtime_restart_lock.release()
        raise HTTPException(status_code=503, detail="Le serveur local ne peut pas être redémarré depuis cette session.")

    _runtime_state["restarting"] = True

    def restart_server() -> None:
        try:
            time.sleep(0.5)
            server.should_exit = True
            current_thread.join(timeout=20)
            if current_thread.is_alive():
                logger.error("Le serveur Cliniva n'a pas terminé son arrêt dans le délai prévu.")
                _runtime_state["restarting"] = False
                return
            next_thread = LocalServerThread(
                load_app_configuration().get("local_host", "0.0.0.0"),
                int(load_app_configuration().get("local_port", 8000)),
            )
            _runtime_state["thread"] = next_thread
            next_thread.start()
        except Exception:
            logger.exception("Échec du redémarrage du serveur Cliniva.")
            _runtime_state["restarting"] = False
        finally:
            _runtime_restart_lock.release()

    threading.Thread(target=restart_server, name="ClinivaRestart", daemon=True).start()
    return JSONResponse({"success": True, "message": "Redémarrage de Cliniva en cours."})


# ============================================================================
# EXÉCUTEUR DYNAMIQUE DE MODULES MÉTIERS (SPA SERVER-DRIVEN)
# ============================================================================
@app.get(
    "/api/modules/{module_name}",
    response_class=HTMLResponse,
    summary="Chargeur dynamique de module Python",
)
async def execute_python_module(module_name: str, request: Request) -> HTMLResponse:
    """Importe dynamiquement un script Python, exécute render_html()

    et renvoie le résultat HTML et ses balises script.
    """
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expirée. Veuillez vous reconnecter.",
        )

    # 1. Validation de sécurité anti Path-Traversal
    if not re.match(r"^[a-zA-Z0-9_]+$", module_name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nom de module invalide.",
        )

    # 2. Recherche du script dans /modules ou à la racine
    module_target = None
    possible_paths = [f"modules.{module_name}", module_name]

    for path in possible_paths:
        try:
            mod = importlib.import_module(path)
            # Rechargement à chaud (Hot-Reloading en direct)
            module_target = mod if module_name == "gestion_backup" else importlib.reload(mod)
            break
        except ModuleNotFoundError:
            continue

    if not module_target:
        logger.warning("Script Python introuvable : %s.py", module_name)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Le module '{module_name}.py' est introuvable.",
        )

    # 3. Vérification de la présence de render_html()
    if not hasattr(module_target, "render_html"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Le module '{module_name}.py' ne possède pas de fonction 'render_html()'.",
        )

    # 4. Exécution protégée du script Python
    try:
        if module_name == "gestion_caisse":
            if not module_target.user_can_access_caisse(user):
                raise HTTPException(status_code=403, detail="Aucun droit de caisse n'est accordé à ce compte.")
            html_output = module_target.render_html(user)
        elif module_name == "gestion_kacel_job":
            html_output = module_target.render_html(user)
        elif module_name in {"gestion_dme", "gestion_worklist", "gestion_dicom_print", "gestion_pacs_server", "gestion_backup", "gestion_telephonie_ip"}:
            html_output = module_target.render_html(user)
        else:
            html_output = module_target.render_html()
        return HTMLResponse(content=html_output, status_code=status.HTTP_200_OK)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Erreur d'exécution dans %s.py : %s", module_name, exc, exc_info=True
        )
        safe_msg = html.escape(str(exc))
        return HTMLResponse(
            content=f"""
            <div class="p-6 bg-rose-500/10 border border-rose-500/30 rounded-2xl text-rose-500 text-xs">
                <h4 class="font-bold text-sm mb-1">Erreur d'exécution dans {module_name}.py :</h4>
                <pre class="font-mono mt-2 p-3 bg-black/40 rounded-xl overflow-x-auto">{safe_msg}</pre>
            </div>
            """,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


# ============================================================================
# THREAD SERVEUR & CYCLE DE VIE DESKTOP (WINDOWS WEBVIEW2)
# ============================================================================
class LocalServerThread(threading.Thread):
    """Thread dédié à l'exécution asynchrone du serveur Uvicorn."""

    def __init__(self, host: str, port: int) -> None:
        super().__init__(daemon=True)
        self.host = host
        self.port = port

    def run(self) -> None:
        """Démarre l'instance Uvicorn en arrière-plan."""
        config = uvicorn.Config(
            app=app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        _runtime_state["server"] = server
        try:
            server.run()
        finally:
            if _runtime_state.get("server") is server:
                _runtime_state["server"] = None


def wait_for_server(port: int, timeout: float = 6.0) -> bool:
    """Sonde la disponibilité du serveur local sur 127.0.0.1.

    Args:
        port (int): Port TCP à écouter.
        timeout (float): Délai d'attente maximal en secondes.

    Returns:
        bool: True si le serveur est prêt, False sinon.
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def main() -> None:
    """Point d'entrée principal : initialise la base et lance la fenêtre logicielle."""
    # 1. Initialisation et synchronisation des schémas PostgreSQL
    try:
        init_database()
    except Exception as db_err:
        logger.error(
            "Attention : Erreur lors de l'initialisation PostgreSQL : %s", db_err
        )

    # 2. Chargement de la configuration
    config = load_app_configuration()
    client_bridge = ClientDicomServicesBridge(config)
    port = config.get("local_port", 8000)

    # DICOM Print s’exécute sur la machine qui lance Cliniva, en mode local
    # comme distant (serveur ou poste client).
    if config.get("dicom_print_enabled"):
        startup_result = client_bridge.dicom_print_start(config.get("dicom_print_config"))
        if not startup_result.get("ok"):
            logger.warning("DICOM Print local non démarré : %s", startup_result.get("error"))
    # Le récepteur PACS peut être activé aussi bien sur le serveur local que sur
    # chaque poste distant. La configuration et les fichiers restent propres
    # au poste qui exécute cette instance de Cliniva.
    if config.get("pacs_auto_start", config.get("pacs_enabled", False)):
        startup_result = client_bridge.pacs_start(config.get("pacs_config"))
        if not startup_result.get("ok"):
            logger.warning("PACS non démarré sur ce poste : %s", startup_result.get("error"))

    # 3. Démarrage du serveur si mode local
    if config.get("server_mode") == "local":
        listen_host = config.get("local_host", "0.0.0.0")
        logger.info("Démarrage du serveur local sur %s:%s...", listen_host, port)

        server_thread = LocalServerThread(listen_host, port)
        _runtime_state["thread"] = server_thread
        server_thread.start()

        if not wait_for_server(port):
            logger.error("Le serveur ne répond pas sur 127.0.0.1:%s", port)
            sys.exit(1)

        # L'affichage Windows local pointe sur 127.0.0.1 (sécurité socket)
        target_url = f"http://127.0.0.1:{port}"
    else:
        # Mode Client Distant
        target_url = config.get("remote_server_url", "http://127.0.0.1:8000")
        if "0.0.0.0" in target_url:
            target_url = target_url.replace("0.0.0.0", "127.0.0.1")

    logger.info("Ouverture de la fenêtre logicielle sur : %s", target_url)

    # 4. Création de la fenêtre logicielle native Windows (Chromeless / Edge WebView2)
    webview.create_window(
        title=config.get("window_title", "Cliniva"),
        url=target_url,
        width=config.get("window_width", 1400),
        height=config.get("window_height", 900),
        resizable=True,
        zoomable=True,
        min_size=(1024, 700),
        background_color="#f0f3f1",
        js_api=client_bridge,
    )

    # 5. Démarrage de la boucle d'événements native
    try:
        webview.start(debug=False)
    finally:
        client_bridge.dicom_services_shutdown()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
