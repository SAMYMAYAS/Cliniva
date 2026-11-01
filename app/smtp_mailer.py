"""Envoi SMTP commun pour les notifications Cliniva."""

import base64
from email.message import EmailMessage
from html import escape
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


def build_cliniva_email_html(title: str, subtitle: str, content_html: str, preheader: str = "") -> str:
    """Cadre de courriel Cliniva sans ressource distante, compatible mobile et clients classiques."""
    safe_title = escape(str(title))
    safe_subtitle = escape(str(subtitle))
    safe_preheader = escape(str(preheader or title))
    return f'''<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f1f5f3;font-family:Arial,Helvetica,sans-serif;color:#17231b;-webkit-text-size-adjust:100%;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent">{safe_preheader}</div>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="width:100%;background:#f1f5f3;border-collapse:collapse"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="620" style="width:100%;max-width:620px;border-collapse:separate;border-spacing:0;background:#ffffff;border:1px solid #e0e9e2;border-radius:16px;overflow:hidden">
<tr><td style="padding:24px 28px;background:#14271b;background-image:linear-gradient(120deg,#101b13,#1d4b2b);color:#ffffff">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td style="font-size:22px;line-height:1.2;font-weight:800;letter-spacing:.2px">Cliniva</td><td align="right" style="font-size:10px;line-height:1.4;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#c9f27b">NOTIFICATION SYSTÈME</td></tr></table>
<div style="margin-top:20px;font-size:22px;line-height:1.25;font-weight:800">{safe_title}</div><div style="margin-top:6px;font-size:13px;line-height:1.5;color:#d4e4d8">{safe_subtitle}</div>
</td></tr><tr><td style="padding:24px 28px 28px">{content_html}</td></tr>
<tr><td style="padding:16px 28px;border-top:1px solid #e6ece8;background:#f8faf8;font-size:11px;line-height:1.55;color:#718078">Ce message automatique a été envoyé par <strong style="color:#31533a">Cliniva</strong>. Consulte le module Service Technique pour le suivi de la déclaration.</td></tr>
</table><div style="max-width:620px;padding:12px 8px 0;font-size:10px;line-height:1.4;color:#8a9690;text-align:center">Cliniva · Système de gestion de clinique</div>
</td></tr></table></body></html>'''


def send_smtp_email(configuration: dict, recipients: list[str], subject: str, body: str,
                    html_body: str | None = None,
                    attachments: list[tuple[str, bytes, str]] | None = None) -> None:
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
    if html_body:
        message.add_alternative(html_body, subtype="html")
    for filename, content, mime_type in attachments or []:
        maintype, _, subtype = str(mime_type or "application/octet-stream").partition("/")
        message.add_attachment(content, maintype=maintype or "application",
                               subtype=subtype or "octet-stream", filename=filename)
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
