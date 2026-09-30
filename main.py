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
import os
import re
import socket
import sys
import threading
import time
from typing import Dict, Any, Optional

import uvicorn
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import webview

# Services et Persistance
from app.services import DashboardService
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
from modules.gestion_caisse import caisse_router
from modules.gestion_rendezvous import appointments_router


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
            return json.load(file_handler)
    except Exception as exc:
        logger.warning(
            "Erreur lors de la lecture de config.json (%s), chargement des valeurs par défaut.",
            exc,
        )
        return default_config


# ============================================================================
# APPLICATION FASTAPI & ATTACHEMENT DES ROUTEURS
# ============================================================================
app = FastAPI(
    title="Cliniva Management Backend",
    description="Système d'exploitation clinique médicale haute performance",
    version="1.0.0",
)

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

app.include_router(caisse_router)
app.include_router(appointments_router)

templates = Jinja2Templates(directory=TEMPLATES_DIR)


class LoginPayload(BaseModel):
    """Schéma de validation des identifiants de connexion."""

    login: str
    password: str


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

    data = DashboardService.get_dashboard_metrics()
    if not data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Les données médicales sont temporairement indisponibles.",
        )

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"data": data, "user": user},
    )


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
            module_target = importlib.reload(mod)
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
        server.run()


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
    port = config.get("local_port", 8000)

    # 3. Démarrage du serveur si mode local
    if config.get("server_mode") == "local":
        listen_host = config.get("local_host", "0.0.0.0")
        logger.info("Démarrage du serveur local sur %s:%s...", listen_host, port)

        server_thread = LocalServerThread(listen_host, port)
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
        min_size=(1024, 700),
        background_color="#f0f3f1",
    )

    # 5. Démarrage de la boucle d'événements native
    webview.start(debug=False)


if __name__ == "__main__":
    main()
