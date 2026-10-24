"""Lanceur léger Cliniva pour un poste client Windows.

Ce fichier utilise uniquement la bibliothèque standard de Python. Il vérifie
le serveur Cliniva puis ouvre l'application dans le navigateur par défaut.
La configuration (adresse du serveur uniquement) est mémorisée à côté du script.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import BOTH, END, LEFT, X, Label, StringVar, Tk, messagebox
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


def save_server_url(server_url: str) -> None:
    CONFIG_PATH.write_text(
        json.dumps({"server_url": server_url}, ensure_ascii=False, indent=2),
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


class ClinivaClient:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("560x250")
        self.root.minsize(500, 230)
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
        threading.Thread(target=self._check_and_open, args=(server_url,), daemon=True).start()

    def _check_and_open(self, server_url: str) -> None:
        reachable, message = check_server(server_url)
        self.root.after(0, self._show_result, server_url, reachable, message)

    def _show_result(self, server_url: str, reachable: bool, message: str) -> None:
        self.status_var.set(message)
        if not reachable:
            messagebox.showerror(
                APP_TITLE,
                message + "\n\nVérifie que Cliniva est lancé et que le pare-feu du serveur autorise le port 8000 sur le réseau local.",
                parent=self.root,
            )
            return
        try:
            save_server_url(server_url)
        except OSError as exc:
            messagebox.showwarning(APP_TITLE, f"Connexion OK, mais adresse non mémorisée : {exc}", parent=self.root)
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
            save_server_url(server_url)
        except OSError:
            pass
        webbrowser.open(server_url, new=2)
        return 0
    ClinivaClient()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
