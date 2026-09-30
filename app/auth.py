"""Module d'authentification, de gestion des sessions et de traçabilité.

Gère les tokens de session sécurisés, les cookies et l'enregistrement
automatique des actions d'audit. Conforme strictement à PEP 8.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Dict, Any, Optional

from fastapi import Request, HTTPException, status
from database import get_db_cursor, record_audit

SESSION_COOKIE_NAME = "cliniva_session"
SESSION_DURATION_HOURS = 24


def hash_password(password: str) -> str:
    """Génère l'empreinte SHA-256 du mot de passe."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def create_user_session(user_id: int) -> str:
    """Génère un token de session cryptographique et l'enregistre en base."""
    token = secrets.token_hex(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_DURATION_HOURS)

    with get_db_cursor() as cursor:
        cursor.execute("""
            INSERT INTO user_sessions (session_token, user_id, expires_at)
            VALUES (%s, %s, %s);
        """, (token, user_id, expires))

    return token


def delete_user_session(token: str) -> None:
    """Supprime une session active lors de la déconnexion."""
    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM user_sessions WHERE session_token = %s;", (token,))


def get_current_user_optional(request: Request) -> Optional[Dict[str, Any]]:
    """Récupère l'utilisateur connecté via son cookie de session, ou None."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None

    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT u.id, u.prenom, u.nom, u.telephone, u.role, u.login, u.status, 
                   u.avatar, u.permissions, u.specialites_autorisees
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.session_token = %s AND s.expires_at > CURRENT_TIMESTAMP;
        """, (token,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_current_user(request: Request) -> Dict[str, Any]:
    """Dépendance FastAPI bloquante : lève une exception 401 si non authentifié."""
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expirée ou utilisateur non connecté."
        )
    return user


def track_action(request: Request, action_type: str, description: str) -> None:
    """Enregistre une action réalisée par l'utilisateur actuellement connecté."""
    user = get_current_user_optional(request)
    ip = request.client.host if request.client else "127.0.0.1"

    if user:
        record_audit(
            user_id=user["id"],
            user_fullname=f"{user['prenom']} {user['nom']}",
            user_role=user["role"],
            action_type=action_type,
            description=description,
            ip_address=ip
        )
    else:
        record_audit(
            user_id=None,
            user_fullname="Système / Anonyme",
            user_role="Visiteur",
            action_type=action_type,
            description=description,
            ip_address=ip
        )