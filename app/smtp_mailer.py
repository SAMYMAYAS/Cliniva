"""Envoi SMTP commun pour les notifications Cliniva."""

import base64
from email.message import EmailMessage
import os
import smtplib
import ssl


def encrypt_smtp_password(password: str) -> str:
    """Protège le secret avec DPAPI Windows, lié au compte qui exécute Cliniva."""
    if not password:
        return ""
    try:
        import win32crypt
    except ImportError as exc:
        raise RuntimeError("pywin32 est requis pour protéger le mot de passe SMTP sur ce serveur.") from exc
    encrypted = win32crypt.CryptProtectData(
        password.encode("utf-8"), "Cliniva SMTP notifications", None, None, None, 0
    )
    return base64.b64encode(encrypted).decode("ascii")


def decrypt_smtp_password(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        import win32crypt
        encrypted = base64.b64decode(ciphertext, validate=True)
        return win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1].decode("utf-8")
    except Exception as exc:
        raise RuntimeError("Le mot de passe SMTP ne peut pas être déchiffré sur ce compte Windows. Réenregistre-le depuis Cliniva.") from exc


def get_smtp_configuration() -> dict:
    """Priorité à la configuration Cliniva chiffrée, puis aux variables d'environnement."""
    try:
        from main import load_app_configuration
        saved_raw = load_app_configuration().get("notification_smtp")
        saved = saved_raw or {}
    except Exception:
        saved_raw = None
        saved = {}
    if saved_raw is not None and not saved.get("enabled"):
        return {"enabled": False}
    if saved.get("enabled") and saved.get("host") and saved.get("from"):
        return {
            "enabled": True,
            "host": str(saved["host"]).strip(),
            "port": int(saved.get("port") or 587),
            "user": str(saved.get("user") or "").strip(),
            "from": str(saved["from"]).strip(),
            "password": decrypt_smtp_password(str(saved.get("password_ciphertext") or "")),
            "starttls": bool(saved.get("starttls", True)),
            "ssl": bool(saved.get("ssl", False)),
        }
    host = os.getenv("CLINIVA_SMTP_HOST", "").strip()
    sender = os.getenv("CLINIVA_SMTP_FROM", os.getenv("CLINIVA_SMTP_USER", "")).strip()
    if host and sender:
        port = int(os.getenv("CLINIVA_SMTP_PORT", "587"))
        return {
            "enabled": True, "host": host, "port": port,
            "user": os.getenv("CLINIVA_SMTP_USER", "").strip(),
            "from": sender, "password": os.getenv("CLINIVA_SMTP_PASSWORD", ""),
            "starttls": os.getenv("CLINIVA_SMTP_STARTTLS", "true").strip().lower() not in {"0", "false", "non"},
            "ssl": os.getenv("CLINIVA_SMTP_SSL", "").strip().lower() in {"1", "true", "oui"} or port == 465,
        }
    return {"enabled": False}


def send_smtp_email(configuration: dict, recipients: list[str], subject: str, body: str) -> None:
    if not configuration.get("enabled"):
        raise RuntimeError("L'envoi SMTP est désactivé ou non configuré.")
    host = str(configuration.get("host") or "").strip()
    sender = str(configuration.get("from") or "").strip()
    port = int(configuration.get("port") or 587)
    if not host or not sender or not recipients:
        raise RuntimeError("La configuration SMTP ou les destinataires sont incomplets.")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(sorted(set(recipients)))
    message.set_content(body)
    username = str(configuration.get("user") or "").strip()
    password = str(configuration.get("password") or "")
    context = ssl.create_default_context()
    if configuration.get("ssl") or port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=12, context=context) as smtp:
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=12) as smtp:
            smtp.ehlo()
            if configuration.get("starttls", True):
                smtp.starttls(context=context)
                smtp.ehlo()
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
