"""Agrégats réels et filtrés par permissions pour la page d'accueil Cliniva."""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from database import get_db_cursor

logger = logging.getLogger(__name__)

_PERIODS = {
    "today": (0, "Aujourd’hui"),
    "7d": (6, "7 derniers jours"),
    "30d": (29, "30 derniers jours"),
}


def _permissions(user: dict[str, Any]) -> dict[str, Any]:
    value = user.get("permissions") or {}
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    return value if isinstance(value, dict) else {}


def _table_exists(cursor, table_name: str) -> bool:
    cursor.execute("""SELECT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = current_schema() AND table_name = %s
    ) AS present""", (table_name,))
    return bool(cursor.fetchone()["present"])


def _resolve_dashboard_dates(
    period: str, today: date, date_from: str | None, date_to: str | None, month: str | None
) -> tuple[date, date, str]:
    """Resolve les périodes rapides, un mois, ou un intervalle saisi."""
    if month:
        try:
            first = date.fromisoformat(f"{month}-01")
            next_month = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
            return first, next_month - timedelta(days=1), f"Mois {first:%m/%Y}"
        except (TypeError, ValueError):
            pass
    if date_from or date_to or period == "custom":
        try:
            first = date.fromisoformat(date_from) if date_from else today
            last = date.fromisoformat(date_to) if date_to else first
            if last < first:
                first, last = last, first
            return first, last, f"Du {first:%d/%m/%Y} au {last:%d/%m/%Y}"
        except (TypeError, ValueError):
            pass
    if period == "yesterday":
        day = today - timedelta(days=1)
        return day, day, "Hier"
    if period == "day_before":
        day = today - timedelta(days=2)
        return day, day, "Avant-hier"
    if period == "month":
        first = today.replace(day=1)
        next_month = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        return first, next_month - timedelta(days=1), f"Mois {today:%m/%Y}"
    days, label = _PERIODS.get(period, _PERIODS["today"])
    return today - timedelta(days=days), today, label


class DashboardService:
    """Construit une vue d'activité à partir des données présentes en base."""

    @staticmethod
    def get_dashboard_metrics(
        user: dict[str, Any], period: str = "today", config: dict[str, Any] | None = None,
        date_from: str | None = None, date_to: str | None = None, month: str | None = None,
    ) -> dict[str, Any]:
        period = period if period in {*_PERIODS, "yesterday", "day_before", "month", "custom"} else "today"
        today = date.today()
        start_date, last_date, period_label = _resolve_dashboard_dates(
            period, today, date_from, date_to, month
        )
        end_date = last_date + timedelta(days=1)
        span_days = (last_date - start_date).days + 1
        grain, interval = ("day", "1 day") if span_days <= 45 else (
            ("week", "1 week") if span_days <= 180 else ("month", "1 month")
        )
        is_admin = user.get("role") == "Administrateur"
        perms = _permissions(user)
        can_dashboard = is_admin or bool(perms.get("dash_view"))
        can_reception = is_admin or bool(perms.get("reception_view"))
        can_dme = is_admin or bool(perms.get("dme_view"))
        can_cash = is_admin or bool(perms.get("dash_financial"))
        can_rdv = is_admin or bool(perms.get("rdv_view"))
        can_pacs = is_admin
        can_print = is_admin
        result: dict[str, Any] = {
            "period": period,
            "period_label": period_label,
            "date_from": start_date.isoformat(),
            "date_to": last_date.isoformat(),
            "can_dashboard": can_dashboard,
            "can_reception": can_reception,
            "can_dme": can_dme,
            "can_cash": can_cash,
            "can_rdv": can_rdv,
            "can_pacs": can_pacs,
            "can_print": can_print,
            "patients_total": 0,
            "patients_new": 0,
            "passages_total": 0,
            "passages_pending": 0,
            "passages_active": 0,
            "passages_done": 0,
            "passages_cancelled": 0,
            "avg_wait_minutes": 0,
            "passages_series": [],
            "passage_status_series": [],
            "activity_series": [],
            "consultations": 0,
            "cash_collected": 0.0,
            "cash_refunded": 0.0,
            "pacs_studies": 0,
            "pacs_images": 0,
            "pacs_receiving": 0,
            "pacs_storage_bytes": 0,
            "pacs_storage_total_bytes": 0,
            "pacs_modalities": [],
            "print_groups": 0,
            "print_images": 0,
            "month_passages": 0,
            "month_consultations": 0,
            "month_new_patients": 0,
            "reports": 0,
            "booklets": 0,
            "appointments": 0,
            "appointments_pending": 0,
            "top_services": [],
            "top_procedures": [],
        }
        try:
            with get_db_cursor() as cursor:
                if can_reception:
                    cursor.execute("""SELECT
                        COUNT(*) AS total,
                        COUNT(*) FILTER (WHERE COALESCE(statut, '') = 'En attente') AS pending,
                        COUNT(*) FILTER (WHERE COALESCE(statut, '') = 'En cours') AS active,
                        COUNT(*) FILTER (WHERE COALESCE(statut, '') = 'Terminé') AS done,
                        COUNT(*) FILTER (WHERE COALESCE(statut_paiement, '') = 'Annulé') AS cancelled,
                        COUNT(DISTINCT id_patient) AS patients,
                        COALESCE(ROUND(AVG(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - created_at)) / 60)
                            FILTER (WHERE COALESCE(statut, '') = 'En attente'
                            AND date_passage = CURRENT_DATE)), 0)::integer AS avg_wait_minutes
                        FROM patient_passages
                        WHERE date_passage >= %s AND date_passage < %s""", (start_date, end_date))
                    row = cursor.fetchone()
                    result.update({
                        "passages_total": int(row["total"] or 0),
                        "passages_pending": int(row["pending"] or 0),
                        "passages_active": int(row["active"] or 0),
                        "passages_done": int(row["done"] or 0),
                        "passages_cancelled": int(row["cancelled"] or 0),
                        "avg_wait_minutes": int(row["avg_wait_minutes"] or 0),
                        "patients_seen": int(row["patients"] or 0),
                    })
                    cursor.execute("""SELECT date_trunc(%s, date_passage::timestamp)::date AS day,
                        COUNT(*) AS total FROM patient_passages
                        WHERE date_passage >= %s AND date_passage < %s GROUP BY 1 ORDER BY 1""",
                        (grain, start_date, end_date))
                    chart_rows = cursor.fetchall()
                    peak = max((int(row["total"] or 0) for row in chart_rows), default=0)
                    result["passages_series"] = [
                        {"label": row["day"].strftime("%d/%m" if grain != "month" else "%m/%Y"),
                         "value": int(row["total"] or 0),
                         "height": max(8, round(int(row["total"] or 0) * 100 / peak)) if peak else 8}
                        for row in chart_rows
                    ]
                    cursor.execute("""SELECT date_trunc(%s, date_passage::timestamp)::date AS day,
                        COUNT(*) FILTER (WHERE COALESCE(statut, '') = 'En attente') AS pending,
                        COUNT(*) FILTER (WHERE COALESCE(statut, '') = 'En cours') AS active,
                        COUNT(*) FILTER (WHERE COALESCE(statut, '') = 'Terminé') AS done,
                        COUNT(*) AS total FROM patient_passages
                        WHERE date_passage >= %s AND date_passage < %s GROUP BY 1 ORDER BY 1""",
                        (grain, start_date, end_date))
                    result["passage_status_series"] = [
                        {"label": row["day"].strftime("%d/%m" if grain != "month" else "%m/%Y"),
                         "pending": int(row["pending"] or 0), "active": int(row["active"] or 0),
                         "done": int(row["done"] or 0), "total": int(row["total"] or 0)}
                        for row in cursor.fetchall()
                    ]
                    cursor.execute("""SELECT COALESCE(NULLIF(act->>'specialite', ''), 'Non précisée') AS label,
                        COUNT(*) AS total FROM patient_passages p
                        CROSS JOIN LATERAL jsonb_array_elements(
                            CASE WHEN jsonb_typeof(p.actes_json) = 'array' THEN p.actes_json ELSE '[]'::jsonb END
                        ) AS elements(act)
                        WHERE p.date_passage >= %s AND p.date_passage < %s
                        GROUP BY label ORDER BY total DESC LIMIT 5""", (start_date, end_date))
                    result["top_services"] = [
                        {"label": row["label"], "value": int(row["total"] or 0)}
                        for row in cursor.fetchall()
                    ]
                    cursor.execute("""SELECT COALESCE(NULLIF(act->>'nom', ''), NULLIF(act->>'designation', ''), 'Prestation non précisée') AS label,
                        COUNT(*) AS total FROM patient_passages p
                        CROSS JOIN LATERAL jsonb_array_elements(
                            CASE WHEN jsonb_typeof(p.actes_json) = 'array' THEN p.actes_json ELSE '[]'::jsonb END
                        ) AS elements(act)
                        WHERE p.date_passage >= %s AND p.date_passage < %s
                        GROUP BY label ORDER BY total DESC LIMIT 5""", (start_date, end_date))
                    result["top_procedures"] = [
                        {"label": row["label"], "value": int(row["total"] or 0)}
                        for row in cursor.fetchall()
                    ]

                if is_admin or perms.get("pat_view") or can_reception:
                    cursor.execute("SELECT COUNT(*) AS total FROM patients")
                    result["patients_total"] = int(cursor.fetchone()["total"] or 0)
                    cursor.execute("""SELECT COUNT(*) AS total FROM patients
                        WHERE created_at >= %s AND created_at < %s""", (start_date, end_date))
                    result["patients_new"] = int(cursor.fetchone()["total"] or 0)
                    cursor.execute("""SELECT COUNT(*) AS total FROM patients
                        WHERE created_at >= date_trunc('month', CURRENT_DATE)
                          AND created_at < date_trunc('month', CURRENT_DATE) + interval '1 month'""")
                    result["month_new_patients"] = int(cursor.fetchone()["total"] or 0)

                if can_reception:
                    cursor.execute("""SELECT COUNT(*) AS total FROM patient_passages
                        WHERE date_passage >= date_trunc('month', CURRENT_DATE)::date
                          AND date_passage < (date_trunc('month', CURRENT_DATE) + interval '1 month')::date""")
                    result["month_passages"] = int(cursor.fetchone()["total"] or 0)

                if can_dme:
                    cursor.execute("""SELECT COUNT(*) AS total FROM patient_dme_records
                        WHERE date_consultation >= %s AND date_consultation < %s""",
                        (start_date, end_date))
                    result["consultations"] = int(cursor.fetchone()["total"] or 0)
                    cursor.execute("""SELECT COUNT(*) AS total FROM patient_dme_records
                        WHERE date_consultation >= date_trunc('month', CURRENT_DATE)
                          AND date_consultation < date_trunc('month', CURRENT_DATE) + interval '1 month'""")
                    result["month_consultations"] = int(cursor.fetchone()["total"] or 0)
                    if _table_exists(cursor, "patient_cr_records"):
                        cursor.execute("""SELECT
                            COUNT(*) FILTER (WHERE COALESCE(type_modele, '') <> 'Livret') AS reports,
                            COUNT(*) FILTER (WHERE COALESCE(type_modele, '') = 'Livret') AS booklets
                            FROM patient_cr_records WHERE created_at >= %s AND created_at < %s""",
                            (start_date, end_date))
                        row = cursor.fetchone()
                        result["reports"] = int(row["reports"] or 0)
                        result["booklets"] = int(row["booklets"] or 0)

                if can_rdv and _table_exists(cursor, "appointments"):
                    cursor.execute("""SELECT COUNT(*) AS total,
                        COUNT(*) FILTER (WHERE statut IN ('En attente', 'En salle d''attente')) AS pending
                        FROM appointments WHERE date_rdv >= %s AND date_rdv < %s""",
                        (start_date, end_date))
                    row = cursor.fetchone()
                    result["appointments"] = int(row["total"] or 0)
                    result["appointments_pending"] = int(row["pending"] or 0)

                if can_cash and _table_exists(cursor, "caisse_mouvements"):
                    cursor.execute("""SELECT
                        COALESCE(SUM(montant) FILTER (WHERE montant > 0), 0) AS collected,
                        COALESCE(-SUM(montant) FILTER (WHERE type_mouvement = 'ANNULATION' AND montant < 0), 0) AS refunded
                        FROM caisse_mouvements
                        WHERE date_mouvement >= %s AND date_mouvement < %s""",
                        (start_date, end_date))
                    row = cursor.fetchone()
                    result["cash_collected"] = float(row["collected"] or 0)
                    result["cash_refunded"] = float(row["refunded"] or 0)

                if can_pacs and _table_exists(cursor, "pacs_studies"):
                    cursor.execute("""SELECT COUNT(*) AS studies,
                        COALESCE(SUM(st.total_size), 0) AS storage_bytes,
                        COUNT(*) FILTER (WHERE st.status = 'RECEIVING') AS receiving,
                        (SELECT COUNT(*) FROM pacs_instances i
                         JOIN pacs_series s ON s.id = i.series_id
                         JOIN pacs_studies study ON study.id = s.study_id
                         WHERE study.created_at >= %s AND study.created_at < %s) AS images
                        FROM pacs_studies st
                        WHERE st.created_at >= %s AND st.created_at < %s""",
                        (start_date, end_date, start_date, end_date))
                    row = cursor.fetchone()
                    result["pacs_studies"] = int(row["studies"] or 0)
                    result["pacs_images"] = int(row["images"] or 0)
                    result["pacs_receiving"] = int(row["receiving"] or 0)
                    result["pacs_storage_bytes"] = int(row["storage_bytes"] or 0)
                    cursor.execute("SELECT COALESCE(SUM(total_size), 0) AS bytes FROM pacs_studies")
                    result["pacs_storage_total_bytes"] = int(cursor.fetchone()["bytes"] or 0)
                    cursor.execute("""SELECT COALESCE(NULLIF(modality, ''), 'Non précisée') AS modality,
                        COUNT(*) AS total FROM pacs_studies
                        WHERE created_at >= %s AND created_at < %s
                        GROUP BY modality ORDER BY total DESC LIMIT 5""", (start_date, end_date))
                    result["pacs_modalities"] = [
                        {"label": row["modality"], "value": int(row["total"] or 0)}
                        for row in cursor.fetchall()
                    ]

                # Série multi-indicateurs pour comparer passages, consultations
                # et encaissements sur le même intervalle, sans données non autorisées.
                joins: list[str] = []
                params: list[Any] = [grain, start_date, grain, last_date, interval]
                selected = ["b.bucket"]
                if can_reception:
                    joins.append("""LEFT JOIN (
                        SELECT date_trunc(%s, date_passage::timestamp)::date AS bucket, COUNT(*) AS total
                        FROM patient_passages WHERE date_passage >= %s AND date_passage < %s GROUP BY 1
                    ) p USING(bucket)""")
                    selected.append("COALESCE(p.total, 0) AS passages")
                    params.extend([grain, start_date, end_date])
                if can_dme:
                    joins.append("""LEFT JOIN (
                        SELECT date_trunc(%s, date_consultation::timestamp)::date AS bucket, COUNT(*) AS total
                        FROM patient_dme_records WHERE date_consultation >= %s AND date_consultation < %s GROUP BY 1
                    ) d USING(bucket)""")
                    selected.append("COALESCE(d.total, 0) AS consultations")
                    params.extend([grain, start_date, end_date])
                if can_cash and _table_exists(cursor, "caisse_mouvements"):
                    joins.append("""LEFT JOIN (
                        SELECT date_trunc(%s, date_mouvement::timestamp)::date AS bucket,
                               COALESCE(SUM(montant) FILTER (WHERE montant > 0), 0) AS total
                        FROM caisse_mouvements WHERE date_mouvement >= %s AND date_mouvement < %s GROUP BY 1
                    ) c USING(bucket)""")
                    selected.append("COALESCE(c.total, 0) AS encaissements")
                    params.extend([grain, start_date, end_date])
                if joins:
                    query = f"""WITH b AS (
                        SELECT generate_series(date_trunc(%s, %s::timestamp),
                            date_trunc(%s, %s::timestamp), %s::interval)::date AS bucket
                    ) SELECT {', '.join(selected)} FROM b {' '.join(joins)} ORDER BY b.bucket"""
                    cursor.execute(query, params)
                    result["activity_series"] = [
                        {
                            "label": row["bucket"].strftime("%d/%m" if grain == "day" else "%d/%m" if grain == "week" else "%m/%Y"),
                            "passages": int(row.get("passages", 0) or 0),
                            "consultations": int(row.get("consultations", 0) or 0),
                            "encaissements": float(row.get("encaissements", 0) or 0),
                        }
                        for row in cursor.fetchall()
                    ]
        except Exception:
            logger.exception("Impossible de calculer tous les indicateurs dashboard")

        if can_print:
            storage = (((config or {}).get("dicom_print_config") or {}).get("storage_dir")
                       or str(Path.home() / "DicomPrints"))
            try:
                root = Path(storage).expanduser()
                if root.is_dir():
                    groups = 0
                    images = 0
                    for _, _, files in os.walk(root):
                        count = sum(1 for name in files if name.lower().endswith(".png"))
                        if count:
                            images += count
                            groups += 1
                    result["print_images"] = images
                    result["print_groups"] = groups
            except (OSError, TypeError, ValueError):
                logger.warning("Impossible de compter les dossiers DICOM Print", exc_info=True)

        targets = (config or {}).get("dashboard_targets") or {}
        for key in ("passages", "consultations", "patients"):
            try:
                result[f"target_{key}"] = max(0, int(targets.get(key) or 0))
            except (TypeError, ValueError):
                result[f"target_{key}"] = 0

        return result
