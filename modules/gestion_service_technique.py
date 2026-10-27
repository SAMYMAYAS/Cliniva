"""Inventaire, maintenance et demandes d'intervention du service technique."""

from datetime import date
from html import escape
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from app.auth import get_current_user, track_action
from database import get_db_cursor

service_tech_router = APIRouter(prefix="/api/service-technique", tags=["Service technique"])

EQUIPMENT_STATUSES = {"En service", "En maintenance", "Hors service", "Réformé"}
TICKET_TYPES = {"Incident", "Maintenance préventive", "Maintenance corrective", "Demande"}
TICKET_STATUSES = {"Nouveau", "Planifié", "En cours", "En attente", "Terminé", "Annulé"}
PRIORITIES = {"Basse", "Normale", "Haute", "Urgente"}


def _allowed(user: Dict[str, Any], permission: str) -> bool:
    if user.get("role") == "Administrateur":
        return True
    return bool((user.get("permissions") or {}).get(permission))


def _require(user: Dict[str, Any], permission: str) -> None:
    if not _allowed(user, permission):
        raise HTTPException(status_code=403, detail="Vous n'avez pas l'autorisation nécessaire pour le Service Technique.")


def _text(data: Dict[str, Any], key: str, label: str, required: bool = False) -> str:
    value = str(data.get(key) or "").strip()
    if required and not value:
        raise HTTPException(status_code=400, detail=f"Le champ {label} est obligatoire.")
    return value


def _date_value(data: Dict[str, Any], key: str, label: str) -> Optional[str]:
    value = _text(data, key, label)
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"La date {label} est invalide.") from exc


@service_tech_router.get("/data")
def service_tech_data(request: Request):
    user = get_current_user(request)
    _require(user, "service_tech_view")
    with get_db_cursor() as cursor:
        cursor.execute("""SELECT id, code, designation, categorie, marque_modele, numero_serie,
            localisation, fournisseur, date_acquisition, statut, derniere_maintenance,
            prochaine_maintenance, notes FROM service_tech_equipment
            ORDER BY designation, id""")
        equipment = [dict(row) for row in cursor.fetchall()]
        cursor.execute("""SELECT t.id, t.equipment_id, e.designation AS equipment_name, t.type,
            t.titre, t.description, t.priorite, t.statut, t.demandeur, t.responsable,
            t.date_echeance, t.date_cloture, t.created_at
            FROM service_tech_tickets t LEFT JOIN service_tech_equipment e ON e.id=t.equipment_id
            ORDER BY CASE t.priorite WHEN 'Urgente' THEN 0 WHEN 'Haute' THEN 1 WHEN 'Normale' THEN 2 ELSE 3 END,
            t.created_at DESC LIMIT 500""")
        tickets = [dict(row) for row in cursor.fetchall()]
    today = date.today()
    overdue = sum(1 for item in equipment if item.get("prochaine_maintenance") and item["prochaine_maintenance"] < today and item["statut"] != "Réformé")
    return {"equipment": equipment, "tickets": tickets, "can_manage": _allowed(user, "service_tech_manage"),
            "kpi": {"equipment": len(equipment), "open": sum(1 for item in tickets if item["statut"] not in {"Terminé", "Annulé"}),
                    "urgent": sum(1 for item in tickets if item["priorite"] == "Urgente" and item["statut"] not in {"Terminé", "Annulé"}),
                    "overdue": overdue}}


@service_tech_router.post("/equipment/save")
async def service_tech_equipment_save(request: Request):
    user = get_current_user(request)
    _require(user, "service_tech_manage")
    data = await request.json()
    designation = _text(data, "designation", "désignation", True)
    status = _text(data, "statut", "statut") or "En service"
    if status not in EQUIPMENT_STATUSES:
        raise HTTPException(status_code=400, detail="Le statut de l'équipement est invalide.")
    equipment_id = data.get("id")
    values = (_text(data, "code", "code"), designation, _text(data, "categorie", "catégorie"),
              _text(data, "marque_modele", "marque et modèle"), _text(data, "numero_serie", "numéro de série"),
              _text(data, "localisation", "localisation"), _text(data, "fournisseur", "fournisseur"),
              _date_value(data, "date_acquisition", "d'acquisition"), status,
              _date_value(data, "derniere_maintenance", "de dernière maintenance"),
              _date_value(data, "prochaine_maintenance", "de prochaine maintenance"),
              _text(data, "notes", "notes"))
    with get_db_cursor() as cursor:
        if equipment_id:
            cursor.execute("""UPDATE service_tech_equipment SET code=%s, designation=%s, categorie=%s,
                marque_modele=%s, numero_serie=%s, localisation=%s, fournisseur=%s, date_acquisition=%s,
                statut=%s, derniere_maintenance=%s, prochaine_maintenance=%s, notes=%s,
                updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING id""", (*values, int(equipment_id)))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Équipement introuvable.")
            saved_id = row["id"]
        else:
            cursor.execute("""INSERT INTO service_tech_equipment
                (code, designation, categorie, marque_modele, numero_serie, localisation, fournisseur,
                 date_acquisition, statut, derniere_maintenance, prochaine_maintenance, notes, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""", (*values, user["id"]))
            saved_id = cursor.fetchone()["id"]
    track_action(request, "SERVICE_TECH_EQUIPMENT_SAVE", f"Équipement {designation} enregistré")
    return {"success": True, "id": saved_id}


@service_tech_router.post("/equipment/delete")
async def service_tech_equipment_delete(request: Request):
    user = get_current_user(request)
    _require(user, "service_tech_manage")
    data = await request.json()
    try:
        equipment_id = int(data.get("id"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Équipement invalide.") from exc
    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM service_tech_equipment WHERE id=%s RETURNING designation", (equipment_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Équipement introuvable.")
    track_action(request, "SERVICE_TECH_EQUIPMENT_DELETE", f"Équipement {row['designation']} supprimé")
    return {"success": True}


@service_tech_router.post("/tickets/save")
async def service_tech_ticket_save(request: Request):
    user = get_current_user(request)
    _require(user, "service_tech_manage")
    data = await request.json()
    title = _text(data, "titre", "titre", True)
    ticket_type = _text(data, "type", "type") or "Incident"
    priority = _text(data, "priorite", "priorité") or "Normale"
    status = _text(data, "statut", "statut") or "Nouveau"
    if ticket_type not in TICKET_TYPES or priority not in PRIORITIES or status not in TICKET_STATUSES:
        raise HTTPException(status_code=400, detail="Le type, la priorité ou le statut de l'intervention est invalide.")
    equipment_id = data.get("equipment_id") or None
    try:
        equipment_id = int(equipment_id) if equipment_id else None
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Équipement associé invalide.") from exc
    ticket_id = data.get("id")
    values = (equipment_id, ticket_type, title, _text(data, "description", "description"), priority, status,
              _text(data, "demandeur", "demandeur"), _text(data, "responsable", "responsable"),
              _date_value(data, "date_echeance", "d'échéance"))
    with get_db_cursor() as cursor:
        if ticket_id:
            cursor.execute("""UPDATE service_tech_tickets SET equipment_id=%s, type=%s, titre=%s,
                description=%s, priorite=%s, statut=%s, demandeur=%s, responsable=%s, date_echeance=%s,
                date_cloture=CASE WHEN %s='Terminé' THEN COALESCE(date_cloture,CURRENT_TIMESTAMP) ELSE NULL END,
                updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING id""", (*values, status, int(ticket_id)))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Demande d'intervention introuvable.")
            saved_id = row["id"]
        else:
            cursor.execute("""INSERT INTO service_tech_tickets
                (equipment_id, type, titre, description, priorite, statut, demandeur, responsable,
                 date_echeance, created_by, date_cloture)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s='Terminé' THEN CURRENT_TIMESTAMP ELSE NULL END)
                RETURNING id""", (*values, user["id"], status))
            saved_id = cursor.fetchone()["id"]
    track_action(request, "SERVICE_TECH_TICKET_SAVE", f"Intervention {title} enregistrée")
    return {"success": True, "id": saved_id}


@service_tech_router.post("/tickets/delete")
async def service_tech_ticket_delete(request: Request):
    user = get_current_user(request)
    _require(user, "service_tech_manage")
    data = await request.json()
    try:
        ticket_id = int(data.get("id"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Intervention invalide.") from exc
    with get_db_cursor() as cursor:
        cursor.execute("DELETE FROM service_tech_tickets WHERE id=%s RETURNING titre", (ticket_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Demande d'intervention introuvable.")
    track_action(request, "SERVICE_TECH_TICKET_DELETE", f"Intervention {row['titre']} supprimée")
    return {"success": True}


def render_html(user: Optional[Dict[str, Any]] = None) -> str:
    if not user or not _allowed(user, "service_tech_view"):
        raise HTTPException(status_code=403, detail="Accès au module Service Technique refusé.")
    can_manage = _allowed(user, "service_tech_manage")
    manage_controls = """
      <div class="flex flex-wrap gap-2"><button onclick="techOpenEquipment()" class="flex items-center gap-2 rounded-2xl bg-brand-neon px-4 py-2.5 text-xs font-bold text-black shadow-md shadow-brand-neon/20 transition hover:bg-brand-neonDark"><i class="ph-bold ph-plus-circle text-base"></i>Ajouter un équipement</button>
      <button onclick="techOpenTicket()" class="flex items-center gap-2 rounded-2xl border border-white/10 bg-white/10 px-4 py-2.5 text-xs font-bold text-white transition hover:bg-white/15"><i class="ph-bold ph-wrench text-base"></i>Nouvelle intervention</button></div>
    """ if can_manage else ""
    page = r'''
<div id="view-service-tech" class="space-y-5 text-slate-800">
 <section class="flex flex-wrap items-center justify-between gap-4 rounded-[2rem] bg-gradient-to-r from-[#101b13] to-[#183b20] px-7 py-6 text-white shadow-lg">
  <div class="flex flex-wrap items-center justify-between gap-4"><div><div class="mb-2 inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/10 px-3 py-1 text-[10px] font-bold uppercase tracking-wider text-brand-neon"><i class="ph-bold ph-wrench"></i> Gestion technique</div><h1 class="mb-1 text-2xl font-extrabold tracking-tight sm:text-3xl">Service Technique</h1><p class="text-xs text-slate-300">Équipements, maintenances, incidents et interventions.</p></div>__ACTIONS__</div>
 </section>
 <section class="grid grid-cols-2 gap-3 xl:grid-cols-4"><article class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Équipements</span><i class="ph-bold ph-wrench text-xl text-brand-neon"></i></div><div id="techKpiEquipment" class="mt-2 text-2xl font-extrabold text-slate-800 dark:text-white">—</div></article><article class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Interventions ouvertes</span><i class="ph-bold ph-warning-circle text-xl text-amber-500"></i></div><div id="techKpiOpen" class="mt-2 text-2xl font-extrabold text-slate-800 dark:text-white">—</div></article><article class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Urgentes</span><i class="ph-bold ph-siren text-xl text-rose-500"></i></div><div id="techKpiUrgent" class="mt-2 text-2xl font-extrabold text-slate-800 dark:text-white">—</div></article><article class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Maintenances en retard</span><i class="ph-bold ph-calendar-x text-xl text-sky-500"></i></div><div id="techKpiOverdue" class="mt-2 text-2xl font-extrabold text-slate-800 dark:text-white">—</div></article></section>
 <section class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15] sm:p-5"><div class="mb-4 flex flex-wrap items-center justify-between gap-3"><div><h2 class="font-extrabold text-slate-800 dark:text-white">Parc des équipements</h2><p class="text-xs text-slate-500">Suivi de l’état et des prochaines maintenances.</p></div><input id="techEquipmentSearch" oninput="techRenderEquipment()" class="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-800 outline-none focus:border-brand-neon dark:border-white/10 dark:bg-black/30 dark:text-white" placeholder="Rechercher un équipement"></div><div class="overflow-x-auto"><table class="w-full min-w-[850px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600 dark:bg-black/20 dark:text-slate-400"><tr><th class="rounded-l-xl px-3 py-3">Équipement</th><th class="px-3 py-3">Catégorie</th><th class="px-3 py-3">Localisation</th><th class="px-3 py-3">État</th><th class="px-3 py-3">Prochaine maintenance</th><th class="rounded-r-xl px-3 py-3 text-right">Actions</th></tr></thead><tbody id="techEquipmentRows"><tr><td colspan="6" class="p-8 text-center text-slate-400">Chargement…</td></tr></tbody></table></div></section>
 <section class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15] sm:p-5"><div class="mb-4 flex flex-wrap items-center justify-between gap-3"><div><h2 class="font-extrabold text-slate-800 dark:text-white">Incidents et interventions</h2><p class="text-xs text-slate-500">Demandes correctives, préventives et incidents signalés.</p></div><select id="techTicketFilter" onchange="techRenderTickets()" class="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-800 dark:border-white/10 dark:bg-black/30 dark:text-white"><option value="">Tous les statuts</option><option>Nouveau</option><option>Planifié</option><option>En cours</option><option>En attente</option><option>Terminé</option><option>Annulé</option></select></div><div class="overflow-x-auto"><table class="w-full min-w-[900px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600 dark:bg-black/20 dark:text-slate-400"><tr><th class="rounded-l-xl px-3 py-3">Demande</th><th class="px-3 py-3">Équipement</th><th class="px-3 py-3">Priorité</th><th class="px-3 py-3">Responsable</th><th class="px-3 py-3">Échéance</th><th class="px-3 py-3">Statut</th><th class="rounded-r-xl px-3 py-3 text-right">Actions</th></tr></thead><tbody id="techTicketRows"><tr><td colspan="7" class="p-8 text-center text-slate-400">Chargement…</td></tr></tbody></table></div></section>
 <div id="techModal" class="fixed inset-0 z-[100000] hidden items-center justify-center bg-slate-950/60 p-3 backdrop-blur-sm"><div class="max-h-[94vh] w-full max-w-2xl overflow-y-auto rounded-3xl bg-white shadow-2xl dark:bg-[#121c15]"><div class="sticky top-0 z-10 flex items-center justify-between bg-gradient-to-r from-[#101b13] to-[#183b20] px-5 py-4 text-white"><div><div id="techModalEyebrow" class="text-[10px] font-bold uppercase tracking-widest text-brand-neon">Service Technique</div><h3 id="techModalTitle" class="text-lg font-black">Formulaire</h3></div><button onclick="techCloseModal()" class="grid h-9 w-9 place-items-center rounded-xl bg-white/10 hover:bg-white/20" aria-label="Fermer"><i class="ph-bold ph-x"></i></button></div><form id="techForm" onsubmit="techSave(event)" class="space-y-4 p-5"><input id="techEntityType" type="hidden"><input id="techEntityId" type="hidden"><div id="techFormFields" class="grid gap-3 sm:grid-cols-2"></div><div class="flex justify-end gap-2 border-t border-slate-100 pt-4 dark:border-white/10"><button type="button" onclick="techCloseModal()" class="rounded-xl border border-slate-200 px-4 py-2.5 text-sm font-bold text-slate-700 dark:border-white/10 dark:text-slate-200">Annuler</button><button class="rounded-2xl bg-brand-neon px-5 py-2.5 text-xs font-bold text-black shadow-md shadow-brand-neon/20 transition hover:bg-brand-neonDark"><i class="ph-bold ph-floppy-disk mr-1"></i>Enregistrer</button></div></form></div></div>
 <div id="techToast" class="fixed bottom-5 right-5 z-[100001] hidden max-w-sm rounded-xl bg-slate-900 px-4 py-3 text-sm font-semibold text-white shadow-xl"></div>
 <script>
 (()=>{const $=id=>document.getElementById(id);let state={equipment:[],tickets:[],can_manage:false};const canManage=__CAN_MANAGE__;const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const notify=(message,bad=false)=>{const el=$('techToast');el.textContent=message;el.className='fixed bottom-5 right-5 z-[100001] max-w-sm rounded-xl px-4 py-3 text-sm font-semibold text-white shadow-xl '+(bad?'bg-rose-700':'bg-slate-900');el.classList.remove('hidden');setTimeout(()=>el.classList.add('hidden'),3500)};const api=async(path,options={})=>{const response=await fetch('/api/service-technique'+path,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(options.headers||{})},...options});const body=await response.json().catch(()=>({}));if(!response.ok)throw new Error(body.detail||'Une erreur est survenue.');return body};
 window.techRefresh=async()=>{try{state=await api('/data');$('techKpiEquipment').textContent=state.kpi.equipment;$('techKpiOpen').textContent=state.kpi.open;$('techKpiUrgent').textContent=state.kpi.urgent;$('techKpiOverdue').textContent=state.kpi.overdue;techRenderEquipment();techRenderTickets()}catch(e){notify(e.message,true)}};
 window.techRenderEquipment=()=>{const search=($('techEquipmentSearch')?.value||'').trim().toLowerCase();const rows=state.equipment.filter(x=>[x.code,x.designation,x.categorie,x.localisation,x.numero_serie].join(' ').toLowerCase().includes(search));$('techEquipmentRows').innerHTML=rows.length?rows.map(x=>`<tr class="border-b border-slate-100 text-slate-700 transition hover:bg-lime-50/50 dark:border-white/5 dark:text-slate-200 dark:hover:bg-white/5"><td class="px-3 py-3"><div class="font-bold">${esc(x.designation)}</div><div class="text-xs text-slate-500">${esc(x.code||x.numero_serie||'Sans référence')} · ${esc(x.marque_modele||'')}</div></td><td class="px-3 py-3">${esc(x.categorie||'—')}</td><td class="px-3 py-3">${esc(x.localisation||'—')}</td><td class="px-3 py-3"><span class="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-bold text-slate-700 dark:bg-white/10 dark:text-slate-200">${esc(x.statut)}</span></td><td class="px-3 py-3">${esc(x.prochaine_maintenance||'—')}</td><td class="px-3 py-3 text-right">${canManage?`<button onclick="techOpenEquipment(${x.id})" class="rounded-lg p-2 text-sky-700 hover:bg-sky-50 dark:text-sky-300 dark:hover:bg-white/5" title="Modifier"><i class="ph-bold ph-pencil-simple"></i></button><button onclick="techDelete('equipment',${x.id})" class="rounded-lg p-2 text-rose-700 hover:bg-rose-50 dark:text-rose-300 dark:hover:bg-white/5" title="Supprimer"><i class="ph-bold ph-trash"></i></button>`:'—'}</td></tr>`).join(''):'<tr><td colspan="6" class="p-8 text-center text-slate-400">Aucun équipement trouvé.</td></tr>'};
 window.techRenderTickets=()=>{const filter=$('techTicketFilter')?.value||'';const rows=state.tickets.filter(x=>!filter||x.statut===filter);$('techTicketRows').innerHTML=rows.length?rows.map(x=>`<tr class="border-b border-slate-100 text-slate-700 transition hover:bg-lime-50/50 dark:border-white/5 dark:text-slate-200 dark:hover:bg-white/5"><td class="px-3 py-3"><div class="font-bold">${esc(x.titre)}</div><div class="text-xs text-slate-500">${esc(x.type)} · ${esc(x.demandeur||'Demandeur non renseigné')}</div></td><td class="px-3 py-3">${esc(x.equipment_name||'—')}</td><td class="px-3 py-3"><span class="rounded-full px-2.5 py-1 text-xs font-bold ${x.priorite==='Urgente'?'bg-rose-100 text-rose-800 dark:bg-rose-400/10 dark:text-rose-300':x.priorite==='Haute'?'bg-amber-100 text-amber-800 dark:bg-amber-400/10 dark:text-amber-300':'bg-slate-100 text-slate-700 dark:bg-white/10 dark:text-slate-300'}">${esc(x.priorite)}</span></td><td class="px-3 py-3">${esc(x.responsable||'—')}</td><td class="px-3 py-3">${esc(x.date_echeance||'—')}</td><td class="px-3 py-3"><span class="rounded-full bg-sky-50 px-2.5 py-1 text-xs font-bold text-sky-800 dark:bg-sky-400/10 dark:text-sky-300">${esc(x.statut)}</span></td><td class="px-3 py-3 text-right">${canManage?`<button onclick="techOpenTicket(${x.id})" class="rounded-lg p-2 text-sky-700 hover:bg-sky-50 dark:text-sky-300 dark:hover:bg-white/5" title="Modifier"><i class="ph-bold ph-pencil-simple"></i></button><button onclick="techDelete('ticket',${x.id})" class="rounded-lg p-2 text-rose-700 hover:bg-rose-50 dark:text-rose-300 dark:hover:bg-white/5" title="Supprimer"><i class="ph-bold ph-trash"></i></button>`:'—'}</td></tr>`).join(''):'<tr><td colspan="7" class="p-8 text-center text-slate-400">Aucune intervention pour ce filtre.</td></tr>'};
 const field=(name,label,type='text',value='',extra='')=>`<label class="block text-xs font-bold text-slate-600 dark:text-slate-300">${label}<input name="${name}" type="${type}" value="${esc(value)}" ${extra} class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm font-medium text-slate-800 outline-none focus:border-brand-neon focus:bg-white dark:border-white/10 dark:bg-black/30 dark:text-white"></label>`;const select=(name,label,items,value)=>`<label class="block text-xs font-bold text-slate-600 dark:text-slate-300">${label}<select name="${name}" class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm font-medium text-slate-800 outline-none focus:border-brand-neon dark:border-white/10 dark:bg-black/30 dark:text-white">${items.map(x=>`<option ${x===value?'selected':''}>${esc(x)}</option>`).join('')}</select></label>`;
 window.techOpenEquipment=(id)=>{if(!canManage)return;const x=state.equipment.find(row=>row.id===id)||{};$('techEntityType').value='equipment';$('techEntityId').value=x.id||'';$('techModalEyebrow').textContent='Parc technique';$('techModalTitle').textContent=x.id?'Modifier un équipement':'Ajouter un équipement';$('techFormFields').innerHTML=field('designation','Désignation *','text',x.designation||'','required')+field('code','Code inventaire','text',x.code||'')+field('categorie','Catégorie','text',x.categorie||'')+field('marque_modele','Marque / modèle','text',x.marque_modele||'')+field('numero_serie','Numéro de série','text',x.numero_serie||'')+field('localisation','Localisation / service','text',x.localisation||'')+field('fournisseur','Fournisseur','text',x.fournisseur||'')+field('date_acquisition','Date d’acquisition','date',x.date_acquisition||'')+select('statut','État',['En service','En maintenance','Hors service','Réformé'],x.statut||'En service')+field('derniere_maintenance','Dernière maintenance','date',x.derniere_maintenance||'')+field('prochaine_maintenance','Prochaine maintenance','date',x.prochaine_maintenance||'')+`<label class="block text-xs font-bold text-slate-600 dark:text-slate-300 sm:col-span-2">Notes<textarea name="notes" rows="2" class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm font-medium outline-none focus:border-brand-neon dark:border-white/10 dark:bg-black/30 dark:text-white">${esc(x.notes||'')}</textarea></label>`;techShowModal()};
 window.techOpenTicket=(id)=>{if(!canManage)return;const x=state.tickets.find(row=>row.id===id)||{};const equipmentOptions=['<option value="">Aucun équipement associé</option>',...state.equipment.map(e=>`<option value="${e.id}" ${Number(x.equipment_id)===Number(e.id)?'selected':''}>${esc(e.designation)}</option>`)].join('');$('techEntityType').value='ticket';$('techEntityId').value=x.id||'';$('techModalEyebrow').textContent='Incidents et maintenance';$('techModalTitle').textContent=x.id?'Modifier une demande':'Nouvelle intervention';$('techFormFields').innerHTML=select('type','Type de demande',['Incident','Maintenance préventive','Maintenance corrective','Demande'],x.type||'Incident')+select('priorite','Priorité',['Basse','Normale','Haute','Urgente'],x.priorite||'Normale')+field('titre','Titre *','text',x.titre||'','required')+`<label class="block text-xs font-bold text-slate-600 dark:text-slate-300">Équipement<select name="equipment_id" class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm dark:border-white/10 dark:bg-black/30 dark:text-white">${equipmentOptions}</select></label>`+field('demandeur','Demandeur','text',x.demandeur||'')+field('responsable','Responsable / technicien','text',x.responsable||'')+field('date_echeance','Date prévue','date',x.date_echeance||'')+select('statut','Statut',['Nouveau','Planifié','En cours','En attente','Terminé','Annulé'],x.statut||'Nouveau')+`<label class="block text-xs font-bold text-slate-600 dark:text-slate-300 sm:col-span-2">Description<textarea name="description" rows="3" class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm outline-none focus:border-brand-neon dark:border-white/10 dark:bg-black/30 dark:text-white">${esc(x.description||'')}</textarea></label>`;techShowModal()};
 function techShowModal(){$('techModal').classList.remove('hidden');$('techModal').classList.add('flex');document.body.style.overflow='hidden'}window.techCloseModal=()=>{$('techModal').classList.add('hidden');$('techModal').classList.remove('flex');document.body.style.overflow=''};
 window.techSave=async event=>{event.preventDefault();const form=event.currentTarget,type=$('techEntityType').value,id=$('techEntityId').value,payload=Object.fromEntries(new FormData(form).entries());if(id)payload.id=Number(id);try{await api(type==='equipment'?'/equipment/save':'/tickets/save',{method:'POST',body:JSON.stringify(payload)});techCloseModal();notify(type==='equipment'?'Équipement enregistré.':'Intervention enregistrée.');await techRefresh()}catch(e){notify(e.message,true)}};
 window.techDelete=async(type,id)=>{if(!canManage||!confirm(type==='equipment'?'Supprimer cet équipement ? Les interventions associées resteront dans le registre.':'Supprimer cette demande d’intervention ?'))return;try{await api(type==='equipment'?'/equipment/delete':'/tickets/delete',{method:'POST',body:JSON.stringify({id})});notify('Suppression effectuée.');await techRefresh()}catch(e){notify(e.message,true)}};
 $('techModal').addEventListener('click',e=>{if(e.target===$('techModal'))techCloseModal()});window.techRefresh()})();
 </script>
</div>
'''
    return page.replace("__ACTIONS__", manage_controls).replace("__CAN_MANAGE__", "true" if can_manage else "false")
