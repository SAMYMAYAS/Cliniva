"""Configuration des courriels et notifications Cliniva."""

import asyncio
import re

from fastapi import APIRouter, HTTPException, Request
from app.auth import get_current_user, track_action
from app.smtp_mailer import encrypt_smtp_password, get_smtp_configuration, send_smtp_email

notification_config_router = APIRouter(prefix="/api/configuration-notif", tags=["Configuration des notifications"])


def _admin(request: Request):
    user = get_current_user(request)
    if user.get("role") != "Administrateur":
        raise HTTPException(status_code=403, detail="Cette configuration est réservée à l’administrateur.")
    return user


def _safe_settings():
    from main import load_app_configuration
    settings = load_app_configuration().get("notification_smtp") or {}
    return {
        "enabled": bool(settings.get("enabled", False)),
        "host": str(settings.get("host") or ""),
        "port": int(settings.get("port") or 587),
        "user": str(settings.get("user") or ""),
        "from": str(settings.get("from") or ""),
        "starttls": bool(settings.get("starttls", True)),
        "ssl": bool(settings.get("ssl", False)),
        "password_configured": bool(settings.get("password_ciphertext")),
    }


@notification_config_router.get("/settings")
def notification_settings_get(request: Request):
    _admin(request)
    return _safe_settings()


@notification_config_router.post("/settings")
async def notification_settings_save(request: Request):
    user = _admin(request)
    data = await request.json()
    host = str(data.get("host") or "").strip()
    sender = str(data.get("from") or "").strip()
    username = str(data.get("user") or "").strip()
    try:
        port = int(data.get("port") or 587)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Le port SMTP doit être un nombre.") from exc
    if not 1 <= port <= 65535:
        raise HTTPException(status_code=400, detail="Le port SMTP doit être compris entre 1 et 65535.")
    if sender and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", sender):
        raise HTTPException(status_code=400, detail="L’adresse d’expédition est invalide.")
    if data.get("enabled") and (not host or not sender):
        raise HTTPException(status_code=400, detail="Renseigne le serveur SMTP et l’adresse d’expédition avant d’activer l’envoi.")
    from main import load_app_configuration, _persist_config_updates
    current = load_app_configuration().get("notification_smtp") or {}
    password = str(data.get("password") or "")
    ciphertext = "" if data.get("clear_password") else str(current.get("password_ciphertext") or "")
    if password:
        try:
            ciphertext = encrypt_smtp_password(password)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    if data.get("enabled") and username and not ciphertext:
        raise HTTPException(status_code=400, detail="Le mot de passe SMTP est requis pour ce compte authentifié.")
    saved = {
        "enabled": bool(data.get("enabled")), "host": host, "port": port,
        "user": username, "from": sender,
        "starttls": bool(data.get("starttls", True)),
        "ssl": bool(data.get("ssl", False)),
        "password_ciphertext": ciphertext,
    }
    if saved["ssl"]:
        saved["starttls"] = False
    _persist_config_updates({"notification_smtp": saved})
    track_action(request, "NOTIFICATION_SMTP_SAVE", "Configuration SMTP des notifications mise à jour")
    return {"success": True, "settings": _safe_settings()}


@notification_config_router.post("/test")
async def notification_settings_test(request: Request):
    user = _admin(request)
    data = await request.json()
    recipient = str(data.get("email") or "").strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", recipient):
        raise HTTPException(status_code=400, detail="Saisis une adresse de destination valide pour le test.")
    configuration = get_smtp_configuration()
    if not configuration.get("enabled"):
        raise HTTPException(status_code=400, detail="Enregistre et active d’abord la configuration SMTP.")
    try:
        await asyncio.to_thread(
            send_smtp_email, configuration, [recipient],
            "Test des notifications Cliniva",
            f"Bonjour,\n\nCe courriel confirme que le serveur SMTP des notifications Cliniva fonctionne.\nTest demandé par {user.get('prenom', '')} {user.get('nom', '')}.\n",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Échec du test SMTP : {exc}") from exc
    track_action(request, "NOTIFICATION_SMTP_TEST", f"Courriel de test envoyé à {recipient}")
    return {"success": True, "message": f"Courriel de test envoyé à {recipient}."}


def render_html(user=None):
    if not user or user.get("role") != "Administrateur":
        return '<div class="rounded-2xl border border-rose-200 bg-rose-50 p-5 font-semibold text-rose-700">Accès réservé à l’administrateur.</div>'
    return r'''
<section class="space-y-5 text-slate-800" id="notificationConfigModule">
  <header class="flex flex-wrap items-center justify-between gap-4 rounded-[2rem] bg-gradient-to-r from-[#101b13] to-[#183b20] px-7 py-6 text-white shadow-lg">
    <div class="flex items-center gap-4"><span class="grid h-12 w-12 place-items-center rounded-2xl bg-brand-neon text-emerald-950 shadow-lg"><i class="ph-bold ph-bell-ringing text-2xl"></i></span><div><p class="text-xs font-bold uppercase tracking-[.18em] text-lime-300">Administration · Alertes système</p><h1 class="mt-1 text-2xl font-extrabold">Configuration Notif</h1><p class="mt-1 text-sm text-slate-300">Courriels SMTP et notifications de panne Cliniva.</p></div></div>
    <div class="flex flex-wrap items-center gap-2"><span id="notifConfigStatus" class="rounded-full border border-white/15 bg-white/10 px-4 py-2 text-xs font-bold">Chargement…</span><button type="button" onclick="document.getElementById('notifTestEmail')?.focus()" class="inline-flex items-center gap-2 rounded-xl bg-brand-neon px-4 py-2.5 text-xs font-extrabold text-black shadow-md shadow-brand-neon/20 transition hover:bg-brand-neonDark"><i class="ph-bold ph-paper-plane-tilt"></i>Tester l’envoi</button></div>
  </header>
  <section class="grid grid-cols-1 gap-3 sm:grid-cols-3">
    <article class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Canal courriel</span><i class="ph-bold ph-envelope-simple text-xl text-sky-500"></i></div><div id="notifKpiEnabled" class="mt-2 text-xl font-extrabold text-slate-800 dark:text-white">—</div></article>
    <article class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Port SMTP</span><i class="ph-bold ph-plugs-connected text-xl text-emerald-600"></i></div><div id="notifKpiPort" class="mt-2 text-xl font-extrabold text-slate-800 dark:text-white">—</div></article>
    <article class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Mot de passe</span><i class="ph-bold ph-shield-check text-xl text-amber-500"></i></div><div id="notifKpiSecret" class="mt-2 text-xl font-extrabold text-slate-800 dark:text-white">—</div></article>
  </section>
  <div class="grid gap-5 lg:grid-cols-[1.35fr_.85fr]">
    <form id="notifSmtpForm" class="space-y-5 rounded-3xl border border-slate-200 bg-white p-6 shadow-sm dark:border-white/10 dark:bg-[#121c15]" onsubmit="saveNotifSmtp(event)">
      <div class="mb-4 border-b border-slate-100 pb-4 dark:border-white/10"><p class="text-[10px] font-extrabold uppercase tracking-wider text-emerald-700 dark:text-brand-neon">Paramètres du serveur</p><h2 class="mt-1 text-lg font-extrabold text-slate-800 dark:text-white">Serveur de courriel SMTP</h2><p class="mt-1 text-sm text-slate-500">Ces paramètres sont enregistrés sur le serveur Cliniva. Le mot de passe est protégé par Windows et n’est jamais réaffiché.</p></div>
      <label class="flex items-center justify-between gap-4 rounded-2xl border border-emerald-100 bg-emerald-50/70 p-4 dark:border-emerald-500/20 dark:bg-emerald-500/5"><span><b class="block text-sm text-slate-800 dark:text-white">Activer les courriels</b><small class="text-xs text-slate-500">Envoyer les déclarations de panne aux ingénieurs du Staff</small></span><input id="notifSmtpEnabled" name="enabled" type="checkbox" class="h-5 w-5 accent-emerald-600"></label>
      <div class="grid gap-4 sm:grid-cols-2">
        <label class="text-xs font-bold text-slate-600 dark:text-slate-300 sm:col-span-2">Serveur SMTP<input id="notifSmtpHost" name="host" class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-3 text-sm font-medium outline-none focus:border-emerald-500 dark:border-white/10 dark:bg-black/20 dark:text-white" placeholder="smtp.exemple.com"></label>
        <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Port<input id="notifSmtpPort" name="port" type="number" min="1" max="65535" value="587" required class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-3 text-sm font-medium outline-none focus:border-emerald-500 dark:border-white/10 dark:bg-black/20 dark:text-white"></label>
        <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Adresse d’expédition<input id="notifSmtpFrom" name="from" type="email" class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-3 text-sm font-medium outline-none focus:border-emerald-500 dark:border-white/10 dark:bg-black/20 dark:text-white" placeholder="cliniva@exemple.com"></label>
        <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Identifiant SMTP<input id="notifSmtpUser" name="user" autocomplete="off" class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-3 text-sm font-medium outline-none focus:border-emerald-500 dark:border-white/10 dark:bg-black/20 dark:text-white" placeholder="Adresse ou identifiant"></label>
        <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Mot de passe SMTP<input id="notifSmtpPassword" name="password" type="password" autocomplete="new-password" class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-3 text-sm font-medium outline-none focus:border-emerald-500 dark:border-white/10 dark:bg-black/20 dark:text-white" placeholder="Laisser vide pour conserver le mot de passe enregistré"></label>
      </div>
      <div class="flex flex-wrap gap-3 rounded-2xl bg-slate-50 p-4 dark:bg-white/[.03]"><label class="inline-flex items-center gap-2 text-sm font-semibold text-slate-700 dark:text-slate-200"><input id="notifSmtpStarttls" type="checkbox" checked class="h-4 w-4 accent-emerald-600"> STARTTLS (port 587)</label><label class="inline-flex items-center gap-2 text-sm font-semibold text-slate-700 dark:text-slate-200"><input id="notifSmtpSsl" type="checkbox" class="h-4 w-4 accent-emerald-600"> SSL implicite (port 465)</label><label class="inline-flex items-center gap-2 text-sm font-semibold text-rose-700 dark:text-rose-300"><input id="notifSmtpClearPassword" type="checkbox" class="h-4 w-4 accent-rose-600"> Effacer le mot de passe enregistré</label></div>
      <div class="flex flex-wrap justify-end gap-3 border-t border-slate-100 pt-4 dark:border-white/10"><button type="submit" class="inline-flex items-center gap-2 rounded-xl bg-emerald-600 px-5 py-3 text-sm font-extrabold text-white shadow transition hover:bg-emerald-700"><i class="ph-bold ph-floppy-disk"></i>Enregistrer la configuration</button></div>
    </form>
    <aside class="space-y-5">
      <div class="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm dark:border-white/10 dark:bg-[#121c15]"><span class="grid h-12 w-12 place-items-center rounded-2xl bg-sky-100 text-sky-700"><i class="ph-bold ph-paper-plane-tilt text-2xl"></i></span><h2 class="mt-4 text-lg font-extrabold text-slate-800 dark:text-white">Tester l’envoi</h2><p class="mt-1 text-sm text-slate-500">Enregistre d’abord les paramètres, puis envoie un courriel de test.</p><form class="mt-4 space-y-3" onsubmit="sendNotifSmtpTest(event)"><label class="block text-xs font-bold text-slate-600 dark:text-slate-300">Adresse de destination<input id="notifTestEmail" type="email" required class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-3 text-sm outline-none focus:border-sky-500 dark:border-white/10 dark:bg-black/20 dark:text-white" placeholder="ingenieur@exemple.com"></label><button class="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-sky-600 px-4 py-3 text-sm font-extrabold text-white transition hover:bg-sky-700"><i class="ph-bold ph-paper-plane-right"></i>Envoyer un courriel de test</button></form></div>
      <div class="rounded-3xl border border-slate-100 bg-white p-5 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><p class="flex items-center gap-2 text-sm font-extrabold text-slate-800 dark:text-white"><i class="ph-bold ph-users-three text-lg text-violet-500"></i>Destinataires des pannes</p><p class="mt-2 text-xs leading-5 text-slate-500">Les courriels sont envoyés aux ingénieurs, au directeur technique et aux responsables de résultats dont l’adresse est renseignée dans le Staff.</p></div>
      <div class="rounded-3xl border border-amber-200 bg-amber-50 p-5 text-sm text-amber-900 shadow-sm dark:border-amber-500/20 dark:bg-amber-500/5 dark:text-amber-100"><b class="flex items-center gap-2"><i class="ph-bold ph-shield-check text-lg"></i>Protection du secret</b><p class="mt-2 text-xs leading-5">Le mot de passe est chiffré avec DPAPI Windows et lié au compte Windows qui lance Cliniva. Lance Cliniva sous le même compte après configuration.</p></div>
    </aside>
  </div>
</section>
<script>
(function(){
 const $=id=>document.getElementById(id),status=$('notifConfigStatus');
 async function jsonFetch(url,options={}){const r=await fetch(url,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(options.headers||{})},...options});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||'Erreur de communication avec Cliniva.');return d;}
 window.loadNotifSmtpSettings=async function(){try{const d=await jsonFetch('/api/configuration-notif/settings');$('notifSmtpEnabled').checked=d.enabled;$('notifSmtpHost').value=d.host;$('notifSmtpPort').value=d.port;$('notifSmtpFrom').value=d.from;$('notifSmtpUser').value=d.user;$('notifSmtpStarttls').checked=d.starttls;$('notifSmtpSsl').checked=d.ssl;status.textContent=d.enabled?'SMTP activé':'SMTP non activé';status.className='rounded-full border border-white/15 px-4 py-2 text-xs font-bold '+(d.enabled?'bg-emerald-500/20 text-emerald-100':'bg-amber-500/20 text-amber-100');$('notifSmtpPassword').placeholder=d.password_configured?'Mot de passe enregistré · laisser vide pour le conserver':'Mot de passe SMTP';$('notifKpiEnabled').textContent=d.enabled?'Activé':'Désactivé';$('notifKpiPort').textContent=String(d.port||587);$('notifKpiSecret').textContent=d.password_configured?'Protégé':'Non configuré';}catch(e){status.textContent='Configuration indisponible';window.showToast?.(e.message,'error');}};
 window.saveNotifSmtp=async function(event){event.preventDefault();const btn=event.submitter;btn.disabled=true;const data={enabled:$('notifSmtpEnabled').checked,host:$('notifSmtpHost').value,port:$('notifSmtpPort').value,from:$('notifSmtpFrom').value,user:$('notifSmtpUser').value,password:$('notifSmtpPassword').value,starttls:$('notifSmtpStarttls').checked,ssl:$('notifSmtpSsl').checked,clear_password:$('notifSmtpClearPassword').checked};try{await jsonFetch('/api/configuration-notif/settings',{method:'POST',body:JSON.stringify(data)});$('notifSmtpPassword').value='';$('notifSmtpClearPassword').checked=false;window.showToast?.('Configuration des notifications enregistrée.','success');await window.loadNotifSmtpSettings();}catch(e){window.showToast?.(e.message,'error',6000);}finally{btn.disabled=false;}};
 window.sendNotifSmtpTest=async function(event){event.preventDefault();const btn=event.submitter;btn.disabled=true;try{const d=await jsonFetch('/api/configuration-notif/test',{method:'POST',body:JSON.stringify({email:$('notifTestEmail').value})});window.showToast?.(d.message,'success',5000);}catch(e){window.showToast?.(e.message,'error',7000);}finally{btn.disabled=false;}};
 $('notifSmtpSsl').addEventListener('change',()=>{if($('notifSmtpSsl').checked){$('notifSmtpStarttls').checked=false;if($('notifSmtpPort').value==='587')$('notifSmtpPort').value='465';}else if(!$('notifSmtpStarttls').checked){$('notifSmtpStarttls').checked=true;if($('notifSmtpPort').value==='465')$('notifSmtpPort').value='587';}});
 $('notifSmtpStarttls').addEventListener('change',()=>{if($('notifSmtpStarttls').checked){$('notifSmtpSsl').checked=false;if($('notifSmtpPort').value==='465')$('notifSmtpPort').value='587';}});
 window.loadNotifSmtpSettings();
})();
</script>
'''
