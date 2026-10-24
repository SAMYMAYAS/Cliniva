"""Lanceur léger Cliniva pour un poste client Windows.

Ce fichier utilise uniquement la bibliothèque standard de Python. Il vérifie
le serveur Cliniva puis ouvre l'application dans le navigateur par défaut.
La configuration (adresse du serveur uniquement) est mémorisée à côté du script.
"""

from __future__ import annotations

import json
import hashlib
from importlib import metadata
import shutil
import sys
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
import webbrowser
import zipfile
from pathlib import Path
from tkinter import LEFT, X, BooleanVar, Label, StringVar, Tk, messagebox
from tkinter.ttk import Button, Entry, Frame


APP_TITLE = "Cliniva — Poste client"
DEFAULT_PORT = 8000
CONFIG_PATH = Path(__file__).resolve().with_name("cliniva_client_config.json")


def normalize_server_url(value: str) -> str:
    """Accepte une IP, un nom d'hôte ou une URL et retourne l'URL racine."""
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
    # Ajoute le port Cliniva si l'utilisateur n'en a pas indiqué un.
    if ":" not in authority and not authority.startswith("["):
        value = f"{value}:{DEFAULT_PORT}"
    return value


def load_server_url() -> str:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return normalize_server_url(str(data.get("server_url", "")))
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        return ""


def load_auto_update() -> bool:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data.get("auto_update", True) is not False
    except (OSError, ValueError, TypeError):
        return True


def save_client_settings(server_url: str, auto_update: bool, version: str | None = None) -> None:
    data = {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    data.update({"server_url": server_url, "auto_update": bool(auto_update)})
    if version:
        data["client_version"] = version
    CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def check_server(server_url: str) -> tuple[bool, str]:
    """Vérifie l'API de santé Cliniva avec un délai d'attente court."""
    health_url = server_url.rstrip("/") + "/api/app/health"
    request = urllib.request.Request(
        health_url,
        headers={"User-Agent": "ClinivaClient/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read(4096).decode("utf-8"))
            if response.status == 200 and payload.get("success") is True:
                return True, "Serveur Cliniva joignable."
            return False, "Le serveur répond, mais Cliniva signale qu'il n'est pas prêt."
    except urllib.error.HTTPError as exc:
        return False, f"Le serveur répond avec HTTP {exc.code}. Vérifie son état."
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"Serveur inaccessible : {reason}"


def update_client_sources(server_url: str, app_dir: Path) -> tuple[str, str | None]:
    """Télécharge et applique l'archive de code publiée par le serveur Cliniva."""
    manifest_url = server_url.rstrip("/") + "/static/client_updates/latest.json"
    request = urllib.request.Request(manifest_url, headers={"User-Agent": "ClinivaClient/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        manifest = json.loads(response.read(64 * 1024).decode("utf-8"))

    version = str(manifest.get("version") or "").strip()
    filename = str(manifest.get("filename") or "").strip()
    expected_hash = str(manifest.get("sha256") or "").strip().lower()
    if not version or not filename or "/" in filename or "\\" in filename or not expected_hash:
        raise ValueError("Le manifeste de mise à jour du serveur est invalide.")

    settings = {}
    try:
        settings = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    if settings.get("client_version") == version:
        return f"Le client est déjà à jour ({version}).", version

    package_url = str(manifest.get("package_url") or f"/static/client_updates/{filename}")
    if not package_url.startswith("/static/client_updates/") or ".." in package_url:
        raise ValueError("Le chemin de mise à jour reçu du serveur est invalide.")
    download_url = server_url.rstrip("/") + package_url
    app_dir = app_dir.resolve()

    with tempfile.TemporaryDirectory(prefix="cliniva-update-", dir=app_dir) as temp_dir:
        temp_root = Path(temp_dir)
        archive_path = temp_root / filename
        digest = hashlib.sha256()
        request = urllib.request.Request(download_url, headers={"User-Agent": "ClinivaClient/1.0"})
        with urllib.request.urlopen(request, timeout=120) as response, archive_path.open("wb") as destination:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                destination.write(chunk)
        if digest.hexdigest().lower() != expected_hash:
            raise ValueError("La vérification SHA-256 de la mise à jour a échoué; aucun fichier n'a été remplacé.")

        staging = temp_root / "staging"
        staging.mkdir()
        allowed_roots = {"app", "modules", "templates", "static", "resources", "worklist", "Sip_app"}
        allowed_files = {"main.py", "database.py", "pip.txt", "RCP.py", "requirements-lock.txt"}
        with zipfile.ZipFile(archive_path) as package:
            for member in package.infolist():
                member_path = Path(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts or not member_path.parts:
                    raise ValueError("L'archive contient un chemin non autorisé.")
                if member_path.parts[0] not in allowed_roots | allowed_files:
                    raise ValueError("L'archive contient un fichier hors du paquet Cliniva autorisé.")
                if member.is_dir():
                    continue
                # Refuse les liens symboliques pour empêcher toute sortie du dossier d'installation.
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("L'archive contient un lien symbolique non autorisé.")
                package.extract(member, staging)

        files = [path for path in staging.rglob("*") if path.is_file()]
        if not files or not (staging / "main.py").is_file():
            raise ValueError("L'archive ne contient pas un paquet Cliniva complet.")

        requirements_lock = staging / "requirements-lock.txt"
        if requirements_lock.is_file():
            required_versions = []
            for line in requirements_lock.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and "==" in line:
                    distribution, required_version = line.split("==", 1)
                    try:
                        installed_version = metadata.version(distribution.strip())
                    except metadata.PackageNotFoundError:
                        installed_version = ""
                    if installed_version != required_version.strip():
                        required_versions.append(line)
            if required_versions:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", str(requirements_lock)],
                    capture_output=True,
                    text=True,
                    timeout=900,
                    check=False,
                )
                if result.returncode != 0:
                    details = (result.stderr or result.stdout or "échec pip sans détail").strip()[-1600:]
                    raise RuntimeError("Installation des dépendances client échouée : " + details)

        backup_root = Path.home() / "AppData" / "Local" / "Cliniva" / "ClientUpdateBackups" / version
        completed: list[tuple[Path, Path | None]] = []
        try:
            for source in files:
                relative = source.relative_to(staging)
                target = (app_dir / relative).resolve()
                if app_dir not in target.parents:
                    raise ValueError("Un chemin de l'archive sort du dossier d'installation.")
                backup = None
                if target.exists():
                    backup = backup_root / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
                target.parent.mkdir(parents=True, exist_ok=True)
                completed.append((target, backup))
                shutil.copy2(source, target)
        except Exception:
            for target, backup in reversed(completed):
                try:
                    if backup and backup.exists():
                        shutil.copy2(backup, target)
                    elif target.exists():
                        target.unlink()
                except OSError:
                    pass
            raise

    return f"Mise à jour du code terminée ({version}).", version


def configure_remote_main(app_dir: Path, server_url: str) -> None:
    """Force le mode distant tout en conservant les autres réglages locaux."""
    config_path = app_dir / "config.json"
    config = {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    config["server_mode"] = "remote"
    config["remote_server_url"] = server_url
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


class ClinivaClient:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("600x300")
        self.root.minsize(520, 280)
        self.root.configure(bg="#f3f6f5")

        Label(
            self.root,
            text="Connexion au serveur Cliniva",
            font=("Segoe UI", 17, "bold"),
            fg="#153c2c",
            bg="#f3f6f5",
        ).pack(anchor="w", padx=24, pady=(22, 4))
        Label(
            self.root,
            text="Entre l'adresse IP du PC serveur (ex. 192.168.1.20).",
            font=("Segoe UI", 10),
            fg="#58665f",
            bg="#f3f6f5",
        ).pack(anchor="w", padx=24, pady=(0, 14))

        self.server_var = StringVar(value=load_server_url())
        self.entry = Entry(self.root, textvariable=self.server_var, font=("Segoe UI", 11))
        self.entry.pack(fill=X, padx=24, ipady=7)
        self.entry.bind("<Return>", lambda _event: self.connect())

        self.auto_update_var = BooleanVar(value=load_auto_update())
        from tkinter.ttk import Checkbutton
        Checkbutton(
            self.root,
            text="Auto-update du code client avant lancement (si main.py est présent)",
            variable=self.auto_update_var,
        ).pack(anchor="w", padx=22, pady=(8, 0))

        self.status_var = StringVar(value="Le port 8000 est ajouté automatiquement si nécessaire.")
        Label(
            self.root,
            textvariable=self.status_var,
            font=("Segoe UI", 9),
            fg="#58665f",
            bg="#f3f6f5",
            wraplength=510,
            justify=LEFT,
        ).pack(anchor="w", padx=24, pady=12)

        buttons = Frame(self.root)
        buttons.pack(fill=X, padx=24, pady=(2, 18))
        Button(buttons, text="Se connecter à Cliniva", command=self.connect).pack(side=LEFT)
        Button(buttons, text="Quitter", command=self.root.destroy).pack(side=LEFT, padx=(10, 0))
        self.root.mainloop()

    def connect(self) -> None:
        try:
            server_url = normalize_server_url(self.server_var.get())
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc), parent=self.root)
            return

        self.status_var.set("Vérification de la connexion au serveur…")
        self.root.update_idletasks()
        auto_update = bool(self.auto_update_var.get())
        threading.Thread(target=self._check_and_open, args=(server_url, auto_update), daemon=True).start()

    def _check_and_open(self, server_url: str, auto_update: bool) -> None:
        reachable, message = check_server(server_url)
        if not reachable:
            self.root.after(0, self._show_error, message)
            return

        app_dir = Path(__file__).resolve().parent
        main_path = app_dir / "main.py"
        if main_path.is_file():
            version = None
            update_message = "Auto-update désactivé."
            if auto_update:
                self.root.after(0, self.status_var.set, "Recherche et téléchargement d'une mise à jour client…")
                try:
                    update_message, version = update_client_sources(server_url, app_dir)
                except Exception as exc:
                    self.root.after(0, self._ask_launch_without_update, server_url, auto_update, str(exc))
                    return
            try:
                configure_remote_main(app_dir, server_url)
                save_client_settings(server_url, auto_update, version)
            except OSError as exc:
                self.root.after(0, self._show_error, f"Impossible d'enregistrer la configuration cliente : {exc}")
                return
            self.root.after(0, self._launch_local_main, main_path, update_message)
            return

        try:
            save_client_settings(server_url, auto_update)
        except OSError as exc:
            self.root.after(0, self._show_error, f"Connexion OK, mais adresse non mémorisée : {exc}")
            return
        self.root.after(0, self._open_browser, server_url)

    def _show_error(self, message: str) -> None:
        self.status_var.set(message)
        messagebox.showerror(
            APP_TITLE,
            message + "\n\nVérifie que Cliniva est lancé et que le pare-feu du serveur autorise le port 8000 sur le réseau local.",
            parent=self.root,
        )

    def _ask_launch_without_update(self, server_url: str, auto_update: bool, error: str) -> None:
        self.status_var.set("Mise à jour indisponible; la version installée est conservée.")
        if not messagebox.askyesno(
            APP_TITLE,
            f"La mise à jour automatique n'a pas abouti :\n{error}\n\nLancer la version déjà installée ?",
            parent=self.root,
        ):
            return
        app_dir = Path(__file__).resolve().parent
        main_path = app_dir / "main.py"
        try:
            configure_remote_main(app_dir, server_url)
            save_client_settings(server_url, auto_update)
        except OSError as exc:
            self._show_error(f"Impossible d'enregistrer la configuration cliente : {exc}")
            return
        self._launch_local_main(main_path, "Lancement sans mise à jour.")

    def _launch_local_main(self, main_path: Path, status: str) -> None:
        try:
            subprocess.Popen([sys.executable, str(main_path)], cwd=str(main_path.parent))
            self.status_var.set(status + " Ouverture de Cliniva…")
            self.root.after(900, self.root.destroy)
        except OSError as exc:
            self._show_error(f"Impossible de lancer main.py : {exc}")

    def _open_browser(self, server_url: str) -> None:
        self.status_var.set("Serveur connecté. Ouverture de Cliniva dans le navigateur…")
        webbrowser.open(server_url, new=2)


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
            save_client_settings(server_url, load_auto_update())
        except OSError:
            pass
        webbrowser.open(server_url, new=2)
        return 0
    ClinivaClient()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
