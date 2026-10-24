"""Pilotage du PBX SIP local Yate depuis Cliniva Desktop.

Yate est exécuté sur le poste Cliniva (aucune VM). Cliniva conserve les
extensions dans PostgreSQL, génère sa configuration isolée puis contrôle le
processus Windows. Les mots de passe d'extension sont chiffrés avec DPAPI.
"""

from __future__ import annotations

import base64
import csv
from contextlib import ExitStack
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave
from array import array
from pathlib import Path
from typing import Any
from urllib.parse import quote

from database import DOCS_DIR, get_db_cursor

logger = logging.getLogger("ClinivaVoIP")


class LocalVoipServer:
    """Gestion locale d'un moteur Yate installé sur le poste Cliniva."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._log_handle = None
        self._last_error = ""
        self._started_at: float | None = None
        self._rmanager_password = ""
        self._cdr_path: Path | None = None
        if sys.platform == "win32":
            voip_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Cliniva" / "VoIP"
        else:
            voip_data = Path(DOCS_DIR) / "voip"
        self._config_dir = voip_data / "conf.d"
        self._user_dir = voip_data / "user"
        self._recordings_dir = voip_data / "calls" / "recordings"
        self._log_path = voip_data / "yate.log"

    @staticmethod
    def ensure_schema() -> None:
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS voip_extensions (
                    id BIGSERIAL PRIMARY KEY,
                    username VARCHAR(32) NOT NULL UNIQUE,
                    display_name VARCHAR(120) NOT NULL DEFAULT '',
                    password_ciphertext TEXT NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    @staticmethod
    def _config_path() -> Path:
        return Path(os.path.abspath("config.json"))

    @classmethod
    def get_settings(cls) -> dict[str, Any]:
        defaults = {
            "sip_server_executable": "",
            "sip_bind_host": "0.0.0.0",
            "sip_bind_port": 5060,
            "sip_domain": "cliniva.local",
            "sip_auto_start": False,
            "sip_enable_transfer": True,
            "sip_record_calls": True,
        }
        try:
            config = json.loads(cls._config_path().read_text(encoding="utf-8"))
            if isinstance(config, dict):
                defaults.update({key: config[key] for key in defaults if key in config})
        except (OSError, ValueError, TypeError):
            pass
        try:
            defaults["sip_bind_port"] = int(defaults["sip_bind_port"])
        except (TypeError, ValueError):
            defaults["sip_bind_port"] = 5060
        defaults["sip_auto_start"] = bool(defaults["sip_auto_start"])
        defaults["sip_enable_transfer"] = bool(defaults["sip_enable_transfer"])
        defaults["sip_record_calls"] = bool(defaults["sip_record_calls"])
        return defaults

    @classmethod
    def save_settings(cls, changes: dict[str, Any]) -> dict[str, Any]:
        current = cls.get_settings()
        executable = str(changes.get("sip_server_executable", current["sip_server_executable"])).strip()
        host = str(changes.get("sip_bind_host", current["sip_bind_host"])).strip()
        domain = str(changes.get("sip_domain", current["sip_domain"])).strip().lower()
        try:
            port = int(changes.get("sip_bind_port", current["sip_bind_port"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("Le port SIP doit être un nombre entier.") from exc
        if not 1 <= port <= 65535:
            raise ValueError("Le port SIP doit être compris entre 1 et 65535.")
        if not host or any(char in host for char in "\r\n;=[]"):
            raise ValueError("Adresse d’écoute SIP invalide.")
        if not re.fullmatch(r"[a-z0-9.-]{1,253}", domain):
            raise ValueError("Le domaine SIP doit contenir uniquement des lettres, chiffres, points et tirets.")
        current.update({
            "sip_server_executable": executable,
            "sip_bind_host": host,
            "sip_bind_port": port,
            "sip_domain": domain,
            "sip_auto_start": bool(changes.get("sip_auto_start", current["sip_auto_start"])),
            "sip_enable_transfer": bool(changes.get("sip_enable_transfer", current["sip_enable_transfer"])),
            "sip_record_calls": bool(changes.get("sip_record_calls", current["sip_record_calls"])),
        })
        path = cls._config_path()
        try:
            config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            config = {}
        config.update(current)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".voip-tmp")
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=4), encoding="utf-8")
        os.replace(temporary, path)
        return current

    @staticmethod
    def _protect_password(password: str) -> str:
        if sys.platform != "win32":
            raise RuntimeError("Le stockage protégé des secrets SIP nécessite Windows DPAPI.")
        try:
            import win32crypt
        except ImportError as exc:
            raise RuntimeError("Le composant pywin32 est nécessaire pour protéger les secrets SIP.") from exc
        ciphertext = win32crypt.CryptProtectData(
            password.encode("utf-8"), "Cliniva SIP extension", None, None, None, 0
        )
        return base64.b64encode(ciphertext).decode("ascii")

    @staticmethod
    def _unprotect_password(ciphertext: str) -> str:
        if sys.platform != "win32":
            raise RuntimeError("Le déchiffrement DPAPI des extensions SIP nécessite Windows.")
        try:
            import win32crypt
        except ImportError as exc:
            raise RuntimeError("Le composant pywin32 est nécessaire pour lire les secrets SIP.") from exc
        encrypted = base64.b64decode(ciphertext.encode("ascii"), validate=True)
        return win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1].decode("utf-8")

    @staticmethod
    def list_extensions() -> list[dict[str, Any]]:
        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT id, username, display_name, enabled, created_at, updated_at "
                "FROM voip_extensions ORDER BY username"
            )
            rows = cursor.fetchall()
        return [
            {
                **dict(row),
                "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
                "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
            }
            for row in rows
        ]

    @staticmethod
    def get_extension_password(extension_id: int) -> str | None:
        """Return a decrypted SIP secret only for an explicit admin reveal request."""
        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT password_ciphertext FROM voip_extensions WHERE id=%s",
                (extension_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return LocalVoipServer._unprotect_password(str(row["password_ciphertext"]))

    @staticmethod
    def _extension_json(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for field in ("created_at", "updated_at"):
            if result.get(field) is not None:
                result[field] = result[field].isoformat()
        return result

    @staticmethod
    def save_extension(
        username: str, display_name: str, password: str | None = None,
        enabled: bool = True, extension_id: int | None = None,
    ) -> dict[str, Any]:
        username = str(username).strip()
        display_name = str(display_name).strip()
        if not re.fullmatch(r"[0-9]{2,12}", username):
            raise ValueError("Le numéro d’extension doit contenir de 2 à 12 chiffres.")
        if not display_name or len(display_name) > 120 or any(c in display_name for c in "\r\n=[]"):
            raise ValueError("Nom d’affichage invalide (1 à 120 caractères).")
        if password is not None and (len(password) < 12 or len(password) > 128 or any(c in password for c in "\r\n")):
            raise ValueError("Le mot de passe SIP doit contenir entre 12 et 128 caractères, sans retour à la ligne.")
        encrypted = LocalVoipServer._protect_password(password) if password else None
        with get_db_cursor() as cursor:
            if extension_id is None:
                if not encrypted:
                    raise ValueError("Un mot de passe SIP est obligatoire pour une nouvelle extension.")
                cursor.execute(
                    "INSERT INTO voip_extensions (username, display_name, password_ciphertext, enabled) "
                    "VALUES (%s, %s, %s, %s) RETURNING id, username, display_name, enabled, created_at, updated_at",
                    (username, display_name, encrypted, bool(enabled)),
                )
            else:
                if encrypted:
                    cursor.execute(
                        "UPDATE voip_extensions SET username=%s, display_name=%s, password_ciphertext=%s, "
                        "enabled=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s "
                        "RETURNING id, username, display_name, enabled, created_at, updated_at",
                        (username, display_name, encrypted, bool(enabled), extension_id),
                    )
                else:
                    cursor.execute(
                        "UPDATE voip_extensions SET username=%s, display_name=%s, enabled=%s, "
                        "updated_at=CURRENT_TIMESTAMP WHERE id=%s "
                        "RETURNING id, username, display_name, enabled, created_at, updated_at",
                        (username, display_name, bool(enabled), extension_id),
                    )
            row = cursor.fetchone()
            if row is None:
                raise ValueError("Extension introuvable.")
        return LocalVoipServer._extension_json(row)

    @staticmethod
    def delete_extension(extension_id: int) -> bool:
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM voip_extensions WHERE id=%s RETURNING id", (extension_id,))
            return cursor.fetchone() is not None

    @staticmethod
    def _find_executable(configured: str) -> Path | None:
        candidates: list[Path] = []
        if configured:
            configured_path = Path(configured).expanduser()
            # yate-service.exe is only the Windows service wrapper. Cliniva must
            # launch the console engine so it can supply its generated config.
            if configured_path.name.casefold() == "yate-service.exe":
                candidates.extend((
                    configured_path.with_name("yate-console.exe"),
                    configured_path.with_name("yate.exe"),
                ))
            else:
                candidates.append(configured_path)
        app_root = Path(__file__).resolve().parent.parent
        for base in (app_root / "Sip_app",):
            candidates.extend((base / "yate-console.exe", base / "yate.exe"))
        for base in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
                     Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))):
            for root in (base / "Yate", base / "Yate" / "bin"):
                candidates.extend((root / "yate-console.exe", root / "yate.exe"))
        for candidate in candidates:
            if candidate.is_file() and candidate.suffix.casefold() == ".exe":
                return candidate.resolve()
        return None

    @staticmethod
    def bundled_installer() -> Path | None:
        installer = Path(__file__).resolve().parent.parent / "Sip_app" / "yate-6.1.0-1-setup.exe"
        return installer if installer.is_file() else None

    @staticmethod
    def _windows_service_status() -> dict[str, Any]:
        if sys.platform != "win32":
            return {"installed": False, "running": False, "pid": None, "error": ""}
        try:
            import win32service
            manager = win32service.OpenSCManager(
                None, None, win32service.SC_MANAGER_CONNECT
            )
            try:
                service = win32service.OpenService(
                    manager, "yate", win32service.SERVICE_QUERY_STATUS
                )
                try:
                    state = win32service.QueryServiceStatusEx(
                        service, win32service.SC_STATUS_PROCESS_INFO
                    )
                    running = state.get("CurrentState") == win32service.SERVICE_RUNNING
                    return {
                        "installed": True, "running": running,
                        "pid": state.get("ProcessId") or None, "error": "",
                    }
                finally:
                    win32service.CloseServiceHandle(service)
            finally:
                win32service.CloseServiceHandle(manager)
        except Exception as exc:
            # ERROR_SERVICE_DOES_NOT_EXIST means a portable/console Yate setup.
            if getattr(exc, "winerror", None) == 1060 or (
                len(getattr(exc, "args", ())) > 0 and getattr(exc, "args", (None,))[0] == 1060
            ):
                return {"installed": False, "running": False, "pid": None, "error": ""}
            return {"installed": False, "running": False, "pid": None, "error": str(exc)}

    @staticmethod
    def _control_windows_service(action: str) -> None:
        if sys.platform != "win32":
            return
        import win32service
        manager = win32service.OpenSCManager(
            None, None, win32service.SC_MANAGER_CONNECT
        )
        try:
            access = win32service.SERVICE_QUERY_STATUS
            access |= win32service.SERVICE_START if action == "start" else win32service.SERVICE_STOP
            service = win32service.OpenService(manager, "yate", access)
            try:
                state = win32service.QueryServiceStatus(service)[1]
                if action == "stop":
                    if state in (win32service.SERVICE_STOPPED, win32service.SERVICE_STOP_PENDING):
                        return
                    win32service.ControlService(service, win32service.SERVICE_CONTROL_STOP)
                    target = win32service.SERVICE_STOPPED
                else:
                    if state in (win32service.SERVICE_RUNNING, win32service.SERVICE_START_PENDING):
                        return
                    if state == win32service.SERVICE_STOP_PENDING:
                        deadline = time.monotonic() + 20
                        while time.monotonic() < deadline:
                            state = win32service.QueryServiceStatus(service)[1]
                            if state == win32service.SERVICE_STOPPED:
                                break
                            time.sleep(0.25)
                        if state != win32service.SERVICE_STOPPED:
                            raise RuntimeError("Le service Yate est toujours en cours d’arrêt.")
                    win32service.StartService(service, None)
                    target = win32service.SERVICE_RUNNING
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    state = win32service.QueryServiceStatus(service)[1]
                    if state == target:
                        return
                    time.sleep(0.25)
                raise RuntimeError(
                    f"Le service Windows Yate n’a pas confirmé son état après {action}."
                )
            finally:
                win32service.CloseServiceHandle(service)
        except Exception as exc:
            code = getattr(exc, "winerror", None) or (
                getattr(exc, "args", (None,))[0] if getattr(exc, "args", ()) else None
            )
            if code in (5, 1058):
                if code == 1058:
                    message = (
                        "Le service Windows Yate est désactivé. Réactive-le dans Services Windows, "
                        "puis réessaie."
                    )
                elif action == "start":
                    message = (
                        "Windows refuse le démarrage du service Yate avec les droits actuels. "
                        "Lance Cliniva en tant qu’administrateur Windows, ou démarre Yate dans Services Windows."
                    )
                else:
                    message = (
                        "Windows refuse le contrôle du service Yate avec les droits actuels. "
                        "Lance Cliniva en tant qu’administrateur Windows."
                    )
                raise RuntimeError(message) from exc
            if code == 1060:
                return
            raise RuntimeError(f"Contrôle du service Windows Yate impossible : {exc}") from exc
        finally:
            win32service.CloseServiceHandle(manager)

    def launch_bundled_installer(self) -> dict[str, Any]:
        if sys.platform != "win32":
            raise RuntimeError("L’installation du moteur Yate est disponible sur Windows uniquement.")
        installer = self.bundled_installer()
        if not installer:
            raise RuntimeError("Installateur Yate introuvable dans le dossier Sip_app.")
        try:
            subprocess.Popen(
                [str(installer)], cwd=str(installer.parent), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except OSError as exc:
            raise RuntimeError(f"Impossible d’ouvrir l’installation Yate : {exc}") from exc
        return {"installer": str(installer), "launched": True}

    def status(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            running = process is not None and process.poll() is None
            if process is not None and not running:
                self._last_error = self._last_error or f"Yate s’est arrêté (code {process.returncode})."
                self._process = None
                self._close_log()
            settings = self.get_settings()
            executable = self._find_executable(str(settings["sip_server_executable"]))
            module_dir = executable.parent / "modules" if executable else None
            recording_modules_available = bool(
                module_dir and module_dir.is_dir()
                and next(module_dir.rglob("wavefile.yate*"), None)
                and next(module_dir.rglob("javascript.yate*"), None)
            )
            installer = self.bundled_installer()
            windows_service = self._windows_service_status()
            process_running = process is not None and process.poll() is None
            service_running = bool(windows_service["running"])
            return {
                "running": process_running or service_running,
                "pid": process.pid if process_running and process else windows_service["pid"],
                "managed_by": "service_windows" if service_running else ("cliniva" if process_running else ""),
                "windows_service": windows_service,
                "started_at": self._started_at if running else None,
                "executable": str(executable) if executable else str(settings["sip_server_executable"] or ""),
                "sip_server_executable": str(settings["sip_server_executable"] or ""),
                "engine_available": executable is not None,
                "installer_available": installer is not None,
                "installer_path": str(installer) if installer else "",
                "bind_host": settings["sip_bind_host"],
                "port": settings["sip_bind_port"],
                "domain": settings["sip_domain"],
                "auto_start": settings["sip_auto_start"],
                "sip_enable_transfer": settings["sip_enable_transfer"],
                "sip_record_calls": settings["sip_record_calls"],
                "recording_available": bool(settings["sip_record_calls"] and recording_modules_available),
                "recordings_dir": str(self._recordings_dir),
                "cdr_path": str(self._cdr_path or (self._config_dir.parent / "calls" / "call-records.csv")),
                "log_path": str(self._log_path),
                "error": self._last_error,
            }

    def _close_log(self) -> None:
        if self._log_handle:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None

    def _generate_config(self, executable: Path, settings: dict[str, Any]) -> None:
        install_root = executable.parent
        module_dir = install_root / "modules"
        if not module_dir.is_dir():
            raise RuntimeError("Dossier des modules Yate introuvable à côté de l’exécutable.")
        sip_module = next((p for p in module_dir.rglob("ysipchan.yate*")), None)
        reg_module = next((p for p in module_dir.rglob("regfile.yate*")), None)
        manager_module = next((p for p in module_dir.rglob("rmanager.yate*")), None)
        if not sip_module or not reg_module or not manager_module:
            raise RuntimeError("Modules Yate ysipchan/regfile/rmanager absents. Réinstalle Yate avec les modules complets.")
        cdr_modules = {name: next((p for p in module_dir.rglob(f"{name}.yate*")), None)
                       for name in ("cdrbuild", "cdrcombine", "cdrfile")}
        recording_modules = {
            name: next((p for p in module_dir.rglob(f"{name}.yate*")), None)
            for name in ("wavefile", "javascript")
        }
        share_dir = install_root / "share"
        if not share_dir.is_dir():
            share_dir = install_root
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._user_dir.mkdir(parents=True, exist_ok=True)
        host = str(settings["sip_bind_host"])
        port = int(settings["sip_bind_port"])
        config_name = "yate-console.conf" if executable.name.casefold() == "yate-console.exe" else "yate.conf"
        enabled_modules = ["ysipchan.yate=true", "regfile.yate=true", "rmanager.yate=true"]
        enabled_modules.extend(f"{name}.yate=true" for name, path in cdr_modules.items() if path)
        enabled_modules.extend(f"{name}.yate=true" for name, path in recording_modules.items()
                               if path and settings.get("sip_record_calls", True))
        (self._config_dir / config_name).write_text(
            "[default]\nmodload=disable\n\n[modules]\n" + "\n".join(enabled_modules) + "\n",
            encoding="utf-8",
        )
        transfer = "yes" if settings.get("sip_enable_transfer", True) else "no"
        (self._config_dir / "ysipchan.conf").write_text(
            f"[general]\ntype=udp\naddr={host}\nport={port}\nipv6=no\ntransfer={transfer}\n",
            encoding="utf-8",
        )
        cdr_dir = self._config_dir.parent / "calls"
        cdr_dir.mkdir(parents=True, exist_ok=True)
        self._cdr_path = cdr_dir / "call-records.csv"
        if cdr_modules["cdrbuild"] and cdr_modules["cdrfile"]:
            (self._config_dir / "cdrbuild.conf").write_text(
                "[general]\nupdates=true\nstatus=true\nstatus_interval=60\nstatus_answer=true\n\n"
                "[parameters]\ncaller=true\ncalled=true\naddress=true\nreason=true\n",
                encoding="utf-8",
            )
            # Yate otherwise writes one CDR line for every leg (ringing, inbound,
            # outbound). cdrcombine + combined=true produces one record per call.
            combined = bool(cdr_modules["cdrcombine"])
            cdr_format = (
                '${time},"${billid}","${chan}","${address}","${caller}","${called}",'
                '${billtime},${ringtime},${duration},"${direction}","${status}","${reason}"'
            )
            if combined:
                cdr_format = (
                    '${time},"${billid}","${chan}","${address}","${caller}","${called}",'
                    '${billtime},${ringtime},${duration},"${direction}","${status}","${reason}",'
                    '"${out_leg.chan}","${out_leg.address}",${out_leg.billtime},'
                    '${out_leg.ringtime},${out_leg.duration},"${out_leg.reason}"'
                )
            (self._config_dir / "cdrfile.conf").write_text(
                f"[general]\nfile={self._cdr_path.as_posix()}\ncombined={'true' if combined else 'false'}\n"
                f"format={cdr_format}\n",
                encoding="utf-8",
            )
        self._recordings_dir.mkdir(parents=True, exist_ok=True)
        if settings.get("sip_record_calls", True) and all(recording_modules.values()):
            recorder_script = Path(__file__).with_name("yate_call_recording.js")
            if not recorder_script.is_file():
                raise RuntimeError("Le script Cliniva d’enregistrement des appels est introuvable.")
            script_target = self._config_dir / "cliniva_call_recording.js"
            script_text = recorder_script.read_text(encoding="utf-8")
            script_target.write_text(
                script_text.replace("__RECORDINGS_DIR__", json.dumps(self._recordings_dir.as_posix())),
                encoding="utf-8",
            )
            (self._config_dir / "javascript.conf").write_text(
                f"[general]\nscripts_dir={self._config_dir.as_posix()}\n\n"
                "[scripts]\ncliniva_call_recorder=cliniva_call_recording.js\n",
                encoding="utf-8",
            )
        elif settings.get("sip_record_calls", True):
            logger.warning("Enregistrement SIP demandé, mais les modules Yate wavefile/javascript sont absents")
        self._rmanager_password = secrets.token_urlsafe(32)
        (self._config_dir / "rmanager.conf").write_text(
            "[general]\naddr=127.0.0.1\nport=5038\n"
            f"password={self._rmanager_password}\ntelnet=no\noutput=no\n",
            encoding="utf-8",
        )
        lines = ["[general]", "auth=100", "register=100", "route=100",
                 f"file={self._user_dir / 'registered-users.conf'}", ""]
        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT username, display_name, password_ciphertext FROM voip_extensions "
                "WHERE enabled=TRUE ORDER BY username"
            )
            extensions = cursor.fetchall()
        for extension in extensions:
            password = self._unprotect_password(str(extension["password_ciphertext"]))
            name = str(extension["display_name"]).replace("\r", " ").replace("\n", " ")
            # SIP Digest authentication identifies the extension by username.
            # Yate attend une section [username] pour l'authentification SIP.
            # Le format username@domain concerne notamment Jabber.
            lines.extend((f"[{extension['username']}]",
                          f"password={password}", f"callername={name}", ""))
        (self._config_dir / "regfile.conf").write_text("\n".join(lines), encoding="utf-8")

    def connected_clients(self) -> dict[str, Any]:
        """Read live SIP registrations from Yate's loopback-only rmanager."""
        if not self._rmanager_password:
            return {"available": False, "clients": [], "error": "Le serveur SIP doit être démarré depuis Cliniva."}
        try:
            with socket.create_connection(("127.0.0.1", 5038), timeout=1.5) as manager:
                manager.settimeout(0.2)
                try:
                    manager.recv(4096)  # Greeting
                except (TimeoutError, OSError):
                    pass
                manager.sendall(f"auth {self._rmanager_password}\r\n".encode("ascii"))
                time.sleep(0.05)
                try:
                    manager.recv(4096)  # Authentication result
                except (TimeoutError, OSError):
                    pass
                manager.sendall(b"machine on\r\nstatus regfile\r\n")
                chunks: list[bytes] = []
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline:
                    try:
                        chunk = manager.recv(8192)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        if b"%%-status" in chunk:
                            break
                    except TimeoutError:
                        if chunks:
                            break
                response = b"".join(chunks).decode("utf-8", errors="replace")
            names = {str(item["username"]): str(item["display_name"]) for item in self.list_extensions()}
            clients = []
            pattern = re.compile(r"(?:^|[,;])(?P<username>[0-9]{2,12})=sip/sip:(?P<contact>[^,\r\n]+)")
            for match in pattern.finditer(response):
                username = match.group("username")
                clients.append({
                    "username": username,
                    "display_name": names.get(username, ""),
                    "contact": match.group("contact").strip(),
                    "connected": True,
                })
            return {"available": True, "clients": clients, "error": ""}
        except (OSError, RuntimeError) as exc:
            return {"available": False, "clients": [], "error": f"État des postes indisponible : {exc}"}

    def _rmanager_query(self, command: str) -> str:
        """Send a read-only command to Yate's loopback-only manager."""
        if not self._rmanager_password:
            raise RuntimeError("Le serveur SIP doit être démarré depuis Cliniva.")
        chunks: list[bytes] = []
        with socket.create_connection(("127.0.0.1", 5038), timeout=2.0) as manager:
            manager.settimeout(0.3)
            try:
                manager.recv(4096)
            except (TimeoutError, OSError):
                pass
            manager.sendall(f"auth {self._rmanager_password}\r\n".encode("ascii"))
            time.sleep(0.05)
            try:
                manager.recv(4096)
            except (TimeoutError, OSError):
                pass
            manager.sendall(f"machine on\r\n{command}\r\n".encode("ascii"))
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    chunk = manager.recv(8192)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"%%-status" in chunk or b"%%-" in chunk:
                        break
                except TimeoutError:
                    if chunks:
                        break
        return b"".join(chunks).decode("utf-8", errors="replace")

    def live_calls(self) -> dict[str, Any]:
        """Read active call legs from Yate's cdrbuild status report."""
        try:
            response = self._rmanager_query("status cdrbuild")
            call_legs: list[dict[str, str]] = []
            # Format: sip/2=answered|103|104|billid|duration
            pattern = (r"(?:^|[;,])(?P<channel>sip/[^=;,\r\n]+)="
                       r"(?P<status>[^|;,\r\n]+)\|(?P<caller>[^|;,\r\n]*)\|"
                       r"(?P<called>[^|;,\r\n]*)\|(?P<billid>[^|;,\r\n]*)\|"
                       r"(?P<duration>[^|;,\r\n]*)")
            for match in re.finditer(pattern, response):
                call_legs.append({key: value.strip() for key, value in match.groupdict().items()})
            calls = self._merge_call_legs(call_legs)
            return {"available": "name=cdrbuild" in response or "cdrs=" in response,
                    "calls": calls, "active_calls": len(calls),
                    "active_call_legs": len(call_legs), "error": ""}
        except (OSError, RuntimeError) as exc:
            return {"available": False, "calls": [], "active_call_legs": 0,
                    "error": f"Appels en direct indisponibles : {exc}"}

    @staticmethod
    def _merge_call_legs(legs: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Collapse Yate's SIP legs into one live-call row per billid."""
        grouped: dict[str, list[dict[str, str]]] = {}
        for leg in legs:
            key = str(leg.get("billid") or leg.get("channel") or "").strip()
            if key:
                grouped.setdefault(key, []).append(leg)

        status_rank = {"answered": 4, "connected": 3, "progress": 2, "ringing": 1}
        calls: list[dict[str, Any]] = []
        for billid, group in grouped.items():
            def rank(leg: dict[str, str]) -> tuple[int, float]:
                try:
                    duration = float(leg.get("duration") or 0)
                except (TypeError, ValueError):
                    duration = 0.0
                return (status_rank.get(str(leg.get("status") or "").casefold(), 0), duration)

            primary = max(group, key=rank)
            call = dict(primary)
            call["billid"] = billid
            call["leg_count"] = len(group)
            call["caller"] = call.get("caller") or next((item.get("caller") for item in group if item.get("caller")), "")
            call["called"] = call.get("called") or next((item.get("called") for item in group if item.get("called")), "")
            try:
                call["duration"] = str(max(float(item.get("duration") or 0) for item in group))
            except (TypeError, ValueError):
                pass
            calls.append(call)
        return calls

    def recent_call_records(self, limit: int = 100) -> dict[str, Any]:
        """Read recent call detail records written by Yate's cdrfile module."""
        path = self._cdr_path
        if path is None or not path.is_file():
            return {"available": False, "records": [], "error": "Le journal CDR n’est pas encore disponible."}
        try:
            with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
                rows = list(csv.reader(handle))[-max(1, min(int(limit), 500)):]
            records_by_billid: dict[str, dict[str, Any]] = {}
            for row in reversed(rows):
                if len(row) < 12:
                    continue
                if len(row) >= 18:  # Nouveau format combiné : une ligne par appel.
                    record = {"timestamp": row[0], "billid": row[1], "channel": row[2],
                        "address": row[3], "caller": row[4], "called": row[5],
                        "billtime": row[6], "ringtime": row[7], "duration": row[8],
                        "direction": row[9], "status": row[10], "reason": row[11],
                        "out_leg_channel": row[12], "out_leg_address": row[13],
                        "out_leg_billtime": row[14], "out_leg_ringtime": row[15],
                        "out_leg_duration": row[16], "out_leg_reason": row[17]}
                else:  # Compatibilité avec les anciens journaux par jambe.
                    record = {"timestamp": row[0], "billid": row[1], "channel": row[2],
                        "address": row[3], "caller": row[4], "called": row[5],
                        "billtime": row[6], "ringtime": row[7], "duration": row[8],
                        "direction": row[9], "status": row[10], "reason": row[11]}
                key = str(record.get("billid") or record.get("channel") or "").strip()
                if not key:
                    continue
                existing = records_by_billid.get(key)
                if existing:
                    existing["leg_count"] = int(existing.get("leg_count") or 1) + 1
                    try:
                        existing["billtime"] = str(max(float(existing.get("billtime") or 0), float(record.get("billtime") or 0)))
                        existing["duration"] = str(max(float(existing.get("duration") or 0), float(record.get("duration") or 0)))
                    except (TypeError, ValueError):
                        pass
                    if str(record.get("status") or "").casefold() == "answered":
                        for field in ("status", "channel", "address", "timestamp"):
                            existing[field] = record.get(field) or existing.get(field)
                        existing["reason"] = record.get("reason") or existing.get("reason")
                    continue
                record["leg_count"] = 1
                records_by_billid[key] = record
            records = list(records_by_billid.values())
            for record in records:
                local_track, remote_track = self._recording_tracks(
                    str(record["channel"]), str(record["caller"]), str(record["called"]),
                    str(record["billid"]),
                )
                record["recording_available"] = bool(local_track or remote_track)
                record["recording_url"] = (
                    f"/api/telephonie/calls/{quote(str(record['billid']), safe='')}/recording"
                    if record["recording_available"] else ""
                )
            return {"available": True, "records": records, "error": ""}
        except (OSError, ValueError) as exc:
            return {"available": False, "records": [], "error": f"Lecture du journal des appels impossible : {exc}"}

    @staticmethod
    def _safe_recording_part(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9.-]", "-", str(value or "unknown"))[:96]

    def _recording_tracks(self, channel: str, caller: str, called: str,
                          billid: str = "") -> tuple[Path | None, Path | None]:
        """Find both Yate audio directions associated with one CDR row."""
        if not self._recordings_dir.is_dir():
            return None, None
        channel_key = self._safe_recording_part(channel)
        caller_key = self._safe_recording_part(caller)
        called_key = self._safe_recording_part(called)
        billid_key = self._safe_recording_part(billid)
        candidates = sorted(
            self._recordings_dir.glob("*.slin"),
            key=lambda item: item.stat().st_mtime if item.exists() else 0,
            reverse=True,
        )
        seen_stems: set[str] = set()
        for candidate in candidates:
            suffix = ".local.slin" if candidate.name.endswith(".local.slin") else ".remote.slin"
            stem = candidate.name[:-len(suffix)]
            if stem in seen_stems:
                continue
            seen_stems.add(stem)
            pieces = stem.split("__")
            billid_match = bool(billid and billid_key != "unknown"
                                and len(pieces) > 1 and pieces[0] == "billid"
                                and pieces[1] == billid_key)
            new_name = len(pieces) >= 7 and pieces[0] == "billid"
            channel_parts = pieces[2:4] if new_name else pieces[:2]
            number_parts = pieces[4:6] if new_name else pieces[2:4]
            channel_match = channel_key in channel_parts
            numbers_match = bool(caller_key and called_key and caller_key != "unknown"
                                 and called_key != "unknown" and len(number_parts) == 2
                                 and set(number_parts) == {caller_key, called_key})
            local_path = self._recordings_dir / (stem + ".local.slin")
            remote_path = local_path.with_name(stem + ".remote.slin")
            if billid_match or channel_match or numbers_match:
                return local_path if local_path.is_file() else None, remote_path if remote_path.is_file() else None
        return None, None

    def create_call_recording_wave(self, billid: str) -> Path:
        """Build a browser-playable stereo WAV from Yate's two raw SLIN tracks."""
        cdr_path = self._cdr_path or (self._config_dir.parent / "calls" / "call-records.csv")
        if not cdr_path.is_file():
            raise FileNotFoundError("Le journal des appels est indisponible.")
        matching_row = None
        with cdr_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            for row in csv.reader(handle):
                if len(row) >= 12 and row[1] == billid:
                    matching_row = row
        if not matching_row:
            raise FileNotFoundError("Cet appel n’existe pas dans le journal téléphonique.")
        local_track, remote_track = self._recording_tracks(
            str(matching_row[2]), str(matching_row[4]), str(matching_row[5]), billid,
        )
        if not local_track and not remote_track:
            raise FileNotFoundError("L’enregistrement audio de cet appel n’est pas disponible ou n’a pas été finalisé.")

        output = tempfile.NamedTemporaryFile(prefix="cliniva-call-", suffix=".wav", delete=False)
        output_path = Path(output.name)
        output.close()
        try:
            with ExitStack() as files:
                local_file = files.enter_context(local_track.open("rb")) if local_track else None
                remote_file = files.enter_context(remote_track.open("rb")) if remote_track else None
                wav_file = files.enter_context(wave.open(str(output_path), "wb"))
                wav_file.setnchannels(2)
                wav_file.setsampwidth(2)
                wav_file.setframerate(8000)
                while True:
                    left_bytes = local_file.read(32768) if local_file else b""
                    right_bytes = remote_file.read(32768) if remote_file else b""
                    if not left_bytes and not right_bytes:
                        break
                    frame_count = max(len(left_bytes), len(right_bytes)) // 2
                    if frame_count == 0:
                        continue
                    left_bytes = left_bytes.ljust(frame_count * 2, b"\0")
                    right_bytes = right_bytes.ljust(frame_count * 2, b"\0")
                    left_samples = array("h")
                    right_samples = array("h")
                    left_samples.frombytes(left_bytes)
                    right_samples.frombytes(right_bytes)
                    stereo = array("h", [0]) * (frame_count * 2)
                    stereo[0::2] = left_samples
                    stereo[1::2] = right_samples
                    if sys.byteorder != "little":
                        stereo.byteswap()
                    wav_file.writeframesraw(stereo.tobytes())
            if output_path.stat().st_size <= 44:
                raise FileNotFoundError("L’enregistrement audio est vide.")
            return output_path
        except Exception:
            output_path.unlink(missing_ok=True)
            raise

    def start(self) -> dict[str, Any]:
        with self._lock:
            current = self.status()
            if current["managed_by"] == "cliniva":
                return current
            # Do not start the separately installed Windows service here. Its
            # yate-service.conf/regfile.conf are independent of Cliniva's database
            # and therefore do not receive extension/password changes. Cliniva
            # must generate regfile.conf from PostgreSQL and launch its own Yate
            # process so that the credentials configured in this screen apply.
            if current["running"]:
                raise RuntimeError(
                    "Le service Windows Yate est encore actif. Clique d’abord sur « Recharger la configuration » "
                    "pour l’arrêter, puis Cliniva démarrera son moteur avec les extensions et mots de passe à jour."
                )
            settings = self.get_settings()
            executable = self._find_executable(str(settings["sip_server_executable"]))
            if not executable:
                raise RuntimeError(
                    "Moteur Yate console introuvable. Installe Yate sur ce PC ou indique le chemin de yate-console.exe "
                    "(pas yate-service.exe)."
                )
            if sys.platform != "win32":
                raise RuntimeError("Le service Cliniva SIP est configuré pour Windows.")
            self._generate_config(executable, settings)
            install_root = executable.parent
            module_dir = install_root / "modules"
            share_dir = install_root / "share"
            if not share_dir.is_dir():
                share_dir = install_root
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._close_log()
            args = [str(executable), "-c", str(self._config_dir), "-m", str(module_dir),
                    "-e", str(share_dir), "-u", str(self._user_dir), "-l", str(self._log_path)]
            try:
                self._process = subprocess.Popen(
                    args, cwd=str(install_root), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as exc:
                self._close_log()
                raise RuntimeError(f"Impossible de lancer Yate : {exc}") from exc
            time.sleep(1.0)
            if self._process.poll() is not None:
                return_code = self._process.returncode
                self._process = None
                self._close_log()
                self._last_error = f"Yate s’est arrêté au démarrage (code {return_code}). Consulte le journal Yate."
                raise RuntimeError(self._last_error)
            self._last_error = ""
            self._started_at = time.time()
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            self._process = None
            self._started_at = None
            self._close_log()
            self._control_windows_service("stop")
            return self.status()

    def restart(self) -> dict[str, Any]:
        self.stop()
        return self.start()

    def autostart(self) -> None:
        if self.get_settings().get("sip_auto_start"):
            try:
                self.start()
            except Exception:
                logger.exception("Démarrage automatique du serveur SIP impossible")


voip_server = LocalVoipServer()
