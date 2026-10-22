"""Agenda des rendez-vous et passerelle vers les passages patients."""

import logging
import json
from datetime import date, datetime, time, timedelta
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


def _allowed_schedule_ids(user: Dict[str, Any]) -> Optional[List[int]]:
    """None = tous les plannings (administrateur ou ancien compte non configuré)."""
    if user.get("role") == "Administrateur":
        return None
    value = user.get("plannings_rdv_autorises")
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if not isinstance(value, list):
        return []
    try:
        return sorted({int(item) for item in value if int(item) > 0})
    except (TypeError, ValueError):
        return []


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
            "patient_id": int(payload["id_patient"]) if payload.get("id_patient") else None,
            "patient_name": str(payload.get("patient_nom_libre") or "").strip(),
            "practitioner_id": int(payload["id_praticien"]),
            "room_id": int(payload["salle_id"]) if payload.get("salle_id") else None,
            "specialty": str(payload.get("specialite") or "").strip(),
            "exam_type_id": int(payload["type_examen_id"]),
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
    if not data["specialty"]:
        raise HTTPException(status_code=400, detail="La spécialité est obligatoire.")
    if not data["patient_id"] and not data["patient_name"]:
        raise HTTPException(status_code=400, detail="Saisissez le nom du patient ou sélectionnez une fiche existante.")
    if len(data["patient_name"]) > 200:
        raise HTTPException(status_code=400, detail="Le nom du patient est limité à 200 caractères.")
    if len(data["reason"]) > 500:
        raise HTTPException(status_code=400, detail="Le motif est limité à 500 caractères.")
    if data["status"] not in EDITABLE_STATUSES:
        raise HTTPException(status_code=400, detail="Statut non modifiable depuis ce formulaire.")
    return data


WEEKDAYS = ("Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche")


def _normalize_schedule(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        schedule_id = int(payload["id"]) if payload.get("id") else None
        specialty = str(payload.get("specialite") or "").strip()
        raw_days = payload.get("jours_semaine")
        if raw_days is None:
            raw_days = [payload.get("jour_semaine")]
        weekdays = sorted({int(day) for day in raw_days})
        start = time.fromisoformat(str(payload.get("heure_debut") or ""))
        end = time.fromisoformat(str(payload.get("heure_fin") or ""))
        interval = int(payload.get("intervalle_minutes"))
        capacity = int(payload.get("capacite_patients"))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Les paramètres du planning sont invalides.")
    if not specialty or not weekdays or any(day not in range(7) for day in weekdays):
        raise HTTPException(status_code=400, detail="Choisissez une spécialité et au moins un jour valides.")
    if start >= end:
        raise HTTPException(status_code=400, detail="L'heure de fin doit être après l'heure de début.")
    duration = int((datetime.combine(date.today(), end) - datetime.combine(date.today(), start)).total_seconds() // 60)
    if interval < 5 or interval > 240 or duration % interval:
        raise HTTPException(status_code=400, detail="L'intervalle doit être de 5 à 240 minutes et diviser exactement la plage horaire.")
    if capacity < 1 or capacity > 500:
        raise HTTPException(status_code=400, detail="La capacité doit être comprise entre 1 et 500 patients par créneau.")
    return {"id": schedule_id, "specialty": specialty, "weekday": weekdays[0], "weekdays": weekdays, "start": start,
            "end": end, "interval": interval, "capacity": capacity,
            "active": bool(payload.get("actif", True))}


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
            cursor.execute("SELECT id, designation, etage, specialite FROM rooms ORDER BY id ASC;")
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
            cursor.execute("""
                SELECT id, designation, specialite, modalite, categorie
                FROM consultation_types
                ORDER BY designation;
            """)
            exam_types = [dict(row) for row in cursor.fetchall()]
        if allowed_specialties:
            specialties = [value for value in specialties if value in allowed_specialties]
        return {
            "practitioners": practitioners,
            "rooms": rooms,
            "specialties": specialties,
            "exam_types": exam_types,
            "statuses": list(EDITABLE_STATUSES),
            "can_manage": can_manage,
        }

    @staticmethod
    def _schedule_dict(row: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(row)
        item["heure_debut"] = item["heure_debut"].strftime("%H:%M")
        item["heure_fin"] = item["heure_fin"].strftime("%H:%M")
        weekdays = item.get("jours_semaine") or [item["jour_semaine"]]
        item["jours_semaine"] = sorted({int(day) for day in weekdays})
        item["jours_nom"] = ", ".join(WEEKDAYS[day] for day in item["jours_semaine"])
        item["jour_nom"] = item["jours_nom"]
        return item

    def list_schedules(self, allowed_specialties: List[str], allowed_schedule_ids: Optional[List[int]]) -> List[Dict[str, Any]]:
        with get_db_cursor() as cursor:
            if allowed_specialties:
                cursor.execute("""
                    SELECT id, specialite, jour_semaine, jours_semaine, heure_debut, heure_fin,
                           intervalle_minutes, capacite_patients, actif
                    FROM appointment_specialty_schedules
                    WHERE specialite = ANY(%s)
                      AND (%s::INTEGER[] IS NULL OR id=ANY(%s::INTEGER[]))
                    ORDER BY specialite, jour_semaine, heure_debut;
                """, (allowed_specialties, allowed_schedule_ids, allowed_schedule_ids))
            else:
                cursor.execute("""
                    SELECT id, specialite, jour_semaine, jours_semaine, heure_debut, heure_fin,
                           intervalle_minutes, capacite_patients, actif
                    FROM appointment_specialty_schedules
                    WHERE (%s::INTEGER[] IS NULL OR id=ANY(%s::INTEGER[]))
                    ORDER BY specialite, jour_semaine, heure_debut;
                """, (allowed_schedule_ids, allowed_schedule_ids))
            return [self._schedule_dict(row) for row in cursor.fetchall()]

    def save_schedule(self, payload: Dict[str, Any], allowed_specialties: List[str]) -> Dict[str, Any]:
        data = _normalize_schedule(payload)
        weekdays = data["weekdays"]
        if allowed_specialties and data["specialty"] not in allowed_specialties:
            raise HTTPException(status_code=403, detail="Cette spécialité n'est pas autorisée pour votre compte.")
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT EXISTS(SELECT 1 FROM ref_specialites WHERE nom=%s AND actif IS DISTINCT FROM FALSE)
                    OR EXISTS(SELECT 1 FROM clinic_staff WHERE specialite=%s AND actif IS DISTINCT FROM FALSE)
                    AS known;
            """, (data["specialty"], data["specialty"]))
            if not cursor.fetchone()["known"]:
                raise HTTPException(status_code=400, detail="La spécialité sélectionnée n'existe plus dans les référentiels.")
            if data["active"]:
                cursor.execute("""
                    SELECT id, jours_semaine FROM appointment_specialty_schedules
                    WHERE specialite=%s AND actif=TRUE AND id IS DISTINCT FROM %s
                      AND jours_semaine && %s::SMALLINT[]
                      AND heure_debut < %s AND heure_fin > %s
                    LIMIT 1;
                """, (data["specialty"], data["id"], weekdays, data["end"], data["start"]))
                conflict = cursor.fetchone()
                if conflict:
                    common_days = sorted(set(int(day) for day in conflict["jours_semaine"]) & set(weekdays))
                    names = ", ".join(WEEKDAYS[day] for day in common_days)
                    raise HTTPException(status_code=409, detail=f"Cette plage chevauche un autre planning actif ({names}).")
            values = (data["specialty"], data["weekday"], weekdays, data["start"], data["end"],
                      data["interval"], data["capacity"], data["active"])
            if data["id"]:
                cursor.execute("""
                    UPDATE appointment_specialty_schedules
                    SET specialite=%s, jour_semaine=%s, jours_semaine=%s::SMALLINT[], heure_debut=%s, heure_fin=%s,
                        intervalle_minutes=%s, capacite_patients=%s, actif=%s, updated_at=CURRENT_TIMESTAMP
                    WHERE id=%s RETURNING id;
                """, values + (data["id"],))
            else:
                cursor.execute("""
                    INSERT INTO appointment_specialty_schedules
                        (specialite, jour_semaine, jours_semaine, heure_debut, heure_fin,
                         intervalle_minutes, capacite_patients, actif)
                    VALUES (%s,%s,%s::SMALLINT[],%s,%s,%s,%s,%s) RETURNING id;
                """, values)
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Planning introuvable.")
        return {"success": True, "id": row["id"], "jours_semaine": weekdays}

    def delete_schedule(self, schedule_id: int, allowed_specialties: List[str], allowed_schedule_ids: Optional[List[int]]) -> Dict[str, Any]:
        with get_db_cursor() as cursor:
            if allowed_schedule_ids is not None and schedule_id not in allowed_schedule_ids:
                raise HTTPException(status_code=403, detail="Ce planning n'est pas attribué à votre compte.")
            if allowed_specialties:
                cursor.execute("""
                    DELETE FROM appointment_specialty_schedules
                    WHERE id=%s AND specialite=ANY(%s) RETURNING id;
                """, (schedule_id, allowed_specialties))
            else:
                cursor.execute("DELETE FROM appointment_specialty_schedules WHERE id=%s RETURNING id;", (schedule_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Planning introuvable ou non autorisé.")
        return {"success": True}

    def get_slots(
        self,
        specialty: str,
        appointment_date: date,
        exclude_id: Optional[int] = None,
        practitioner_id: Optional[int] = None,
        patient_id: Optional[int] = None,
        room_id: Optional[int] = None,
        allowed_schedule_ids: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        with get_db_cursor() as cursor:
            query = """
                SELECT id, heure_debut, heure_fin, intervalle_minutes, capacite_patients
                FROM appointment_specialty_schedules
                WHERE specialite=%s AND %s = ANY(jours_semaine) AND actif=TRUE
            """
            params: List[Any] = [specialty, appointment_date.weekday()]
            if allowed_schedule_ids is not None:
                query += " AND id=ANY(%s)"
                params.append(allowed_schedule_ids)
            query += " ORDER BY heure_debut;"
            cursor.execute(query, tuple(params))
            schedules = cursor.fetchall()
            if not schedules:
                return []
            cursor.execute("""
                SELECT id_patient, id_praticien, salle_id, specialite, heure_debut, heure_fin
                FROM appointments
                WHERE date_rdv=%s AND statut <> 'Annulé' AND id IS DISTINCT FROM %s;
            """, (appointment_date, exclude_id))
            existing = [dict(row) for row in cursor.fetchall()]

        slots = []
        for schedule in schedules:
            start = datetime.combine(appointment_date, schedule["heure_debut"])
            end = datetime.combine(appointment_date, schedule["heure_fin"])
            duration = timedelta(minutes=int(schedule["intervalle_minutes"]))
            while start + duration <= end:
                slot_end = start + duration
                count = sum(1 for row in existing if row["specialite"] == specialty
                            and row["heure_debut"] == start.time() and row["heure_fin"] == slot_end.time())
                capacity = int(schedule["capacite_patients"])
                overlapping = [row for row in existing
                               if row["heure_debut"] < slot_end.time() and row["heure_fin"] > start.time()]
                reason = ""
                if patient_id and any(row["id_patient"] == patient_id for row in overlapping):
                    reason = "Patient déjà occupé"
                elif room_id and any(row["salle_id"] == room_id for row in overlapping):
                    reason = "Salle déjà occupée"
                elif practitioner_id and any(
                    row["id_praticien"] == practitioner_id
                    and not (row["specialite"] == specialty
                             and row["heure_debut"] == start.time()
                             and row["heure_fin"] == slot_end.time())
                    for row in overlapping
                ):
                    reason = "Praticien déjà occupé"
                elif count >= capacity:
                    reason = "Capacité atteinte"
                slots.append({"planning_id": schedule["id"], "heure_debut": start.strftime("%H:%M"),
                              "heure_fin": slot_end.strftime("%H:%M"), "capacite": capacity,
                              "affectes": count, "places_restantes": max(0, capacity - count),
                              "indisponible": bool(reason), "motif_indisponibilite": reason})
                start = slot_end
        return slots

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
        allowed_schedule_ids: Optional[List[int]],
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
        if allowed_schedule_ids is not None:
            filters.append("""EXISTS (
                SELECT 1 FROM appointment_specialty_schedules sch
                WHERE sch.id=ANY(%s) AND sch.specialite=a.specialite
                  AND ((EXTRACT(ISODOW FROM a.date_rdv)::INTEGER - 1) = ANY(sch.jours_semaine))
                  AND a.heure_debut >= sch.heure_debut AND a.heure_fin <= sch.heure_fin
            )""")
            params.append(allowed_schedule_ids)
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT a.id, a.id_patient, a.id_praticien, a.salle_id,
                       a.specialite, a.type_examen_id, a.type_examen,
                       a.date_rdv, a.heure_debut, a.heure_fin,
                       a.motif, a.statut, a.id_passage, a.created_at,
                       COALESCE(NULLIF(a.patient_nom_saisi, ''), p.nom, '') AS patient_nom,
                       CASE WHEN NULLIF(a.patient_nom_saisi, '') IS NOT NULL THEN '' ELSE COALESCE(p.prenom, '') END AS patient_prenom,
                       s.nom AS praticien_nom, s.prenom AS praticien_prenom,
                       r.designation AS salle_nom
                FROM appointments a
                LEFT JOIN patients p ON p.id = a.id_patient
                LEFT JOIN clinic_staff s ON s.id = a.id_praticien
                LEFT JOIN rooms r ON r.id = a.salle_id
                WHERE """ + " AND ".join(filters) + """
                ORDER BY a.date_rdv, a.heure_debut, COALESCE(a.patient_nom_saisi, p.nom), p.prenom;
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
                    SELECT statut, id_passage, id_praticien, salle_id, id_patient,
                           specialite, date_rdv, heure_debut, heure_fin
                    FROM appointments WHERE id=%s FOR UPDATE;
                """, (data["id"],))
                existing = cursor.fetchone()
                if not existing:
                    raise HTTPException(status_code=404, detail="Rendez-vous introuvable.")
                if existing["statut"] in ("Honoré", "Annulé") or existing["id_passage"]:
                    raise HTTPException(status_code=409, detail="Ce rendez-vous est terminé ou annulé.")
                allowed_ids = _allowed_schedule_ids(user)
                if allowed_ids is not None:
                    cursor.execute("""
                        SELECT EXISTS(SELECT 1 FROM appointment_specialty_schedules
                            WHERE id=ANY(%s) AND specialite=%s
                              AND %s=ANY(jours_semaine)
                              AND %s >= heure_debut AND %s <= heure_fin) AS allowed;
                    """, (allowed_ids, existing["specialite"], existing["date_rdv"].weekday(),
                          existing["heure_debut"], existing["heure_fin"]))
                    if not cursor.fetchone()["allowed"]:
                        raise HTTPException(status_code=403, detail="Ce rendez-vous dépend d'un planning qui ne vous est pas attribué.")
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
            cursor.execute("SELECT designation, specialite FROM consultation_types WHERE id=%s;", (data["exam_type_id"],))
            exam_type = cursor.fetchone()
            if not exam_type:
                raise HTTPException(status_code=400, detail="Le type d'examen sélectionné n'existe plus.")
            if exam_type["specialite"] and exam_type["specialite"] != data["specialty"]:
                raise HTTPException(status_code=400, detail="Le type d'examen ne correspond pas à la spécialité du rendez-vous.")
            allowed = _allowed_specialties(user)
            if allowed and data["specialty"] not in allowed:
                raise HTTPException(status_code=403, detail="Cette spécialité n'est pas autorisée pour votre compte.")
            if data["patient_id"]:
                cursor.execute("SELECT 1 FROM patients WHERE id=%s;", (data["patient_id"],))
                if not cursor.fetchone():
                    raise HTTPException(status_code=400, detail="Patient introuvable.")
            if data["room_id"]:
                cursor.execute("SELECT id FROM rooms WHERE id=%s;", (data["room_id"],))
                room = cursor.fetchone()
                if not room:
                    raise HTTPException(status_code=400, detail="Salle introuvable.")

            schedule_query = """
                SELECT id, heure_debut, heure_fin, intervalle_minutes, capacite_patients
                FROM appointment_specialty_schedules
                WHERE specialite=%s AND %s = ANY(jours_semaine) AND actif=TRUE
            """
            schedule_params: List[Any] = [data["specialty"], data["appointment_date"].weekday()]
            allowed_schedule_ids = _allowed_schedule_ids(user)
            if allowed_schedule_ids is not None:
                schedule_query += " AND id=ANY(%s)"
                schedule_params.append(allowed_schedule_ids)
            schedule_query += " ORDER BY heure_debut;"
            cursor.execute(schedule_query, tuple(schedule_params))
            day_schedules = cursor.fetchall()
            has_specialty_schedule = bool(day_schedules)
            configured_slot = None
            for schedule in day_schedules:
                slot_minutes = int(schedule["intervalle_minutes"])
                slot_delta = timedelta(minutes=slot_minutes)
                schedule_start = datetime.combine(data["appointment_date"], schedule["heure_debut"])
                appointment_start = datetime.combine(data["appointment_date"], data["start_time"])
                appointment_end = datetime.combine(data["appointment_date"], data["end_time"])
                elapsed_minutes = int((appointment_start - schedule_start).total_seconds() // 60)
                if (appointment_start >= schedule_start and appointment_end <= datetime.combine(data["appointment_date"], schedule["heure_fin"])
                        and elapsed_minutes >= 0 and elapsed_minutes % slot_minutes == 0
                        and appointment_end - appointment_start == slot_delta):
                    configured_slot = schedule
                    break
            if not has_specialty_schedule:
                raise HTTPException(status_code=400, detail="Aucun planning actif n'est configuré pour cette spécialité. Configurez le planning avant de prendre le rendez-vous.")
            if not configured_slot:
                raise HTTPException(status_code=400, detail="Choisissez un créneau prévu dans le planning de cette spécialité.")
            if configured_slot:
                slot_lock = f"appointment-slot:{data['specialty']}:{data['appointment_date'].isoformat()}:{data['start_time'].isoformat()}:{data['end_time'].isoformat()}"
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", (slot_lock,))
                cursor.execute("""
                    SELECT COUNT(*) AS assigned
                    FROM appointments
                    WHERE specialite=%s AND date_rdv=%s
                      AND heure_debut=%s AND heure_fin=%s AND statut <> 'Annulé'
                      AND id IS DISTINCT FROM %s;
                """, (data["specialty"], data["appointment_date"], data["start_time"],
                      data["end_time"], data["id"]))
                assigned = int(cursor.fetchone()["assigned"] or 0)
                if assigned >= int(configured_slot["capacite_patients"]):
                    raise HTTPException(status_code=409, detail="Ce créneau a atteint sa capacité maximale de patients.")

            cursor.execute("""
                SELECT id, id_praticien, salle_id, id_patient
                FROM appointments
                WHERE date_rdv=%s AND statut <> 'Annulé'
                  AND id <> COALESCE(%s, -1)
                  AND heure_debut < %s AND heure_fin > %s
                  AND (id_patient=%s
                       OR (id_praticien=%s AND NOT (%s=TRUE AND heure_debut=%s AND heure_fin=%s AND specialite=%s))
                       OR (%s IS NOT NULL AND salle_id=%s))
                LIMIT 1;
            """, (
                data["appointment_date"], data["id"], data["end_time"],
                data["start_time"], data["patient_id"], data["practitioner_id"],
                bool(configured_slot), data["start_time"], data["end_time"], data["specialty"],
                data["room_id"], data["room_id"],
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
                data["patient_id"], data["patient_name"] if not data["patient_id"] else "",
                data["practitioner_id"], data["specialty"], data["exam_type_id"], exam_type["designation"],
                data["room_id"], data["appointment_date"], data["start_time"],
                data["end_time"], data["reason"], data["status"],
            )
            if existing:
                cursor.execute("""
                    UPDATE appointments
                    SET id_patient=%s, patient_nom_saisi=%s, id_praticien=%s, specialite=%s,
                        type_examen_id=%s, type_examen=%s, salle_id=%s,
                        date_rdv=%s, heure_debut=%s, heure_fin=%s, motif=%s,
                        statut=%s, updated_at=CURRENT_TIMESTAMP
                    WHERE id=%s RETURNING id;
                """, values + (data["id"],))
            else:
                cursor.execute("""
                    INSERT INTO appointments
                        (id_patient,patient_nom_saisi,id_praticien,specialite,type_examen_id,type_examen,salle_id,date_rdv,
                         heure_debut,heure_fin,motif,statut,created_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id;
                """, values + (str(user.get("login") or ""),))
            appointment_id = cursor.fetchone()["id"]
        return {"success": True, "id": appointment_id}

    def cancel(self, appointment_id: int, user: Dict[str, Any]) -> Dict[str, Any]:
        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT statut, id_passage, specialite, date_rdv, heure_debut, heure_fin FROM appointments WHERE id=%s FOR UPDATE;
            """, (appointment_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Rendez-vous introuvable.")
            allowed_ids = _allowed_schedule_ids(user)
            if allowed_ids is not None:
                cursor.execute("""SELECT EXISTS(SELECT 1 FROM appointment_specialty_schedules
                    WHERE id=ANY(%s) AND specialite=%s AND %s=ANY(jours_semaine)
                      AND %s>=heure_debut AND %s<=heure_fin) AS allowed;""",
                    (allowed_ids, row["specialite"], row["date_rdv"].weekday(), row["heure_debut"], row["heure_fin"]))
                if not cursor.fetchone()["allowed"]:
                    raise HTTPException(status_code=403, detail="Ce rendez-vous dépend d'un planning qui ne vous est pas attribué.")
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
            allowed_ids = _allowed_schedule_ids(user)
            if allowed_ids is not None:
                cursor.execute("""SELECT EXISTS(SELECT 1 FROM appointment_specialty_schedules
                    WHERE id=ANY(%s) AND specialite=%s AND %s=ANY(jours_semaine)
                      AND %s>=heure_debut AND %s<heure_fin) AS allowed;""",
                    (allowed_ids, appointment["specialite"], appointment["date_rdv"].weekday(), appointment["heure_debut"], appointment["heure_debut"]))
                if not cursor.fetchone()["allowed"]:
                    raise HTTPException(status_code=403, detail="Ce rendez-vous dépend d'un planning qui ne vous est pas attribué.")
            if appointment["id_passage"]:
                return {"success": True, "id_passage": appointment["id_passage"], "already_converted": True}
            if appointment["statut"] == "Annulé":
                raise HTTPException(status_code=409, detail="Un rendez-vous annulé ne peut pas être converti.")
            if not appointment["id_patient"]:
                raise HTTPException(status_code=409, detail="Ce rendez-vous est au nom d'un patient non enregistré. Créez d'abord sa fiche patient pour générer un passage.")
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


@appointments_router.get("/schedules")
async def api_appointment_schedules(request: Request):
    user = _require_permission(request, "rdv_view")
    return {"schedules": appointment_manager.list_schedules(_allowed_specialties(user), _allowed_schedule_ids(user))}


@appointments_router.post("/schedules/save")
async def api_save_appointment_schedule(payload: Dict[str, Any], request: Request):
    user = _require_permission(request, "rdv_manage")
    allowed_ids = _allowed_schedule_ids(user)
    if allowed_ids is not None:
        try:
            requested_id = int(payload["id"]) if payload.get("id") else None
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Identifiant de planning invalide.")
        if requested_id is None or requested_id not in allowed_ids:
            raise HTTPException(status_code=403, detail="Vous ne pouvez gérer que les plannings attribués à votre compte. Contactez un administrateur pour en créer un autre.")
    result = appointment_manager.save_schedule(payload, _allowed_specialties(user))
    track_action(request, "PLANNING_RENDEZ_VOUS_ENREGISTRE", f"Planning #{result['id']} enregistré")
    return result


@appointments_router.delete("/schedules/{schedule_id}")
async def api_delete_appointment_schedule(schedule_id: int, request: Request):
    user = _require_permission(request, "rdv_manage")
    result = appointment_manager.delete_schedule(schedule_id, _allowed_specialties(user), _allowed_schedule_ids(user))
    track_action(request, "PLANNING_RENDEZ_VOUS_SUPPRIME", f"Planning #{schedule_id} supprimé")
    return result


@appointments_router.get("/slots")
async def api_appointment_slots(
    request: Request,
    specialite: str,
    date_rdv: str,
    exclude_id: Optional[int] = None,
    id_praticien: Optional[int] = None,
    id_patient: Optional[int] = None,
    salle_id: Optional[int] = None,
):
    user = _require_permission(request, "rdv_manage")
    specialty = specialite.strip()
    allowed = _allowed_specialties(user)
    if allowed and specialty not in allowed:
        raise HTTPException(status_code=403, detail="Cette spécialité n'est pas autorisée pour votre compte.")
    slots = appointment_manager.get_slots(
        specialty, _parse_date(date_rdv, "du rendez-vous"), exclude_id,
        id_praticien, id_patient, salle_id, _allowed_schedule_ids(user),
    )
    with get_db_cursor() as cursor:
        allowed_ids = _allowed_schedule_ids(user)
        query = """SELECT EXISTS(
            SELECT 1 FROM appointment_specialty_schedules
            WHERE specialite=%s AND actif=TRUE AND %s = ANY(jours_semaine)
        """
        params: List[Any] = [specialty, _parse_date(date_rdv, "du rendez-vous").weekday()]
        if allowed_ids is not None:
            query += " AND id=ANY(%s)"
            params.append(allowed_ids)
        query += ") AS configured;"
        cursor.execute(query, tuple(params))
        configured = bool(cursor.fetchone()["configured"])
    return {"slots": slots, "planning_configured": configured}


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
        id_praticien, specialite, statut, _allowed_specialties(user), _allowed_schedule_ids(user)
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
    user = _require_permission(request, "rdv_manage")
    result = appointment_manager.cancel(appointment_id, user)
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
  <div id="rdvHeader" class="flex flex-wrap items-center justify-between gap-3">
    <div><h1 id="rdvHeaderTitle" class="text-2xl font-black text-slate-800 dark:text-white">Rendez-vous</h1><p id="rdvHeaderSubtitle" class="text-sm text-slate-500 dark:text-slate-400">Connectez-vous à un planning pour ouvrir son agenda.</p></div>
    <div class="flex flex-wrap gap-2"><button id="rdvChangePlanning" type="button" class="hidden px-4 py-2.5 rounded-xl border border-slate-200 bg-white text-slate-700 font-bold dark:border-white/10 dark:bg-white/5 dark:text-white"><i class="ph-bold ph-arrows-left-right mr-1"></i>Changer de planning</button><button id="rdvSchedules" class="px-4 py-2.5 rounded-xl border border-slate-200 bg-white text-slate-700 font-bold dark:border-white/10 dark:bg-white/5 dark:text-white"><i class="ph-bold ph-calendar-dots mr-1"></i>Gérer les plannings</button><button id="rdvNew" class="px-4 py-2.5 rounded-xl bg-brand-neon text-black font-bold">+ Nouveau rendez-vous</button></div>
  </div>
  <div id="rdvMessage" class="hidden p-3 rounded-xl text-sm"></div>
  <div id="rdvEmptyState" class="hidden"></div>
  <div id="rdvPlannerLayout" class="hidden">
    <section class="min-w-0 rounded-2xl border border-slate-200 bg-white p-4 dark:border-white/10 dark:bg-[#131d16]"><header class="mb-4 flex flex-wrap items-center justify-between gap-3"><div><p class="text-[10px] font-extrabold uppercase tracking-widest text-slate-400">Agenda journalier</p><h2 id="rdvDailyTitle" class="text-lg font-black text-slate-800 dark:text-white">Sélectionnez un planning</h2></div><div class="flex flex-wrap items-center justify-end gap-2"><button id="rdvDailyPrint" type="button" class="hidden rounded-xl bg-brand-neon px-3 py-2 text-xs font-extrabold text-black"><i class="ph-bold ph-printer mr-1"></i>Imprimer / PDF</button><button id="rdvDailyPrev" type="button" aria-label="Jour précédent" class="rounded-xl border px-3 py-2 font-bold dark:border-white/10 dark:text-white">‹</button><button id="rdvDailyToday" type="button" class="rounded-xl border px-3 py-2 text-xs font-bold dark:border-white/10 dark:text-white">Aujourd’hui</button><button id="rdvDailyNext" type="button" aria-label="Jour suivant" class="rounded-xl border px-3 py-2 font-bold dark:border-white/10 dark:text-white">›</button><strong id="rdvDailyDate" class="min-w-32 text-right text-sm text-slate-700 dark:text-slate-200"></strong></div></header><div id="rdvDailyAgenda" class="overflow-x-auto"></div></section>
  </div>
</div>
<div id="rdvConnectModal" class="fixed inset-0 z-[2147482000] hidden items-center justify-center bg-slate-950/75 p-4 backdrop-blur-sm"><section role="dialog" aria-modal="true" aria-labelledby="rdvConnectTitle" class="w-full max-w-2xl rounded-3xl border border-slate-200 bg-white p-6 shadow-2xl dark:border-white/10 dark:bg-[#131d16]"><div class="mb-5"><span class="mb-3 inline-flex h-12 w-12 items-center justify-center rounded-2xl bg-indigo-50 text-indigo-600 dark:bg-indigo-400/10 dark:text-indigo-300"><i class="ph-bold ph-calendar-check text-2xl"></i></span><h2 id="rdvConnectTitle" class="text-xl font-black text-slate-900 dark:text-white">Choisissez votre planning</h2><p id="rdvConnectSubtitle" class="mt-1 text-sm text-slate-500">Sélectionnez le service et le planning auxquels vous connecter.</p></div><div id="rdvPlanningList" class="grid max-h-[55vh] grid-cols-1 gap-3 overflow-y-auto sm:grid-cols-2"></div><div id="rdvConnectEmpty" class="hidden rounded-xl bg-amber-50 p-4 text-sm text-amber-800 dark:bg-amber-400/10 dark:text-amber-200">Aucun planning actif n’est disponible pour votre compte.</div><div class="mt-5 flex justify-end"><button id="rdvConnectManage" type="button" class="hidden rounded-xl bg-brand-neon px-4 py-2.5 text-sm font-extrabold text-black"><i class="ph-bold ph-plus mr-1"></i>Créer un planning</button></div></section></div>
<div id="rdvModal" class="fixed inset-0 z-[110] hidden items-center justify-center bg-black/60 p-4">
  <div class="w-full max-w-2xl max-h-[90vh] overflow-y-auto bg-white dark:bg-[#131d16] rounded-2xl p-5 shadow-2xl">
    <div class="flex justify-between items-center mb-4"><h2 id="rdvModalTitle" class="text-lg font-black text-slate-800 dark:text-white">Nouveau rendez-vous</h2><button id="rdvClose" class="text-2xl text-slate-400">×</button></div>
    <form id="rdvForm" class="grid grid-cols-1 md:grid-cols-2 gap-3">
      <input id="rdvId" type="hidden"><div class="md:col-span-2 relative">
        <label class="block text-xs font-bold mb-1 text-slate-600 dark:text-slate-300">Patient <span class="font-normal text-slate-400">(rechercher une fiche ou saisir un nom libre)</span></label><input id="rdvPatientSearch" autocomplete="off" required maxlength="200" placeholder="Nom et prénom du patient" class="w-full p-3 rounded-xl border bg-slate-50 dark:bg-black/20 text-slate-800 dark:text-white"><input id="rdvPatientId" type="hidden">
        <div id="rdvPatientResults" class="absolute z-20 top-full left-0 right-0 hidden max-h-52 overflow-y-auto rounded-xl border bg-white dark:bg-[#101812] shadow-xl"></div>
      </div>
      <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Spécialité<select id="rdvSpecialty" required class="mt-1 w-full p-3 rounded-xl border bg-slate-50 dark:bg-black/20 text-slate-800 dark:text-white"></select></label>
      <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Type d'examen<select id="rdvExamType" required class="mt-1 w-full p-3 rounded-xl border bg-slate-50 dark:bg-black/20 text-slate-800 dark:text-white"><option value="">Choisir un type d'examen</option></select></label>
      <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Praticien<select id="rdvPractitioner" required class="mt-1 w-full p-3 rounded-xl border bg-slate-50 dark:bg-black/20 text-slate-800 dark:text-white"></select></label>
      <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Salle (facultatif)<select id="rdvRoom" class="mt-1 w-full p-3 rounded-xl border bg-slate-50 dark:bg-black/20 text-slate-800 dark:text-white"></select></label>
      <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Statut<select id="rdvStatus" class="mt-1 w-full p-3 rounded-xl border bg-slate-50 dark:bg-black/20 text-slate-800 dark:text-white"></select></label>
      <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Date<input id="rdvDate" type="date" required class="mt-1 w-full p-3 rounded-xl border bg-slate-50 dark:bg-black/20 text-slate-800 dark:text-white"></label>
      <select id="rdvSlotSelect" class="hidden" aria-hidden="true" tabindex="-1"><option value="">Aucun créneau disponible</option></select>
      <label class="text-xs font-bold text-slate-600 dark:text-slate-300">Heure du rendez-vous<input id="rdvSlotTimeDisplay" type="text" readonly placeholder="Choisir une case verte dans l’agenda" class="mt-1 w-full rounded-xl border bg-slate-100 p-3 text-slate-700 dark:border-white/10 dark:bg-white/5 dark:text-white"><span id="rdvSlotCapacity" class="mt-1 block text-[10px] font-medium text-slate-500">Le premier créneau disponible sera proposé automatiquement.</span></label>
      <input id="rdvStart" type="hidden"><input id="rdvEnd" type="hidden">
      <label class="md:col-span-2 text-xs font-bold text-slate-600 dark:text-slate-300">Motif (facultatif)<textarea id="rdvReason" maxlength="500" rows="3" class="mt-1 w-full p-3 rounded-xl border bg-slate-50 dark:bg-black/20 text-slate-800 dark:text-white"></textarea></label>
      <div class="md:col-span-2 flex justify-end gap-2 pt-2"><button type="button" id="rdvCancel" class="px-4 py-2 rounded-xl bg-slate-100 dark:bg-white/10">Fermer</button><button id="rdvSaveBtn" type="submit" disabled class="px-4 py-2 rounded-xl bg-brand-neon text-black font-bold disabled:cursor-not-allowed disabled:opacity-40">Enregistrer</button></div>
    </form>
  </div>
</div>
<div id="rdvScheduleModal" style="z-index:2147483000" class="fixed inset-0 hidden items-center justify-center bg-slate-950/70 p-3 sm:p-5 backdrop-blur-sm">
  <section role="dialog" aria-modal="true" aria-labelledby="rdvScheduleTitle" class="flex max-h-[94vh] w-full max-w-5xl flex-col overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-2xl dark:border-white/10 dark:bg-[#131d16]">
    <header class="flex items-center justify-between border-b border-slate-100 p-5 dark:border-white/10"><div><h2 id="rdvScheduleTitle" class="text-lg font-black text-slate-900 dark:text-white">Plannings par spécialité</h2><p class="mt-1 text-xs text-slate-500">Définissez les plages hebdomadaires et la capacité de chaque créneau.</p></div><button id="rdvScheduleClose" type="button" aria-label="Fermer" class="rounded-xl p-2 text-xl text-slate-500 hover:bg-slate-100 dark:hover:bg-white/10">×</button></header>
    <div class="grid min-h-0 gap-5 overflow-y-auto p-5 lg:grid-cols-[minmax(260px,0.8fr)_1.2fr]">
      <form id="rdvScheduleForm" class="h-fit space-y-3 rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-white/10 dark:bg-black/20">
        <input id="rdvScheduleId" type="hidden">
        <label class="block text-xs font-bold text-slate-600 dark:text-slate-300">Spécialité<select id="rdvScheduleSpecialty" required class="mt-1 w-full rounded-xl border bg-white p-3 dark:border-white/10 dark:bg-black/20 dark:text-white"></select></label>
        <fieldset class="rounded-xl border border-slate-200 bg-white p-3 dark:border-white/10 dark:bg-black/20"><legend class="px-1 text-xs font-bold text-slate-600 dark:text-slate-300">Jours du planning</legend><label class="mb-2 flex items-center gap-2 rounded-lg bg-indigo-50 px-2.5 py-2 text-xs font-extrabold text-indigo-700 dark:bg-indigo-400/10 dark:text-indigo-200"><input id="rdvScheduleAllDays" type="checkbox" class="rounded border-indigo-300">Toute la semaine</label><div id="rdvScheduleWeekdayChoices" class="grid grid-cols-2 gap-2 sm:grid-cols-4"><label class="flex items-center gap-2 rounded-lg border border-slate-100 px-2.5 py-2 text-xs font-semibold dark:border-white/5"><input type="checkbox" name="rdvScheduleWeekdays" value="0" class="rounded">Lundi</label><label class="flex items-center gap-2 rounded-lg border border-slate-100 px-2.5 py-2 text-xs font-semibold dark:border-white/5"><input type="checkbox" name="rdvScheduleWeekdays" value="1" class="rounded">Mardi</label><label class="flex items-center gap-2 rounded-lg border border-slate-100 px-2.5 py-2 text-xs font-semibold dark:border-white/5"><input type="checkbox" name="rdvScheduleWeekdays" value="2" class="rounded">Mercredi</label><label class="flex items-center gap-2 rounded-lg border border-slate-100 px-2.5 py-2 text-xs font-semibold dark:border-white/5"><input type="checkbox" name="rdvScheduleWeekdays" value="3" class="rounded">Jeudi</label><label class="flex items-center gap-2 rounded-lg border border-slate-100 px-2.5 py-2 text-xs font-semibold dark:border-white/5"><input type="checkbox" name="rdvScheduleWeekdays" value="4" class="rounded">Vendredi</label><label class="flex items-center gap-2 rounded-lg border border-slate-100 px-2.5 py-2 text-xs font-semibold dark:border-white/5"><input type="checkbox" name="rdvScheduleWeekdays" value="5" class="rounded">Samedi</label><label class="flex items-center gap-2 rounded-lg border border-slate-100 px-2.5 py-2 text-xs font-semibold dark:border-white/5"><input type="checkbox" name="rdvScheduleWeekdays" value="6" class="rounded">Dimanche</label></div></fieldset>
        <div class="grid grid-cols-2 gap-2"><label class="text-xs font-bold text-slate-600 dark:text-slate-300">Début<input id="rdvScheduleStart" type="time" required class="mt-1 w-full rounded-xl border bg-white p-3 dark:border-white/10 dark:bg-black/20 dark:text-white"></label><label class="text-xs font-bold text-slate-600 dark:text-slate-300">Fin<input id="rdvScheduleEnd" type="time" required class="mt-1 w-full rounded-xl border bg-white p-3 dark:border-white/10 dark:bg-black/20 dark:text-white"></label></div>
        <div class="grid grid-cols-2 gap-2"><label class="text-xs font-bold text-slate-600 dark:text-slate-300">Intervalle (minutes)<input id="rdvScheduleInterval" type="number" min="5" max="240" step="5" value="30" required class="mt-1 w-full rounded-xl border bg-white p-3 dark:border-white/10 dark:bg-black/20 dark:text-white"></label><label class="text-xs font-bold text-slate-600 dark:text-slate-300">Patients / créneau<input id="rdvScheduleCapacity" type="number" min="1" max="500" value="1" required class="mt-1 w-full rounded-xl border bg-white p-3 dark:border-white/10 dark:bg-black/20 dark:text-white"></label></div>
        <label class="flex items-center gap-2 text-xs font-semibold text-slate-600 dark:text-slate-300"><input id="rdvScheduleActive" type="checkbox" checked class="rounded">Planning actif</label>
        <div class="flex gap-2"><button type="submit" class="flex-1 rounded-xl bg-brand-neon px-4 py-2.5 text-xs font-extrabold text-black">Enregistrer le planning</button><button id="rdvScheduleReset" type="button" class="rounded-xl border px-3 py-2.5 text-xs font-bold dark:border-white/10 dark:text-white">Nouveau</button></div>
        <p id="rdvScheduleMessage" class="hidden rounded-xl p-3 text-xs"></p>
      </form>
      <div class="min-w-0 overflow-x-auto rounded-2xl border border-slate-200 dark:border-white/10"><table class="w-full min-w-[620px] text-left text-xs"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-500 dark:bg-white/5"><tr><th class="p-3">Spécialité / jour</th><th class="p-3">Plage</th><th class="p-3">Intervalle</th><th class="p-3">Capacité</th><th class="p-3">État</th><th class="p-3 text-right">Actions</th></tr></thead><tbody id="rdvScheduleRows" class="divide-y divide-slate-100 dark:divide-white/5"></tbody></table></div>
    </div>
  </section>
</div>
<script>
(function(){
 const oldScheduleModal=document.querySelector('body > #rdvScheduleModal');if(oldScheduleModal)oldScheduleModal.remove();
 const root=document.getElementById('rdvDailyAgenda'); if(!root)return;
 const state={date:new Date(),view:'day',options:{},appointments:[],patients:[],schedules:[],currentSlots:[],selectedPlanningId:''};let slotRequest=0;
 const el=id=>document.getElementById(id), pad=n=>String(n).padStart(2,'0');
 const scheduleModal=el('rdvScheduleModal');if(scheduleModal&&scheduleModal.parentElement!==document.body)document.body.appendChild(scheduleModal);
 const iso=d=>d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate()), datePart=v=>String(v).slice(0,10);
 const esc=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const dayLabel=s=>new Date(s+'T12:00:00').toLocaleDateString('fr-FR',{weekday:'long',day:'numeric',month:'long',year:'numeric'});
 const message=(s,error)=>{const x=el('rdvMessage');x.textContent=s;x.className='p-3 rounded-xl text-sm '+(error?'bg-rose-100 text-rose-700':'bg-emerald-100 text-emerald-700');if(!s)x.classList.add('hidden');};
 const card=a=>'<article class="rdvCard rounded-lg border-l-4 border-white/80 bg-white/95 px-2.5 py-1.5 text-left shadow-sm" data-id="'+a.id+'"><div class="flex flex-wrap items-center gap-x-2 text-sm font-extrabold leading-5 text-slate-800"><span>'+esc((a.patient_prenom+' '+a.patient_nom).trim())+(a.id_patient?'':' <span class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-bold text-slate-500">Non enregistré</span>')+'</span><span class="text-slate-400">·</span><span>'+esc(a.type_examen||'Examen non précisé')+'</span><span class="text-slate-400">·</span><span class="text-slate-600">'+esc(a.salle_nom||'Salle non affectée')+'</span></div><div class="mt-0.5 flex flex-wrap items-center gap-x-3 text-xs font-bold leading-5 text-slate-700"><span>'+esc(datePart(a.date_rdv))+' · '+esc(String(a.heure_debut).slice(0,5))+'–'+esc(String(a.heure_fin).slice(0,5))+'</span><span>'+esc((a.praticien_prenom+' '+a.praticien_nom).trim()||'Médecin non affecté')+'</span><span class="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-extrabold">'+esc(a.statut)+'</span>'+(a.id_passage?'<span class="text-[10px] font-extrabold text-emerald-700">Passage #'+a.id_passage+'</span>':'')+'</div></article>';
 const printDailyAgenda=()=>{const plan=selectedPlan();if(!plan){message('Connectez-vous à un planning avant de lancer l’impression.',true);return;}const printFrame=document.createElement('iframe');printFrame.setAttribute('aria-hidden','true');printFrame.style.cssText='position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden';document.body.appendChild(printFrame);const doc=printFrame.contentDocument;const agenda=root.cloneNode(true);agenda.querySelectorAll('button[data-book-slot]').forEach(button=>{const label=document.createElement('div');label.className=button.className;label.textContent='Aucun rendez-vous · Créneau disponible';button.replaceWith(label);});const styles=[...document.querySelectorAll('link[rel="stylesheet"]')].map(link=>'<link rel="stylesheet" href="'+esc(link.href)+'">').join('');doc.open();doc.write('<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Planning du '+esc(dayLabel(iso(state.date)))+'</title>'+styles+'<style>@page{size:A4 portrait;margin:12mm}*{-webkit-print-color-adjust:exact!important;print-color-adjust:exact!important}body{margin:0;background:#fff;color:#172033;font:10pt Arial,sans-serif}.print-title{margin:0 0 4px;font-size:20pt;color:#172033}.print-meta{margin:0 0 16px;color:#475569;font-size:10pt}#printAgenda{width:100%;overflow:visible!important}#printAgenda>div{overflow:visible!important;border:1px solid #cbd5e1}#printAgenda table{width:100%;border-collapse:collapse}#printAgenda tr{break-inside:avoid;page-break-inside:avoid}#printAgenda th,#printAgenda td{padding:6px;border-bottom:1px solid #dbe2ea}#printAgenda .rdvCard{margin:3px 0;padding:6px;border:1px solid #cbd5e1;border-left:4px solid #6366f1;border-radius:5px;background:#f8fafc;color:#172033}#printAgenda .rdvCard *{color:#172033!important}#printAgenda [data-book-slot],#printAgenda button{display:block;width:100%;padding:7px;border:1px solid #86efac;border-radius:5px;background:#f0fdf4;color:#166534;text-align:left;font-size:9pt}.print-footer{margin-top:12px;color:#64748b;font-size:8pt}@media print{a{color:inherit;text-decoration:none}}</style></head><body><h1 class="print-title">Planning des rendez-vous</h1><p class="print-meta">'+esc(plan.specialite)+' · '+esc(dayLabel(iso(state.date)))+' · '+esc(plan.heure_debut)+'–'+esc(plan.heure_fin)+'</p><main id="printAgenda">'+agenda.innerHTML+'</main><p class="print-footer">Document généré depuis Cliniva · '+esc(new Date().toLocaleString('fr-FR'))+'</p></body></html>');doc.close();setTimeout(()=>{try{printFrame.contentWindow.focus();printFrame.contentWindow.print();}catch(error){message('La fenêtre d’impression n’a pas pu être ouverte : '+error.message,true);}setTimeout(()=>printFrame.remove(),1200);},500);};
 const scheduleDays=plan=>Array.isArray(plan.jours_semaine)&&plan.jours_semaine.length?plan.jours_semaine.map(Number):[Number(plan.jour_semaine)];
 const activeSchedules=()=>state.schedules.filter(s=>s.actif).sort((a,b)=>String(a.specialite).localeCompare(String(b.specialite),'fr')||Math.min(...scheduleDays(a))-Math.min(...scheduleDays(b))||String(a.heure_debut).localeCompare(String(b.heure_debut)));
 const selectedPlan=()=>activeSchedules().find(s=>String(s.id)===String(state.selectedPlanningId));
 const moveToNextPlanDay=plan=>{const today=(state.date.getDay()+6)%7,delta=Math.min(...scheduleDays(plan).map(day=>(day-today+7)%7));if(delta)state.date.setDate(state.date.getDate()+delta);};
 const renderPlanningList=()=>{const schedules=activeSchedules();el('rdvPlanningList').innerHTML=schedules.map(plan=>{const selected=String(state.selectedPlanningId)===String(plan.id);return '<button type="button" data-planning="'+esc(plan.id)+'" class="w-full rounded-xl border p-3 text-left transition '+(selected?'border-indigo-500 bg-indigo-50 ring-2 ring-indigo-100 dark:bg-indigo-500/10':'border-slate-200 hover:border-indigo-300 dark:border-white/10 dark:hover:border-indigo-400')+'"><span class="flex items-start gap-3"><span class="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl '+(selected?'bg-indigo-600 text-white':'bg-slate-100 text-indigo-600 dark:bg-white/10')+'"><i class="ph-bold ph-calendar-blank text-lg"></i></span><span class="min-w-0"><b class="block text-sm text-slate-800 dark:text-white">'+esc(plan.specialite)+'</b><small class="mt-1 block text-[10px] leading-4 text-slate-500">'+esc(plan.jours_nom||plan.jour_nom)+' · '+esc(plan.heure_debut)+'–'+esc(plan.heure_fin)+'</small><small class="block text-[10px] text-slate-400">Créneaux de '+esc(plan.intervalle_minutes)+' min · '+esc(plan.capacite_patients)+' patient(s) max.</small></span></span></button>';}).join('')||'<p class="rounded-xl bg-slate-50 p-4 text-center text-xs text-slate-400 dark:bg-white/5">Aucun planning actif.</p>';};
 const render=()=>{const day=iso(state.date),plan=selectedPlan(),specialty=plan&&plan.specialite;el('rdvDailyDate').textContent=state.date.toLocaleDateString('fr-FR',{weekday:'short',day:'2-digit',month:'short',year:'numeric'});el('rdvDailyTitle').textContent=specialty||'Sélectionnez un planning';el('rdvHeaderTitle').textContent=plan?'Rendez-vous ('+plan.specialite+' · '+plan.jour_nom+' '+plan.heure_debut+'–'+plan.heure_fin+')':'Rendez-vous';el('rdvHeaderSubtitle').textContent=plan?'Planning connecté · '+plan.capacite_patients+' patient(s) maximum par créneau':'Connectez-vous à un planning pour ouvrir son agenda.';el('rdvChangePlanning').classList.toggle('hidden',!plan);el('rdvDailyPrint').classList.toggle('hidden',!plan);renderPlanningList();if(!plan){root.innerHTML='<div class="flex min-h-80 items-center justify-center p-8 text-center"><div><div class="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-2xl bg-indigo-50 text-indigo-600 dark:bg-indigo-400/10"><i class="ph-bold ph-hand-tap text-2xl"></i></div><h3 class="font-bold text-slate-700 dark:text-white">Choisissez un planning</h3><p class="mt-1 text-xs text-slate-500">La journée et les créneaux du planning sélectionné s’afficheront ici.</p></div></div>';return;}
  const weekday=(state.date.getDay()+6)%7,plans=state.schedules.filter(s=>s.actif&&s.specialite===specialty&&scheduleDays(s).includes(weekday)).sort((a,b)=>String(a.heure_debut).localeCompare(String(b.heure_debut)));
  if(!plans.length){root.innerHTML='<div class="flex min-h-80 items-center justify-center p-8 text-center"><div><div class="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-2xl bg-amber-50 text-amber-600 dark:bg-amber-400/10"><i class="ph-bold ph-calendar-x text-2xl"></i></div><h3 class="font-bold text-slate-700 dark:text-white">Aucun créneau planifié pour '+esc(dayLabel(day))+'</h3><p class="mt-1 text-xs text-slate-500">Choisissez un autre planning ou une autre journée.</p></div></div>';return;}
  const rows=[];for(const plan of plans){const interval=Number(plan.intervalle_minutes),start=Number(String(plan.heure_debut).slice(0,2))*60+Number(String(plan.heure_debut).slice(3,5)),end=Number(String(plan.heure_fin).slice(0,2))*60+Number(String(plan.heure_fin).slice(3,5));for(let minute=start;minute<end;minute+=interval){const slotEnd=Math.min(minute+interval,end),hh=pad(Math.floor(minute/60)),mm=pad(minute%60),eh=pad(Math.floor(slotEnd/60)),em=pad(slotEnd%60),from=hh+':'+mm,to=eh+':'+em;const appointments=state.appointments.filter(a=>{if(a.specialite!==specialty||datePart(a.date_rdv)!==day||a.statut==='Annulé')return false;const apptStart=Number(String(a.heure_debut).slice(0,2))*60+Number(String(a.heure_debut).slice(3,5)),apptEnd=Number(String(a.heure_fin).slice(0,2))*60+Number(String(a.heure_fin).slice(3,5));return apptStart<slotEnd&&apptEnd>minute;});const taken=appointments.length>0,capacity=Number(plan.capacite_patients),emptyCell='<button type="button" data-book-slot="'+from+'|'+to+'" class="flex min-h-10 w-full items-center gap-2 rounded-lg border border-emerald-300 bg-emerald-50 px-3 py-2 text-left text-xs font-semibold text-emerald-800 transition hover:border-emerald-500 hover:bg-emerald-100 focus:outline-none focus:ring-2 focus:ring-emerald-400 dark:border-emerald-400/20 dark:bg-emerald-400/10 dark:text-emerald-100 dark:hover:bg-emerald-400/20"><i class="ph-bold ph-plus-circle text-base"></i>Aucun rendez-vous · Cliquer pour ajouter</button>';rows.push('<tr><th scope="row" class="w-24 whitespace-nowrap border-b border-r border-slate-200 bg-slate-50 px-3 py-3 text-right align-top font-black text-slate-700 dark:border-white/10 dark:bg-white/5 dark:text-white"><span class="text-sm">'+from+'</span><span class="mt-1 block text-[10px] font-medium text-slate-400">'+(slotEnd-minute)+' min</span></th><td class="border-b border-slate-200 p-1 align-top dark:border-white/10">'+(taken?'<div class="rounded-lg border border-rose-300 bg-rose-500 p-1.5 shadow-sm dark:border-rose-300/30 dark:bg-rose-500/80"><div class="space-y-1">'+appointments.map(card).join('')+'</div><div class="mt-1 text-right text-[10px] font-extrabold text-white">Occupé · '+appointments.length+' / '+capacity+'</div></div>':emptyCell)+'</td></tr>');}}
  root.innerHTML='<div class="overflow-hidden rounded-xl border border-slate-200 dark:border-white/10"><div class="grid grid-cols-[6rem_minmax(0,1fr)] border-b border-slate-200 bg-slate-800 text-[10px] font-extrabold uppercase tracking-wider text-white dark:border-white/10"><div class="border-r border-white/10 px-3 py-3 text-right">Heure</div><div class="px-4 py-3">'+esc(dayLabel(day))+' · '+esc(specialty)+' · '+state.appointments.filter(a=>a.specialite===specialty&&datePart(a.date_rdv)===day&&a.statut!=='Annulé').length+' rendez-vous</div></div><table class="w-full border-collapse text-left"><tbody>'+rows.join('')+'</tbody></table></div><p class="mt-3 text-[10px] text-slate-400">Planning : '+esc(plan.heure_debut+'–'+plan.heure_fin+' · intervalle '+plan.intervalle_minutes+' min · capacité '+plan.capacite_patients+' par créneau')+'</p>';
 };
 const load=async()=>{try{const day=iso(state.date),params=new URLSearchParams({date_debut:day,date_fin:day});const res=await fetch('/api/appointments?'+params),data=await res.json();if(!res.ok)throw Error(data.detail||'Chargement des rendez-vous impossible.');state.appointments=data.appointments||[];message('',false);render();}catch(error){message(error.message,true);}};
 const fill=(select,items,label,val,text)=>{select.innerHTML='<option value="">'+esc(label)+'</option>'+items.map(x=>'<option value="'+esc(val(x))+'">'+esc(text(x))+'</option>').join('');};
 const options=async()=>{const res=await fetch('/api/appointments/options');if(!res.ok)throw Error('Chargement des référentiels impossible.');state.options=await res.json();fill(el('rdvSpecialty'),state.options.specialties,'Choisir une spécialité',x=>x,x=>x);fill(el('rdvScheduleSpecialty'),state.options.specialties,'Choisir une spécialité',x=>x,x=>x);fill(el('rdvPractitioner'),state.options.practitioners,'Choisir un praticien',x=>x.id,x=>x.prenom+' '+x.nom);fill(el('rdvRoom'),state.options.rooms,'Sans salle dédiée',x=>x.id,x=>x.designation);fill(el('rdvStatus'),state.options.statuses,'En attente',x=>x,x=>x);};
 const renderExamTypes=selectedId=>{const specialty=el('rdvSpecialty').value,items=(state.options.exam_types||[]).filter(x=>!specialty||!x.specialite||x.specialite===specialty);fill(el('rdvExamType'),items,'Choisir un type d’examen',x=>x.id,x=>x.designation);if(selectedId)el('rdvExamType').value=String(selectedId);};
 const scheduleMessage=(text,error)=>{const box=el('rdvScheduleMessage');box.textContent=text;box.className='rounded-xl p-3 text-xs '+(error?'bg-rose-100 text-rose-700':'bg-emerald-100 text-emerald-700')+(text?'':' hidden');};
 const checkedScheduleDays=()=>[...document.querySelectorAll('input[name="rdvScheduleWeekdays"]:checked')].map(input=>Number(input.value));
 const setScheduleDays=days=>{const selected=new Set((days||[]).map(Number));document.querySelectorAll('input[name="rdvScheduleWeekdays"]').forEach(input=>{input.checked=selected.has(Number(input.value));});el('rdvScheduleAllDays').checked=selected.size===7;};
 const resetSchedule=()=>{el('rdvScheduleForm').reset();el('rdvScheduleId').value='';el('rdvScheduleInterval').value='30';el('rdvScheduleCapacity').value='1';el('rdvScheduleActive').checked=true;setScheduleDays([0]);scheduleMessage('',false);};
 const renderSchedules=()=>{el('rdvScheduleRows').innerHTML=state.schedules.map(s=>'<tr><td class="p-3 font-bold text-slate-800 dark:text-white">'+esc(s.specialite)+'<div class="mt-1 text-[10px] font-medium text-slate-500">'+esc(s.jours_nom||s.jour_nom)+'</div></td><td class="p-3 text-slate-600 dark:text-slate-300">'+esc(s.heure_debut)+'–'+esc(s.heure_fin)+'</td><td class="p-3">'+s.intervalle_minutes+' min</td><td class="p-3">'+s.capacite_patients+' patient(s)</td><td class="p-3">'+(s.actif?'<span class="rounded-full bg-emerald-100 px-2 py-1 text-[10px] font-bold text-emerald-700">Actif</span>':'<span class="rounded-full bg-slate-100 px-2 py-1 text-[10px] font-bold text-slate-500">Inactif</span>')+'</td><td class="p-3 text-right"><button type="button" data-schedule-edit="'+s.id+'" class="rounded-lg px-2 py-1 font-bold text-indigo-600">Modifier</button><button type="button" data-schedule-delete="'+s.id+'" class="rounded-lg px-2 py-1 font-bold text-rose-600">Supprimer</button></td></tr>').join('')||'<tr><td colspan="6" class="p-8 text-center text-slate-400">Aucun planning configuré.</td></tr>';};
 const loadSchedules=async()=>{const res=await fetch('/api/appointments/schedules');const data=await res.json();if(!res.ok)throw Error(data.detail||'Chargement des plannings impossible.');state.schedules=data.schedules||[];renderSchedules();renderPlanningList();};
 const openConnectModal=()=>{const schedules=activeSchedules(),empty=el('rdvConnectEmpty'),manage=el('rdvConnectManage');empty.classList.toggle('hidden',schedules.length>0);manage.classList.toggle('hidden',!state.options.can_manage);el('rdvConnectSubtitle').textContent=state.selectedPlanningId?'Vous pouvez vous connecter à un autre planning.':'Choisissez le service et le planning sur lesquels vous souhaitez travailler.';const modal=el('rdvConnectModal');modal.classList.remove('hidden');modal.classList.add('flex');};
 const closeConnectModal=()=>{el('rdvConnectModal').classList.add('hidden');el('rdvConnectModal').classList.remove('flex');};
 const updateAgendaAvailability=()=>{const schedules=activeSchedules(),hasPlans=schedules.length>0;el('rdvPlannerLayout').classList.toggle('hidden',!hasPlans);el('rdvEmptyState').classList.toggle('hidden',hasPlans);if(!hasPlans){el('rdvEmptyState').innerHTML='<div class="flex min-h-[60vh] items-center justify-center p-6"><div class="max-w-lg text-center"><div class="mx-auto mb-5 flex h-16 w-16 items-center justify-center rounded-2xl bg-amber-50 text-amber-600 dark:bg-amber-400/10 dark:text-amber-300"><i class="ph-bold ph-calendar-x text-3xl"></i></div><h2 class="text-xl font-black text-slate-800 dark:text-white">Aucun planning disponible</h2><p class="mt-2 text-sm leading-6 text-slate-500 dark:text-slate-400">Un administrateur doit créer et activer un planning par spécialité avant que les rendez-vous puissent être affichés ou ajoutés.</p>'+(state.options.can_manage?'<button id="rdvEmptyCreateSchedule" type="button" class="mt-5 rounded-xl bg-brand-neon px-5 py-3 text-sm font-extrabold text-black"><i class="ph-bold ph-plus mr-1"></i>Créer un planning</button>':'')+'</div></div>';const button=el('rdvEmptyCreateSchedule');if(button)button.onclick=openScheduleModal;state.selectedPlanningId='';return false;}if(!schedules.some(s=>String(s.id)===String(state.selectedPlanningId)))state.selectedPlanningId='';el('rdvEmptyState').innerHTML='';renderPlanningList();render();return true;};
 const open=(a,slot)=>{el('rdvForm').reset();el('rdvId').value=a?a.id:'';el('rdvPatientId').value=a&&a.id_patient?a.id_patient:'';el('rdvPatientSearch').value=a?(a.patient_prenom+' '+a.patient_nom).trim():'';const plan=selectedPlan();el('rdvSpecialty').value=a?a.specialite:(plan?plan.specialite:'');fill(el('rdvPractitioner'),state.options.practitioners,'Choisir un praticien',p=>p.id,p=>p.prenom+' '+p.nom);el('rdvPractitioner').value=a?a.id_praticien:'';renderExamTypes(a&&a.type_examen_id);el('rdvRoom').value=a&&a.salle_id?a.salle_id:'';el('rdvStatus').value=a?a.statut:'En attente';el('rdvDate').value=a?datePart(a.date_rdv):(slot&&slot.date?slot.date:iso(state.date));const slotStart=slot?slot.heure_debut:(a?String(a.heure_debut).slice(0,5):''),slotEnd=slot?slot.heure_fin:(a?String(a.heure_fin).slice(0,5):'');el('rdvStart').value=slotStart;el('rdvEnd').value=slotEnd;el('rdvSlotTimeDisplay').value=slotStart&&slotEnd?slotStart+' – '+slotEnd:'';el('rdvReason').value=a?a.motif:'';el('rdvModalTitle').textContent=a?'Modifier le rendez-vous':'Nouveau rendez-vous';el('rdvModal').classList.remove('hidden');el('rdvModal').classList.add('flex');refreshSlots();};
 const close=()=>{el('rdvModal').classList.add('hidden');el('rdvModal').classList.remove('flex');};
 const req=async(url,method,body)=>{const res=await fetch(url,{method:method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const data=await res.json();if(!res.ok)throw Error(data.detail||'La demande a échoué.');return data;};
 const refreshSlots=async()=>{
  const requestId=++slotRequest,specialty=el('rdvSpecialty').value,appointmentDate=el('rdvDate').value,
   practitioner=el('rdvPractitioner').value,select=el('rdvSlotSelect'),capacity=el('rdvSlotCapacity'),save=el('rdvSaveBtn'),prior=el('rdvStart').value+'|'+el('rdvEnd').value;
  save.disabled=true;
  if(!specialty||!appointmentDate||!practitioner){
   select.innerHTML='<option value="">Choisissez une spécialité, un praticien et une date</option>';
   capacity.textContent='Les créneaux seront calculés à partir du planning hebdomadaire.';return;
  }
  el('rdvStart').value='';el('rdvEnd').value='';el('rdvSlotTimeDisplay').value='';
  try{
   const query=new URLSearchParams({specialite:specialty,date_rdv:appointmentDate,id_praticien:practitioner});
   if(el('rdvId').value)query.set('exclude_id',el('rdvId').value);
   if(el('rdvPatientId').value)query.set('id_patient',el('rdvPatientId').value);
   if(el('rdvRoom').value)query.set('salle_id',el('rdvRoom').value);
   const res=await fetch('/api/appointments/slots?'+query),data=await res.json();
   if(requestId!==slotRequest)return;
   if(!res.ok)throw Error(data.detail||'Chargement des créneaux impossible.');
   const slots=data.slots||[];
   if(!data.planning_configured){select.innerHTML='<option value="">Aucun planning actif pour cette spécialité</option>';capacity.textContent='Configurez un planning avant de prendre ce rendez-vous.';return;}
   const emptySlots=slots.filter(s=>!s.indisponible&&Number(s.affectes||0)===0);state.currentSlots=emptySlots;
   select.innerHTML='<option value="">'+(emptySlots.length?'Choisir une case libre du planning':'Aucune case libre pour cette date')+'</option>'+emptySlots.map(s=>'<option value="'+s.heure_debut+'|'+s.heure_fin+'">'+s.heure_debut+'–'+s.heure_fin+' · case libre</option>').join('');
   const matching=emptySlots.find(s=>s.heure_debut+'|'+s.heure_fin===prior);
   if(matching){select.value=prior;select.dispatchEvent(new Event('change'));}
   else if(emptySlots.length){select.value=emptySlots[0].heure_debut+'|'+emptySlots[0].heure_fin;select.dispatchEvent(new Event('change'));capacity.textContent='Premier créneau disponible proposé. Pour en choisir un autre, cliquez sur sa case verte dans l’agenda.';}
   else if(!slots.length)capacity.textContent='Aucun créneau n’est configuré pour ce service à cette date.';
   else if(!emptySlots.length)capacity.textContent='Toutes les cases de cette date sont déjà occupées ou indisponibles.';
   else capacity.textContent='Sélectionnez une case verte du planning ; les cases déjà prises ne sont pas proposées.';
  }catch(error){if(requestId===slotRequest){capacity.textContent=error.message;message(error.message,true);}}
 };
 const openScheduleModal=async()=>{const modal=el('rdvScheduleModal');if(modal.parentElement!==document.body)document.body.appendChild(modal);modal.classList.remove('hidden');modal.classList.add('flex');document.body.style.overflow='hidden';resetSchedule();try{await loadSchedules();}catch(error){scheduleMessage(error.message,true);}};
 const closeScheduleModal=()=>{el('rdvScheduleModal').classList.add('hidden');el('rdvScheduleModal').classList.remove('flex');document.body.style.overflow='';if(!selectedPlan())openConnectModal();};
 el('rdvSlotSelect').onchange=()=>{const [start,end]=(el('rdvSlotSelect').value||'|').split('|'),slot=state.currentSlots.find(s=>s.heure_debut===start&&s.heure_fin===end);if(start&&end&&slot&&!slot.indisponible){el('rdvStart').value=start;el('rdvEnd').value=end;el('rdvSlotTimeDisplay').value=start+' – '+end;el('rdvSaveBtn').disabled=false;el('rdvSlotCapacity').textContent=`${slot.places_restantes} place(s) restante(s) sur ${slot.capacite}.`;}else{el('rdvStart').value='';el('rdvEnd').value='';el('rdvSlotTimeDisplay').value='';el('rdvSaveBtn').disabled=true;}};
 const moveDay=amount=>{state.date.setDate(state.date.getDate()+amount);load();};el('rdvDailyPrev').onclick=()=>moveDay(-1);el('rdvDailyNext').onclick=()=>moveDay(1);el('rdvDailyToday').onclick=()=>{state.date=new Date();load();};el('rdvDailyPrint').onclick=printDailyAgenda;
 el('rdvPlanningList').onclick=e=>{const button=e.target.closest('[data-planning]');if(!button)return;const plan=activeSchedules().find(s=>String(s.id)===button.dataset.planning);if(!plan)return;state.selectedPlanningId=String(plan.id);moveToNextPlanDay(plan);closeConnectModal();updateAgendaAvailability();load();};
 el('rdvNew').onclick=()=>open(null);el('rdvClose').onclick=close;el('rdvCancel').onclick=close;
 el('rdvChangePlanning').onclick=openConnectModal;el('rdvConnectManage').onclick=()=>{closeConnectModal();openScheduleModal();};
 el('rdvSchedules').onclick=openScheduleModal;el('rdvScheduleClose').onclick=closeScheduleModal;el('rdvScheduleReset').onclick=resetSchedule;
 el('rdvScheduleModal').onclick=e=>{if(e.target===el('rdvScheduleModal'))closeScheduleModal();};
 el('rdvScheduleAllDays').onchange=()=>setScheduleDays(el('rdvScheduleAllDays').checked?[0,1,2,3,4,5,6]:[]);
 document.querySelectorAll('input[name="rdvScheduleWeekdays"]').forEach(input=>input.onchange=()=>{el('rdvScheduleAllDays').checked=checkedScheduleDays().length===7;});
 el('rdvScheduleRows').onclick=async e=>{const edit=e.target.closest('[data-schedule-edit]'),remove=e.target.closest('[data-schedule-delete]');if(edit){const s=state.schedules.find(x=>String(x.id)===edit.dataset.scheduleEdit);if(!s)return;el('rdvScheduleId').value=s.id;el('rdvScheduleSpecialty').value=s.specialite;setScheduleDays(s.jours_semaine||[s.jour_semaine]);el('rdvScheduleStart').value=s.heure_debut;el('rdvScheduleEnd').value=s.heure_fin;el('rdvScheduleInterval').value=s.intervalle_minutes;el('rdvScheduleCapacity').value=s.capacite_patients;el('rdvScheduleActive').checked=s.actif;scheduleMessage('Modification du planning sélectionné.',false);return;}if(remove&&confirm('Supprimer ce planning ? Les rendez-vous déjà créés restent conservés.')){try{await req('/api/appointments/schedules/'+remove.dataset.scheduleDelete,'DELETE');await loadSchedules();const available=updateAgendaAvailability();if(available)await load();else localStorage.removeItem('cliniva-rdv-selected-planning');scheduleMessage('Planning supprimé.',false);}catch(error){scheduleMessage(error.message,true);}}};
 el('rdvScheduleForm').onsubmit=async e=>{e.preventDefault();try{const days=checkedScheduleDays();if(!days.length)throw Error('Cochez au moins un jour pour ce planning.');const scheduleId=el('rdvScheduleId').value||null,saved=await req('/api/appointments/schedules/save','POST',{id:scheduleId,specialite:el('rdvScheduleSpecialty').value,jours_semaine:days,heure_debut:el('rdvScheduleStart').value,heure_fin:el('rdvScheduleEnd').value,intervalle_minutes:Number(el('rdvScheduleInterval').value),capacite_patients:Number(el('rdvScheduleCapacity').value),actif:el('rdvScheduleActive').checked});await loadSchedules();if(el('rdvScheduleActive').checked){state.selectedPlanningId=String(saved.id);const plan=selectedPlan();if(plan)moveToNextPlanDay(plan);closeConnectModal();}const available=updateAgendaAvailability();resetSchedule();scheduleMessage('Planning enregistré pour '+days.map(day=>['Lundi','Mardi','Mercredi','Jeudi','Vendredi','Samedi','Dimanche'][day]).join(', ')+'.',false);if(available)await load();}catch(error){scheduleMessage(error.message,true);}};
 el('rdvPatientSearch').oninput=async e=>{el('rdvPatientId').value='';const box=el('rdvPatientResults');if(e.target.value.length<2){box.classList.add('hidden');return;}try{const res=await fetch('/api/appointments/patients?q='+encodeURIComponent(e.target.value));const data=await res.json();state.patients=data.patients||[];box.innerHTML=state.patients.map(p=>'<button type="button" data-patient="'+p.id+'" class="block w-full border-b p-2 text-left text-sm hover:bg-slate-100">'+esc(p.nom+' '+p.prenom)+' · #'+p.id+'</button>').join('')||'<div class="p-3 text-xs">Aucun patient trouvé</div>';box.classList.remove('hidden');}catch(_){box.classList.add('hidden');}};
 el('rdvPatientResults').onclick=e=>{const b=e.target.closest('[data-patient]');if(!b)return;const p=state.patients.find(x=>String(x.id)===b.dataset.patient);if(p){el('rdvPatientId').value=p.id;el('rdvPatientSearch').value=p.nom+' '+p.prenom;el('rdvPatientResults').classList.add('hidden');refreshSlots();}};
 el('rdvSpecialty').onchange=()=>{renderExamTypes();refreshSlots();};
 el('rdvDate').onchange=refreshSlots;el('rdvPractitioner').onchange=refreshSlots;el('rdvRoom').onchange=refreshSlots;
 el('rdvForm').onsubmit=async e=>{e.preventDefault();try{if(!el('rdvPatientSearch').value.trim())throw Error('Saisissez le nom du patient ou sélectionnez une fiche existante.');if(!el('rdvExamType').value)throw Error('Choisissez le type d’examen.');if(!el('rdvStart').value||!el('rdvEnd').value)throw Error('Aucun créneau disponible pour cette date.');const selectedPatient=el('rdvPatientId').value;const x=await req('/api/appointments/save','POST',{id:el('rdvId').value||null,id_patient:selectedPatient||null,patient_nom_libre:selectedPatient?'':el('rdvPatientSearch').value.trim(),id_praticien:el('rdvPractitioner').value,salle_id:el('rdvRoom').value||null,specialite:el('rdvSpecialty').value,type_examen_id:el('rdvExamType').value,date_rdv:el('rdvDate').value,heure_debut:el('rdvStart').value,heure_fin:el('rdvEnd').value,motif:el('rdvReason').value,statut:el('rdvStatus').value});close();message('Rendez-vous #'+x.id+' enregistré.',false);await load();}catch(err){if(err.message.includes('capacité maximale')||err.message.includes('chevauche')){message('Ce créneau n’est plus disponible. Choisissez un autre créneau.',true);await refreshSlots();}else message(err.message,true);}};
 root.onclick=async e=>{const freeSlot=e.target.closest('[data-book-slot]');if(freeSlot){if(!state.options.can_manage)return;const [heure_debut,heure_fin]=freeSlot.dataset.bookSlot.split('|');open(null,{date:iso(state.date),heure_debut,heure_fin});return;}const aEl=e.target.closest('.rdvCard');if(!aEl)return;const a=state.appointments.find(x=>String(x.id)===aEl.dataset.id);if(!a)return;const button=e.target.closest('[data-action]');if(button){try{if(button.dataset.action==='edit')open(a);if(button.dataset.action==='cancel'&&confirm('Annuler ce rendez-vous ?')){await req('/api/appointments/'+a.id+'/cancel','POST');await load();}if(button.dataset.action==='convert'&&confirm('Créer un passage à partir de ce rendez-vous ?')){const r=await req('/api/appointments/'+a.id+'/convert','POST');message('Passage #'+r.id_passage+' créé.',false);await load();}}catch(err){message(err.message,true);}return;}if(!state.options.can_manage){message(a.motif,false);return;}if(a.id_passage){message('Passage associé : #'+a.id_passage,false);return;}if(a.statut==='Annulé'){message('Ce rendez-vous est annulé.',true);return;}if(aEl.querySelector('[data-actions]'))return;const actions=document.createElement('div');actions.dataset.actions='1';actions.className='flex gap-1 mt-2';actions.innerHTML='<button data-action="edit" class="px-2 py-1 rounded bg-white text-[10px] font-bold">Modifier</button>'+(a.id_patient?'<button data-action="convert" class="px-2 py-1 rounded bg-emerald-200 text-[10px] font-bold">Créer le passage</button>':'')+'<button data-action="cancel" class="px-2 py-1 rounded bg-rose-100 text-[10px] font-bold">Annuler</button>';aEl.appendChild(actions);};
 (async()=>{try{await options();await loadSchedules();if(!state.options.can_manage){el('rdvNew').classList.add('hidden');el('rdvSchedules').classList.add('hidden');}state.selectedPlanningId='';updateAgendaAvailability();openConnectModal();}catch(e){message(e.message,true);}})();
})();
</script>
"""


__all__ = ["appointments_router", "render_html"]
