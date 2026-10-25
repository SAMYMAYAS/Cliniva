"""Temporary, token-scoped phone capture for patient and passage documents."""

from __future__ import annotations

import base64
import io
import ipaddress
import secrets
import socket
import threading
import time
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.auth import get_current_user_optional

mobile_scan_router = APIRouter(tags=["Capture mobile des documents"])

SESSION_TTL_SECONDS = 20 * 60
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGES_PER_SESSION = 30
MAX_SESSION_BYTES = 60 * 1024 * 1024
_sessions: dict[str, dict[str, Any]] = {}
_sessions_lock = threading.Lock()


def _prune_sessions() -> None:
    now = time.time()
    with _sessions_lock:
        expired = [key for key, value in _sessions.items() if value["expires_at"] <= now]
        for key in expired:
            _sessions.pop(key, None)


def _session_for(token: str) -> dict[str, Any]:
    _prune_sessions()
    with _sessions_lock:
        session = _sessions.get(token)
        if not session or session["expires_at"] <= time.time():
            _sessions.pop(token, None)
            raise HTTPException(status_code=404, detail="Cette session de scan a expiré.")
        return session


def _lan_ipv4() -> str | None:
    """Find the IPv4 interface used by the server for LAN traffic."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))
        address = sock.getsockname()[0]
        if not ipaddress.ip_address(address).is_loopback:
            return address
    except OSError:
        pass
    finally:
        sock.close()

    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = result[4][0]
            parsed = ipaddress.ip_address(address)
            if not parsed.is_loopback and not parsed.is_link_local:
                return address
    except OSError:
        pass
    return None


def _phone_url(request: Request, token: str) -> str:
    hostname = request.url.hostname or "127.0.0.1"
    try:
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = hostname.lower() == "localhost"

    if is_loopback:
        hostname = _lan_ipv4() or hostname

    port = request.url.port
    netloc = f"{hostname}:{port}" if port else hostname
    return f"{request.url.scheme}://{netloc}/mobile-scan/{token}"


@mobile_scan_router.post("/api/mobile-scan/session")
async def create_mobile_scan_session(request: Request) -> JSONResponse:
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Connectez-vous pour lancer une session de scan.")

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Requête invalide.") from exc

    target = str(payload.get("target") or "").lower()
    if target not in {"patient", "passage"}:
        raise HTTPException(status_code=400, detail="Destination de scan invalide.")

    token = secrets.token_urlsafe(32)
    expires_at = time.time() + SESSION_TTL_SECONDS
    with _sessions_lock:
        _sessions[token] = {
            "target": target,
            "user_id": user.get("id"),
            "expires_at": expires_at,
            "uploads": [],
            "bytes": 0,
        }

    phone_url = _phone_url(request, token)
    try:
        import qrcode

        image = qrcode.make(phone_url)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        qr_data = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    except ImportError as exc:
        with _sessions_lock:
            _sessions.pop(token, None)
        raise HTTPException(
            status_code=503,
            detail="Le générateur QR manque sur le serveur. Installez les dépendances de pip.txt puis redémarrez Cliniva.",
        ) from exc

    return JSONResponse({
        "success": True,
        "token": token,
        "url": phone_url,
        "qr": qr_data,
        "target": target,
        "expires_in": SESSION_TTL_SECONDS,
    })


@mobile_scan_router.get("/mobile-scan/{token}", response_class=HTMLResponse)
async def mobile_scan_page(token: str) -> HTMLResponse:
    _session_for(token)
    safe_token = quote(token, safe="")
    page = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#10251b"><title>Scan mobile · Cliniva</title>
<style>
:root{color-scheme:light;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#f2f6f3;color:#14251b}*{box-sizing:border-box}body{margin:0;min-height:100vh;padding:20px 14px 32px}.card{max-width:520px;margin:0 auto;background:white;border:1px solid #e1eae3;border-radius:24px;padding:22px;box-shadow:0 18px 50px #12332214}.brand{font-weight:800;letter-spacing:.02em;color:#147442}.sub{color:#64746a;font-size:14px;line-height:1.5}.camera{display:block;width:100%;margin:22px 0 12px;padding:18px;border:0;border-radius:14px;background:#18844b;color:white;font-weight:800;font-size:16px;text-align:center}.camera input{display:none}.preview{display:none;width:100%;max-height:55vh;object-fit:contain;border-radius:14px;background:#101510}.actions{display:none;gap:10px;margin:12px 0}.actions button{flex:1;padding:14px;border:0;border-radius:12px;font-weight:800}.send{background:#173b28;color:white}.cancel{background:#e8eee9;color:#26372c}.status{min-height:24px;margin-top:12px;color:#53645a;font-size:14px}.count{display:inline-block;margin-top:10px;padding:6px 10px;border-radius:20px;background:#e9f6ec;color:#147442;font-weight:700;font-size:13px}.notice{padding:12px;border-radius:12px;background:#fff7df;color:#715516;font-size:13px}
</style></head><body><main class="card"><div class="brand">CLINIVA · SCAN MOBILE</div><p class="sub">Prenez une photo nette du document. Vérifiez l’aperçu, puis envoyez-le au formulaire Cliniva qui a affiché ce QR code. Cette session expire automatiquement.</p>
<label class="camera">📷 Prendre / choisir une photo<input id="capture" type="file" accept="image/*" capture="environment"></label>
<img id="preview" class="preview" alt="Aperçu du document à envoyer"><div id="actions" class="actions"><button id="cancel" class="cancel" type="button">Reprendre</button><button id="send" class="send" type="button">Valider et envoyer</button></div>
<div id="status" class="status" role="status">En attente d’une photo…</div><span id="count" class="count">0 document envoyé</span><p class="notice">Gardez cette page ouverte pendant le transfert. Le document apparaîtra dans le formulaire Cliniva correspondant.</p></main>
<script>
(()=>{const token="__TOKEN__",input=document.getElementById('capture'),preview=document.getElementById('preview'),actions=document.getElementById('actions'),status=document.getElementById('status'),count=document.getElementById('count');let selected=null,sent=0;
function clear(){selected=null;input.value='';preview.removeAttribute('src');preview.style.display='none';actions.style.display='none';status.textContent='En attente d’une photo…'}
input.addEventListener('change',()=>{selected=input.files&&input.files[0];if(!selected)return;if(!selected.type.startsWith('image/')){status.textContent='Choisissez une image.';clear();return}preview.src=URL.createObjectURL(selected);preview.style.display='block';actions.style.display='flex';status.textContent=selected.name+' · '+(selected.size/1048576).toLocaleString('fr-FR',{maximumFractionDigits:1})+' Mo'});
document.getElementById('cancel').onclick=clear;
document.getElementById('send').onclick=async()=>{if(!selected)return;const button=document.getElementById('send');button.disabled=true;status.textContent='Envoi en cours…';try{let file=selected;if(window.createImageBitmap){try{const bitmap=await createImageBitmap(file);const scale=Math.min(1,2400/bitmap.width,3200/bitmap.height),canvas=document.createElement('canvas');canvas.width=Math.round(bitmap.width*scale);canvas.height=Math.round(bitmap.height*scale);canvas.getContext('2d').drawImage(bitmap,0,0,canvas.width,canvas.height);const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',.88));bitmap.close();if(blob)file=blob}catch(error){if(file.size>12*1024*1024)throw new Error('Impossible de réduire cette image sur le téléphone. Choisissez une photo JPEG moins lourde.')}}if(!file||file.size>12*1024*1024)throw new Error('Image supérieure à 12 Mo. Choisissez une résolution plus basse.');
const response=await fetch('/api/mobile-scan/'+token+'/upload',{method:'POST',headers:{'Content-Type':file.type||'image/jpeg','X-File-Name':encodeURIComponent(selected.name)},body:file});const result=await response.json();if(!response.ok)throw new Error(result.detail||'Échec de l’envoi.');sent++;count.textContent=sent+' document'+(sent>1?'s':'')+' envoyé'+(sent>1?'s':'');status.textContent='Document transmis à Cliniva.';clear()}catch(error){status.textContent=error.message||'Erreur pendant l’envoi.'}finally{button.disabled=false}};
})();
</script></body></html>"""
    return HTMLResponse(page.replace("__TOKEN__", safe_token), headers={"Cache-Control": "no-store"})


@mobile_scan_router.post("/api/mobile-scan/{token}/upload")
async def upload_mobile_scan(token: str, request: Request) -> JSONResponse:
    session = _session_for(token)
    content_type = (request.headers.get("content-type") or "").split(";", 1)[0].lower()
    if content_type not in {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}:
        raise HTTPException(status_code=415, detail="Format refusé. Utilisez une photo JPEG, PNG ou WebP.")

    content_length = request.headers.get("content-length")
    try:
        if content_length and int(content_length) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="La photo dépasse la limite de 12 Mo.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Taille de fichier invalide.") from exc
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="La photo dépasse la limite de 12 Mo.")
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Photo vide ou supérieure à 12 Mo.")

    name = request.headers.get("x-file-name", "photo-mobile.jpg")
    name = name.replace("/", "_").replace("\\", "_")[:120] or "photo-mobile.jpg"
    with _sessions_lock:
        current = _sessions.get(token)
        if current is not session or current["expires_at"] <= time.time():
            raise HTTPException(status_code=404, detail="Cette session de scan a expiré.")
        if len(current["uploads"]) >= MAX_IMAGES_PER_SESSION:
            raise HTTPException(status_code=429, detail="La limite de 30 documents par session est atteinte.")
        if current["bytes"] + len(content) > MAX_SESSION_BYTES:
            raise HTTPException(status_code=413, detail="La limite de 60 Mo par session est atteinte.")
        current["uploads"].append({
            "name": name,
            "mime": content_type,
            "data": "data:" + content_type + ";base64," + base64.b64encode(content).decode("ascii"),
        })
        current["bytes"] += len(content)
        index = len(current["uploads"])
    return JSONResponse({"success": True, "index": index, "message": "Document reçu par Cliniva."})


@mobile_scan_router.get("/api/mobile-scan/{token}/uploads")
async def list_mobile_scans(token: str, request: Request) -> JSONResponse:
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session Cliniva expirée.")
    session = _session_for(token)
    if session["user_id"] != user.get("id"):
        raise HTTPException(status_code=403, detail="Cette session de scan appartient à un autre utilisateur.")
    requested_target = str(request.query_params.get("target") or "").lower()
    if requested_target != session["target"]:
        raise HTTPException(status_code=403, detail="La destination de cette session de scan ne correspond pas au formulaire.")
    try:
        after = max(0, int(request.query_params.get("after", "0")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Position invalide.") from exc
    with _sessions_lock:
        uploads = session["uploads"][after:]
        total = len(session["uploads"])
        return JSONResponse({"uploads": uploads, "cursor": total, "expires_in": max(0, int(session["expires_at"] - time.time()))})


@mobile_scan_router.delete("/api/mobile-scan/{token}")
async def close_mobile_scan_session(token: str, request: Request) -> JSONResponse:
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session Cliniva expirée.")
    with _sessions_lock:
        session = _sessions.get(token)
        if session and session["user_id"] != user.get("id"):
            raise HTTPException(status_code=403, detail="Cette session de scan appartient à un autre utilisateur.")
        _sessions.pop(token, None)
    return JSONResponse({"success": True})
