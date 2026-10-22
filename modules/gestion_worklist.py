"""Interface et API d'administration de la DICOM Modality Worklist Cliniva."""

import json
import logging
import os
import re
from datetime import date, datetime
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.auth import get_current_user_optional
from database import get_db_cursor

logger = logging.getLogger("ClinivaWorklistUI")
worklist_router = APIRouter(prefix="/api/worklist", tags=["DICOM Worklist"])
WORKLIST_STATUSES = {"SCHEDULED", "ARRIVED", "READY", "STARTED", "COMPLETED", "DISCONTINUED"}


def _require_access(request: Request, permission: str = "reception_view") -> Dict[str, Any]:
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    if user.get("status") != "Actif":
        raise HTTPException(status_code=403, detail="Ce compte utilisateur est inactif.")
    permissions = user.get("permissions") or {}
    if isinstance(permissions, str):
        try:
            permissions = json.loads(permissions)
        except (TypeError, ValueError):
            permissions = {}
    if user.get("role") != "Administrateur" and not (
        isinstance(permissions, dict) and permissions.get(permission)
    ):
        raise HTTPException(status_code=403, detail="Vous n'avez pas l'autorisation pour cette action.")
    return user


def _require_admin(request: Request) -> Dict[str, Any]:
    user = _require_access(request)
    if user.get("role") != "Administrateur":
        raise HTTPException(status_code=403, detail="Cette commande est réservée à l'administrateur.")
    return user


def sync_worklist_items() -> int:
    """Ajoute les actes marqués MWL des passages qui ne sont pas encore gérés.

    Les modifications faites dans l'écran Worklist sont conservées : la
    synchronisation n'écrase jamais une entrée déjà créée.
    """
    with get_db_cursor() as cursor:
        cursor.execute("SELECT designation, modalite FROM consultation_types")
        modalities = {
            str(row.get("designation") or "").strip().casefold(): str(row.get("modalite") or "").strip()
            for row in cursor.fetchall()
        }
        cursor.execute(
            "SELECT id, id_patient, date_passage, heure_passage, actes_json "
            "FROM patient_passages ORDER BY id"
        )
        passages = cursor.fetchall()
        inserted = 0
        for passage in passages:
            acts = passage.get("actes_json") or []
            if isinstance(acts, str):
                try:
                    acts = json.loads(acts)
                except (TypeError, ValueError):
                    acts = []
            if not isinstance(acts, list):
                continue
            for index, act in enumerate(acts, start=1):
                if not isinstance(act, dict) or act.get("transmettre_hl7") is not True:
                    continue
                description = str(act.get("nom") or act.get("designation") or "").strip()
                modality = str(act.get("modalite") or modalities.get(description.casefold()) or "").strip()
                if not description or not modality:
                    continue
                cursor.execute(
                    """
                    INSERT INTO mwl_worklist_items
                        (patient_id, passage_id, act_index, accession_number,
                         requested_procedure_id, procedure_description, modality,
                         scheduled_date, scheduled_time)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (passage_id, act_index) DO NOTHING
                    RETURNING id
                    """,
                    (
                        passage["id_patient"], passage["id"], index, str(passage["id"]),
                        f"CL-{passage['id']}-{index}", description, modality,
                        passage["date_passage"], str(passage.get("heure_passage") or ""),
                    ),
                )
                inserted += int(cursor.fetchone() is not None)
    return inserted


def _serialize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(row)
    for key in ("scheduled_date", "date_naissance"):
        if result.get(key) is not None:
            result[key] = result[key].isoformat()
    if result.get("enabled") is not None:
        result["enabled"] = bool(result["enabled"])
    return result


@worklist_router.get("/items")
async def list_worklist_items(request: Request):
    _require_access(request)
    sync_worklist_items()
    search = (request.query_params.get("q") or "").strip()
    modality = (request.query_params.get("modality") or "").strip()
    status = (request.query_params.get("status") or "").strip()
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT w.id, w.patient_id, w.passage_id, w.accession_number,
                   w.requested_procedure_id, w.procedure_description,
                   w.modality, w.scheduled_date, w.scheduled_time,
                   w.station_ae_title, w.status, w.enabled,
                   p.nom, p.prenom, p.date_naissance, p.genre
            FROM mwl_worklist_items w
            JOIN patients p ON p.id = w.patient_id
            WHERE (%s = '' OR concat_ws(' ', p.nom, p.prenom, p.id::text,
                    w.accession_number, w.procedure_description) ILIKE %s)
              AND (%s = '' OR w.modality = %s)
              AND ((%s = 'ACTIVE' AND w.enabled = TRUE AND w.status NOT IN ('COMPLETED', 'DISCONTINUED'))
                   OR (%s = '' OR w.status = %s))
            ORDER BY w.scheduled_date DESC, w.scheduled_time DESC, w.id DESC
            LIMIT 1000
            """,
            (search, f"%{search}%", modality, modality, status, status, status),
        )
        rows = cursor.fetchall()
        cursor.execute("SELECT DISTINCT modality FROM mwl_worklist_items WHERE modality <> '' ORDER BY modality")
        modalities = [row["modality"] for row in cursor.fetchall()]
    return JSONResponse({"items": [_serialize_row(row) for row in rows], "modalities": modalities})


@worklist_router.get("/station-titles")
async def list_station_titles(request: Request):
    _require_access(request)
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT id, ae_title, designation, modality, active "
            "FROM mwl_station_titles ORDER BY ae_title"
        )
        rows = [dict(row) for row in cursor.fetchall()]
    return JSONResponse({"items": rows})


@worklist_router.post("/station-titles/save")
async def save_station_title(request: Request):
    _require_admin(request)
    payload = await request.json()
    ae_title = str(payload.get("ae_title") or "").strip().upper()
    designation = str(payload.get("designation") or "").strip()
    modality = str(payload.get("modality") or "").strip().upper()
    active = bool(payload.get("active", True))
    if not re.fullmatch(r"[A-Z0-9 _.-]{1,16}", ae_title) or ae_title != ae_title.strip():
        raise HTTPException(status_code=400, detail="L'AE Title doit contenir de 1 à 16 caractères valides.")
    if len(modality) > 16:
        raise HTTPException(status_code=400, detail="La modalité ne peut pas dépasser 16 caractères.")
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO mwl_station_titles (ae_title, designation, modality, active)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (ae_title) DO UPDATE SET
                designation = EXCLUDED.designation, modality = EXCLUDED.modality,
                active = EXCLUDED.active, updated_at = CURRENT_TIMESTAMP
            RETURNING id
            """,
            (ae_title, designation, modality, active),
        )
        row = cursor.fetchone()
    return JSONResponse({"success": True, "id": row["id"]})


@worklist_router.post("/station-titles/{title_id}/toggle")
async def toggle_station_title(title_id: int, request: Request):
    _require_admin(request)
    payload = await request.json()
    active = bool(payload.get("active"))
    with get_db_cursor() as cursor:
        cursor.execute(
            "UPDATE mwl_station_titles SET active = %s, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = %s RETURNING id",
            (active, title_id),
        )
        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="AE Title introuvable.")
    return JSONResponse({"success": True})


@worklist_router.post("/station-titles/{title_id}/delete")
async def delete_station_title(title_id: int, request: Request):
    _require_admin(request)
    with get_db_cursor() as cursor:
        cursor.execute("SELECT ae_title FROM mwl_station_titles WHERE id = %s FOR UPDATE", (title_id,))
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="AE Title introuvable.")
        cursor.execute(
            "UPDATE mwl_worklist_items SET station_ae_title = '' "
            "WHERE station_ae_title = %s",
            (row["ae_title"],),
        )
        cursor.execute("DELETE FROM mwl_station_titles WHERE id = %s", (title_id,))
    return JSONResponse({"success": True})


@worklist_router.get("/service")
async def get_worklist_service_status(request: Request):
    _require_access(request)
    from app.worklist_service import get_mwl_server_status

    return JSONResponse(get_mwl_server_status())


@worklist_router.post("/service/{action}")
async def control_worklist_service(action: str, request: Request):
    _require_admin(request)
    from app.worklist_service import restart_mwl_server, stop_mwl_server

    try:
        if action == "start":
            restart_mwl_server()
        elif action == "stop":
            stop_mwl_server()
        else:
            raise HTTPException(status_code=404, detail="Commande de service inconnue.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Échec de commande du serveur Worklist (%s).", action)
        raise HTTPException(status_code=503, detail=f"Le serveur MWL n'a pas pu être modifié : {exc}") from exc
    try:
        from main import CONFIG_FILE, load_app_configuration

        config = load_app_configuration()
        config["mwl_enabled"] = action == "start"
        temporary_path = f"{CONFIG_FILE}.tmp"
        with open(temporary_path, "w", encoding="utf-8") as config_file:
            json.dump(config, config_file, ensure_ascii=False, indent=4)
        os.replace(temporary_path, CONFIG_FILE)
    except Exception:
        logger.exception("L'état MWL a changé en mémoire mais n'a pas été enregistré dans config.json.")
    from app.worklist_service import get_mwl_server_status

    return JSONResponse(get_mwl_server_status())


@worklist_router.post("/patients/{patient_id}/delete")
async def remove_patient_from_worklist(patient_id: int, request: Request):
    _require_access(request, "reception_edit")
    with get_db_cursor() as cursor:
        cursor.execute(
            "UPDATE mwl_worklist_items SET enabled = FALSE, status = 'DISCONTINUED', "
            "updated_at = CURRENT_TIMESTAMP WHERE patient_id = %s",
            (patient_id,),
        )
        count = cursor.rowcount
    return JSONResponse({"success": True, "removed": count})


@worklist_router.post("/items/delete-all")
async def clear_worklist(request: Request):
    _require_admin(request)
    with get_db_cursor() as cursor:
        cursor.execute(
            "UPDATE mwl_worklist_items SET enabled = FALSE, status = 'DISCONTINUED', "
            "updated_at = CURRENT_TIMESTAMP"
        )
        count = cursor.rowcount
    return JSONResponse({"success": True, "removed": count})


@worklist_router.post("/sync")
async def sync_worklist_endpoint(request: Request):
    _require_access(request, "reception_edit")
    count = sync_worklist_items()
    return JSONResponse({"success": True, "added": count})


@worklist_router.post("/items/{item_id}/save")
async def save_worklist_item(item_id: int, request: Request):
    _require_access(request, "reception_edit")
    payload = await request.json()
    description = str(payload.get("procedure_description") or "").strip()
    modality = str(payload.get("modality") or "").strip().upper()
    scheduled_date = str(payload.get("scheduled_date") or "").strip()
    scheduled_time = str(payload.get("scheduled_time") or "").strip()
    station_ae = str(payload.get("station_ae_title") or "").strip().upper()
    status = str(payload.get("status") or "SCHEDULED").strip().upper()
    try:
        date.fromisoformat(scheduled_date)
        datetime.strptime(scheduled_time, "%H:%M")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Date ou heure invalide.") from exc
    if not description or not modality or len(modality) > 16 or len(station_ae) > 16:
        raise HTTPException(status_code=400, detail="Examen, modalité ou AE Title invalide.")
    if status not in WORKLIST_STATUSES:
        raise HTTPException(status_code=400, detail="État de worklist invalide.")
    enabled = bool(payload.get("enabled", True))
    with get_db_cursor() as cursor:
        if station_ae:
            cursor.execute(
                "SELECT active, modality FROM mwl_station_titles WHERE ae_title = %s",
                (station_ae,),
            )
            title = cursor.fetchone()
            if not title or not title["active"]:
                raise HTTPException(status_code=400, detail="Sélectionnez un AE Title actif enregistré.")
            if title["modality"] and title["modality"].upper() != modality:
                raise HTTPException(status_code=400, detail="Cet AE Title est associé à une autre modalité.")
        cursor.execute(
            """
            UPDATE mwl_worklist_items
            SET procedure_description = %s, modality = %s,
                scheduled_date = %s, scheduled_time = %s,
                station_ae_title = %s, status = %s, enabled = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s RETURNING id
            """,
            (description, modality, scheduled_date, scheduled_time,
             station_ae, status, enabled, item_id),
        )
        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Élément Worklist introuvable.")
    return JSONResponse({"success": True})


@worklist_router.post("/items/{item_id}/delete")
async def delete_worklist_item(item_id: int, request: Request):
    _require_access(request, "reception_edit")
    with get_db_cursor() as cursor:
        cursor.execute(
            "UPDATE mwl_worklist_items SET enabled = FALSE, status = 'DISCONTINUED', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = %s RETURNING id",
            (item_id,),
        )
        if cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Élément Worklist introuvable.")
    return JSONResponse({"success": True})


def render_html(user: Dict[str, Any] | None = None) -> str:
    """Écran de suivi et de gestion des examens publiés à la MWL DICOM."""
    permissions = (user or {}).get("permissions") or {}
    if isinstance(permissions, str):
        try:
            permissions = json.loads(permissions)
        except (TypeError, ValueError):
            permissions = {}
    can_manage = (user or {}).get("role") == "Administrateur" or (
        isinstance(permissions, dict) and bool(permissions.get("reception_edit"))
    )
    is_admin = (user or {}).get("role") == "Administrateur"
    markup = r'''<section class="space-y-6">
      <header class="rounded-[2rem] bg-gradient-to-r from-[#101b13] to-[#183b20] px-7 py-6 text-white shadow-lg flex flex-wrap items-center justify-between gap-4">
        <div><p class="text-xs font-bold uppercase tracking-[.18em] text-lime-300">Imagerie • DICOM</p><h1 class="mt-1 text-2xl font-extrabold">Modality Worklist</h1><p class="mt-1 text-sm text-slate-300">Examens transmis aux modalités radiologiques via le serveur Cliniva.</p></div>
        <div class="flex flex-wrap items-center gap-2"><span id="mwlServiceBadge" class="rounded-full bg-slate-700 px-3 py-2 text-xs font-bold">Vérification du serveur…</span><button data-mwl-admin id="mwlServiceToggle" onclick="clinivaMwlToggleService()" class="rounded-xl border border-white/20 bg-white/10 px-4 py-2.5 text-sm font-bold text-white hover:bg-white/20">Démarrer</button><button data-mwl-manage onclick="clinivaMwlSync()" class="rounded-xl bg-lime-400 px-4 py-2.5 text-sm font-bold text-slate-900 hover:bg-lime-300"><i class="ph-bold ph-arrows-clockwise mr-1"></i> Synchroniser</button></div>
      </header>
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <article class="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm"><p class="text-xs font-semibold uppercase text-slate-500">Examens listés</p><p id="mwlCount" class="mt-2 text-2xl font-extrabold text-slate-800">—</p></article>
        <article class="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm"><p class="text-xs font-semibold uppercase text-slate-500">À transmettre</p><p id="mwlEnabledCount" class="mt-2 text-2xl font-extrabold text-emerald-600">—</p></article>
        <article class="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm"><p class="text-xs font-semibold uppercase text-slate-500">Modalités</p><p id="mwlModalitiesCount" class="mt-2 text-2xl font-extrabold text-indigo-600">—</p></article>
      </div>
      <div class="rounded-[1.75rem] border border-slate-200 bg-white p-4 shadow-sm sm:p-5">
        <div class="mb-4 grid grid-cols-1 gap-3 md:grid-cols-[1fr_190px_190px_auto]">
          <input id="mwlSearch" oninput="clinivaMwlLoad()" placeholder="Patient, numéro ou examen…" class="rounded-xl border border-slate-200 px-3 py-2.5 text-sm outline-none focus:ring-2 focus:ring-lime-300">
          <select id="mwlModality" onchange="clinivaMwlLoad()" class="rounded-xl border border-slate-200 px-3 py-2.5 text-sm"><option value="">Toutes les modalités</option></select>
          <select id="mwlStatus" onchange="clinivaMwlLoad()" class="rounded-xl border border-slate-200 px-3 py-2.5 text-sm"><option value="ACTIVE" selected>Actifs</option><option value="">Tous les états</option><option>SCHEDULED</option><option>ARRIVED</option><option>READY</option><option>STARTED</option><option>COMPLETED</option><option>DISCONTINUED</option></select>
          <button onclick="clinivaMwlLoad()" class="rounded-xl border border-slate-200 px-4 py-2 text-sm font-bold text-slate-700 hover:bg-slate-50"><i class="ph-bold ph-magnifying-glass mr-1"></i> Actualiser</button>
        </div>
        <div class="overflow-x-auto"><table class="w-full min-w-[980px] border-separate border-spacing-y-2 text-left text-sm"><thead><tr class="text-[11px] uppercase tracking-wider text-slate-500"><th class="px-3 py-2">Patient</th><th class="px-3 py-2">Examen / Modalité</th><th class="px-3 py-2">Date planifiée</th><th class="px-3 py-2">Accession / Passage</th><th class="px-3 py-2">État DICOM</th><th class="px-3 py-2">Transmission</th><th class="px-3 py-2 text-right">Actions</th></tr></thead><tbody id="mwlRows"><tr><td colspan="7" class="p-8 text-center text-slate-400">Chargement…</td></tr></tbody></table></div>
        <div class="mt-3 flex flex-wrap items-center justify-between gap-2"><p id="mwlMessage" class="min-h-5 text-sm text-slate-500"></p><button data-mwl-admin onclick="clinivaMwlClearAll()" class="rounded-lg border border-rose-200 px-3 py-2 text-xs font-bold text-rose-700 hover:bg-rose-50"><i class="ph-bold ph-trash mr-1"></i> Retirer tous les patients de la Worklist</button></div>
      </div>
      <div class="rounded-[1.75rem] border border-slate-200 bg-white p-5 shadow-sm">
        <div class="mb-4 flex flex-wrap items-center justify-between gap-3"><div><h2 class="text-lg font-extrabold text-slate-800">AE Titles des modalités</h2><p class="text-xs text-slate-500">Destinations DICOM autorisées pour les examens de la Worklist.</p></div><button data-mwl-admin onclick="clinivaMwlTitleEdit()" class="rounded-xl bg-indigo-600 px-4 py-2.5 text-sm font-bold text-white hover:bg-indigo-700"><i class="ph-bold ph-plus mr-1"></i> Ajouter un AE Title</button></div>
        <div class="overflow-x-auto"><table class="w-full min-w-[650px] text-left text-sm"><thead class="text-[11px] uppercase tracking-wider text-slate-500"><tr><th class="px-3 py-2">AE Title</th><th class="px-3 py-2">Désignation</th><th class="px-3 py-2">Modalité</th><th class="px-3 py-2">État</th><th class="px-3 py-2 text-right">Actions</th></tr></thead><tbody id="mwlTitleRows"><tr><td colspan="5" class="p-5 text-center text-slate-400">Chargement…</td></tr></tbody></table></div>
      </div>
      <div id="mwlEditModal" class="fixed inset-0 z-[100] hidden items-center justify-center bg-slate-950/60 p-4" onclick="if(event.target===this)clinivaMwlClose()">
        <form onsubmit="clinivaMwlSave(event)" class="w-full max-w-2xl space-y-4 rounded-3xl bg-white p-6 shadow-2xl">
          <div class="flex items-start justify-between"><div><p class="text-xs font-bold uppercase tracking-widest text-emerald-600">Gestion de l’examen</p><h2 class="mt-1 text-xl font-extrabold text-slate-900">Modifier l’élément Worklist</h2></div><button type="button" onclick="clinivaMwlClose()" class="rounded-xl p-2 text-slate-500 hover:bg-slate-100"><i class="ph-bold ph-x text-xl"></i></button></div>
          <input type="hidden" id="mwlEditId">
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2"><label class="text-xs font-semibold text-slate-600 sm:col-span-2">Examen<input id="mwlEditDescription" required class="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm"></label><label class="text-xs font-semibold text-slate-600">Modalité<input id="mwlEditModality" required maxlength="16" class="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm"></label><label class="text-xs font-semibold text-slate-600">État<select id="mwlEditStatus" class="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm"><option>SCHEDULED</option><option>ARRIVED</option><option>READY</option><option>STARTED</option><option>COMPLETED</option><option>DISCONTINUED</option></select></label><label class="text-xs font-semibold text-slate-600">Date<input id="mwlEditDate" type="date" required class="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm"></label><label class="text-xs font-semibold text-slate-600">Heure<input id="mwlEditTime" type="time" required class="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm"></label><label class="text-xs font-semibold text-slate-600 sm:col-span-2">AE Title de destination (facultatif)<select id="mwlEditAE" class="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm"><option value="">Aucune station assignée</option></select></label></div>
          <label class="flex items-center gap-2 text-sm font-semibold text-slate-700"><input id="mwlEditEnabled" type="checkbox" class="h-4 w-4 accent-emerald-600"> Publier cet examen dans les réponses DICOM MWL</label>
          <div class="flex justify-end gap-2 pt-2"><button type="button" onclick="clinivaMwlClose()" class="rounded-xl border border-slate-200 px-4 py-2.5 text-sm font-bold text-slate-600">Annuler</button><button class="rounded-xl bg-emerald-600 px-5 py-2.5 text-sm font-bold text-white hover:bg-emerald-700">Enregistrer</button></div>
        </form>
      </div>
      <div id="mwlTitleModal" class="fixed inset-0 z-[110] hidden items-center justify-center bg-slate-950/60 p-4" onclick="if(event.target===this)clinivaMwlTitleClose()"><form onsubmit="clinivaMwlTitleSave(event)" class="w-full max-w-lg space-y-4 rounded-3xl bg-white p-6 shadow-2xl"><div class="flex items-center justify-between"><div><p class="text-xs font-bold uppercase tracking-widest text-indigo-600">Configuration DICOM</p><h2 class="mt-1 text-xl font-extrabold text-slate-900">AE Title de station</h2></div><button type="button" onclick="clinivaMwlTitleClose()" class="rounded-xl p-2 text-slate-500 hover:bg-slate-100"><i class="ph-bold ph-x text-xl"></i></button></div><input id="mwlTitleId" type="hidden"><label class="block text-xs font-semibold text-slate-600">AE Title (1 à 16 caractères)<input id="mwlTitleAE" maxlength="16" required class="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm uppercase"></label><label class="block text-xs font-semibold text-slate-600">Nom de la station<input id="mwlTitleDesignation" class="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm"></label><label class="block text-xs font-semibold text-slate-600">Modalité associée (facultatif)<input id="mwlTitleModality" maxlength="16" class="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2.5 text-sm uppercase" placeholder="IRM, CT, CR…"></label><label class="flex items-center gap-2 text-sm font-semibold text-slate-700"><input id="mwlTitleActive" type="checkbox" checked class="h-4 w-4 accent-indigo-600"> AE Title actif</label><div class="flex justify-end gap-2"><button type="button" onclick="clinivaMwlTitleClose()" class="rounded-xl border border-slate-200 px-4 py-2.5 text-sm font-bold text-slate-600">Annuler</button><button class="rounded-xl bg-indigo-600 px-5 py-2.5 text-sm font-bold text-white">Enregistrer</button></div></form></div>
    </section>
    <script>
    (() => {
      window.CLINIVA_MWL_CAN_MANAGE = __CAN_MANAGE__;
      window.CLINIVA_MWL_IS_ADMIN = __IS_ADMIN__;
      document.querySelectorAll('[data-mwl-admin]').forEach(el=>el.classList.toggle('hidden',!window.CLINIVA_MWL_IS_ADMIN));
      let items = [], stationTitles = [];
      const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      const message = (text, error=false) => { const el=document.getElementById('mwlMessage'); if(el){el.textContent=text;el.className=`mt-3 min-h-5 text-sm ${error?'text-rose-600':'text-slate-500'}`;} };
      window.clinivaMwlLoad = async () => {
        try {
          const q = encodeURIComponent(document.getElementById('mwlSearch')?.value || '');
          const m = encodeURIComponent(document.getElementById('mwlModality')?.value || '');
          const s = encodeURIComponent(document.getElementById('mwlStatus')?.value || '');
          const response = await fetch(`/api/worklist/items?q=${q}&modality=${m}&status=${s}`);
          const data = await response.json(); if(!response.ok) throw new Error(data.detail || 'Chargement impossible');
          items=data.items||[];
          const mod=document.getElementById('mwlModality'), selected=mod.value;
          mod.innerHTML='<option value="">Toutes les modalités</option>'+(data.modalities||[]).map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join(''); mod.value=selected;
          document.getElementById('mwlCount').textContent=items.length;
          document.getElementById('mwlEnabledCount').textContent=items.filter(x=>x.enabled && !['COMPLETED','DISCONTINUED'].includes(x.status)).length;
          document.getElementById('mwlModalitiesCount').textContent=(data.modalities||[]).length;
          const body=document.getElementById('mwlRows');
          body.innerHTML=items.length ? items.map(x=>`<tr class="bg-slate-50 hover:bg-emerald-50/70"><td class="rounded-l-xl px-3 py-3"><div class="font-extrabold text-slate-800">${esc(x.prenom)} ${esc(x.nom)}</div><div class="text-xs text-slate-500">Patient #${esc(x.patient_id)} · Né(e) le ${esc(x.date_naissance||'—')}</div></td><td class="px-3 py-3"><div class="font-bold text-slate-800">${esc(x.procedure_description)}</div><span class="mt-1 inline-flex rounded-full bg-indigo-100 px-2 py-0.5 text-xs font-bold text-indigo-700">${esc(x.modality)}</span></td><td class="px-3 py-3 font-semibold text-slate-700">${esc(x.scheduled_date)}<div class="text-xs text-slate-500">${esc(x.scheduled_time||'')}</div></td><td class="px-3 py-3 text-xs text-slate-600">${esc(x.accession_number)}<div>Passage #${esc(x.passage_id)}</div></td><td class="px-3 py-3"><span class="rounded-full bg-slate-200 px-2.5 py-1 text-xs font-bold text-slate-700">${esc(x.status)}</span></td><td class="px-3 py-3"><span class="rounded-full px-2.5 py-1 text-xs font-bold ${x.enabled?'bg-emerald-100 text-emerald-700':'bg-rose-100 text-rose-700'}">${x.enabled?'Publié':'Suspendu'}</span></td><td class="rounded-r-xl px-3 py-3 text-right"><button onclick="clinivaMwlEdit(${Number(x.id)})" title="Modifier" class="rounded-lg p-2 text-indigo-600 hover:bg-indigo-100"><i class="ph-bold ph-pencil-simple"></i></button><button onclick="clinivaMwlDelete(${Number(x.id)})" title="Retirer" class="rounded-lg p-2 text-rose-600 hover:bg-rose-100"><i class="ph-bold ph-trash"></i></button></td></tr>`).join(''):'<tr><td colspan="7" class="p-10 text-center text-slate-400">Aucun examen Worklist trouvé. Synchronisez les passages dont les actes sont cochés pour transmission.</td></tr>';
          body.querySelectorAll('tr').forEach(row=>{const patientCell=row.cells[0],actions=row.lastElementChild;if(!patientCell||!actions)return;const match=patientCell.textContent.match(/Patient #(\d+)/);if(!match)return;if(window.CLINIVA_MWL_CAN_MANAGE){const button=document.createElement('button');button.type='button';button.title='Retirer toutes les entrées de ce patient';button.className='rounded-lg p-2 text-rose-600 hover:bg-rose-100';button.innerHTML='<i class="ph-bold ph-user-minus"></i>';button.onclick=()=>clinivaMwlRemovePatient(Number(match[1]));actions.append(button);}else{actions.querySelectorAll('button').forEach(el=>el.remove());}});
          message(`${items.length} examen(s) affiché(s).`);
        } catch(e){message(e.message||'Erreur de chargement',true);}
      };
      window.clinivaMwlEdit = id => { const x=items.find(item=>item.id===id); if(!x)return; document.getElementById('mwlEditId').value=x.id; document.getElementById('mwlEditDescription').value=x.procedure_description; document.getElementById('mwlEditModality').value=x.modality; document.getElementById('mwlEditStatus').value=x.status; document.getElementById('mwlEditDate').value=x.scheduled_date; document.getElementById('mwlEditTime').value=(x.scheduled_time||'').slice(0,5); const ae=document.getElementById('mwlEditAE');ae.innerHTML='<option value="">Aucune station assignée</option>'+stationTitles.filter(t=>t.active).map(t=>`<option value="${esc(t.ae_title)}">${esc(t.ae_title)}${t.designation?' • '+esc(t.designation):''}</option>`).join('');ae.value=x.station_ae_title||'';document.getElementById('mwlEditEnabled').checked=x.enabled; const modal=document.getElementById('mwlEditModal'); modal.classList.remove('hidden'); modal.classList.add('flex'); };
      window.clinivaMwlClose = () => {const modal=document.getElementById('mwlEditModal');modal.classList.add('hidden');modal.classList.remove('flex');};
      window.clinivaMwlSave = async event => {event.preventDefault();try{const id=document.getElementById('mwlEditId').value;const payload={procedure_description:document.getElementById('mwlEditDescription').value,modality:document.getElementById('mwlEditModality').value,scheduled_date:document.getElementById('mwlEditDate').value,scheduled_time:document.getElementById('mwlEditTime').value,station_ae_title:document.getElementById('mwlEditAE').value,status:document.getElementById('mwlEditStatus').value,enabled:document.getElementById('mwlEditEnabled').checked};const r=await fetch(`/api/worklist/items/${id}/save`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Enregistrement impossible');clinivaMwlClose();await clinivaMwlLoad();message('Élément Worklist mis à jour.');}catch(e){message(e.message,true);}};
      window.clinivaMwlDelete = async id => {if(!confirm('Retirer cet examen du serveur Worklist ? Le passage clinique restera dans Cliniva.'))return;try{const r=await fetch(`/api/worklist/items/${id}/delete`,{method:'POST'});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Suppression impossible');await clinivaMwlLoad();message('Examen retiré de la Worklist.');}catch(e){message(e.message,true);}};
      window.clinivaMwlRemovePatient = async id => {if(!confirm(`Retirer toutes les entrées Worklist du patient #${id} ? Son dossier Cliniva sera conservé.`))return;try{const r=await fetch(`/api/worklist/patients/${id}/delete`,{method:'POST'});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Retrait impossible');await clinivaMwlLoad();message(`${d.removed} examen(s) retiré(s) pour ce patient.`);}catch(e){message(e.message,true);}};
      window.clinivaMwlClearAll = async () => {if(!confirm('Retirer tous les patients de la Worklist DICOM ? Cela suspend toutes les entrées MWL, sans supprimer les dossiers patients ou passages Cliniva.'))return;try{const r=await fetch('/api/worklist/items/delete-all',{method:'POST'});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Vidage impossible');await clinivaMwlLoad();message(`${d.removed} examen(s) retiré(s) de la Worklist.`);}catch(e){message(e.message,true);}};
      window.clinivaMwlRefreshService = async () => {try{const r=await fetch('/api/worklist/service');const d=await r.json();if(!r.ok)throw new Error(d.detail||'État indisponible');const badge=document.getElementById('mwlServiceBadge'),button=document.getElementById('mwlServiceToggle');badge.textContent=d.running?`Serveur actif • ${d.ae_title} • ${d.port}`:`Serveur arrêté • ${d.ae_title}`;badge.className=`rounded-full px-3 py-2 text-xs font-bold ${d.running?'bg-emerald-500/20 text-lime-200':'bg-rose-500/20 text-rose-200'}`;button.textContent=d.running?'Arrêter le serveur':'Démarrer le serveur';button.dataset.running=String(d.running);}catch(e){message(e.message,true);}};
      window.clinivaMwlToggleService = async () => {const button=document.getElementById('mwlServiceToggle'),running=button.dataset.running==='true';if(running&&!confirm('Arrêter le serveur DICOM MWL ? Les modalités ne recevront plus de réponses Worklist jusqu’à son redémarrage.'))return;try{const r=await fetch(`/api/worklist/service/${running?'stop':'start'}`,{method:'POST'});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Commande impossible');await clinivaMwlRefreshService();message(d.running?'Serveur DICOM MWL démarré.':'Serveur DICOM MWL arrêté.');}catch(e){message(e.message,true);}};
      window.clinivaMwlLoadTitles = async () => {try{const r=await fetch('/api/worklist/station-titles'),d=await r.json();if(!r.ok)throw new Error(d.detail||'Chargement des AE Titles impossible');stationTitles=d.items||[];const body=document.getElementById('mwlTitleRows');body.innerHTML=stationTitles.length?stationTitles.map(x=>`<tr class="border-t border-slate-100"><td class="px-3 py-3 font-mono font-bold text-slate-800">${esc(x.ae_title)}</td><td class="px-3 py-3">${esc(x.designation||'—')}</td><td class="px-3 py-3">${esc(x.modality||'Toutes')}</td><td class="px-3 py-3"><span class="rounded-full px-2.5 py-1 text-xs font-bold ${x.active?'bg-emerald-100 text-emerald-700':'bg-slate-200 text-slate-600'}">${x.active?'Actif':'Inactif'}</span></td><td class="px-3 py-3 text-right">${window.CLINIVA_MWL_IS_ADMIN?`<button onclick="clinivaMwlTitleEdit(${Number(x.id)})" class="rounded-lg p-2 text-indigo-600 hover:bg-indigo-100" title="Modifier"><i class="ph-bold ph-pencil-simple"></i></button><button onclick="clinivaMwlTitleToggle(${Number(x.id)},${!x.active})" class="rounded-lg p-2 text-amber-600 hover:bg-amber-100" title="Activer / désactiver"><i class="ph-bold ${x.active?'ph-pause':'ph-play'}"></i></button><button onclick="clinivaMwlTitleDelete(${Number(x.id)})" class="rounded-lg p-2 text-rose-600 hover:bg-rose-100" title="Supprimer"><i class="ph-bold ph-trash"></i></button>`:'Administration'}</td></tr>`).join(''):'<tr><td colspan="5" class="p-5 text-center text-slate-400">Aucun AE Title enregistré.</td></tr>';}catch(e){message(e.message,true);}};
      window.clinivaMwlTitleEdit = id => {const x=stationTitles.find(v=>v.id===id);document.getElementById('mwlTitleId').value=x?.id||'';document.getElementById('mwlTitleAE').value=x?.ae_title||'';document.getElementById('mwlTitleAE').readOnly=!!x;document.getElementById('mwlTitleDesignation').value=x?.designation||'';document.getElementById('mwlTitleModality').value=x?.modality||'';document.getElementById('mwlTitleActive').checked=x?x.active:true;const modal=document.getElementById('mwlTitleModal');modal.classList.remove('hidden');modal.classList.add('flex');};
      window.clinivaMwlTitleClose = () => {const modal=document.getElementById('mwlTitleModal');modal.classList.add('hidden');modal.classList.remove('flex');};
      window.clinivaMwlTitleSave = async event => {event.preventDefault();try{const payload={ae_title:document.getElementById('mwlTitleAE').value,designation:document.getElementById('mwlTitleDesignation').value,modality:document.getElementById('mwlTitleModality').value,active:document.getElementById('mwlTitleActive').checked};const r=await fetch('/api/worklist/station-titles/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),d=await r.json();if(!r.ok)throw new Error(d.detail||'Enregistrement impossible');clinivaMwlTitleClose();await clinivaMwlLoadTitles();message('AE Title enregistré.');}catch(e){message(e.message,true);}};
      window.clinivaMwlTitleToggle = async (id,active) => {try{const r=await fetch(`/api/worklist/station-titles/${id}/toggle`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({active})}),d=await r.json();if(!r.ok)throw new Error(d.detail||'Mise à jour impossible');await clinivaMwlLoadTitles();await clinivaMwlLoad();}catch(e){message(e.message,true);}};
      window.clinivaMwlTitleDelete = async id => {if(!confirm('Supprimer cet AE Title ? Les examens qui lui sont affectés seront désassignés de cette station.'))return;try{const r=await fetch(`/api/worklist/station-titles/${id}/delete`,{method:'POST'}),d=await r.json();if(!r.ok)throw new Error(d.detail||'Suppression impossible');await clinivaMwlLoadTitles();await clinivaMwlLoad();}catch(e){message(e.message,true);}};
      window.clinivaMwlSync = async () => {try{const r=await fetch('/api/worklist/sync',{method:'POST'});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Synchronisation impossible');await clinivaMwlLoad();message(`${d.added} nouvel examen ajouté(s).`);}catch(e){message(e.message,true);}};
      window.clinivaMwlLoad();
      window.clinivaMwlLoadTitles();
      window.clinivaMwlRefreshService();
    })();
    </script>'''
    return markup.replace("__CAN_MANAGE__", "true" if can_manage else "false").replace(
        "__IS_ADMIN__", "true" if is_admin else "false"
    )
