"""Agenda des rendez-vous et passerelle vers les passages patients."""

import logging
import json
from datetime import date, time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from app.auth import get_current_user_optional, track_action
from database import get_db_cursor

logger = logging.getLogger("AppointmentsModule")
appointments_router = APIRouter(prefix="/api/appointments", tags=["Rendez-vous"])

STATUSES = ("En attente", "Confirmé", "En salle d'attente", "Honoré", "Annulé")
EDITABLE_STATUSES = ("En attente", "Confirmé", "En salle d'attente")


def _require_user(request: Request) -> Dict[str, Any]:
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    return user


def _allowed_specialties(user: Dict[str, Any]) -> List[str]:
    value = user.get("specialites_autorisees") or []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _permissions(user: Dict[str, Any]) -> Dict[str, Any]:
    value = user.get("permissions") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _require_permission(request: Request, key: str) -> Dict[str, Any]:
    user = _require_user(request)
    if user.get("role") != "Administrateur" and not _permissions(user).get(key):
        raise HTTPException(status_code=403, detail="Vous n'avez pas l'autorisation pour cette action.")
    return user


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Date invalide : {label}.")


def _normalize(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        data = {
            "id": int(payload["id"]) if payload.get("id") else None,
            "patient_id": int(payload["id_patient"]),
            "practitioner_id": int(payload["id_praticien"]),
            "room_id": int(payload["salle_id"]) if payload.get("salle_id") else None,
            "specialty": str(payload.get("specialite") or "").strip(),
            "appointment_date": _parse_date(str(payload.get("date_rdv") or ""), "du rendez-vous"),
            "start_time": time.fromisoformat(str(payload.get("heure_debut") or "")),
            "end_time": time.fromisoformat(str(payload.get("heure_fin") or "")),
            "reason": str(payload.get("motif") or "").strip(),
            "status": str(payload.get("statut") or "En attente").strip(),
        }
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Les champs obligatoires du rendez-vous sont invalides.")
    if data["start_time"] >= data["end_time"]:
        raise HTTPException(status_code=400, detail="L'heure de fin doit être après l'heure de début.")
    if not data["specialty"] or not data["reason"]:
        raise HTTPException(status_code=400, detail="La spécialité et le motif sont obligatoires.")
    if len(data["reason"]) > 500:
        raise HTTPException(status_code=400, detail="Le motif est limité à 500 caractères.")
    if data["status"] not in EDITABLE_STATUSES:
        raise HTTPException(status_code=400, detail="Statut non modifiable depuis ce formulaire.")
    return data


class AppointmentManager:
    """Accès aux données et règles de réservation."""

    def get_options(self, allowed_specialties: List[str], can_manage: bool) -> Dict[str, Any]:
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT id, nom, prenom, type_personnel, specialite
                FROM clinic_staff
                WHERE actif IS DISTINCT FROM FALSE
                  AND type_personnel IN ('Professeur', 'Docteur', 'Médecin')
                ORDER BY nom, prenom;
            """)
            practitioners = [dict(row) for row in cursor.fetchall()]
            cursor.execute("SELECT id, designation, etage, specialite FROM rooms ORDER BY designation;")
            rooms = [dict(row) for row in cursor.fetchall()]
            cursor.execute("""
                SELECT DISTINCT nom FROM ref_specialites
                WHERE actif IS DISTINCT FROM FALSE ORDER BY nom;
            """)
            specialties = [row["nom"] for row in cursor.fetchall()]
            cursor.execute("""
                SELECT DISTINCT specialite FROM clinic_staff
                WHERE actif IS DISTINCT FROM FALSE
                  AND specialite IS NOT NULL AND btrim(specialite) <> ''
                ORDER BY specialite;
            """)
            specialties.extend(
                row["specialite"] for row in cursor.fetchall()
                if row["specialite"] not in specialties
            )
        if allowed_specialties:
            specialties = [value for value in specialties if value in allowed_specialties]
            practitioners = [
                item for item in practitioners
                if item["specialite"] in allowed_specialties
            ]
            rooms = [
                room for room in rooms
                if room["specialite"] in allowed_specialties
                or room["specialite"] in (None, "", "Polyvalente / Toutes Spécialités")
            ]
        return {
            "practitioners": practitioners,
            "rooms": rooms,
            "specialties": specialties,
            "statuses": list(EDITABLE_STATUSES),
            "can_manage": can_manage,
        }

    def search_patients(self, query: str) -> List[Dict[str, Any]]:
        query = query.strip()
        if len(query) < 2:
            return []
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT id, nom, prenom
                FROM patients
                WHERE nom ILIKE %s OR prenom ILIKE %s
                   OR (nom || ' ' || prenom) ILIKE %s
                   OR (prenom || ' ' || nom) ILIKE %s
                ORDER BY nom, prenom LIMIT 20;
            """, (f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%"))
            return [dict(row) for row in cursor.fetchall()]

    def list_appointments(
        self,
        start_date: date,
        end_date: date,
        practitioner_id: Optional[int],
        specialty: Optional[str],
        status: Optional[str],
        allowed_specialties: List[str],
    ) -> List[Dict[str, Any]]:
        if end_date < start_date or (end_date - start_date).days > 93:
            raise HTTPException(status_code=400, detail="Période invalide (maximum 94 jours).")
        filters = ["a.date_rdv BETWEEN %s AND %s"]
        params: List[Any] = [start_date, end_date]
        if practitioner_id:
            filters.append("a.id_praticien = %s")
            params.append(practitioner_id)
        if specialty:
            filters.append("a.specialite = %s")
            params.append(specialty)
        if status:
            if status not in STATUSES:
                raise HTTPException(status_code=400, detail="Statut de filtre invalide.")
            filters.append("a.statut = %s")
            params.append(status)
        if allowed_specialties:
            filters.append("a.specialite = ANY(%s)")
            params.append(allowed_specialties)
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT a.id, a.id_patient, a.id_praticien, a.salle_id,
                       a.specialite, a.date_rdv, a.heure_debut, a.heure_fin,
                       a.motif, a.statut, a.id_passage, a.created_at,
                       p.nom AS patient_nom, p.prenom AS patient_prenom,
                       s.nom AS praticien_nom, s.prenom AS praticien_prenom,
                       r.designation AS salle_nom
                FROM appointments a
                JOIN patients p ON p.id = a.id_patient
                LEFT JOIN clinic_staff s ON s.id = a.id_praticien
                LEFT JOIN rooms r ON r.id = a.salle_id
                WHERE """ + " AND ".join(filters) + """
                ORDER BY a.date_rdv, a.heure_debut, p.nom, p.prenom;
            """, tuple(params))
            return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def _lock_resources(
        cursor: Any,
        practitioner_ids: List[int],
        room_ids: List[int],
        patient_ids: List[int],
    ) -> None:
        keys = [(1 << 32) + item for item in set(practitioner_ids) if item]
        keys.extend((2 << 32) + item for item in set(room_ids) if item)
        keys.extend((3 << 32) + item for item in set(patient_ids) if item)
        for key in sorted(keys):
            cursor.execute("SELECT pg_advisory_xact_lock(%s);", (key,))

    def save(self, payload: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
        data = _normalize(payload)
        with get_db_cursor() as cursor:
            existing = None
            if data["id"]:
                cursor.execute("""
                    SELECT statut, id_passage, id_praticien, salle_id, id_patient
                    FROM appointments WHERE id=%s FOR UPDATE;
                """, (data["id"],))
                existing = cursor.fetchone()
                if not existing:
                    raise HTTPException(status_code=404, detail="Rendez-vous introuvable.")
                if existing["statut"] in ("Honoré", "Annulé") or existing["id_passage"]:
                    raise HTTPException(status_code=409, detail="Ce rendez-vous est terminé ou annulé.")
            self._lock_resources(
                cursor,
                [data["practitioner_id"], existing["id_praticien"] if existing else None],
                [data["room_id"], existing["salle_id"] if existing else None],
                [data["patient_id"], existing["id_patient"] if existing else None],
            )

            cursor.execute("""
                SELECT specialite FROM clinic_staff
                WHERE id=%s AND actif IS DISTINCT FROM FALSE
                  AND type_personnel IN ('Professeur', 'Docteur', 'Médecin');
            """, (data["practitioner_id"],))
            practitioner = cursor.fetchone()
            if not practitioner:
                raise HTTPException(status_code=400, detail="Praticien introuvable ou inactif.")
            if practitioner["specialite"] and practitioner["specialite"] != data["specialty"]:
                raise HTTPException(status_code=400, detail="La spécialité ne correspond pas au praticien sélectionné.")
            allowed = _allowed_specialties(user)
            if allowed and data["specialty"] not in allowed:
                raise HTTPException(status_code=403, detail="Cette spécialité n'est pas autorisée pour votre compte.")
            cursor.execute("SELECT 1 FROM patients WHERE id=%s;", (data["patient_id"],))
            if not cursor.fetchone():
                raise HTTPException(status_code=400, detail="Patient introuvable.")
            if data["room_id"]:
                cursor.execute("SELECT specialite FROM rooms WHERE id=%s;", (data["room_id"],))
                room = cursor.fetchone()
                if not room:
                    raise HTTPException(status_code=400, detail="Salle introuvable.")
                if room["specialite"] not in (None, "", "Polyvalente / Toutes Spécialités", data["specialty"]):
                    raise HTTPException(status_code=400, detail="La salle n'est pas affectée à cette spécialité.")

            cursor.execute("""
                SELECT id, id_praticien, salle_id, id_patient
                FROM appointments
                WHERE date_rdv=%s AND statut <> 'Annulé'
                  AND id <> COALESCE(%s, -1)
                  AND heure_debut < %s AND heure_fin > %s
                  AND (id_praticien=%s OR (%s IS NOT NULL AND salle_id=%s)
                       OR id_patient=%s)
                LIMIT 1;
            """, (
                data["appointment_date"], data["id"], data["end_time"],
                data["start_time"], data["practitioner_id"],
                data["room_id"], data["room_id"], data["patient_id"],
            ))
            conflict = cursor.fetchone()
            if conflict:
                if conflict["id_praticien"] == data["practitioner_id"]:
                    resource = "praticien"
                elif data["room_id"] and conflict["salle_id"] == data["room_id"]:
                    resource = "salle"
                else:
                    resource = "patient"
                raise HTTPException(status_code=409, detail=f"Ce créneau chevauche un autre rendez-vous pour ce {resource}.")

            values = (
                data["patient_id"], data["practitioner_id"], data["specialty"],
                data["room_id"], data["appointment_date"], data["start_time"],
                data["end_time"], data["reason"], data["status"],
            )
            if existing:
                cursor.execute("""
                    UPDATE appointments
                    SET id_patient=%s, id_praticien=%s, specialite=%s, salle_id=%s,
                        date_rdv=%s, heure_debut=%s, heure_fin=%s, motif=%s,
                        statut=%s, updated_at=CURRENT_TIMESTAMP
                    WHERE id=%s RETURNING id;
                """, values + (data["id"],))
            else:
                cursor.execute("""
                    INSERT INTO appointments
                        (id_patient,id_praticien,specialite,salle_id,date_rdv,
                         heure_debut,heure_fin,motif,statut,created_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id;
                """, values + (str(user.get("login") or ""),))
            appointment_id = cursor.fetchone()["id"]
        return {"success": True, "id": appointment_id}

    def cancel(self, appointment_id: int) -> Dict[str, Any]:
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT statut, id_passage FROM appointments WHERE id=%s FOR UPDATE;
            """, (appointment_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Rendez-vous introuvable.")
            if row["id_passage"] or row["statut"] == "Honoré":
                raise HTTPException(status_code=409, detail="Un rendez-vous converti en passage ne peut pas être annulé ici.")
            cursor.execute("""
                UPDATE appointments SET statut='Annulé', updated_at=CURRENT_TIMESTAMP
                WHERE id=%s;
            """, (appointment_id,))
        return {"success": True, "id": appointment_id}

    def convert_to_passage(self, appointment_id: int, user: Dict[str, Any]) -> Dict[str, Any]:
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT id, id_patient, specialite, date_rdv, heure_debut, statut, id_passage
                FROM appointments WHERE id=%s FOR UPDATE;
            """, (appointment_id,))
            appointment = cursor.fetchone()
            if not appointment:
                raise HTTPException(status_code=404, detail="Rendez-vous introuvable.")
            if appointment["id_passage"]:
                return {"success": True, "id_passage": appointment["id_passage"], "already_converted": True}
            if appointment["statut"] == "Annulé":
                raise HTTPException(status_code=409, detail="Un rendez-vous annulé ne peut pas être converti.")
            cursor.execute("""
                INSERT INTO patient_passages
                    (id_patient,date_passage,heure_passage,utilisateur,type_passage,
                     actes_json,montant_total,remise,versement,reste,mode_paiement,
                     statut_paiement,questionnaire_json,documents_scannes,statut)
                VALUES (%s,%s,%s,%s,%s,'[]'::jsonb,0,0,0,0,'Espèces','Non payé',
                        '{}'::jsonb,'[]'::jsonb,'En attente')
                RETURNING id;
            """, (
                appointment["id_patient"], appointment["date_rdv"],
                appointment["heure_debut"].strftime("%H:%M"),
                str(user.get("login") or "admin"), appointment["specialite"],
            ))
            passage_id = cursor.fetchone()["id"]
            cursor.execute("""
                UPDATE appointments SET statut='Honoré', id_passage=%s,
                    updated_at=CURRENT_TIMESTAMP WHERE id=%s;
            """, (passage_id, appointment_id))
        return {"success": True, "id_passage": passage_id}


appointment_manager = AppointmentManager()


@appointments_router.get("/options")
async def api_appointment_options(request: Request):
    user = _require_permission(request, "rdv_view")
    can_manage = user.get("role") == "Administrateur" or bool(_permissions(user).get("rdv_manage"))
    return appointment_manager.get_options(_allowed_specialties(user), can_manage)


@appointments_router.get("/patients")
async def api_appointment_patients(request: Request, q: str = Query(default="", max_length=80)):
    _require_permission(request, "rdv_manage")
    return {"patients": appointment_manager.search_patients(q)}


@appointments_router.get("")
async def api_list_appointments(
    request: Request,
    date_debut: str,
    date_fin: str,
    id_praticien: Optional[int] = None,
    specialite: Optional[str] = None,
    statut: Optional[str] = None,
):
    user = _require_permission(request, "rdv_view")
    return {"appointments": appointment_manager.list_appointments(
        _parse_date(date_debut, "de début"), _parse_date(date_fin, "de fin"),
        id_praticien, specialite, statut, _allowed_specialties(user)
    )}


@appointments_router.post("/save")
async def api_save_appointment(payload: Dict[str, Any], request: Request):
    user = _require_permission(request, "rdv_manage")
    result = appointment_manager.save(payload, user)
    if result.get("success"):
        track_action(request, "RENDEZ_VOUS_ENREGISTRE", f"Rendez-vous #{result['id']} enregistré")
    return result


@appointments_router.post("/{appointment_id}/cancel")
async def api_cancel_appointment(appointment_id: int, request: Request):
    _require_permission(request, "rdv_manage")
    result = appointment_manager.cancel(appointment_id)
    track_action(request, "RENDEZ_VOUS_ANNULE", f"Rendez-vous #{appointment_id} annulé")
    return result


@appointments_router.post("/{appointment_id}/convert")
async def api_convert_appointment(appointment_id: int, request: Request):
    user = _require_permission(request, "rdv_manage")
    result = appointment_manager.convert_to_passage(appointment_id, user)
    if not result.get("already_converted"):
        track_action(request, "RENDEZ_VOUS_CONVERTI", f"Rendez-vous #{appointment_id} transféré en passage #{result['id_passage']}")
    return result


def render_html() -> str:
    """Construit l'interface de l'agenda."""
    return r"""
<div class="p-4 md:p-6 space-y-5">
  <div class="flex flex-wrap items-center justify-between gap-3">
    <div><h1 class="text-2xl font-black text-slate-800 dark:text-white">Rendez-vous</h1><p class="text-sm text-slate-500 dark:text-slate-400">Agenda des praticiens et des salles</p></div>
    <button id="rdvNew" class="px-4 py-2.5 rounded-xl bg-brand-neon text-black font-bold">+ Nouveau rendez-vous</button>
  </div>
  <div class="flex flex-wrap items-center gap-2 p-3 bg-white dark:bg-[#131d16] border border-slate-200 dark:border-white/10 rounded-2xl">
    <button id="rdvPrev" class="px-3 py-2 rounded-lg bg-slate-100 dark:bg-white/10">‹</button><button id="rdvToday" class="px-3 py-2 rounded-lg bg-slate-100 dark:bg-white/10">Aujourd'hui</button><button id="rdvNext" class="px-3 py-2 rounded-lg bg-slate-100 dark:bg-white/10">›</button>
    <strong id="rdvPeriod" class="min-w-40 px-2 text-slate-800 dark:text-white"></strong>
    <div class="ml-auto flex gap-1"><button data-view="day" class="rdvView px-3 py-2 rounded-lg">Jour</button><button data-view="week" class="rdvView px-3 py-2 rounded-lg">Semaine</button><button data-view="month" class="rdvView px-3 py-2 rounded-lg">Mois</button></div>
  </div>
  <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
    <select id="rdvFilterPractitioner" class="p-3 rounded-xl border bg-white dark:bg-[#131d16] text-slate-800 dark:text-white"><option value="">Tous les praticiens</option></select>
    <select id="rdvFilterSpecialty" class="p-3 rounded-xl border bg-white dark:bg-[#131d16] text-slate-800 dark:text-white"><option value="">Toutes les spécialités</option></select>
    <select id="rdvFilterStatus" class="p-3 rounded-xl border bg-white dark:bg-[#131d16] text-slate-800 dark:text-white"><option value="">Tous les statuts</option><option>En attente</option><option>Confirmé</option><option>En salle d'attente</option><option>Honoré</option><option>Annulé</option></select>
  </div>
  <div id="rdvMessage" class="hidden p-3 rounded-xl text-sm"></div>
  <div id="rdvCalendar" class="bg-white dark:bg-[#131d16] border border-slate-200 dark:border-white/10 rounded-2xl p-3 md:p-4 overflow-x-auto"></div>
</div>
<div id="rdvModal" class="fixed inset-0 z-[110] hidden items-center justify-center bg-black/60 p-4">
  <div class="w-full max-w-2xl max-h-[90vh] overflow-y-auto bg-white dark:bg-[#131d16] rounded-2xl p-5 shadow-2xl">
    <div class="flex justify-between items-center mb-4"><h2 id="rdvModalTitle" class="text-lg font-black text-slate-800 dark:text-white">Nouveau rendez-vous</h2><button id="rdvClose" class="text-2xl text-slate-400">×</button></div>
    <form id="rdvForm" class="grid grid-cols-1 md:grid-cols-2 gap-3">
      <input id="rdvId" type="hidden"><div class="md:col-span-2 relative">
        <label class="block text-xs font-bold mb-1 text-slate-600 dark:text-slate-300">Patient</label><input id="rdvPatientSearch" autocomplete="off" required placeholder="Saisir au moins 2 caractères du nom" class="w-full p-3 rounded-xl border bg-slate-50 dark:bg-black/20 text-slate-800 dark:text-white"><input id="rdvPatientId" type="hidden">
        <div id="rdvPatientResults" class="absolute z-20 top-full left-0 right-0 hidden max-h-52 overflow-y-auto rounded-xl border bg-white dark:bg-[#101812] shadow-xl"></div>
      </div>
      <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Spécialité<select id="rdvSpecialty" required class="mt-1 w-full p-3 rounded-xl border bg-slate-50 dark:bg-black/20 text-slate-800 dark:text-white"></select></label>
      <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Praticien<select id="rdvPractitioner" required class="mt-1 w-full p-3 rounded-xl border bg-slate-50 dark:bg-black/20 text-slate-800 dark:text-white"></select></label>
      <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Salle (facultatif)<select id="rdvRoom" class="mt-1 w-full p-3 rounded-xl border bg-slate-50 dark:bg-black/20 text-slate-800 dark:text-white"></select></label>
      <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Statut<select id="rdvStatus" class="mt-1 w-full p-3 rounded-xl border bg-slate-50 dark:bg-black/20 text-slate-800 dark:text-white"></select></label>
      <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Date<input id="rdvDate" type="date" required class="mt-1 w-full p-3 rounded-xl border bg-slate-50 dark:bg-black/20 text-slate-800 dark:text-white"></label>
      <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Début<input id="rdvStart" type="time" required class="mt-1 w-full p-3 rounded-xl border bg-slate-50 dark:bg-black/20 text-slate-800 dark:text-white"></label>
      <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Fin<input id="rdvEnd" type="time" required class="mt-1 w-full p-3 rounded-xl border bg-slate-50 dark:bg-black/20 text-slate-800 dark:text-white"></label>
      <label class="md:col-span-2 text-xs font-bold text-slate-600 dark:text-slate-300">Motif<textarea id="rdvReason" required maxlength="500" rows="3" class="mt-1 w-full p-3 rounded-xl border bg-slate-50 dark:bg-black/20 text-slate-800 dark:text-white"></textarea></label>
      <div class="md:col-span-2 flex justify-end gap-2 pt-2"><button type="button" id="rdvCancel" class="px-4 py-2 rounded-xl bg-slate-100 dark:bg-white/10">Fermer</button><button type="submit" class="px-4 py-2 rounded-xl bg-brand-neon text-black font-bold">Enregistrer</button></div>
    </form>
  </div>
</div>
<script>
(function(){
 const root=document.getElementById('rdvCalendar'); if(!root)return;
 const state={date:new Date(),view:'week',options:{},appointments:[],patients:[]};
 const el=id=>document.getElementById(id), pad=n=>String(n).padStart(2,'0');
 const iso=d=>d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate()), datePart=v=>String(v).slice(0,10);
 const esc=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const dayLabel=s=>new Date(s+'T12:00:00').toLocaleDateString('fr-FR',{weekday:'short',day:'numeric',month:'short'});
 const range=()=>{const d=new Date(state.date);if(state.view==='day')return[iso(d),iso(d)];if(state.view==='week'){const a=new Date(d);a.setDate(d.getDate()-(d.getDay()+6)%7);const b=new Date(a);b.setDate(a.getDate()+6);return[iso(a),iso(b)];}return[iso(new Date(d.getFullYear(),d.getMonth(),1)),iso(new Date(d.getFullYear(),d.getMonth()+1,0))];};
 const message=(s,error)=>{const x=el('rdvMessage');x.textContent=s;x.className='p-3 rounded-xl text-sm '+(error?'bg-rose-100 text-rose-700':'bg-emerald-100 text-emerald-700');if(!s)x.classList.add('hidden');};
 const card=a=>'<article class="rdvCard mb-1 rounded-lg border-l-4 p-2 text-left '+(a.statut==='Annulé'?'border-rose-400 bg-rose-50':a.statut==='Honoré'?'border-emerald-500 bg-emerald-50':a.statut==='Confirmé'?'border-sky-400 bg-sky-50':'border-amber-400 bg-amber-50')+'" data-id="'+a.id+'"><div class="font-bold text-xs text-slate-800">'+esc(String(a.heure_debut).slice(0,5))+'–'+esc(String(a.heure_fin).slice(0,5))+' · '+esc(a.patient_prenom+' '+a.patient_nom)+'</div><div class="text-[11px] text-slate-600">'+esc(a.praticien_prenom+' '+a.praticien_nom)+(a.salle_nom?' · '+esc(a.salle_nom):'')+'</div><div class="text-[10px] text-slate-500">'+esc(a.specialite)+' · '+esc(a.statut)+'</div>'+(a.id_passage?'<div class="text-[10px] font-bold text-emerald-700">Passage #'+a.id_passage+'</div>':'')+'</article>';
 const render=()=>{const r=range();el('rdvPeriod').textContent=state.view==='month'?state.date.toLocaleDateString('fr-FR',{month:'long',year:'numeric'}):(r[0]===r[1]?dayLabel(r[0]):dayLabel(r[0])+' – '+dayLabel(r[1]));
  let days=[],d=new Date(r[0]+'T12:00:00'),last=new Date(r[1]+'T12:00:00');for(;d<=last;d.setDate(d.getDate()+1))days.push(iso(d));
  if(state.view==='month'){const f=new Date(r[0]+'T12:00:00'),l=new Date(r[1]+'T12:00:00');f.setDate(f.getDate()-(f.getDay()+6)%7);l.setDate(l.getDate()+(6-(l.getDay()+6)%7));days=[];for(d=new Date(f);d<=l;d.setDate(d.getDate()+1))days.push(iso(d));const names=['Lun','Mar','Mer','Jeu','Ven','Sam','Dim'];let html='<div class="grid grid-cols-7 min-w-[700px] text-center text-xs font-bold text-slate-500">'+names.map(x=>'<div class="p-2">'+x+'</div>').join('')+'</div><div class="grid grid-cols-7 min-w-[700px] border-l border-t">';html+=days.map(day=>'<div class="min-h-28 border-r border-b p-1 '+(day.slice(0,7)===iso(state.date).slice(0,7)?'':'opacity-40')+'"><div class="text-xs font-bold mb-1">'+Number(day.slice(8,10))+'</div>'+state.appointments.filter(a=>datePart(a.date_rdv)===day).map(card).join('')+'</div>').join('');root.innerHTML=html+'</div>';return;}
  if(state.view==='day'){root.innerHTML='<div class="max-w-4xl mx-auto"><div class="mb-3 font-bold">'+dayLabel(days[0])+'</div>'+state.appointments.map(card).join('')+'</div>';return;}
  root.innerHTML='<div class="grid grid-cols-1 md:grid-cols-7 min-w-[700px] border-l border-t">'+days.map(day=>'<section class="min-h-48 border-r border-b p-2"><h3 class="mb-2 text-xs font-bold">'+dayLabel(day)+'</h3>'+state.appointments.filter(a=>datePart(a.date_rdv)===day).map(card).join('')+'</section>').join('')+'</div>';
 };
 const load=async()=>{try{const r=range(),p=new URLSearchParams({date_debut:r[0],date_fin:r[1]});if(el('rdvFilterPractitioner').value)p.set('id_praticien',el('rdvFilterPractitioner').value);if(el('rdvFilterSpecialty').value)p.set('specialite',el('rdvFilterSpecialty').value);if(el('rdvFilterStatus').value)p.set('statut',el('rdvFilterStatus').value);const res=await fetch('/api/appointments?'+p);const data=await res.json();if(!res.ok)throw Error(data.detail||'Chargement impossible.');state.appointments=data.appointments||[];message('',false);render();}catch(e){message(e.message,true);}};
 const fill=(select,items,label,val,text)=>{select.innerHTML='<option value="">'+esc(label)+'</option>'+items.map(x=>'<option value="'+esc(val(x))+'">'+esc(text(x))+'</option>').join('');};
 const options=async()=>{const res=await fetch('/api/appointments/options');if(!res.ok)throw Error('Chargement des référentiels impossible.');state.options=await res.json();fill(el('rdvFilterPractitioner'),state.options.practitioners,'Tous les praticiens',x=>x.id,x=>x.prenom+' '+x.nom);fill(el('rdvFilterSpecialty'),state.options.specialties,'Toutes les spécialités',x=>x,x=>x);fill(el('rdvSpecialty'),state.options.specialties,'Choisir une spécialité',x=>x,x=>x);fill(el('rdvPractitioner'),state.options.practitioners,'Choisir un praticien',x=>x.id,x=>x.prenom+' '+x.nom);fill(el('rdvRoom'),state.options.rooms,'Sans salle dédiée',x=>x.id,x=>x.designation);fill(el('rdvStatus'),state.options.statuses,'En attente',x=>x,x=>x);};
 const open=a=>{el('rdvForm').reset();el('rdvId').value=a?a.id:'';el('rdvPatientId').value=a?a.id_patient:'';el('rdvPatientSearch').value=a?a.patient_nom+' '+a.patient_prenom:'';el('rdvSpecialty').value=a?a.specialite:'';el('rdvSpecialty').onchange();el('rdvPractitioner').value=a?a.id_praticien:'';el('rdvRoom').value=a&&a.salle_id?a.salle_id:'';el('rdvStatus').value=a?a.statut:'En attente';el('rdvDate').value=a?datePart(a.date_rdv):iso(state.date);el('rdvStart').value=a?String(a.heure_debut).slice(0,5):'09:00';el('rdvEnd').value=a?String(a.heure_fin).slice(0,5):'09:30';el('rdvReason').value=a?a.motif:'';el('rdvModalTitle').textContent=a?'Modifier le rendez-vous':'Nouveau rendez-vous';el('rdvModal').classList.remove('hidden');el('rdvModal').classList.add('flex');};
 const close=()=>{el('rdvModal').classList.add('hidden');el('rdvModal').classList.remove('flex');};
 const req=async(url,method,body)=>{const res=await fetch(url,{method:method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const data=await res.json();if(!res.ok)throw Error(data.detail||'La demande a échoué.');return data;};
 const movePeriod=direction=>{if(state.view==='month')state.date=new Date(state.date.getFullYear(),state.date.getMonth()+direction,1);else state.date.setDate(state.date.getDate()+direction*(state.view==='day'?1:7));load();};
 el('rdvPrev').onclick=()=>movePeriod(-1);el('rdvNext').onclick=()=>movePeriod(1);el('rdvToday').onclick=()=>{state.date=new Date();load();};
 document.querySelectorAll('.rdvView').forEach(b=>b.onclick=()=>{state.view=b.dataset.view;document.querySelectorAll('.rdvView').forEach(x=>x.className='rdvView px-3 py-2 rounded-lg');b.className='rdvView px-3 py-2 rounded-lg bg-brand-neon text-black font-bold';load();});
 ['rdvFilterPractitioner','rdvFilterSpecialty','rdvFilterStatus'].forEach(id=>el(id).onchange=load);el('rdvNew').onclick=()=>open(null);el('rdvClose').onclick=close;el('rdvCancel').onclick=close;
 el('rdvPatientSearch').oninput=async e=>{el('rdvPatientId').value='';const box=el('rdvPatientResults');if(e.target.value.length<2){box.classList.add('hidden');return;}try{const res=await fetch('/api/appointments/patients?q='+encodeURIComponent(e.target.value));const data=await res.json();state.patients=data.patients||[];box.innerHTML=state.patients.map(p=>'<button type="button" data-patient="'+p.id+'" class="block w-full border-b p-2 text-left text-sm hover:bg-slate-100">'+esc(p.nom+' '+p.prenom)+' · #'+p.id+'</button>').join('')||'<div class="p-3 text-xs">Aucun patient trouvé</div>';box.classList.remove('hidden');}catch(_){box.classList.add('hidden');}};
 el('rdvPatientResults').onclick=e=>{const b=e.target.closest('[data-patient]');if(!b)return;const p=state.patients.find(x=>String(x.id)===b.dataset.patient);if(p){el('rdvPatientId').value=p.id;el('rdvPatientSearch').value=p.nom+' '+p.prenom;el('rdvPatientResults').classList.add('hidden');}};
 el('rdvSpecialty').onchange=()=>{const selected=el('rdvSpecialty').value,providers=state.options.practitioners.filter(p=>!selected||!p.specialite||p.specialite===selected);fill(el('rdvPractitioner'),providers,'Choisir un praticien',p=>p.id,p=>p.prenom+' '+p.nom);};
 el('rdvForm').onsubmit=async e=>{e.preventDefault();try{if(!el('rdvPatientId').value)throw Error('Choisissez un patient dans la liste.');const x=await req('/api/appointments/save','POST',{id:el('rdvId').value||null,id_patient:el('rdvPatientId').value,id_praticien:el('rdvPractitioner').value,salle_id:el('rdvRoom').value||null,specialite:el('rdvSpecialty').value,date_rdv:el('rdvDate').value,heure_debut:el('rdvStart').value,heure_fin:el('rdvEnd').value,motif:el('rdvReason').value,statut:el('rdvStatus').value});close();message('Rendez-vous #'+x.id+' enregistré.',false);await load();}catch(err){message(err.message,true);}};
 root.onclick=async e=>{const aEl=e.target.closest('.rdvCard');if(!aEl)return;const a=state.appointments.find(x=>String(x.id)===aEl.dataset.id);if(!a)return;const button=e.target.closest('[data-action]');if(button){try{if(button.dataset.action==='edit')open(a);if(button.dataset.action==='cancel'&&confirm('Annuler ce rendez-vous ?')){await req('/api/appointments/'+a.id+'/cancel','POST');await load();}if(button.dataset.action==='convert'&&confirm('Créer un passage à partir de ce rendez-vous ?')){const r=await req('/api/appointments/'+a.id+'/convert','POST');message('Passage #'+r.id_passage+' créé.',false);await load();}}catch(err){message(err.message,true);}return;}if(!state.options.can_manage){message(a.motif,false);return;}if(a.id_passage){message('Passage associé : #'+a.id_passage,false);return;}if(a.statut==='Annulé'){message('Ce rendez-vous est annulé.',true);return;}if(aEl.querySelector('[data-actions]'))return;const actions=document.createElement('div');actions.dataset.actions='1';actions.className='flex gap-1 mt-2';actions.innerHTML='<button data-action="edit" class="px-2 py-1 rounded bg-white text-[10px] font-bold">Modifier</button><button data-action="convert" class="px-2 py-1 rounded bg-emerald-200 text-[10px] font-bold">Créer le passage</button><button data-action="cancel" class="px-2 py-1 rounded bg-rose-100 text-[10px] font-bold">Annuler</button>';aEl.appendChild(actions);};
 (async()=>{try{await options();if(!state.options.can_manage)el('rdvNew').classList.add('hidden');await load();}catch(e){message(e.message,true);}})();
})();
</script>
"""


__all__ = ["appointments_router", "render_html"]
