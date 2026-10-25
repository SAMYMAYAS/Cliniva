"""Lanceur Cliniva pour un poste client Windows, sans code applicatif serveur.

Le client affiche le serveur distant dans une fenêtre WebView locale. Le seul
code Cliniva installé sur le poste est ce lanceur ; les formulaires et données
restent servis par le serveur. Le pont local fournit le scanner et Office Word.
"""

from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
import os
import re
import shutil
import sys
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from tkinter import LEFT, X, Label, StringVar, Tk, messagebox
from tkinter.ttk import Button, Entry, Frame


APP_TITLE = "Cliniva — Poste client"
DEFAULT_PORT = 8000
CONFIG_PATH = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Cliniva" / "client_config.json"
DOCX_MIME = "data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64"
CLIENT_UPDATED_ON_START = False


def normalize_server_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        raise ValueError("Saisis l'adresse IP ou le nom du PC serveur.")
    if "://" not in value:
        value = "http://" + value
    if not value.lower().startswith(("http://", "https://")):
        raise ValueError("L'adresse doit commencer par http:// ou https://.")
    authority = value.split("://", 1)[1].split("/", 1)[0]
    if not authority:
        raise ValueError("L'adresse du serveur est invalide.")
    if ":" not in authority and not authority.startswith("["):
        value = f"{value}:{DEFAULT_PORT}"
    return value


def load_server_url() -> str:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return normalize_server_url(str(data.get("server_url", "")))
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        return ""


def save_server_url(server_url: str, client_version: str | None = None) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {"server_url": server_url}
    if client_version:
        data["client_version"] = client_version
    CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def update_client_launcher(server_url: str, report_status=None) -> str | None:
    """Actualise uniquement ce fichier client.py; aucune source serveur n'est acceptée."""
    if report_status:
        report_status("Recherche d'une mise à jour sur le serveur…")
    manifest_url = server_url.rstrip("/") + "/static/client_updates/latest.json"
    request = urllib.request.Request(manifest_url, headers={"User-Agent": "ClinivaClient/2.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        manifest = json.loads(response.read(64 * 1024).decode("utf-8"))
    version = str(manifest.get("version") or "").strip()
    filename = str(manifest.get("filename") or "").strip()
    expected_hash = str(manifest.get("sha256") or "").strip().lower()
    package_url = str(manifest.get("package_url") or "")
    if (not version or not filename or "/" in filename or "\\" in filename
            or len(expected_hash) != 64 or not package_url.startswith("/static/client_updates/")
            or ".." in package_url or manifest.get("contents") != ["client.py"]):
        raise ValueError("Le serveur n'a pas publié une mise à jour client.py valide.")
    # Compare le contenu réellement exécuté, pas seulement la version mémorisée
    # dans la configuration, afin qu'un ancien client ne saute pas la mise à jour.
    global CLIENT_UPDATED_ON_START
    current_version = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
    if current_version == version:
        if report_status:
            report_status(f"Client déjà à jour ({version}).")
        return version

    if report_status:
        report_status(f"Mise à jour disponible ({version}) : téléchargement…")
    download_url = server_url.rstrip("/") + package_url
    request = urllib.request.Request(download_url, headers={"User-Agent": "ClinivaClient/2.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read(32 * 1024 * 1024 + 1)
    if len(payload) > 32 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != expected_hash:
        raise ValueError("La vérification de la mise à jour client a échoué.")

    if report_status:
        report_status("Archive vérifiée : installation de la mise à jour…")
    app_dir = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="cliniva-client-update-", dir=app_dir) as staging_dir:
        archive_path = Path(staging_dir) / filename
        archive_path.write_bytes(payload)
        with zipfile.ZipFile(archive_path) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) != 1 or members[0].filename != "client.py":
                raise ValueError("L'archive client contient autre chose que client.py.")
            source = archive.read(members[0])
        replacement = app_dir / ".client.py.update"
        replacement.write_bytes(source)
        shutil.copystat(Path(__file__), replacement)
        os.replace(replacement, Path(__file__))
        CLIENT_UPDATED_ON_START = True
    if report_status:
        report_status("Mise à jour installée avec succès. Redémarrage du client…")
    return version


def check_server(server_url: str) -> tuple[bool, str]:
    request = urllib.request.Request(
        server_url.rstrip("/") + "/api/app/health",
        headers={"User-Agent": "ClinivaClient/2.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.loads(response.read(4096).decode("utf-8"))
            if response.status == 200 and payload.get("success") is True:
                return True, "Serveur Cliniva joignable."
            return False, "Le serveur répond, mais Cliniva signale qu'il n'est pas prêt."
    except urllib.error.HTTPError as exc:
        return False, f"Le serveur répond avec HTTP {exc.code}. Vérifie son état."
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return False, f"Serveur inaccessible : {getattr(exc, 'reason', exc)}"


def _safe_filename(value: str, fallback: str, extension: str) -> str:
    name = os.path.basename(str(value or fallback))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or fallback
    if not name.lower().endswith(extension):
        name += extension
    return name


class ClientHardwareBridge:
    """Fonctions strictement locales appelées par les pages du serveur Cliniva."""

    def __init__(self) -> None:
        self._zoom_percent = 100
        self._word_sync_jobs: set[tuple[str, str, int]] = set()
        self._word_sync_states: dict[tuple[str, str, int], dict] = {}
        self._word_sync_lock = threading.Lock()

    @staticmethod
    def _word_sync_key(path: Path, document_kind: str, record_id: int) -> tuple[str, str, int]:
        return (str(path.resolve()).casefold(), str(document_kind), int(record_id))

    def client_word_sync_status(self, document_kind: str, record_id: int) -> dict:
        """Expose l'état de la dernière synchronisation Word au navigateur Cliniva."""
        with self._word_sync_lock:
            matches = [state for key, state in self._word_sync_states.items()
                       if key[1] == str(document_kind) and key[2] == int(record_id)]
            return dict(matches[-1]) if matches else {"status": "waiting", "message": "En attente d'une sauvegarde Word."}

    @staticmethod
    def _valid_docx_bytes(content: bytes) -> bool:
        try:
            with zipfile.ZipFile(BytesIO(content)) as document:
                names = set(document.namelist())
                return "[Content_Types].xml" in names and "word/document.xml" in names
        except (OSError, zipfile.BadZipFile):
            return False

    def _upload_word_document(self, server_url: str, document_kind: str,
                              record_id: int, path: Path, sync_token: str | None = None) -> None:
        """Envoie au serveur une version Word stable enregistrée depuis le client."""
        content = path.read_bytes()
        if not content or len(content) > 100 * 1024 * 1024 or not self._valid_docx_bytes(content):
            raise ValueError("Le fichier local n'est pas encore un document DOCX enregistré et valide.")

        routes = {
            "template": f"/api/cr-templates/upload-word/{int(record_id)}",
            "dme_cr": f"/api/dme/cr/upload-word/{int(record_id)}",
        }
        route = routes.get(str(document_kind or "template"))
        if not route:
            raise ValueError("Type de document à synchroniser non autorisé.")
        if document_kind == "dme_cr" and not sync_token:
            raise ValueError("Autorisation de synchronisation absente. Rouvrez le compte rendu depuis le DME.")
        boundary = "----ClinivaWordSync" + uuid.uuid4().hex
        head = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="modele_compte_rendu.docx"\r\n'
            "Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n"
        ).encode("ascii")
        body = head + content + f"\r\n--{boundary}--\r\n".encode("ascii")
        request = urllib.request.Request(
            f"{server_url.rstrip('/')}{route}",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
                "User-Agent": "ClinivaClient/2.0",
                **({"X-Cliniva-CR-Sync-Token": sync_token} if document_kind == "dme_cr" else {}),
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            result = json.loads(response.read(16 * 1024).decode("utf-8"))
        if response.status != 200 or result.get("success") is not True:
            raise RuntimeError(result.get("detail") or result.get("message") or "Le serveur a refusé la synchronisation du modèle.")

    def _watch_word_document(self, path: Path, server_url: str,
                             document_kind: str, record_id: int, sync_token: str | None = None) -> None:
        """Synchronise automatiquement chaque sauvegarde Word après stabilisation du fichier."""
        try:
            initial = path.stat()
            previous_signature = (initial.st_mtime_ns, initial.st_size)
        except OSError:
            return

        pending_since = None
        last_attempt = 0.0
        while path.exists():
            time.sleep(1.0)
            try:
                current = path.stat()
                signature = (current.st_mtime_ns, current.st_size)
            except OSError:
                continue

            now = time.monotonic()
            if signature != previous_signature:
                previous_signature = signature
                pending_since = now
                last_attempt = 0.0
                with self._word_sync_lock:
                    self._word_sync_states[self._word_sync_key(path, document_kind, record_id)] = {
                        "status": "changed", "message": "Modification détectée, attente de la fin de l'enregistrement Word."
                    }
                continue
            if pending_since is None or now - pending_since < 1.5 or now - last_attempt < 5.0:
                continue

            last_attempt = now
            key = self._word_sync_key(path, document_kind, record_id)
            with self._word_sync_lock:
                self._word_sync_states[key] = {"status": "uploading", "message": "Envoi du compte rendu vers le serveur…"}
            try:
                self._upload_word_document(server_url, document_kind, record_id, path, sync_token)
                pending_since = None
                with self._word_sync_lock:
                    self._word_sync_states[key] = {
                        "status": "synced", "message": "Compte rendu enregistré sur le serveur.", "synced_at": int(time.time())
                    }
            except Exception as exc:
                # Word peut encore verrouiller le fichier ou le réseau être
                # momentanément indisponible : un nouvel essai sera fait.
                with self._word_sync_lock:
                    self._word_sync_states[key] = {"status": "retrying", "message": str(exc) or "Échec de synchronisation."}
                continue

    def _start_word_document_sync(self, path: Path, document_kind: str, record_id: int,
                                  sync_token: str | None = None) -> bool:
        server_url = load_server_url()
        if not server_url or not record_id or document_kind not in {"template", "dme_cr"}:
            return False
        key = self._word_sync_key(path, document_kind, record_id)
        with self._word_sync_lock:
            if key in self._word_sync_jobs:
                return True
            self._word_sync_jobs.add(key)
            self._word_sync_states[key] = {"status": "watching", "message": "En attente d'une sauvegarde dans Word."}
        threading.Thread(
            target=self._watch_word_document,
            args=(path, server_url, str(document_kind), int(record_id), sync_token),
            name=f"ClinivaWordSync-{document_kind}-{int(record_id)}",
            daemon=True,
        ).start()
        return True

    def client_scanner_detect(self) -> dict:
        if sys.platform != "win32":
            return {"success": False, "scanners": [], "message": "Le scan matériel est disponible sur Windows."}
        pythoncom = None
        try:
            import pythoncom as com
            import win32com.client
            pythoncom = com
            pythoncom.CoInitialize()
            manager = win32com.client.Dispatch("WIA.DeviceManager")
            devices = []
            for info in manager.DeviceInfos:
                try:
                    devices.append({"id": str(info.DeviceID), "name": str(info.Properties("Name").Value or "Scanner WIA")})
                except Exception:
                    continue
            return {"success": True, "scanners": devices,
                    "message": "Scanner(s) détecté(s) sur ce poste." if devices else "Aucun scanner WIA détecté sur ce poste."}
        except Exception as exc:
            return {"success": False, "scanners": [], "message": f"WIA/pywin32 indisponible : {exc}"}
        finally:
            if pythoncom:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    def client_scanner_acquire(self, device_id: str = "") -> dict:
        if sys.platform != "win32":
            return {"success": False, "message": "Le scan matériel est disponible sur Windows."}
        pythoncom = None
        temporary_path = None
        try:
            import pythoncom as com
            import win32com.client
            pythoncom = com
            pythoncom.CoInitialize()
            manager = win32com.client.Dispatch("WIA.DeviceManager")
            info = next((item for item in manager.DeviceInfos if str(item.DeviceID) == str(device_id)), None)
            if info is None:
                return {"success": False, "message": "Le scanner sélectionné n'est plus disponible."}
            device = info.Connect()
            if device.Items.Count < 1:
                return {"success": False, "message": "Le scanner ne contient aucun élément de numérisation."}
            item = device.Items.Item(1)
            # 3CAB = BMP (non compressé) ; le vrai identifiant WIA JPEG est 3CAE.
            jpeg_format = "{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}"
            try:
                # Préférence facultative : couleur avec une taille de fichier réduite.
                item.Properties("Current Intent").Value = 0x00010001
            except Exception:
                pass
            image = None
            transfer_error = None
            try:
                image = item.Transfer(jpeg_format)
            except Exception as exc:
                transfer_error = exc

            # Certains pilotes WIA renvoient None pour Item.Transfer, même si le
            # scanner est présent. Le CommonDialog gère leur transfert avec la
            # boîte de progression native Windows et retourne l'ImageFile.
            if image is None:
                try:
                    common_dialog = win32com.client.Dispatch("WIA.CommonDialog")
                    image = common_dialog.ShowTransfer(item, jpeg_format, False)
                except Exception as exc:
                    transfer_error = exc
            if image is None:
                detail = f" ({transfer_error})" if transfer_error else ""
                return {"success": False, "message":
                        "Le pilote WIA n'a fourni aucune image. Vérifie le pilote du scanner et réessaie." + detail}

            # Lire le vecteur d'octets WIA évite les pilotes qui échouent à
            # créer le fichier demandé par ImageFile.SaveFile.
            try:
                content = bytes(image.FileData.BinaryData)
            except Exception:
                # Certains pilotes exposent mal FileData.BinaryData ; le chemin
                # fichier reste disponible en secours avec un nom neuf.
                temporary_path = str(Path(tempfile.gettempdir()) / f"cliniva_scan_{uuid.uuid4().hex}.jpg")
                save_image = getattr(image, "SaveFile", None)
                if not callable(save_image):
                    return {"success": False, "message":
                            "Le pilote a terminé le scan sans fournir les octets de l'image. "
                            "Vérifie le pilote WIA du scanner."}
                save_image(temporary_path)
                if not os.path.isfile(temporary_path):
                    return {"success": False, "message": "Le pilote n'a pas fourni de données image exploitables."}
                content = Path(temporary_path).read_bytes()

            max_bytes = 12 * 1024 * 1024
            image_is_jpeg = str(getattr(image, "FormatID", "")).strip().upper() == jpeg_format.upper()
            if content and (not image_is_jpeg or len(content) > max_bytes):
                # Certains pilotes ignorent le format demandé ou livrent un BMP.
                # Conversion + réduction WIA gardent le document lisible et
                # respectent la limite acceptée par le formulaire serveur.
                last_candidate = b""
                for max_width, max_height, quality in ((2600, 3400, 82), (2200, 3000, 72), (1800, 2500, 60)):
                    try:
                        processor = win32com.client.Dispatch("WIA.ImageProcess")
                        processor.Filters.Add(processor.FilterInfos("Scale").FilterID)
                        scale_filter = processor.Filters.Item(1)
                        scale_filter.Properties("MaximumWidth").Value = max_width
                        scale_filter.Properties("MaximumHeight").Value = max_height
                        processor.Filters.Add(processor.FilterInfos("Convert").FilterID)
                        convert_filter = processor.Filters.Item(2)
                        convert_filter.Properties("FormatID").Value = jpeg_format
                        convert_filter.Properties("Quality").Value = quality
                        converted = processor.Apply(image)
                        last_candidate = bytes(converted.FileData.BinaryData)
                        if last_candidate and len(last_candidate) <= max_bytes:
                            content = last_candidate
                            break
                    except Exception:
                        continue
                else:
                    if last_candidate:
                        content = last_candidate

            if not content:
                return {"success": False, "message": "Le scanner a retourné une image vide. Vérifie le pilote WIA."}
            if len(content) > max_bytes:
                size_mb = len(content) / (1024 * 1024)
                return {"success": False, "message":
                        f"Le scan fait encore {size_mb:.1f} Mo après compression. "
                        "Choisis une résolution de 200 dpi ou moins dans les paramètres du scanner."}
            return {"success": True, "image_data": "data:image/jpeg;base64," + base64.b64encode(content).decode("ascii"),
                    "name": Path(temporary_path).name if temporary_path else f"cliniva_scan_{uuid.uuid4().hex}.jpg"}
        except Exception as exc:
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

    def client_open_word_document(self, docx_data_url: str, suggested_name: str = "Compte_rendu.docx",
                                  template_id: int | None = None, sync_kind: str = "template",
                                  sync_token: str | None = None) -> dict:
        try:
            if sys.platform != "win32":
                return {"ok": False, "error": "L'ouverture dans Microsoft Word nécessite Windows."}
            header, separator, encoded = str(docx_data_url or "").partition(",")
            if not separator or header.strip().lower() != DOCX_MIME:
                return {"ok": False, "error": "Le document Word reçu est invalide."}
            content = base64.b64decode(encoded, validate=True)
            if not content or len(content) > 100 * 1024 * 1024:
                return {"ok": False, "error": "Le document est vide ou dépasse 100 Mo."}
            filename = _safe_filename(suggested_name, "Compte_rendu.docx", ".docx")
            folder = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Cliniva" / "Documents"
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / filename
            if target.exists():
                target = folder / f"{target.stem}_{int(time.time())}{target.suffix}"
            target.write_bytes(content)
            os.startfile(str(target))
            sync_started = self._start_word_document_sync(target, str(sync_kind), int(template_id), sync_token) if template_id else False
            return {"ok": True, "path": str(target), "sync_started": sync_started}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def kacel_job_save_and_open_docx(self, docx_data_url: str, suggested_name: str = "Convention.docx") -> dict:
        try:
            import webview
            if sys.platform != "win32" or not webview.windows:
                return {"ok": False, "error": "La fenêtre cliente Windows n'est pas prête."}
            header, separator, encoded = str(docx_data_url or "").partition(",")
            if not separator or header.strip().lower() != DOCX_MIME:
                return {"ok": False, "error": "Le document Word reçu est invalide."}
            content = base64.b64decode(encoded, validate=True)
            if not content or len(content) > 100 * 1024 * 1024:
                return {"ok": False, "error": "Le document est vide ou dépasse 100 Mo."}
            filename = _safe_filename(suggested_name, "Convention.docx", ".docx")
            selected = webview.windows[0].create_file_dialog(
                webview.FileDialog.SAVE, save_filename=filename, file_types=("Document Word (*.docx)",)
            )
            if not selected:
                return {"ok": True, "cancelled": True}
            target = Path(selected[0] if isinstance(selected, (list, tuple)) else selected)
            if target.suffix.lower() != ".docx":
                target = target.with_suffix(".docx")
            target.write_bytes(content)
            os.startfile(str(target))
            return {"ok": True, "path": str(target)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def print_server_open_pdf(self, pdf_data_url: str, suggested_name: str = "Livret.pdf") -> dict:
        try:
            if sys.platform != "win32":
                return {"ok": False, "error": "L'ouverture avec le lecteur PDF nécessite Windows."}
            header, separator, encoded = str(pdf_data_url or "").partition(",")
            if not separator or header.strip().lower() != "data:application/pdf;base64":
                return {"ok": False, "error": "Le fichier PDF reçu est invalide."}
            content = base64.b64decode(encoded, validate=True)
            if not content or len(content) > 200 * 1024 * 1024:
                return {"ok": False, "error": "Le PDF est vide ou dépasse 200 Mo."}
            stem = Path(str(suggested_name or "Livret.pdf")).stem
            stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .") or "Livret"
            folder = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Cliniva" / "Documents"
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{stem}_{int(time.time())}.pdf"
            target.write_bytes(content)
            os.startfile(str(target))
            return {"ok": True, "path": str(target)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def interface_zoom(self, action="status", value=None) -> dict:
        if action == "in":
            self._zoom_percent = min(300, self._zoom_percent + 10)
        elif action == "out":
            self._zoom_percent = max(50, self._zoom_percent - 10)
        elif action == "reset":
            self._zoom_percent = 100
        elif action == "set":
            try:
                self._zoom_percent = max(50, min(300, int(value)))
            except (TypeError, ValueError):
                return {"ok": False, "error": "Pourcentage de zoom invalide."}
        elif action != "status":
            return {"ok": False, "error": "Commande de zoom invalide."}
        try:
            import webview
            if webview.windows:
                webview.windows[0].evaluate_js(f"document.documentElement.style.zoom='{self._zoom_percent}%'"
                                               f";document.body.style.zoom='{self._zoom_percent}%' ")
        except Exception:
            pass
        return {"ok": True, "percent": self._zoom_percent}


def open_client_window(server_url: str) -> None:
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError(
            "Le composant de fenêtre cliente manque. Installe-le une fois sur ce PC avec :\n"
            "py -m pip install pywebview pywin32\n\n"
            "Le code Cliniva du serveur n'est pas nécessaire sur le poste client."
        ) from exc
    bridge = ClientHardwareBridge()
    webview.create_window(
        "Cliniva", server_url, width=1440, height=940, min_size=(900, 650),
        resizable=True, zoomable=True, js_api=bridge,
    )
    webview.start(debug=False)


class ClinivaClient:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("600x285")
        self.root.minsize(520, 260)
        self.root.configure(bg="#f3f6f5")
        Label(self.root, text="Connexion au serveur Cliniva", font=("Segoe UI", 17, "bold"),
              fg="#153c2c", bg="#f3f6f5").pack(anchor="w", padx=24, pady=(22, 4))
        Label(self.root, text="Les pages restent sur le serveur. Ce poste utilise seulement le lanceur et ses périphériques locaux.",
              font=("Segoe UI", 10), fg="#58665f", bg="#f3f6f5", wraplength=530, justify=LEFT
              ).pack(anchor="w", padx=24, pady=(0, 14))
        self.server_var = StringVar(value=load_server_url())
        self.entry = Entry(self.root, textvariable=self.server_var, font=("Segoe UI", 11))
        self.entry.pack(fill=X, padx=24, ipady=7)
        self.entry.bind("<Return>", lambda _event: self.connect())
        self.status_var = StringVar(value="Le port 8000 est ajouté automatiquement si nécessaire.")
        self.status_label = Label(self.root, textvariable=self.status_var, font=("Segoe UI", 9), fg="#58665f",
                                  bg="#f3f6f5", wraplength=510, justify=LEFT)
        self.status_label.pack(anchor="w", padx=24, pady=12)
        buttons = Frame(self.root)
        buttons.pack(fill=X, padx=24, pady=(2, 18))
        self.connect_button = Button(buttons, text="Se connecter à Cliniva", command=self.connect)
        self.connect_button.pack(side=LEFT)
        Button(buttons, text="Quitter", command=self.root.destroy).pack(side=LEFT, padx=(10, 0))
        self.root.mainloop()

    def connect(self) -> None:
        try:
            server_url = normalize_server_url(self.server_var.get())
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc), parent=self.root)
            return
        self.status_var.set("Vérification du serveur…")
        self.status_label.configure(fg="#58665f")
        self.connect_button.state(["disabled"])
        self.root.update_idletasks()
        threading.Thread(target=self._check_and_open, args=(server_url,), daemon=True).start()

    def _check_and_open(self, server_url: str) -> None:
        reachable, message = check_server(server_url)
        if not reachable:
            self.root.after(0, self._show_error, message)
            return
        try:
            save_server_url(server_url)
        except OSError as exc:
            self.root.after(0, self._show_error, f"Impossible d'enregistrer l'adresse du serveur : {exc}")
            return
        self.root.after(0, self._begin_update, server_url)

    def _set_status(self, message: str, color: str = "#58665f") -> None:
        try:
            self.status_var.set(message)
            self.status_label.configure(fg=color)
            self.root.update_idletasks()
        except Exception:
            pass

    def _begin_update(self, server_url: str) -> None:
        self._set_status("Connexion établie. Recherche d'une mise à jour…", "#2563eb")
        threading.Thread(target=self._update_and_open, args=(server_url,), daemon=True).start()

    def _update_and_open(self, server_url: str) -> None:
        update_error = None
        version = None
        try:
            version = update_client_launcher(
                server_url,
                report_status=lambda message: self.root.after(0, self._set_status, message, "#2563eb"),
            )
            save_server_url(server_url, version)
        except Exception as exc:
            update_error = str(exc)
        self.root.after(0, self._finish_update, server_url, version, update_error)

    def _finish_update(self, server_url: str, version: str | None, update_error: str | None) -> None:
        if update_error:
            self._set_status(f"Échec de la mise à jour : {update_error}", "#b91c1c")
            messagebox.showwarning(
                APP_TITLE,
                "La mise à jour du client a échoué. La version installée sera lancée.\n\n"
                f"Détail : {update_error}\n\n"
                "Vérifie que le serveur a été redémarré et que le dossier du client est accessible en écriture.",
                parent=self.root,
            )
            self.root.after(100, self._open_webview, server_url)
            return
        if CLIENT_UPDATED_ON_START:
            self._set_status(f"Mise à jour réussie ({version}). Redémarrage du client…", "#15803d")
            self.root.after(1000, self._restart_updated_client, server_url)
            return
        self._set_status(f"Aucune mise à jour nécessaire : client à jour ({version}).", "#15803d")
        self.root.after(900, self._open_webview, server_url)

    def _restart_updated_client(self, server_url: str) -> None:
        try:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), server_url],
                cwd=str(Path(__file__).resolve().parent),
            )
            self.root.destroy()
        except Exception as exc:
            self._set_status(f"Mise à jour faite, mais redémarrage impossible : {exc}", "#b91c1c")
            self.connect_button.state(["!disabled"])

    def _open_webview(self, server_url: str) -> None:
        self.root.destroy()
        try:
            open_client_window(server_url)
        except Exception as exc:
            fallback = Tk()
            fallback.withdraw()
            messagebox.showerror(APP_TITLE, str(exc), parent=fallback)
            fallback.destroy()

    def _show_error(self, message: str) -> None:
        self._set_status(message, "#b91c1c")
        self.connect_button.state(["!disabled"])
        messagebox.showerror(APP_TITLE, message + "\n\nVérifie que le serveur est lancé et que le port 8000 est autorisé sur le réseau.", parent=self.root)

def main() -> int:
    if len(sys.argv) > 1:
        try:
            server_url = normalize_server_url(sys.argv[1])
        except ValueError as exc:
            print(f"Erreur : {exc}", file=sys.stderr)
            return 2
        reachable, message = check_server(server_url)
        print(message)
        if not reachable:
            return 1
        try:
            save_server_url(server_url)
            try:
                version = update_client_launcher(server_url)
                save_server_url(server_url, version)
                if CLIENT_UPDATED_ON_START:
                    subprocess.Popen(
                        [sys.executable, str(Path(__file__).resolve()), server_url],
                        cwd=str(Path(__file__).resolve().parent),
                    )
                    return 0
            except Exception:
                pass
            open_client_window(server_url)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    ClinivaClient()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
