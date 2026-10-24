"""Administration de la téléphonie IP locale Cliniva/Yate."""

import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from app.auth import get_current_user_optional
from app.voip_server import voip_server

logger = logging.getLogger("ClinivaVoIPUI")
telephony_router = APIRouter(prefix="/api/telephonie", tags=["Téléphonie IP"])
_voip_deferred_restart_lock = threading.Lock()


def _require_admin(request: Request) -> dict[str, Any]:
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    if user.get("status") != "Actif":
        raise HTTPException(status_code=403, detail="Ce compte utilisateur est inactif.")
    if user.get("role") != "Administrateur":
        raise HTTPException(status_code=403, detail="La gestion de la téléphonie IP est réservée à l’administrateur.")
    return user


def _ensure_schema() -> None:
    try:
        voip_server.ensure_schema()
    except Exception as exc:
        logger.exception("Initialisation des extensions SIP impossible")
        raise HTTPException(status_code=503, detail=f"Base de données téléphonie indisponible : {exc}") from exc


def _apply_extension_change() -> dict[str, Any]:
    """Restart only a running PBX; a stopped one will read DB on next start."""
    try:
        before = voip_server.status()
        if not before.get("running"):
            return {"applied": False, "restarted": False, "message": "Le serveur SIP est arrêté; la nouvelle configuration sera chargée à son prochain démarrage."}
        live = voip_server.live_calls()
        if live.get("available") and live.get("active_call_legs", 0):
            if _voip_deferred_restart_lock.acquire(blocking=False):
                def restart_when_idle():
                    try:
                        deadline = time.monotonic() + 30 * 60
                        while time.monotonic() < deadline:
                            time.sleep(5)
                            current = voip_server.live_calls()
                            if current.get("available") and not current.get("active_call_legs", 0):
                                voip_server.restart()
                                logger.info("Redémarrage SIP différé terminé après la fin des appels actifs")
                                return
                        logger.warning("Redémarrage SIP différé non exécuté dans les 30 minutes; action manuelle requise")
                    except Exception:
                        logger.exception("Redémarrage SIP différé impossible")
                    finally:
                        _voip_deferred_restart_lock.release()
                threading.Thread(target=restart_when_idle, name="ClinivaVoipDeferredRestart", daemon=True).start()
            return {"applied": False, "restarted": False, "queued": True,
                    "message": "Extension enregistrée. Le redémarrage automatique attend la fin des appels en cours pour ne pas les couper."}
        server = voip_server.restart()
        return {"applied": True, "restarted": True, "server": server, "message": "Configuration appliquée: moteur SIP Cliniva redémarré."}
    except Exception as exc:
        # The extension has already been committed to PostgreSQL. Report that
        # separately so the UI never implies that the saved account was lost.
        logger.exception("Extension SIP enregistrée, mais le redémarrage automatique a échoué")
        return {"applied": False, "restarted": False,
                "message": f"Extension enregistrée, mais le serveur SIP n’a pas pu redémarrer: {exc}"}


@telephony_router.get("/status")
async def telephony_status(request: Request):
    _require_admin(request)
    _ensure_schema()
    return JSONResponse({"server": voip_server.status(), "extensions": voip_server.list_extensions()})


@telephony_router.get("/clients")
async def telephony_clients(request: Request):
    _require_admin(request)
    _ensure_schema()
    return JSONResponse(voip_server.connected_clients())


@telephony_router.get("/calls/live")
async def telephony_live_calls(request: Request):
    _require_admin(request)
    return JSONResponse(voip_server.live_calls())


@telephony_router.get("/calls/recent")
async def telephony_recent_calls(request: Request):
    _require_admin(request)
    return JSONResponse(voip_server.recent_call_records())


@telephony_router.get("/calls/{billid}/recording")
async def telephony_call_recording(billid: str, request: Request):
    _require_admin(request)
    try:
        audio_path = voip_server.create_call_recording_wave(billid)
        return FileResponse(
            audio_path,
            media_type="audio/wav",
            filename=f"appel-{billid}.wav",
            content_disposition_type="inline",
            background=BackgroundTask(lambda: audio_path.unlink(missing_ok=True)),
            headers={"Cache-Control": "private, no-store"},
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Préparation de l’enregistrement de l’appel %s impossible", billid)
        raise HTTPException(status_code=500, detail="Impossible de préparer l’écoute de cet appel.") from exc


@telephony_router.post("/settings")
async def telephony_save_settings(request: Request):
    _require_admin(request)
    try:
        payload = await request.json()
        settings = voip_server.save_settings(payload if isinstance(payload, dict) else {})
        apply = _apply_extension_change()
        return JSONResponse({"ok": True, "settings": settings, "server": voip_server.status(), "apply": apply})
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@telephony_router.post("/service/{action}")
async def telephony_service_action(action: str, request: Request):
    _require_admin(request)
    if action not in {"start", "stop", "restart"}:
        raise HTTPException(status_code=404, detail="Commande téléphonie inconnue.")
    try:
        result = getattr(voip_server, action)()
        return JSONResponse({"ok": True, "server": result})
    except Exception as exc:
        logger.exception("Commande Yate %s impossible", action)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@telephony_router.post("/installer")
async def telephony_launch_installer(request: Request):
    _require_admin(request)
    try:
        return JSONResponse({"ok": True, **voip_server.launch_bundled_installer()})
    except Exception as exc:
        logger.exception("Lancement de l’installateur Yate impossible")
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@telephony_router.post("/extensions")
async def telephony_create_extension(request: Request):
    _require_admin(request)
    _ensure_schema()
    try:
        payload = await request.json()
        extension = voip_server.save_extension(
            username=payload.get("username", ""),
            display_name=payload.get("display_name", ""),
            password=payload.get("password", ""),
            enabled=payload.get("enabled", True),
        )
        apply = _apply_extension_change()
        return JSONResponse({"ok": True, "extension": extension, "apply": apply})
    except Exception as exc:
        logger.warning("Création d’extension SIP refusée : %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@telephony_router.put("/extensions/{extension_id}")
async def telephony_update_extension(extension_id: int, request: Request):
    _require_admin(request)
    _ensure_schema()
    try:
        payload = await request.json()
        extension = voip_server.save_extension(
            username=payload.get("username", ""),
            display_name=payload.get("display_name", ""),
            password=payload.get("password") or None,
            enabled=payload.get("enabled", True),
            extension_id=extension_id,
        )
        apply = _apply_extension_change()
        return JSONResponse({"ok": True, "extension": extension, "apply": apply})
    except Exception as exc:
        logger.warning("Modification de l’extension SIP %s refusée : %s", extension_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@telephony_router.get("/extensions/{extension_id}/password")
async def telephony_reveal_extension_password(extension_id: int, request: Request):
    _require_admin(request)
    _ensure_schema()
    try:
        password = voip_server.get_extension_password(extension_id)
    except Exception as exc:
        logger.exception("Lecture explicite du mot de passe SIP impossible")
        raise HTTPException(status_code=500, detail="Impossible de lire le mot de passe enregistré.") from exc
    if password is None:
        raise HTTPException(status_code=404, detail="Extension introuvable.")
    return JSONResponse(
        {"password": password},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@telephony_router.delete("/extensions/{extension_id}")
async def telephony_delete_extension(extension_id: int, request: Request):
    _require_admin(request)
    _ensure_schema()
    if not voip_server.delete_extension(extension_id):
        raise HTTPException(status_code=404, detail="Extension introuvable.")
    apply = _apply_extension_change()
    return JSONResponse({"ok": True, "apply": apply})


@telephony_router.get("/log")
async def telephony_log(request: Request):
    _require_admin(request)
    path = voip_server.status()["log_path"]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-160:]
        return JSONResponse({"lines": "".join(lines)})
    except OSError:
        return JSONResponse({"lines": "Aucun journal Yate disponible pour le moment."})


def render_html(user=None) -> str:
    if not user or user.get("role") != "Administrateur":
        raise HTTPException(status_code=403, detail="La téléphonie IP est réservée au compte administrateur.")
    return r"""
<section class="space-y-6">
  <header class="flex flex-wrap items-center justify-between gap-4 rounded-[2rem] bg-gradient-to-r from-[#101b13] to-[#183b20] px-7 py-6 text-white shadow-lg">
    <div><p class="text-xs font-bold uppercase tracking-[.18em] text-lime-300"><i class="ph-bold ph-phone-call mr-1"></i>Réseau local · Téléphonie</p><h1 class="mt-1 text-2xl font-extrabold">Téléphonie IP</h1><p class="mt-1 text-sm text-slate-300">Serveur SIP Yate hébergé et piloté sur ce PC par Cliniva.</p></div>
    <div class="flex flex-wrap items-center gap-2"><button type="button" onclick="clinivaVoipOpenSettings()" class="rounded-xl border border-white/20 bg-white/10 px-4 py-2.5 text-sm font-bold text-white hover:bg-white/20"><i class="ph-bold ph-gear-six mr-1"></i>Configuration SIP</button><button type="button" onclick="clinivaVoipRefresh()" class="rounded-xl bg-lime-400 px-4 py-2.5 text-sm font-extrabold text-slate-900 hover:bg-lime-300"><i class="ph-bold ph-arrows-clockwise mr-1"></i>Actualiser</button></div>
  </header>
  <div id="voipBrowserWarning" class="hidden rounded-2xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900"><b>Ce service tourne sur le PC qui héberge Cliniva.</b> La configuration doit être faite depuis Cliniva Desktop sur ce poste.</div>
  <div id="voipMessage" class="hidden rounded-2xl border p-4 text-sm"></div>
  <div class="grid gap-4 xl:grid-cols-[minmax(0,1.5fr)_minmax(300px,.8fr)]">
    <article class="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm sm:p-6">
      <div class="flex flex-wrap items-start justify-between gap-4"><div><p class="text-xs font-extrabold uppercase tracking-widest text-emerald-700">PBX local</p><h2 class="mt-1 text-xl font-extrabold text-slate-900">État du serveur SIP</h2><p class="mt-1 text-sm text-slate-500">Les postes SIP s’enregistrent avec l’adresse de ce PC et le port configuré.</p></div><span id="voipStatusBadge" class="rounded-full bg-slate-100 px-3 py-1.5 text-xs font-extrabold text-slate-600">Vérification…</span></div>
      <div class="mt-5 grid gap-3 sm:grid-cols-3"><div class="rounded-2xl bg-slate-50 p-4"><p class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Adresse d’écoute</p><p id="voipListen" class="mt-1 break-all font-mono text-sm font-extrabold text-slate-800">—</p></div><div class="rounded-2xl bg-slate-50 p-4"><p class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Domaine SIP</p><p id="voipDomain" class="mt-1 font-mono text-sm font-extrabold text-slate-800">—</p></div><div class="rounded-2xl bg-slate-50 p-4"><p class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Processus local</p><p id="voipProcess" class="mt-1 font-mono text-sm font-extrabold text-slate-800">—</p></div></div>
      <div class="mt-5 flex flex-wrap gap-2"><button id="voipStart" type="button" onclick="clinivaVoipService('start')" class="rounded-xl bg-emerald-600 px-4 py-2.5 text-sm font-extrabold text-white hover:bg-emerald-700"><i class="ph-bold ph-play mr-1"></i>Démarrer</button><button id="voipRestart" type="button" onclick="clinivaVoipService('restart')" class="rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm font-bold text-amber-800 hover:bg-amber-100"><i class="ph-bold ph-arrows-clockwise mr-1"></i>Recharger la configuration</button><button id="voipStop" type="button" onclick="clinivaVoipService('stop')" class="rounded-xl bg-rose-600 px-4 py-2.5 text-sm font-bold text-white hover:bg-rose-700"><i class="ph-bold ph-stop mr-1"></i>Arrêter</button><button type="button" onclick="clinivaVoipShowLog()" class="rounded-xl border border-slate-200 px-4 py-2.5 text-sm font-bold text-slate-700 hover:bg-slate-50"><i class="ph-bold ph-terminal-window mr-1"></i>Journal</button></div>
      <div id="voipEngineInfo" class="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs leading-relaxed text-amber-900">Recherche du moteur Yate…</div>
    </article>
    <article class="rounded-3xl border border-emerald-100 bg-gradient-to-br from-emerald-50 to-white p-5 shadow-sm sm:p-6"><div class="flex items-center gap-3"><span class="grid h-11 w-11 place-items-center rounded-2xl bg-emerald-700 text-white"><i class="ph-bold ph-info text-xl"></i></span><div><p class="text-xs font-extrabold uppercase tracking-widest text-emerald-800">Paramètres des postes</p><h2 class="font-extrabold text-slate-900">Connexion SIP</h2></div></div><dl class="mt-5 space-y-3 text-sm"><div><dt class="text-xs font-semibold text-slate-500">Serveur / proxy</dt><dd id="voipPhoneHost" class="mt-0.5 break-all font-mono font-bold text-slate-800">Adresse IP de ce PC</dd></div><div><dt class="text-xs font-semibold text-slate-500">Port</dt><dd id="voipPhonePort" class="mt-0.5 font-mono font-bold text-slate-800">5060 / UDP</dd></div><div><dt class="text-xs font-semibold text-slate-500">Identifiant</dt><dd class="mt-0.5 font-bold text-slate-800">Numéro d’extension</dd></div><div><dt class="text-xs font-semibold text-slate-500">Transport</dt><dd class="mt-0.5 font-bold text-slate-800">UDP sur le réseau local</dd></div></dl><p class="mt-4 rounded-xl bg-white/80 p-3 text-[11px] leading-relaxed text-slate-600">Les téléphones et ce PC doivent être sur le même réseau local. Le port SIP doit être autorisé dans le pare-feu Windows.</p></article>
  </div>
  <article class="overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm"><div class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-4 sm:px-6"><div><p class="text-xs font-extrabold uppercase tracking-widest text-emerald-700">Comptes SIP</p><h2 class="mt-1 text-lg font-extrabold text-slate-900">Extensions téléphoniques</h2></div><button type="button" onclick="clinivaVoipOpenExtension()" class="rounded-xl bg-emerald-700 px-4 py-2.5 text-sm font-extrabold text-white shadow hover:bg-emerald-800"><i class="ph-bold ph-plus mr-1"></i>Nouvelle extension</button></div><div class="overflow-x-auto p-3 sm:p-5"><table class="w-full min-w-[620px] border-separate border-spacing-y-2 text-left text-sm"><thead><tr class="text-[10px] font-extrabold uppercase tracking-wider text-slate-500"><th class="px-3 py-2">Extension</th><th class="px-3 py-2">Nom affiché</th><th class="px-3 py-2">État</th><th class="px-3 py-2 text-right">Actions</th></tr></thead><tbody id="voipExtensions"><tr><td colspan="4" class="rounded-xl bg-slate-50 p-8 text-center text-slate-400">Chargement des extensions…</td></tr></tbody></table></div></article>
  <article class="overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm"><div class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-4 sm:px-6"><div><p class="text-xs font-extrabold uppercase tracking-widest text-sky-700">Présence SIP</p><h2 class="mt-1 text-lg font-extrabold text-slate-900">Postes connectés en direct</h2><p id="voipRefreshStamp" class="mt-1 text-xs text-slate-500">Actualisation automatique toutes les 5 secondes…</p></div><span id="voipClientsCount" class="rounded-full bg-slate-100 px-3 py-1.5 text-xs font-extrabold text-slate-600">0 connecté</span></div><div class="overflow-x-auto p-3 sm:p-5"><table class="w-full min-w-[620px] border-separate border-spacing-y-2 text-left text-sm"><thead><tr class="text-[10px] font-extrabold uppercase tracking-wider text-slate-500"><th class="px-3 py-2">Extension</th><th class="px-3 py-2">Nom</th><th class="px-3 py-2">Adresse enregistrée</th><th class="px-3 py-2">État</th></tr></thead><tbody id="voipClients"><tr><td colspan="4" class="rounded-xl bg-slate-50 p-6 text-center text-slate-400">Lecture des enregistrements SIP…</td></tr></tbody></table></div></article>
  <article class="overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm"><div class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-4 sm:px-6"><div><p class="text-xs font-extrabold uppercase tracking-widest text-indigo-700">Supervision des appels</p><h2 class="mt-1 text-lg font-extrabold text-slate-900">Appels en cours et journal CDR</h2><p class="mt-1 text-xs text-slate-500">État direct Yate et historique des communications terminées.</p></div><div class="flex items-center gap-2"><span id="voipActiveCallsCount" class="rounded-full bg-indigo-50 px-3 py-1.5 text-xs font-extrabold text-indigo-800">0 appel actif</span><button type="button" onclick="clinivaVoipRefreshCalls()" class="rounded-lg border border-slate-200 px-3 py-2 text-xs font-bold text-slate-700 hover:bg-slate-50"><i class="ph-bold ph-arrows-clockwise mr-1"></i>Actualiser</button></div></div><div class="overflow-x-auto p-3 sm:p-5"><table class="w-full min-w-[720px] border-separate border-spacing-y-2 text-left text-sm"><thead><tr class="text-[10px] font-extrabold uppercase tracking-wider text-slate-500"><th class="px-3 py-2">État</th><th class="px-3 py-2">Appelant</th><th class="px-3 py-2">Destinataire</th><th class="px-3 py-2">Référence</th><th class="px-3 py-2">Durée (s)</th></tr></thead><tbody id="voipLiveCalls"><tr><td colspan="5" class="rounded-xl bg-slate-50 p-6 text-center text-slate-400">Lecture des appels…</td></tr></tbody></table><h3 class="mb-2 mt-5 text-sm font-extrabold text-slate-800">Dernières communications</h3><table class="w-full min-w-[980px] border-separate border-spacing-y-2 text-left text-xs"><thead><tr class="text-[10px] font-extrabold uppercase tracking-wider text-slate-500"><th class="px-3 py-2">Date</th><th class="px-3 py-2">Sens</th><th class="px-3 py-2">Appelant</th><th class="px-3 py-2">Destinataire</th><th class="px-3 py-2">État</th><th class="px-3 py-2">Durée facturée</th><th class="px-3 py-2">Écoute</th></tr></thead><tbody id="voipRecentCalls"><tr><td colspan="7" class="rounded-xl bg-slate-50 p-5 text-center text-slate-400">Chargement du journal…</td></tr></tbody></table><p class="mt-3 rounded-xl border border-amber-100 bg-amber-50 p-3 text-xs leading-relaxed text-amber-900">Les appels répondus sont enregistrés en deux pistes audio. La lecture est disponible après la fin de l’appel et affiche les numéros du journal CDR. Informe les interlocuteurs de l’enregistrement et applique les règles de conservation de ta clinique.</p></div></article>
  <div id="voipSettingsModal" class="fixed inset-0 z-[150] hidden items-center justify-center bg-slate-950/60 p-4" onclick="if(event.target===this)clinivaVoipCloseSettings()"><section class="max-h-[92vh] w-full max-w-2xl overflow-y-auto rounded-3xl bg-white p-5 shadow-2xl sm:p-7" role="dialog" aria-modal="true" aria-label="Configuration SIP"><div class="mb-5 flex items-start justify-between"><div><p class="text-xs font-extrabold uppercase tracking-widest text-emerald-700">Serveur local Cliniva</p><h2 class="mt-1 text-xl font-extrabold text-slate-900">Configuration SIP</h2><p class="mt-1 text-sm text-slate-500">Le moteur tourne sur ce PC, hors de toute VM.</p></div><button type="button" onclick="clinivaVoipCloseSettings()" class="rounded-xl p-2 text-slate-500 hover:bg-slate-100" aria-label="Fermer"><i class="ph-bold ph-x text-xl"></i></button></div><form id="voipSettingsForm" class="grid gap-4 sm:grid-cols-2"><label class="text-xs font-semibold text-slate-600 sm:col-span-2">Exécutable Yate<input id="voipExecutable" placeholder="C:\Program Files\Yate\yate-console.exe" class="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 font-mono text-xs outline-none focus:ring-2 focus:ring-emerald-200"><span class="mt-1 block text-[11px] font-normal text-slate-500">Détecté automatiquement dans le dossier d’installation standard si ce champ reste vide.</span></label><label class="text-xs font-semibold text-slate-600">Adresse d’écoute<input id="voipBindHost" value="0.0.0.0" class="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 font-mono text-sm outline-none focus:ring-2 focus:ring-emerald-200"><span class="mt-1 block text-[11px] font-normal text-slate-500">0.0.0.0 accepte les téléphones du réseau local.</span></label><label class="text-xs font-semibold text-slate-600">Port UDP<input id="voipBindPort" type="number" min="1" max="65535" value="5060" class="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 font-mono text-sm outline-none focus:ring-2 focus:ring-emerald-200"></label><label class="text-xs font-semibold text-slate-600 sm:col-span-2">Domaine SIP<input id="voipDomainInput" value="cliniva.local" class="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 font-mono text-sm outline-none focus:ring-2 focus:ring-emerald-200"></label><label class="flex cursor-pointer items-start gap-3 rounded-2xl border border-emerald-100 bg-emerald-50 p-4 sm:col-span-2"><input id="voipAutoStart" type="checkbox" class="mt-0.5 h-4 w-4 accent-emerald-700"><span><b class="block text-sm text-slate-800">Démarrer Yate automatiquement avec Cliniva</b><span class="mt-1 block text-xs font-normal text-slate-600">Le serveur ne démarre automatiquement que sur ce poste, si Yate est installé et si cette option est activée.</span></span></label><label class="flex cursor-pointer items-start gap-3 rounded-2xl border border-indigo-100 bg-indigo-50 p-4 sm:col-span-2"><input id="voipEnableTransfer" type="checkbox" checked class="mt-0.5 h-4 w-4 accent-indigo-700"><span><b class="block text-sm text-slate-800">Autoriser le transfert SIP (REFER)</b><span class="mt-1 block text-xs font-normal text-slate-600">Permet aux téléphones compatibles de transférer un appel.</span></span></label><label class="flex cursor-pointer items-start gap-3 rounded-2xl border border-rose-100 bg-rose-50 p-4 sm:col-span-2"><input id="voipRecordCalls" type="checkbox" checked class="mt-0.5 h-4 w-4 accent-rose-700"><span><b class="block text-sm text-slate-800">Enregistrer les appels répondus</b><span class="mt-1 block text-xs font-normal text-slate-600">Cliniva conserve les deux pistes audio localement pour permettre l'écoute après raccrochage. Informe les interlocuteurs et définis une durée de conservation adaptée.</span></span></label><div class="flex justify-end gap-2 border-t border-slate-100 pt-4 sm:col-span-2"><button type="button" onclick="clinivaVoipCloseSettings()" class="rounded-xl border border-slate-200 px-4 py-2.5 text-sm font-bold text-slate-600">Annuler</button><button type="submit" class="rounded-xl bg-emerald-700 px-5 py-2.5 text-sm font-extrabold text-white"><i class="ph-bold ph-floppy-disk mr-1"></i>Enregistrer</button></div></form></section></div>
  <div id="voipExtensionModal" class="fixed inset-0 z-[150] hidden items-center justify-center bg-slate-950/60 p-4" onclick="if(event.target===this)clinivaVoipCloseExtension()"><section class="w-full max-w-lg rounded-3xl bg-white p-5 shadow-2xl sm:p-7" role="dialog" aria-modal="true" aria-label="Extension SIP"><div class="mb-5 flex items-start justify-between"><div><p class="text-xs font-extrabold uppercase tracking-widest text-emerald-700">Compte téléphonique local</p><h2 id="voipExtensionTitle" class="mt-1 text-xl font-extrabold text-slate-900">Nouvelle extension</h2></div><button type="button" onclick="clinivaVoipCloseExtension()" class="rounded-xl p-2 text-slate-500 hover:bg-slate-100" aria-label="Fermer"><i class="ph-bold ph-x text-xl"></i></button></div><form id="voipExtensionForm" class="grid gap-4"><input id="voipExtensionId" type="hidden"><label class="text-xs font-semibold text-slate-600">Numéro d’extension<input id="voipExtensionNumber" inputmode="numeric" pattern="[0-9]{2,12}" maxlength="12" required placeholder="Ex. 201" class="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 font-mono text-sm outline-none focus:ring-2 focus:ring-emerald-200"></label><label class="text-xs font-semibold text-slate-600">Nom affiché<input id="voipExtensionName" maxlength="120" required placeholder="Ex. Accueil" class="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm outline-none focus:ring-2 focus:ring-emerald-200"></label><label id="voipPasswordLabel" class="text-xs font-semibold text-slate-600">Mot de passe SIP<input id="voipExtensionPassword" type="password" minlength="12" maxlength="128" autocomplete="new-password" class="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 font-mono text-sm outline-none focus:ring-2 focus:ring-emerald-200"><span class="mt-1 block text-[11px] font-normal text-slate-500">12 caractères minimum. Il est chiffré avec Windows DPAPI et ne sera pas réaffiché.</span></label><div class="flex justify-between"><button type="button" onclick="clinivaVoipGeneratePassword()" class="text-xs font-bold text-emerald-700 hover:underline"><i class="ph-bold ph-key mr-1"></i>Générer un mot de passe sécurisé</button><label class="flex items-center gap-2 text-xs font-semibold text-slate-600"><input id="voipExtensionEnabled" type="checkbox" checked class="h-4 w-4 accent-emerald-700">Activée</label></div><div class="flex justify-end gap-2 border-t border-slate-100 pt-4"><button type="button" onclick="clinivaVoipCloseExtension()" class="rounded-xl border border-slate-200 px-4 py-2.5 text-sm font-bold text-slate-600">Annuler</button><button type="submit" class="rounded-xl bg-emerald-700 px-5 py-2.5 text-sm font-extrabold text-white"><i class="ph-bold ph-floppy-disk mr-1"></i>Enregistrer l’extension</button></div></form></section></div>
  <div id="voipLogModal" class="fixed inset-0 z-[151] hidden items-center justify-center bg-slate-950/70 p-4" onclick="if(event.target===this)clinivaVoipCloseLog()"><section class="flex max-h-[90vh] w-full max-w-5xl flex-col overflow-hidden rounded-3xl bg-slate-950 text-white shadow-2xl" role="dialog" aria-modal="true" aria-label="Journal Yate"><header class="flex items-center justify-between border-b border-white/10 px-5 py-4"><div><p class="text-xs font-bold uppercase tracking-widest text-lime-300">Diagnostic du serveur</p><h2 class="font-extrabold">Journal Yate</h2></div><button type="button" onclick="clinivaVoipCloseLog()" class="rounded-lg p-2 text-slate-300 hover:bg-white/10" aria-label="Fermer"><i class="ph-bold ph-x text-xl"></i></button></header><pre id="voipLogContent" class="min-h-[300px] overflow-auto whitespace-pre-wrap p-5 font-mono text-xs leading-relaxed text-emerald-100">Chargement…</pre></section></div>
</section>
<script>
(() => {
  const $ = id => document.getElementById(id);
  let voipSettings = {};
  let voipExtensions = [];
  let voipLiveTimer = null;
  let voipLiveRefreshBusy = false;
  let voipSettingsDraftReady = false;
  function message(text, error=false){const el=$('voipMessage');el.textContent=text;el.className=`rounded-2xl border p-4 text-sm ${error?'border-rose-200 bg-rose-50 text-rose-800':'border-emerald-200 bg-emerald-50 text-emerald-800'}`;el.classList.remove('hidden');setTimeout(()=>el.classList.add('hidden'),7000);}
  async function api(url,options={}){const r=await fetch(url,{credentials:'same-origin',...options,headers:{...(options.body?{'Content-Type':'application/json'}:{}),...(options.headers||{})}});let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.detail||`Erreur HTTP ${r.status}`);return d;}
  function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  function openModal(id){$(id).classList.remove('hidden');$(id).classList.add('flex');}
  function closeModal(id){$(id).classList.add('hidden');$(id).classList.remove('flex');}
  window.clinivaVoipCloseSettings=()=>closeModal('voipSettingsModal');
  window.clinivaVoipCloseExtension=()=>closeModal('voipExtensionModal');
  window.clinivaVoipCloseLog=()=>closeModal('voipLogModal');
  // Keep the configuration form open when the backdrop is clicked and preserve
  // its draft when it is closed/reopened before saving.
  $('voipSettingsModal').onclick=null;
  window.clinivaVoipOpenSettings=()=>{if(!voipSettingsDraftReady){const s=voipSettings;$('voipExecutable').value=s.sip_server_executable||'';$('voipBindHost').value=s.sip_bind_host||'0.0.0.0';$('voipBindPort').value=s.sip_bind_port||5060;$('voipDomainInput').value=s.sip_domain||'cliniva.local';$('voipAutoStart').checked=!!s.sip_auto_start;$('voipEnableTransfer').checked=s.sip_enable_transfer!==false;$('voipRecordCalls').checked=s.sip_record_calls!==false;voipSettingsDraftReady=true;}openModal('voipSettingsModal');};
  window.clinivaVoipGeneratePassword=()=>{const bytes=new Uint8Array(18);crypto.getRandomValues(bytes);const alphabet='ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%*-_';$('voipExtensionPassword').value=Array.from(bytes,b=>alphabet[b%alphabet.length]).join('');$('voipExtensionPassword').type='text';};
  const revealPasswordButton=document.createElement('button');revealPasswordButton.type='button';revealPasswordButton.className='-mt-2 justify-self-start rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-50';revealPasswordButton.textContent='Afficher le mot de passe enregistré';$('voipPasswordLabel').insertAdjacentElement('afterend',revealPasswordButton);
  revealPasswordButton.addEventListener('click',async()=>{const id=$('voipExtensionId').value;if(!id)return;const field=$('voipExtensionPassword');if(field.dataset.revealed==='1'){field.type='password';field.dataset.revealed='0';revealPasswordButton.textContent='Afficher le mot de passe enregistré';return;}try{const data=await api(`/api/telephonie/extensions/${id}/password`);field.value=data.password;field.type='text';field.dataset.revealed='1';revealPasswordButton.textContent='Masquer le mot de passe';}catch(e){message(e.message,true);}});
  window.clinivaVoipOpenExtension=(id=null)=>{const x=voipExtensions.find(v=>v.id===id);$('voipExtensionForm').reset();$('voipExtensionId').value=x?.id||'';$('voipExtensionNumber').value=x?.username||'';$('voipExtensionNumber').disabled=!!x;$('voipExtensionName').value=x?.display_name||'';$('voipExtensionEnabled').checked=x?.enabled!==false;$('voipExtensionPassword').value='';$('voipExtensionPassword').type='password';$('voipExtensionPassword').dataset.revealed='0';revealPasswordButton.hidden=!x;$('voipExtensionPassword').required=!x;$('voipPasswordLabel').querySelector('span').textContent=x?'Mot de passe enregistré chiffré. Affiche-le à la demande ou saisis-en un nouveau.':'12 caractères minimum. Il est chiffré avec Windows DPAPI.';$('voipExtensionTitle').textContent=x?'Modifier l’extension':'Nouvelle extension';openModal('voipExtensionModal');};
  window.clinivaVoipService=async action=>{if(action!=='start'&&!confirm(action==='stop'?'Arrêter le serveur SIP local ?':'Arrêter le moteur actif puis relancer le serveur géré par Cliniva avec les extensions et mots de passe enregistrés ? Les appels actifs seront interrompus.'))return;try{await api(`/api/telephonie/service/${action}`,{method:'POST'});message(action==='start'?'Serveur SIP Cliniva démarré sur ce PC.':action==='stop'?'Serveur SIP arrêté.':'Serveur Cliniva relancé avec la configuration actuelle.');await window.clinivaVoipRefresh();}catch(e){message(e.message,true);}};
  window.clinivaVoipRefresh=async()=>{try{const d=await api('/api/telephonie/status');voipSettings=d.server;voipExtensions=d.extensions||[];const s=d.server;const badge=$('voipStatusBadge');badge.textContent=s.managed_by==='service_windows'?'Service Windows actif':s.running?'Serveur Cliniva actif':s.engine_available?'Arrêté':'Moteur non installé';badge.className=`rounded-full px-3 py-1.5 text-xs font-extrabold ${s.running?'bg-emerald-100 text-emerald-800':s.engine_available?'bg-slate-100 text-slate-600':'bg-amber-100 text-amber-800'}`;$('voipListen').textContent=`${s.bind_host}:${s.port} / UDP`;$('voipDomain').textContent=s.domain;$('voipProcess').textContent=s.running?`PID ${s.pid} · ${s.managed_by==='service_windows'?'Service Windows':'Cliniva'}`:'Non démarré';$('voipPhoneHost').textContent=s.bind_host==='0.0.0.0'?'Adresse IP locale de ce PC':s.bind_host;$('voipPhonePort').textContent=`${s.port} / UDP`;$('voipStart').classList.toggle('hidden',s.running);$('voipStop').classList.toggle('hidden',!s.running);$('voipRestart').classList.toggle('hidden',!s.running);$('voipEngineInfo').innerHTML=s.engine_available?`Moteur détecté : <code>${esc(s.executable)}</code>${s.error?' · '+esc(s.error):''}`:`<span>Le dossier <code>Sip_app</code> contient l’installateur Yate, mais le moteur n’est pas encore installé. Lance l’installation, puis actualise cet écran.</span>${s.installer_available?'<button type="button" onclick="clinivaVoipInstallEngine()" class="ml-3 mt-2 rounded-lg bg-amber-700 px-3 py-2 font-extrabold text-white hover:bg-amber-800"><i class="ph-bold ph-download-simple mr-1"></i>Lancer l’installation Yate</button>':''}`;$('voipEngineInfo').className=`mt-4 rounded-xl border p-3 text-xs leading-relaxed ${s.engine_available?'border-emerald-200 bg-emerald-50 text-emerald-900':'border-amber-200 bg-amber-50 text-amber-900'}`;const body=$('voipExtensions');body.innerHTML=voipExtensions.length?voipExtensions.map(x=>`<tr class="bg-slate-50"><td class="rounded-l-xl px-3 py-3 font-mono font-extrabold text-slate-900">${esc(x.username)}</td><td class="px-3 py-3 font-semibold text-slate-700">${esc(x.display_name)}</td><td class="px-3 py-3"><span class="rounded-full px-2.5 py-1 text-[10px] font-extrabold ${x.enabled?'bg-emerald-100 text-emerald-800':'bg-slate-200 text-slate-600'}">${x.enabled?'Activée':'Désactivée'}</span></td><td class="rounded-r-xl px-3 py-2 text-right"><button onclick="clinivaVoipOpenExtension(${Number(x.id)})" title="Modifier" class="rounded-lg p-2 text-indigo-600 hover:bg-indigo-100"><i class="ph-bold ph-pencil-simple"></i></button><button onclick="clinivaVoipDeleteExtension(${Number(x.id)})" title="Supprimer" class="rounded-lg p-2 text-rose-600 hover:bg-rose-100"><i class="ph-bold ph-trash"></i></button></td></tr>`).join(''):'<tr><td colspan="4" class="rounded-xl bg-slate-50 p-8 text-center text-slate-400">Aucune extension créée.</td></tr>';await window.clinivaVoipRefreshClients();await window.clinivaVoipRefreshCalls();}catch(e){message(e.message,true);}};
  window.clinivaVoipRefreshClients=async()=>{const table=$('voipClients');if(!table)return;try{const d=await api('/api/telephonie/clients');const clients=d.clients||[];$('voipClientsCount').textContent=d.available?`${clients.length} connecté${clients.length===1?'':'s'}`:'État indisponible';table.innerHTML=d.available?(clients.length?clients.map(c=>`<tr class="bg-slate-50"><td class="rounded-l-xl px-3 py-3 font-mono font-extrabold text-slate-900">${esc(c.username)}</td><td class="px-3 py-3 font-semibold text-slate-700">${esc(c.display_name||'—')}</td><td class="px-3 py-3 font-mono text-xs text-slate-600">${esc(c.contact)}</td><td class="rounded-r-xl px-3 py-3"><span class="rounded-full bg-emerald-100 px-2.5 py-1 text-[10px] font-extrabold text-emerald-800">Connecté</span></td></tr>`).join(''):'<tr><td colspan="4" class="rounded-xl bg-emerald-50 p-6 text-center font-semibold text-emerald-800">Aucun poste SIP enregistré actuellement.</td></tr>'):`<tr><td colspan="4" class="rounded-xl bg-amber-50 p-6 text-center text-amber-800">${esc(d.error||'Démarre le serveur Yate depuis Cliniva pour activer le suivi des postes.')}</td></tr>`;}catch(e){$('voipClientsCount').textContent='État indisponible';table.innerHTML=`<tr><td colspan="4" class="rounded-xl bg-rose-50 p-6 text-center text-rose-800">${esc(e.message)}</td></tr>`;}}
  window.clinivaVoipRefreshCalls=async()=>{const live=$('voipLiveCalls');if(!live)return;try{const [d,h]=await Promise.all([api('/api/telephonie/calls/live'),api('/api/telephonie/calls/recent')]);const calls=d.calls||[];const activeCount=new Set(calls.map(c=>c.billid||c.channel)).size;$('voipActiveCallsCount').textContent=`${activeCount} appel${activeCount===1?'':'s'} actif${activeCount===1?'':'s'}`;live.innerHTML=d.available?(calls.length?calls.map(c=>`<tr class="bg-indigo-50"><td class="rounded-l-xl px-3 py-3 font-bold text-indigo-800">${esc(c.status)}</td><td class="px-3 py-3 font-mono font-bold">${esc(c.caller||'—')}</td><td class="px-3 py-3 font-mono font-bold">${esc(c.called||'—')}</td><td class="px-3 py-3 font-mono text-xs">${esc(c.billid)}</td><td class="rounded-r-xl px-3 py-3">${esc(c.duration||'0')}</td></tr>`).join(''):'<tr><td colspan="5" class="rounded-xl bg-emerald-50 p-5 text-center font-semibold text-emerald-800">Aucun appel actif.</td></tr>'):`<tr><td colspan="5" class="rounded-xl bg-amber-50 p-5 text-center text-amber-800">${esc(d.error||'Module cdrbuild indisponible.')}</td></tr>`;const hist=$('voipRecentCalls');hist.innerHTML=h.available?(h.records.length?h.records.map(c=>`<tr class="bg-slate-50"><td class="rounded-l-xl px-3 py-2">${esc(c.timestamp)}</td><td class="px-3 py-2">${esc(c.direction)}</td><td class="px-3 py-2 font-mono">${esc(c.caller)}</td><td class="px-3 py-2 font-mono">${esc(c.called)}</td><td class="px-3 py-2">${esc(c.status)}</td><td class="px-3 py-2">${esc(c.billtime)} s</td><td class="rounded-r-xl px-3 py-2">${c.recording_available?'<audio controls preload="none" class="h-9 max-w-[190px]" src="'+esc(c.recording_url)+'" aria-label="Lecture de l’appel"></audio>':'<span class="text-slate-400">Audio indisponible</span>'}</td></tr>`).join(''):'<tr><td colspan="7" class="rounded-xl bg-slate-50 p-5 text-center text-slate-500">Aucune communication terminée enregistrée.</td></tr>'):`<tr><td colspan="7" class="rounded-xl bg-amber-50 p-5 text-center text-amber-800">${esc(h.error)}</td></tr>`;}catch(e){live.innerHTML=`<tr><td colspan="5" class="rounded-xl bg-rose-50 p-5 text-center text-rose-800">${esc(e.message)}</td></tr>`;}};window.clinivaVoipRefreshLive=async()=>{if(voipLiveRefreshBusy||!$('voipClients'))return;voipLiveRefreshBusy=true;try{await Promise.all([window.clinivaVoipRefreshClients(),window.clinivaVoipRefreshCalls()]);const stamp=$('voipRefreshStamp');if(stamp)stamp.textContent=`Mis à jour automatiquement · ${new Date().toLocaleTimeString('fr-FR',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})} · prochaine vérification dans 5 s`;}finally{voipLiveRefreshBusy=false;}};window.clinivaVoipInstallEngine=async()=>{if(!confirm('Lancer le programme d’installation Yate fourni dans Sip_app ? Windows peut demander une confirmation administrateur.'))return;try{await api('/api/telephonie/installer',{method:'POST'});message('Installateur Yate lancé. Termine l’installation, puis actualise cet écran.');}catch(e){message(e.message,true);}};
  window.clinivaVoipDeleteExtension=async id=>{if(!confirm('Supprimer cette extension SIP ? Le serveur redémarrera automatiquement si Cliniva le pilote actuellement.'))return;try{const d=await api(`/api/telephonie/extensions/${id}`,{method:'DELETE'});message(d.apply?.message||'Extension supprimée.',!d.apply?.applied&&!d.apply?.queued);await window.clinivaVoipRefresh();}catch(e){message(e.message,true);}};
  window.clinivaVoipShowLog=async()=>{openModal('voipLogModal');$('voipLogContent').textContent='Chargement…';try{$('voipLogContent').textContent=(await api('/api/telephonie/log')).lines||'Journal vide.';}catch(e){$('voipLogContent').textContent=e.message;}};
  $('voipSettingsForm').addEventListener('submit',async e=>{e.preventDefault();try{const payload={sip_server_executable:$('voipExecutable').value,sip_bind_host:$('voipBindHost').value,sip_bind_port:$('voipBindPort').value,sip_domain:$('voipDomainInput').value,sip_auto_start:$('voipAutoStart').checked,sip_enable_transfer:$('voipEnableTransfer').checked,sip_record_calls:$('voipRecordCalls').checked};const d=await api('/api/telephonie/settings',{method:'POST',body:JSON.stringify(payload)});voipSettings=d.server;closeModal('voipSettingsModal');message(d.apply?.message||'Configuration SIP enregistrée.',!d.apply?.applied&&!d.apply?.queued);await window.clinivaVoipRefresh();}catch(e){message(e.message,true);}});
  $('voipExtensionForm').addEventListener('submit',async e=>{e.preventDefault();try{const id=$('voipExtensionId').value;const payload={username:$('voipExtensionNumber').value,display_name:$('voipExtensionName').value,password:$('voipExtensionPassword').value,enabled:$('voipExtensionEnabled').checked};const d=await api(id?`/api/telephonie/extensions/${id}`:'/api/telephonie/extensions',{method:id?'PUT':'POST',body:JSON.stringify(payload)});closeModal('voipExtensionModal');message(d.apply?.message||'Extension enregistrée.',!d.apply?.applied&&!d.apply?.queued);await window.clinivaVoipRefresh();}catch(e){message(e.message,true);}});
  window.clinivaVoipRefresh();
  if(voipLiveTimer)clearInterval(voipLiveTimer);
  window.clinivaVoipRefreshLive();
  voipLiveTimer=setInterval(()=>{if(!$('voipClients')){clearInterval(voipLiveTimer);voipLiveTimer=null;return;}window.clinivaVoipRefreshLive();},5000);
})();
</script>
"""
