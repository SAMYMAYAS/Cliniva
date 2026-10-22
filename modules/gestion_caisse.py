

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import logging
import math
from pathlib import Path
from io import BytesIO
from html import escape as html_escape
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from database import get_db_cursor
from app.auth import hash_password, track_action, get_current_user_optional
from modules.prestation_categories import allocate_movement_categories, classify_prestation
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.graphics import renderPDF
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.barcode.qr import QrCodeWidget

logger = logging.getLogger("CaisseModule")

caisse_router = APIRouter(prefix="/api/finance", tags=["Caisse & Finances"])


def _ensure_invoice_registry_schema() -> None:
    with get_db_cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS caisse_factures (
                id BIGSERIAL PRIMARY KEY,
                numero_facture TEXT NOT NULL UNIQUE,
                id_passage INTEGER NOT NULL UNIQUE
                    REFERENCES patient_passages(id) ON DELETE RESTRICT,
                date_generation TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                utilisateur TEXT NOT NULL DEFAULT '',
                patient_nom TEXT NOT NULL DEFAULT '',
                montant_ttc NUMERIC(14, 2) NOT NULL DEFAULT 0,
                pdf_data BYTEA NOT NULL,
                statut TEXT NOT NULL DEFAULT 'VALIDEE',
                date_annulation TIMESTAMPTZ,
                utilisateur_annulation TEXT NOT NULL DEFAULT '',
                motif_annulation TEXT NOT NULL DEFAULT '',
                numero_avoir TEXT NOT NULL DEFAULT '',
                avoir_pdf_data BYTEA
            );
            ALTER TABLE caisse_factures ADD COLUMN IF NOT EXISTS statut TEXT NOT NULL DEFAULT 'VALIDEE';
            ALTER TABLE caisse_factures ADD COLUMN IF NOT EXISTS date_annulation TIMESTAMPTZ;
            ALTER TABLE caisse_factures ADD COLUMN IF NOT EXISTS utilisateur_annulation TEXT NOT NULL DEFAULT '';
            ALTER TABLE caisse_factures ADD COLUMN IF NOT EXISTS motif_annulation TEXT NOT NULL DEFAULT '';
            ALTER TABLE caisse_factures ADD COLUMN IF NOT EXISTS numero_avoir TEXT NOT NULL DEFAULT '';
            ALTER TABLE caisse_factures ADD COLUMN IF NOT EXISTS avoir_pdf_data BYTEA;
            CREATE INDEX IF NOT EXISTS idx_caisse_factures_date
                ON caisse_factures(date_generation DESC);
            CREATE INDEX IF NOT EXISTS idx_caisse_factures_patient
                ON caisse_factures(patient_nom);
        """)

ACCORDEURS_REMISE_LIST: List[str] = [
    "Direction Générale",
    "Directeur Médical",
    "Gérant de la Clinique",
    "Médecin Chef de Service",
    "Superviseur Caisse",
]

RAISONS_ANNULATION: List[str] = [
    "Erreur de saisie",
    "Erreur de montant",
    "Paiement enregistré sur le mauvais patient",
    "Paiement enregistré sur le mauvais passage",
    "Demande du patient",
    "Remboursement",
    "Double paiement",
    "Passage impossible",
    "Patient claustrophobe",
    "Patient non coopérant",
    "Patient obèse",
    "Patient a besoin de sédation",
    "Attente trop longue",
    "Cas psychiatrique",
    "Machine en panne",
    "Technicien absent",
    "Médecin absent",
]

MODES_PAIEMENT = {
    "Espèces", "Carte CIB / Edahabia", "Chèque", "Prise en Charge (PEC)", "Autre"
}
STATUTS_PAIEMENT = {"Payé", "Partiel", "Non payé", "Annulé"}


def _permissions(user: Dict[str, Any]) -> Dict[str, Any]:
    value = user.get("permissions") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def user_can_access_caisse(user: Optional[Dict[str, Any]]) -> bool:
    """Autorise l'ouverture du module seulement si un droit de caisse est accordé."""
    if not user or user.get("status") != "Actif":
        return False
    return any(
        _permissions(user).get(key)
        for key in ("cash_collect", "cash_collect_rest", "cash_refund", "cash_remise", "cash_reports")
    )


def _has_cash_permission(user: Dict[str, Any], permission: str) -> bool:
    permissions = _permissions(user)
    if permission == "cash_collect_rest" and permission not in permissions:
        # Compatibilité : avant le droit dédié aux restes, cash_collect couvrait les deux.
        return bool(permissions.get("cash_collect"))
    if permission == "cash_refund" and permission not in permissions:
        # Compatibilité avec les comptes existants, avant le droit de remboursement dédié.
        return bool(permissions.get("cash_remise"))
    return bool(permissions.get(permission))


def _require_any_cash_permission(request: Request, *permissions: str) -> Dict[str, Any]:
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    if user.get("status") != "Actif":
        raise HTTPException(status_code=403, detail="Ce compte utilisateur est inactif.")
    if not any(_has_cash_permission(user, p) for p in permissions):
        raise HTTPException(status_code=403, detail="Vous n'avez pas l'autorisation pour cette action de caisse.")
    return user


def _require_cash_permission(request: Request, permission: str) -> Dict[str, Any]:
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    if user.get("status") != "Actif":
        raise HTTPException(status_code=403, detail="Ce compte utilisateur est inactif.")
    if not _has_cash_permission(user, permission):
        raise HTTPException(status_code=403, detail="Vous n'avez pas l'autorisation pour cette action de caisse.")
    return user


def _validate_reauthentication(data: Dict[str, Any], session_user: Optional[Dict[str, Any]]) -> str:
    login = str(data.get("login") or "").strip().lower()
    password = str(data.get("password") or "")
    if not session_user:
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    if not login or not password or login != str(session_user.get("login") or "").strip().lower():
        raise HTTPException(status_code=403, detail="La confirmation doit correspondre à l'utilisateur connecté.")
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT id FROM users WHERE id = %s AND login = %s AND password_hash = %s AND status = 'Actif';",
            (session_user["id"], login, hash_password(password))
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=403, detail="Mot de passe de confirmation incorrect.")
    return login


def _parse_money(value: Any, label: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Montant invalide : {label}.")
    if not amount.is_finite():
        raise HTTPException(status_code=400, detail=f"Montant invalide : {label}.")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _track_financial_action(request: Request, action: str, description: str) -> None:
    """Keep committed cash operations successful even if the secondary audit sink is down."""
    try:
        track_action(request, action, description)
    except Exception:
        logger.exception("Échec du journal d'audit secondaire après l'opération de caisse %s", action)


# ============================================================================
# LOGIQUE MÉTIER POSTGRESQL (FLUX DE CAISSE EN TEMPS RÉEL)
# ============================================================================
class CaisseManager:
    """Gestionnaire de persistance et des mouvements de caisse réels."""

    @staticmethod
    def ensure_caisse_schema():
        """Initialise la table des mouvements de caisse et synchronise l'existant."""
        with get_db_cursor() as cursor:
            # Serialize startup migrations so simultaneous requests cannot duplicate backfills.
            cursor.execute("SELECT pg_advisory_xact_lock(721940183);")
            cursor.execute("""
                ALTER TABLE patient_passages
                    ADD COLUMN IF NOT EXISTS raison_annulation TEXT DEFAULT '',
                    ADD COLUMN IF NOT EXISTS date_annulation TEXT DEFAULT '',
                    ADD COLUMN IF NOT EXISTS utilisateur_annulation TEXT DEFAULT '',
                    ADD COLUMN IF NOT EXISTS montant_annule NUMERIC DEFAULT 0;

                CREATE TABLE IF NOT EXISTS caisse_mouvements (
                    id SERIAL PRIMARY KEY,
                    id_passage INT NOT NULL REFERENCES patient_passages(id) ON DELETE RESTRICT,
                    type_mouvement TEXT NOT NULL,
                    montant NUMERIC NOT NULL,
                    mode_paiement TEXT DEFAULT 'Espèces',
                    date_mouvement DATE NOT NULL DEFAULT CURRENT_DATE,
                    heure_mouvement TEXT NOT NULL DEFAULT '',
                    utilisateur TEXT DEFAULT '',
                    motif TEXT DEFAULT '',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );

                -- Keep cash history if someone attempts to delete its passage.
                DO $$
                DECLARE fk RECORD;
                BEGIN
                    FOR fk IN
                        SELECT conname
                        FROM pg_constraint
                        WHERE conrelid = 'caisse_mouvements'::regclass
                          AND contype = 'f'
                          AND pg_get_constraintdef(oid) ILIKE '%id_passage%'
                          AND pg_get_constraintdef(oid) ILIKE '%ON DELETE CASCADE%'
                    LOOP
                        EXECUTE format('ALTER TABLE caisse_mouvements DROP CONSTRAINT %I', fk.conname);
                    END LOOP;
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conrelid = 'caisse_mouvements'::regclass
                          AND contype = 'f'
                          AND pg_get_constraintdef(oid) ILIKE '%id_passage%'
                    ) THEN
                        ALTER TABLE caisse_mouvements
                            ADD CONSTRAINT caisse_mouvements_id_passage_fkey
                            FOREIGN KEY (id_passage) REFERENCES patient_passages(id) ON DELETE RESTRICT;
                    END IF;
                END $$;

                -- Restore the original deposit for legacy records without double counting
                -- later regularizations already present in the movement ledger.
                INSERT INTO caisse_mouvements
                    (id_passage, type_mouvement, montant, mode_paiement, date_mouvement, heure_mouvement, utilisateur)
                SELECT p.id, 'ENCAISSEMENT',
                       GREATEST(COALESCE(NULLIF(p.montant_annule, 0), p.versement, 0)
                           - COALESCE((SELECT SUM(cm.montant) FROM caisse_mouvements cm
                                       WHERE cm.id_passage = p.id AND cm.type_mouvement IN ('REGULARISATION', 'ENCAISSEMENT_RESTE')), 0), 0),
                       p.mode_paiement, p.date_passage, p.heure_passage, p.utilisateur
                FROM patient_passages p
                WHERE GREATEST(COALESCE(NULLIF(p.montant_annule, 0), p.versement, 0)
                           - COALESCE((SELECT SUM(cm.montant) FROM caisse_mouvements cm
                                       WHERE cm.id_passage = p.id AND cm.type_mouvement IN ('REGULARISATION', 'ENCAISSEMENT_RESTE')), 0), 0) > 0
                  AND NOT EXISTS (SELECT 1 FROM caisse_mouvements cm
                                  WHERE cm.id_passage = p.id AND cm.type_mouvement IN ('ENCAISSEMENT', 'ENCAISSEMENT_TOTAL', 'ENCAISSEMENT_PARTIEL'));

                INSERT INTO caisse_mouvements
                    (id_passage, type_mouvement, montant, mode_paiement, date_mouvement, heure_mouvement,
                     utilisateur, motif)
                SELECT p.id, 'ANNULATION', -p.montant_annule, p.mode_paiement,
                       CASE WHEN p.date_annulation ~ '^\\d{2}/\\d{2}/\\d{4}'
                            THEN to_date(split_part(p.date_annulation, ' ', 1), 'DD/MM/YYYY')
                            ELSE p.date_passage END,
                       '', p.utilisateur_annulation, COALESCE(NULLIF(p.raison_annulation, ''), 'Annulation historique')
                FROM patient_passages p
                WHERE p.statut_paiement = 'Annulé'
                  AND COALESCE(p.montant_annule, 0) > 0
                  AND NOT EXISTS (SELECT 1 FROM caisse_mouvements cm
                                  WHERE cm.id_passage = p.id AND cm.type_mouvement = 'ANNULATION');

                -- Normalize legacy movement labels to the current accounting vocabulary.
                UPDATE caisse_mouvements cm
                SET type_mouvement = CASE
                    WHEN ABS(cm.montant) >= GREATEST(COALESCE(p.montant_total, 0) - COALESCE(p.remise, 0), 0) - 0.01
                        THEN 'ENCAISSEMENT_TOTAL'
                    ELSE 'ENCAISSEMENT_PARTIEL'
                END
                FROM patient_passages p
                WHERE cm.id_passage = p.id AND cm.type_mouvement = 'ENCAISSEMENT';
                UPDATE caisse_mouvements SET type_mouvement = 'ENCAISSEMENT_RESTE'
                WHERE type_mouvement = 'REGULARISATION';

                CREATE OR REPLACE FUNCTION trg_sync_passage_caisse_func() RETURNS TRIGGER AS $$
                BEGIN
                    IF TG_OP = 'INSERT' AND NEW.versement > 0 THEN
                        INSERT INTO caisse_mouvements
                            (id_passage, type_mouvement, montant, mode_paiement, date_mouvement, heure_mouvement, utilisateur)
                        VALUES (NEW.id,
                                CASE WHEN NEW.versement >= GREATEST(COALESCE(NEW.montant_total, 0) - COALESCE(NEW.remise, 0), 0) - 0.01
                                     THEN 'ENCAISSEMENT_TOTAL' ELSE 'ENCAISSEMENT_PARTIEL' END,
                                NEW.versement, NEW.mode_paiement,
                                NEW.date_passage, NEW.heure_passage, NEW.utilisateur);
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;

                DROP TRIGGER IF EXISTS trg_sync_passage_caisse ON patient_passages;
                CREATE TRIGGER trg_sync_passage_caisse AFTER INSERT ON patient_passages
                    FOR EACH ROW EXECUTE FUNCTION trg_sync_passage_caisse_func();
            """)

    def get_stats(self, filters: Dict[str, Any]) -> Dict[str, Any]:
        """Récupère les flux réels de caisse selon la date effective du paiement/remboursement."""
        self.ensure_caisse_schema()

        d_deb = filters.get("date_debut") or datetime.now().strftime("%Y-%m-%d")
        d_fin = filters.get("date_fin") or datetime.now().strftime("%Y-%m-%d")
        try:
            parsed_deb = date.fromisoformat(d_deb)
            parsed_fin = date.fromisoformat(d_fin)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Période de caisse invalide.")
        if parsed_fin < parsed_deb:
            raise HTTPException(status_code=400, detail="La date de fin doit être postérieure ou égale à la date de début.")
        op = (filters.get("operateur") or "").strip()
        mode = (filters.get("mode_paiement") or "").strip()
        statut = (filters.get("statut_paiement") or "").strip()
        if mode and mode != "TOUS" and mode not in MODES_PAIEMENT:
            raise HTTPException(status_code=400, detail="Filtre de mode de paiement invalide.")
        if statut and statut != "TOUS" and statut not in STATUTS_PAIEMENT:
            raise HTTPException(status_code=400, detail="Filtre de statut invalide.")

        with get_db_cursor() as cursor:
            # 1. FLUX RÉELS DE CAISSE DE LA PÉRIODE (Encaissements, Restes perçus, Remboursements)
            query_mouvements = """
                               SELECT cm.id                                    as id_mouvement, \
                                      cm.id_passage, \
                                      cm.type_mouvement, \
                                      cm.montant                               as montant_mouvement,
                                      cm.mode_paiement                         as mode_mouvement, \
                                      TO_CHAR(cm.date_mouvement, 'YYYY-MM-DD') as date_mouvement,
                                      cm.heure_mouvement, \
                                      cm.utilisateur                           as operateur_mouvement, \
                                      cm.motif                                 as motif_mouvement,
                                      p.id_patient, \
                                      TO_CHAR(p.date_passage, 'YYYY-MM-DD')    as date_passage,
                                      p.heure_passage, \
                                      p.type_passage, \
                                      p.actes_json,
                                      p.montant_total, \
                                      p.remise, \
                                      p.remise_type, \
                                      p.remise_valeur,
                                      p.accordeur_remise, \
                                      p.versement, \
                                      p.reste, \
                                      p.statut_paiement,
                                      COALESCE(pat.nom, 'Patient')             as nom, \
                                      COALESCE(pat.prenom, '')                 as prenom,
                                      COALESCE(pat.telephone_1, '-')           as telephone_1, \
                                      pat.wilaya, \
                                      pat.commune
                               FROM caisse_mouvements cm
                                        JOIN patient_passages p ON p.id = cm.id_passage
                                        LEFT JOIN patients pat ON p.id_patient = pat.id
                               WHERE cm.date_mouvement BETWEEN %s AND %s \
                               """
            params_mouvements: List[Any] = [d_deb, d_fin]

            if op and op != "TOUS":
                query_mouvements += " AND cm.utilisateur = %s"
                params_mouvements.append(op)
            if mode and mode != "TOUS":
                query_mouvements += " AND cm.mode_paiement = %s"
                params_mouvements.append(mode)
            if statut and statut != "TOUS":
                query_mouvements += " AND p.statut_paiement = %s"
                params_mouvements.append(statut)

            query_mouvements += " ORDER BY cm.date_mouvement DESC, cm.heure_mouvement DESC, cm.id DESC;"
            cursor.execute(query_mouvements, params_mouvements)
            rows_mouvements = [dict(r) for r in cursor.fetchall()]

            # Reconstituer l'historique complet des passages concernés afin de
            # répartir chaque paiement (et remboursement) sur les mêmes catégories.
            passage_ids = sorted({r.get("id_passage") for r in rows_mouvements if r.get("id_passage") is not None})
            category_allocations = {}
            if passage_ids:
                cursor.execute("""
                    SELECT cm.id AS id_mouvement, cm.id_passage, cm.type_mouvement,
                           cm.montant AS montant_mouvement,
                           TO_CHAR(cm.date_mouvement, 'YYYY-MM-DD') AS date_mouvement,
                           cm.heure_mouvement, p.actes_json, p.montant_total, p.remise
                    FROM caisse_mouvements cm
                    JOIN patient_passages p ON p.id = cm.id_passage
                    WHERE cm.id_passage = ANY(%s)
                    ORDER BY cm.date_mouvement, cm.heure_mouvement, cm.id
                """, (passage_ids,))
                history = [dict(r) for r in cursor.fetchall()]
                passages_for_allocation = {}
                for item in history:
                    pid = item.get("id_passage")
                    passage = passages_for_allocation.setdefault(pid, {
                        "montant_total": item.get("montant_total"),
                        "remise": item.get("remise"),
                        "actes_json": item.get("actes_json"),
                    })
                    raw_acts = passage.get("actes_json")
                    if not isinstance(raw_acts, list):
                        try:
                            passage["actes"] = json.loads(raw_acts or "[]")
                        except (TypeError, ValueError):
                            passage["actes"] = []
                category_allocations = allocate_movement_categories(passages_for_allocation, history)

            # 2. PASSAGES CRÉÉS DANS LA PÉRIODE (pour Chiffre d'Affaires Brut & Remises accordées)
            cursor.execute("""
                           SELECT p.montant_total,
                                  p.remise,
                                  p.remise_type,
                                  p.remise_valeur,
                                  p.accordeur_remise,
                                  p.id                                  as id_passage,
                                  TO_CHAR(p.date_passage, 'YYYY-MM-DD') as date_passage,
                                  p.heure_passage,
                                  p.utilisateur,
                                  COALESCE(pat.nom, 'Patient')          as nom,
                                  COALESCE(pat.prenom, '')              as prenom
                           FROM patient_passages p
                                    LEFT JOIN patients pat ON p.id_patient = pat.id
                           WHERE p.date_passage BETWEEN %s AND %s
                             AND COALESCE(p.statut_paiement, '') <> 'Annulé';
                           """, (d_deb, d_fin))
            rows_passages = [dict(r) for r in cursor.fetchall()]

            # 3. PATIENTS NON ENCAISSÉS (global, en attente de paiement)
            cursor.execute("""
                           SELECT p.id                                  as id_passage,
                                  p.id_patient,
                                  TO_CHAR(p.date_passage, 'YYYY-MM-DD') as date_passage,
                                  p.heure_passage,
                                  p.utilisateur,
                                  p.type_passage,
                                  p.actes_json,
                                  p.montant_total,
                                  p.remise,
                                  p.remise_type,
                                  p.remise_valeur,
                                  p.accordeur_remise,
                                  p.versement,
                                  p.reste,
                                  p.mode_paiement,
                                  p.statut_paiement,
                                  COALESCE(pat.nom, 'Patient')          as nom,
                                  COALESCE(pat.prenom, '')              as prenom,
                                  COALESCE(pat.telephone_1, '-')        as telephone_1,
                                  pat.wilaya,
                                  pat.commune
                           FROM patient_passages p
                                    LEFT JOIN patients pat ON p.id_patient = pat.id
                           WHERE COALESCE(p.statut_paiement, '') <> 'Annulé'
                             AND COALESCE(p.versement, 0) <= 0
                             AND (COALESCE(p.montant_total, 0) - COALESCE(p.remise, 0)) > 0
                           ORDER BY p.date_passage DESC, p.heure_passage DESC, p.id DESC;
                           """)
            non_payes_rows = [dict(r) for r in cursor.fetchall()]

            # 4. IMPAYÉS & RESTES DÛS
            cursor.execute("""
                           SELECT p.id                                  as id_passage,
                                  p.id_patient,
                                  TO_CHAR(p.date_passage, 'YYYY-MM-DD') as date_passage,
                                  p.heure_passage,
                                  p.utilisateur,
                                  p.type_passage,
                                  p.actes_json,
                                  p.montant_total,
                                  p.remise,
                                  p.versement,
                                  p.reste,
                                  p.mode_paiement,
                                  p.statut_paiement,
                                  COALESCE(pat.nom, 'Patient')          as nom,
                                  COALESCE(pat.prenom, '')              as prenom,
                                  COALESCE(pat.telephone_1, '-')        as telephone_1,
                                  pat.wilaya,
                                  pat.commune
                           FROM patient_passages p
                                    LEFT JOIN patients pat ON p.id_patient = pat.id
                           WHERE COALESCE(p.statut_paiement, '') <> 'Annulé'
                             AND COALESCE(p.reste, 0) > 0
                           ORDER BY p.id DESC;
                           """)
            impayes_rows = [dict(r) for r in cursor.fetchall()]

            # 5. HISTORIQUE DES ANNULATIONS
            cursor.execute("""
                           SELECT p.id                                  as id_passage,
                                  p.id_patient,
                                  TO_CHAR(p.date_passage, 'YYYY-MM-DD') as date_passage,
                                  p.heure_passage,
                                  p.utilisateur,
                                  p.type_passage,
                                  p.montant_total,
                                  p.remise,
                                  p.versement,
                                  p.reste,
                                  p.montant_annule,
                                  p.statut_paiement,
                                  p.raison_annulation,
                                  p.date_annulation,
                                  p.utilisateur_annulation,
                                  COALESCE(pat.nom, 'Patient')          as nom,
                                  COALESCE(pat.prenom, '')              as prenom,
                                  COALESCE(pat.telephone_1, '-')        as telephone_1
                           FROM patient_passages p
                                    LEFT JOIN patients pat ON p.id_patient = pat.id
                           WHERE p.statut_paiement = 'Annulé'
                           ORDER BY p.id DESC;
                           """)
            annules_rows = [dict(r) for r in cursor.fetchall()]

            # 6. Caissiers distincts
            cursor.execute(
                "SELECT DISTINCT utilisateur FROM ("
                "SELECT utilisateur FROM caisse_mouvements UNION ALL "
                "SELECT utilisateur FROM patient_passages"
                ") operateurs WHERE utilisateur IS NOT NULL AND utilisateur != '' ORDER BY utilisateur ASC;")
            operateurs = [r["utilisateur"] for r in cursor.fetchall()]

            # 7. Clinique
            cursor.execute("SELECT nom_raison_sociale, telephone, adresse FROM clinic_info WHERE id = 1;")
            clinic_row = cursor.fetchone()
            clinic_info = dict(clinic_row) if clinic_row else {
                "nom_raison_sociale": "Cliniva Medical Center",
                "telephone": "+213 (0) 23 45 67 89",
                "adresse": "Alger, Algérie"
            }

        # ---------------- CALCULS DU FLUX DE CAISSE RÉEL ----------------
        total_encaisse_reel = 0.0
        total_remboursements = 0.0
        total_contraste = 0.0
        total_sedation = 0.0
        modes_breakdown = {
            "Espèces": 0.0,
            "Carte CIB / Edahabia": 0.0,
            "Chèque": 0.0,
            "Prise en Charge (PEC)": 0.0,
            "Autre": 0.0
        }

        journal_flux = []
        for m in rows_mouvements:
            val_mvt = float(m.get("montant_mouvement") or 0.0)
            mode_m = m.get("mode_mouvement") or "Espèces"

            # RÈGLES APPLIQUÉES :
            # Les encaissements et régularisations entrent (+)
            # Les annulations et remboursements sortent (-)
            total_encaisse_reel += val_mvt
            if m.get("type_mouvement") == "ANNULATION" and val_mvt < 0:
                total_remboursements += abs(val_mvt)

            if mode_m in modes_breakdown:
                modes_breakdown[mode_m] += val_mvt
            else:
                modes_breakdown["Autre"] += val_mvt

            actes = m.get("actes_json")
            m["actes"] = actes if isinstance(actes, list) else json.loads(actes or "[]")
            allocation = category_allocations.get(m.get("id_mouvement"), {})
            m["montant_principal"] = float(allocation.get("principal", 0))
            m["montant_contraste"] = float(allocation.get("contraste", 0))
            m["montant_sedation"] = float(allocation.get("sedation", 0))
            total_contraste += m["montant_contraste"]
            total_sedation += m["montant_sedation"]
            journal_flux.append(m)

        # Chiffre d'Affaires Brut & Remises
        total_ca_brut = sum(float(p.get("montant_total") or 0.0) for p in rows_passages)
        total_remises = sum(float(p.get("remise") or 0.0) for p in rows_passages)
        total_net_du = max(0.0, total_ca_brut - total_remises)
        total_restes = sum(float(i.get("reste") or 0.0) for i in impayes_rows)

        remises_list = []
        for p in rows_passages:
            rem = float(p.get("remise") or 0.0)
            if rem > 0:
                remises_list.append({
                    "id_passage": p["id_passage"],
                    "date_passage": p["date_passage"],
                    "heure_passage": p["heure_passage"],
                    "patient_nom": f"{p['nom']} {p['prenom']}",
                    "montant_brut": float(p.get("montant_total") or 0.0),
                    "remise_valeur": p.get("remise_valeur") or rem,
                    "remise_type": p.get("remise_type") or "DA",
                    "remise_montant": rem,
                    "accordeur": p.get("accordeur_remise") or "Direction",
                    "utilisateur": p["utilisateur"]
                })

        impayes_list = []
        for i in impayes_rows:
            brut = float(i.get("montant_total") or 0.0)
            rem = float(i.get("remise") or 0.0)
            net = max(0.0, brut - rem)
            vers = float(i.get("versement") or 0.0)
            rst = max(0.0, net - vers)
            actes = i.get("actes_json")
            impayes_list.append({
                "id_passage": i["id_passage"],
                "id_patient": i["id_patient"],
                "date_passage": i["date_passage"],
                "heure_passage": i["heure_passage"],
                "patient_nom": f"{i['nom']} {i['prenom']}",
                "telephone_1": i["telephone_1"],
                "wilaya": i.get("wilaya") or "-",
                "commune": i.get("commune") or "",
                "type_passage": i["type_passage"],
                "montant_net": net,
                "versement": vers,
                "reste_du": rst,
                "statut_paiement": "Partiel" if vers > 0 else "Non payé",
                "mode_paiement": i.get("mode_paiement") or "Espèces",
                "utilisateur": i["utilisateur"],
                "actes": actes if isinstance(actes, list) else json.loads(actes or "[]")
            })

        non_payes_list = []
        for r in non_payes_rows:
            brut = float(r.get("montant_total") or 0.0)
            rem = float(r.get("remise") or 0.0)
            net = max(0.0, brut - rem)
            actes = r.get("actes_json")
            non_payes_list.append({
                "id_passage": r["id_passage"],
                "id_patient": r["id_patient"],
                "date_passage": r["date_passage"],
                "heure_passage": r["heure_passage"],
                "patient_nom": f"{r['nom']} {r['prenom']}",
                "telephone_1": r["telephone_1"],
                "type_passage": r["type_passage"],
                "montant_net": net,
                "remise": rem,
                "remise_type": r.get("remise_type") or "DA",
                "remise_valeur": r.get("remise_valeur") or 0,
                "accordeur_remise": r.get("accordeur_remise") or "",
                "reste_du": net,
                "utilisateur": r["utilisateur"],
                "actes": actes if isinstance(actes, list) else json.loads(actes or "[]")
            })

        return {
            "success": True,
            "kpis": {
                "ca_brut": total_ca_brut,
                "remises_total": total_remises,
                "net_du": total_net_du,
                "encaisse_total": total_encaisse_reel,
                "remboursements_total": total_remboursements,
                "produits_contraste_total": total_contraste,
                "sedations_total": total_sedation,
                "restes_total": total_restes,
                "count_passages": len(rows_passages),
                "count_remises": len(remises_list),
                "count_impayes": len(impayes_list),
                "count_non_payes": len(non_payes_list),
                "modes_breakdown": modes_breakdown
            },
            "journal": journal_flux,
            "remises": remises_list,
            "impayes": impayes_list,
            "non_payes": non_payes_list,
            "annules": annules_rows,
            "operateurs": operateurs,
            "accordeurs_remise": ACCORDEURS_REMISE_LIST,
            "clinic_info": clinic_info,
            "filtres_actifs": {
                "date_debut": d_deb,
                "date_fin": d_fin,
                "operateur": op or "TOUS",
                "mode_paiement": mode or "TOUS",
                "statut_paiement": statut or "TOUS"
            }
        }

    def regulariser(self, data: Dict[str, Any], user_context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Encaisse un reste dû : entre immédiatement dans la caisse d'aujourd'hui."""
        try:
            passage_id = int(data.get("id_passage"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Identifiant de passage invalide.")
        nouveau_versement = _parse_money(data.get("montant_versement"), "versement")
        if nouveau_versement <= 0:
            raise HTTPException(status_code=400, detail="Le montant du versement doit être supérieur à 0.")
        mode_paiement = str(data.get("mode_paiement") or "Espèces").strip()
        remise_type = str(data.get("remise_type") or "DA").strip().upper()
        remise_valeur = _parse_money(data.get("remise_valeur") or 0, "remise")
        accordeur = str(data.get("accordeur_remise") or "").strip()
        if mode_paiement not in MODES_PAIEMENT:
            raise HTTPException(status_code=400, detail="Mode de paiement invalide.")
        if remise_type not in {"DA", "%"} or remise_valeur < 0 or (remise_type == "%" and remise_valeur > 100):
            raise HTTPException(status_code=400, detail="Paramètres de remise invalides.")
        login = _validate_reauthentication(data, user_context)

        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT montant_total, remise, remise_type, remise_valeur, accordeur_remise, versement, statut_paiement
                FROM patient_passages WHERE id = %s FOR UPDATE;
            """, (passage_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Passage introuvable.")
            if row.get("statut_paiement") == "Annulé":
                raise HTTPException(status_code=409, detail="Un passage annulé ne peut pas recevoir de paiement.")

            montant_total = _parse_money(row.get("montant_total") or 0, "total")
            remise_actuelle = _parse_money(row.get("remise") or 0, "remise existante")
            if remise_valeur > 0:
                remise = (montant_total * remise_valeur / Decimal("100")) if remise_type == "%" else remise_valeur
                remise = min(remise, montant_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            else:
                remise = remise_actuelle
                remise_type = str(row.get("remise_type") or "DA").upper()
                remise_valeur = _parse_money(row.get("remise_valeur") or 0, "remise existante")
                accordeur = str(row.get("accordeur_remise") or "")
            if remise > remise_actuelle and user_context and not _has_cash_permission(user_context, "cash_remise"):
                raise HTTPException(status_code=403, detail="Le droit d'accorder une remise est requis.")

            net_du = max(Decimal("0"), montant_total - remise)
            ancien_versement = _parse_money(row.get("versement") or 0, "versement existant")
            reste_actuel = max(Decimal("0"), net_du - ancien_versement)
            if nouveau_versement > reste_actuel:
                raise HTTPException(status_code=400, detail=f"Le montant dépasse le reste dû ({reste_actuel:.2f} DA).")
            total_versement = ancien_versement + nouveau_versement
            nouveau_reste = max(Decimal("0"), net_du - total_versement)
            nouveau_statut = "Payé" if nouveau_reste == Decimal("0") else "Partiel"
            movement_type = (
                "ENCAISSEMENT_RESTE" if ancien_versement > 0
                else ("ENCAISSEMENT_TOTAL" if nouveau_versement >= net_du - Decimal("0.01") else "ENCAISSEMENT_PARTIEL")
            )
            movement_motif = (
                f"Reste perçu aujourd'hui (Accord: {accordeur or 'Aucun'})"
                if ancien_versement > 0 else "Premier encaissement depuis le module Caisse"
            )
            now_dt = datetime.now()
            now_time = now_dt.strftime("%H:%M:%S")

            cursor.execute("""
                UPDATE patient_passages
                SET remise = %s, remise_type = %s, remise_valeur = %s,
                    accordeur_remise = %s, versement = %s, reste = %s,
                    statut_paiement = %s, mode_paiement = %s
                WHERE id = %s;
            """, (remise, remise_type, remise_valeur, accordeur or "Direction Générale",
                  total_versement, nouveau_reste, nouveau_statut, mode_paiement, passage_id))
            cursor.execute("""
                INSERT INTO caisse_mouvements
                    (id_passage, type_mouvement, montant, mode_paiement, date_mouvement,
                     heure_mouvement, utilisateur, motif)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id;
            """, (passage_id, movement_type, nouveau_versement, mode_paiement, now_dt.date(), now_time, login,
                  movement_motif))
            mouvement = cursor.fetchone()

        return {
            "success": True,
            "message": f"Encaissement validé : {nouveau_versement:,.2f} DA ajoutés à la caisse du jour. Reste : {nouveau_reste:,.2f} DA ({nouveau_statut}).",
            "id_passage": passage_id,
            "id_mouvement": mouvement["id"] if mouvement else None,
            "reste": nouveau_reste,
            "statut": nouveau_statut
        }

    def annuler_paiement(self, data: Dict[str, Any], session_user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Annule un versement : sort immédiatement de la caisse d'aujourd'hui (remboursement)."""
        try:
            passage_id = int(data.get("id_passage"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Identifiant de passage invalide.")
        raison = str(data.get("raison_annulation") or "").strip()
        login = _validate_reauthentication(data, session_user)
        if raison not in RAISONS_ANNULATION:
            raise HTTPException(status_code=400, detail="Motif d'annulation invalide.")

        with get_db_cursor() as cursor:
            cursor.execute("""
                SELECT montant_total, remise, versement, mode_paiement, statut_paiement
                FROM patient_passages WHERE id = %s FOR UPDATE;
            """, (passage_id,))
            row = cursor.fetchone()

            if not row:
                raise HTTPException(status_code=404, detail="Passage introuvable.")
            if row.get("statut_paiement") == "Annulé":
                raise HTTPException(status_code=409, detail="Ce paiement a déjà été annulé.")

            ancien_vers = _parse_money(row.get("versement") or 0, "versement existant")
            if ancien_vers <= Decimal("0"):
                raise HTTPException(status_code=409, detail="Aucun versement à annuler pour ce passage.")

            net = max(Decimal("0"), _parse_money(row.get("montant_total") or 0, "total") - _parse_money(row.get("remise") or 0, "remise"))
            now_dt = datetime.now()
            now_str = now_dt.strftime("%d/%m/%Y %H:%M")
            now_time = now_dt.strftime("%H:%M:%S")

            # 1. Mise à jour de l'état du passage
            cursor.execute("""
                           UPDATE patient_passages
                           SET versement              = 0,
                               reste                  = %s,
                               statut_paiement        = 'Annulé',
                               montant_annule         = %s,
                               raison_annulation      = %s,
                               date_annulation        = %s,
                               utilisateur_annulation = %s
                           WHERE id = %s;
                           """, (net, ancien_vers, raison, now_str, login, passage_id))

            # 2. RÈGLE 2 : Le remboursement SORT de la caisse d'aujourd'hui (- montant_annule)
            cursor.execute("""
                           INSERT INTO caisse_mouvements
                           (id_passage, type_mouvement, montant, mode_paiement, date_mouvement, heure_mouvement,
                            utilisateur, motif)
                           VALUES (%s, 'ANNULATION', %s, %s, %s, %s, %s, %s);
                           """, (passage_id, -ancien_vers, row.get("mode_paiement") or "Espèces", now_dt.date(), now_time, login,
                                 f"Remboursement / Annulation : {raison}"))

        return {
            "success": True,
            "message": f"Paiement du passage #{passage_id} annulé. {ancien_vers:,.2f} DA déduits de la caisse d'aujourd'hui."
        }


caisse_manager = CaisseManager()


# ============================================================================
# POINTS D'ACCÈS FASTAPI
# ============================================================================
@caisse_router.get("/stats")
async def api_get_caisse_stats(
        request: Request,
        date_debut: Optional[str] = None,
        date_fin: Optional[str] = None,
        operateur: Optional[str] = "TOUS",
        mode_paiement: Optional[str] = "TOUS",
        statut_paiement: Optional[str] = "TOUS"
):
    user = _require_any_cash_permission(request, "cash_reports", "cash_collect", "cash_collect_rest", "cash_refund", "cash_remise")
    filters = {
        "date_debut": date_debut,
        "date_fin": date_fin,
        "operateur": operateur,
        "mode_paiement": mode_paiement,
        "statut_paiement": statut_paiement
    }
    data = caisse_manager.get_stats(filters)
    can_collect = _has_cash_permission(user, "cash_collect")
    can_collect_rest = _has_cash_permission(user, "cash_collect_rest")
    can_reports = _has_cash_permission(user, "cash_reports")
    can_remise = _has_cash_permission(user, "cash_remise")
    can_refund = _has_cash_permission(user, "cash_refund")
    data["access"] = {"cash_refund": can_refund}
    if not can_reports:
        data.update({"journal": [], "operateurs": [], "kpis": {}})
        if not can_remise:
            data["remises"] = []
    if not can_collect:
        data["non_payes"] = []
    if not can_collect_rest:
        data["impayes"] = []
    if not (can_reports or can_refund):
        data["annules"] = []
    return data


@caisse_router.get("/rapport-utilisateur/pdf")
async def api_download_user_cash_report_pdf(
        request: Request,
        operateur: str,
        type_rapport: str = "jour",
        date_debut: Optional[str] = None,
        date_fin: Optional[str] = None,
        mode_paiement: Optional[str] = "TOUS",
        statut_paiement: Optional[str] = "TOUS"
):
    """Génère un PDF détaillé des mouvements de caisse d'un opérateur."""
    _require_cash_permission(request, "cash_reports")
    operator_name = str(operateur or "").strip()
    if not operator_name:
        raise HTTPException(status_code=400, detail="Sélectionnez un utilisateur de caisse.")
    report_type = str(type_rapport or "jour").strip().lower()
    if report_type not in {"jour", "garde", "complet"}:
        raise HTTPException(status_code=400, detail="Type de rapport invalide.")

    report = caisse_manager.get_stats({
        "date_debut": date_debut,
        "date_fin": date_fin,
        "operateur": operator_name,
        "mode_paiement": mode_paiement,
        "statut_paiement": statut_paiement,
    })
    movements = report.get("journal") or []
    if not movements:
        raise HTTPException(status_code=404, detail="Aucun mouvement à exporter pour cet utilisateur et cette période.")

    # Le rapport jour/garde suit l'heure effective de chaque mouvement de caisse.
    # La période portée par les actes du passage sert uniquement à calculer le tarif.
    if report_type != "complet":
        with get_db_cursor() as cursor:
            cursor.execute("SELECT horaires_tarification FROM clinic_info WHERE id = 1;")
            horaires_row = cursor.fetchone()
        horaires = (horaires_row or {}).get("horaires_tarification") or {}
        if isinstance(horaires, str):
            try:
                horaires = json.loads(horaires or "{}")
            except (TypeError, ValueError):
                horaires = {}
        jours_config = horaires.get("jours", {}) if isinstance(horaires, dict) else {}
        noms_jours = ("Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche")

        def movement_period(movement: Dict[str, Any]) -> str:
            raw_date = movement.get("date_mouvement")
            raw_time = movement.get("heure_mouvement")
            try:
                if isinstance(raw_date, datetime):
                    movement_date = raw_date.date()
                elif isinstance(raw_date, date):
                    movement_date = raw_date
                else:
                    movement_date = date.fromisoformat(str(raw_date)[:10])
            except (TypeError, ValueError):
                # Une date de mouvement manquante ne doit pas être reclassée selon
                # la date d'enregistrement du passage : on conserve le jour par défaut.
                return "jour"
            if isinstance(raw_time, time):
                movement_time = raw_time.replace(tzinfo=None)
            else:
                try:
                    movement_time = time.fromisoformat(str(raw_time or "").strip()[:8])
                except ValueError:
                    movement_time = None
            if movement_time is None:
                return "jour"

            day_config = jours_config.get(noms_jours[movement_date.weekday()], {}) if isinstance(jours_config, dict) else {}
            if not isinstance(day_config, dict):
                day_config = {}
            if day_config.get("nuit_24h"):
                return "garde"
            try:
                day_start = time.fromisoformat(str(day_config.get("jour_debut") or "08:00")[:8])
                day_end = time.fromisoformat(str(day_config.get("jour_fin") or "20:00")[:8])
            except ValueError:
                day_start, day_end = time(8, 0), time(20, 0)

            if day_start == day_end:
                is_day = True
            elif day_start < day_end:
                is_day = day_start <= movement_time < day_end
            else:  # plage jour traversant minuit
                is_day = movement_time >= day_start or movement_time < day_end
            return "jour" if is_day else "garde"

        movements = [
            movement for movement in movements
            if movement_period(movement) == report_type
        ]
    if not movements:
        type_label = "de garde" if report_type == "garde" else "de jour"
        raise HTTPException(status_code=404, detail=f"Aucun mouvement {type_label} à exporter pour cet utilisateur et cette période.")

    def money(value: Any) -> str:
        try:
            amount = float(value or 0)
        except (TypeError, ValueError):
            amount = 0.0
        return f"{amount:,.2f}".replace(",", " ") + " DA"

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CashReportTitle", parent=styles["Title"], fontName="Times-Bold",
        fontSize=15, leading=19, textColor=colors.black, alignment=1,
        spaceAfter=14,
    )
    meta_style = ParagraphStyle(
        "CashReportMeta", parent=styles["Normal"], fontName="Times-Roman",
        fontSize=9, leading=12, textColor=colors.black, spaceAfter=3,
    )
    header_style = ParagraphStyle(
        "CashReportHeader", parent=styles["Normal"], fontName="Times-Bold",
        fontSize=8, leading=10, textColor=colors.black,
    )
    cell_style = ParagraphStyle(
        "CashReportCell", parent=styles["Normal"], fontName="Times-Roman",
        fontSize=7.5, leading=9.5, textColor=colors.black,
    )

    active_filters = report.get("filtres_actifs") or {}
    period_text = (
        f"Période : {html_escape(str(active_filters.get('date_debut') or date_debut or ''))} "
        f"au {html_escape(str(active_filters.get('date_fin') or date_fin or ''))}"
    )
    received_total = sum(max(0.0, float(item.get("montant_mouvement") or 0)) for item in movements)
    refunded_total = sum(max(0.0, -float(item.get("montant_mouvement") or 0)) for item in movements)
    net_total = received_total - refunded_total

    clinic = report.get("clinic_info") or {}
    clinic_name = html_escape(str(clinic.get("nom_raison_sociale") or "CLINIVA"))
    clinic_phone = html_escape(str(clinic.get("telephone") or "-"))
    story = [
        Paragraph(f"<b>{clinic_name}</b>", meta_style),
        Paragraph(f"Service de Facturation &amp; Caisse Centrale | Tél : {clinic_phone}", meta_style),
        Paragraph(
            f"Date d'édition : <b>{datetime.now().strftime('%d/%m/%Y à %H:%M')}</b><br/>"
            f"Période : {period_text}<br/>Utilisateur caisse : <b>{'Tous les caissiers' if operator_name == 'TOUS' else '@' + html_escape(operator_name)}</b>", meta_style
        ),
        Paragraph(
            "JOURNAL DE CAISSE — JOUR" if report_type == "jour" else
            "JOURNAL DE CAISSE — GARDE" if report_type == "garde" else
            ("RAPPORT DÉTAILLÉ DE CAISSE PAR UTILISATEUR" if operator_name != "TOUS" else "JOURNAL DÉTAILLÉ DE CAISSE"),
            title_style,
        ),
    ]
    summary_data = [
        [Paragraph("Nombre de mouvements", cell_style), Paragraph(f"<b>{len(movements)}</b>", cell_style),
         Paragraph("Encaissements", cell_style), Paragraph(f"<b>{money(received_total)}</b>", cell_style)],
        [Paragraph("Remboursements", cell_style), Paragraph(f"<b>{money(refunded_total)}</b>", cell_style),
         Paragraph("Net de caisse", cell_style), Paragraph(f"<b>{money(net_total)}</b>", cell_style)],
    ]
    summary = Table(summary_data, colWidths=[112, 125, 112, 145])
    summary.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("FONTNAME", (1, 0), (1, -1), "Times-Bold"),
        ("FONTNAME", (3, 0), (3, -1), "Times-Bold"),
    ]))
    story.extend([summary, Spacer(1, 12)])

    table_data = [[
        Paragraph(label, header_style)
        for label in ("Passage / date", "Patient", "Mouvement / prestation", "Caissier", "Mode", "Montant")
    ]]
    movement_labels = {
        "ENCAISSEMENT_TOTAL": "Encaissement total",
        "ENCAISSEMENT_PARTIEL": "Encaissement partiel",
        "ENCAISSEMENT_RESTE": "Encaissement du reste",
        "REGULARISATION": "Encaissement du reste",
        "ENCAISSEMENT": "Encaissement total",
        "ANNULATION": "Remboursement / annulation",
    }
    for movement in movements:
        acts = movement.get("actes") or []
        service_names = ", ".join(
            str(act.get("nom") or act.get("designation") or "").strip()
            for act in acts if isinstance(act, dict)
            and str(act.get("nom") or act.get("designation") or "").strip()
        ) or "Prestation non renseignée"
        amount = float(movement.get("montant_mouvement") or 0)
        amount_style = ParagraphStyle(
            "CashAmount", parent=cell_style,
            textColor=colors.HexColor("#b42332") if amount < 0 else colors.HexColor("#176b3a"),
            alignment=2,
        )
        description = (
            f"<b>{html_escape(movement_labels.get(movement.get('type_mouvement'), 'Mouvement de caisse'))}</b>"
            f"<br/>{html_escape(service_names)}"
        )
        table_data.append([
            Paragraph(
                f"#PASS-{int(movement.get('id_passage') or 0):04d}<br/>"
                f"{html_escape(str(movement.get('date_mouvement') or ''))}<br/>"
                f"{html_escape(str(movement.get('heure_mouvement') or ''))}", cell_style
            ),
            Paragraph(
                html_escape(f"{movement.get('nom') or ''} {movement.get('prenom') or ''}".strip()),
                cell_style,
            ),
            Paragraph(description, cell_style),
            Paragraph(f"@{html_escape(str(movement.get('operateur_mouvement') or operator_name))}", cell_style),
            Paragraph(html_escape(str(movement.get("mode_mouvement") or "Espèces")), cell_style),
            Paragraph(f"{'−' if amount < 0 else '+' if amount > 0 else ''}{money(abs(amount))}", amount_style),
        ])

    details = Table(
        table_data,
        # Keep the table inside the printable A4 width (595.28 - 2 * 42 pt).
        # The previous widths summed to 514 pt and overflowed the 511 pt frame,
        # causing ReportLab to fail when the day/guard journal had rows.
        colWidths=[68, 82, 145, 63, 65, 72],
        repeatRows=1,
        hAlign="LEFT",
    )
    details.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(details)

    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4,
        leftMargin=42, rightMargin=42, topMargin=38, bottomMargin=38,
        title=f"Rapport de caisse {report_type} - {operator_name}",
    )
    document.build(story)
    safe_operator = "".join(char if char.isascii() and (char.isalnum() or char in "-_") else "_" for char in operator_name).strip("_") or "utilisateur"
    filename = f"rapport_caisse_{safe_operator}_{active_filters.get('date_debut') or date_debut or 'periode'}.pdf"
    pdf_content = output.getvalue()
    return Response(
        content=pdf_content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@caisse_router.get("/factures/registre")
async def api_list_cash_invoices(
    request: Request,
    date_debut: Optional[str] = None,
    date_fin: Optional[str] = None,
    recherche: Optional[str] = None,
    utilisateur: Optional[str] = None,
):
    _require_any_cash_permission(request, "cash_reports", "cash_collect", "cash_collect_rest", "cash_refund")
    _ensure_invoice_registry_schema()
    start = date_debut or date.today().isoformat()
    end = date_fin or start
    try:
        date.fromisoformat(start)
        date.fromisoformat(end)
    except ValueError:
        raise HTTPException(status_code=400, detail="Période de factures invalide.")
    if end < start:
        raise HTTPException(status_code=400, detail="La date de fin doit suivre la date de début.")
    term = str(recherche or '').strip()
    operator = str(utilisateur or '').strip()
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT id, numero_facture, id_passage,
                   TO_CHAR(date_generation, 'YYYY-MM-DD HH24:MI') AS date_generation,
                   utilisateur, patient_nom, montant_ttc, statut,
                   TO_CHAR(date_annulation, 'YYYY-MM-DD HH24:MI') AS date_annulation,
                   utilisateur_annulation, motif_annulation, numero_avoir
            FROM caisse_factures
            WHERE date_generation::date BETWEEN %s::date AND %s::date
              AND (%s = '' OR patient_nom ILIKE %s OR numero_facture ILIKE %s)
              AND (%s = '' OR utilisateur ILIKE %s)
            ORDER BY date_generation DESC, id DESC;
        """, (start, end, term, f"%{term}%", f"%{term}%", operator, f"%{operator}%"))
        rows = [dict(row) for row in cursor.fetchall()]
    for row in rows:
        row["montant_ttc"] = float(row.get("montant_ttc") or 0)
    return {"success": True, "factures": rows}


@caisse_router.get("/factures/registre/{invoice_id}/pdf")
async def api_get_registered_invoice_pdf(invoice_id: int, request: Request, download: bool = False):
    _require_any_cash_permission(request, "cash_reports", "cash_collect", "cash_collect_rest", "cash_refund")
    _ensure_invoice_registry_schema()
    with get_db_cursor() as cursor:
        cursor.execute("SELECT numero_facture, pdf_data FROM caisse_factures WHERE id = %s;", (invoice_id,))
        row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Facture validée introuvable.")
    return Response(
        content=bytes(row["pdf_data"]), media_type="application/pdf",
        headers={"Content-Disposition": f'{"attachment" if download else "inline"}; filename="facture_{row["numero_facture"]}.pdf"', "Cache-Control": "no-store"},
    )


@caisse_router.get("/factures/{passage_id}/status")
async def api_cash_invoice_status(passage_id: int, request: Request):
    _require_any_cash_permission(request, "cash_reports", "cash_collect", "cash_collect_rest", "cash_refund")
    _ensure_invoice_registry_schema()
    with get_db_cursor() as cursor:
        cursor.execute("SELECT id, numero_facture, statut FROM caisse_factures WHERE id_passage = %s;", (passage_id,))
        row = cursor.fetchone()
    return {"success": True, "validated": bool(row), "invoice_id": row["id"] if row else None,
            "numero_facture": row["numero_facture"] if row else None,
            "statut": row["statut"] if row else None}


@caisse_router.post("/factures/registre/{invoice_id}/annuler")
async def api_cancel_registered_invoice(invoice_id: int, request: Request):
    user = _require_cash_permission(request, "cash_refund")
    _ensure_invoice_registry_schema()
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    reason = str(payload.get("motif") or "").strip() if isinstance(payload, dict) else ""
    if len(reason) < 4:
        raise HTTPException(status_code=400, detail="Indiquez un motif d’annulation (au moins 4 caractères).")
    if len(reason) > 500:
        raise HTTPException(status_code=400, detail="Le motif ne peut pas dépasser 500 caractères.")

    operator = str(user.get("login") or user.get("username") or user.get("nom_utilisateur") or "Utilisateur")
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT id, numero_facture, id_passage, patient_nom, montant_ttc, statut
            FROM caisse_factures WHERE id = %s FOR UPDATE;
        """, (invoice_id,))
        invoice = cursor.fetchone()
        if not invoice:
            raise HTTPException(status_code=404, detail="Facture introuvable dans le registre.")
        if str(invoice.get("statut") or "VALIDEE").upper() == "ANNULEE":
            return {"success": True, "already_cancelled": True, "numero_avoir": invoice.get("numero_avoir") or f"AV-{invoice['numero_facture']}"}

        cancelled_at = datetime.now()
        avoir_number = f"AV-{invoice['numero_facture']}"
        output = BytesIO()
        pdf = canvas.Canvas(output, pagesize=A4)
        width, height = A4
        def pdf_safe_text(value: Any) -> str:
            text = str(value or "").replace("’", "'").replace("‘", "'").replace("—", "-").replace("–", "-")
            return text.encode("cp1252", errors="replace").decode("cp1252")

        pdf.setTitle(f"Avoir {avoir_number}")
        pdf.setFillColor(colors.HexColor("#991b1b"))
        pdf.setFont("Helvetica-Bold", 24)
        pdf.drawCentredString(width / 2, height - 70, "AVOIR / ANNULATION DE FACTURE")
        pdf.setStrokeColor(colors.HexColor("#b91c1c"))
        pdf.setLineWidth(2)
        pdf.roundRect(45, height - 155, width - 90, 44, 8, stroke=1, fill=0)
        pdf.setFillColor(colors.HexColor("#7f1d1d"))
        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawCentredString(width / 2, height - 137, f"N° Avoir : {avoir_number}")

        pdf.setFillColor(colors.black)
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(55, height - 195, "RÉFÉRENCE DE LA FACTURE ANNULÉE")
        pdf.setFont("Helvetica", 11)
        pdf.drawString(55, height - 220, f"N° facture : {invoice['numero_facture']}")
        pdf.drawString(55, height - 240, f"N° passage : Pas-{invoice['id_passage']}")
        pdf.drawString(55, height - 260, pdf_safe_text(f"Patient : {invoice['patient_nom'] or 'Patient'}"))
        pdf.drawString(55, height - 280, pdf_safe_text(f"Date d'annulation : {cancelled_at.strftime('%d/%m/%Y à %H:%M')}"))
        pdf.drawString(55, height - 300, pdf_safe_text(f"Annulee par : {operator}"))

        pdf.setFillColor(colors.HexColor("#fef2f2"))
        pdf.setStrokeColor(colors.HexColor("#fecaca"))
        pdf.roundRect(50, height - 380, width - 100, 48, 8, stroke=1, fill=1)
        pdf.setFillColor(colors.HexColor("#7f1d1d"))
        pdf.setFont("Helvetica-Bold", 12)
        pdf.drawString(65, height - 360, "Montant TTC annulé")
        pdf.drawRightString(width - 65, height - 360, f"{float(invoice['montant_ttc'] or 0):,.2f} DA".replace(",", " "))

        pdf.setFillColor(colors.black)
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(55, height - 420, "Motif de l’annulation :")
        pdf.setFont("Helvetica", 11)
        words = pdf_safe_text(reason).split()
        lines = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if pdf.stringWidth(candidate, "Helvetica", 11) > width - 110 and current:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        y = height - 442
        for line in lines[:8]:
            pdf.drawString(55, y, line)
            y -= 17
        pdf.setFillColor(colors.HexColor("#991b1b"))
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawCentredString(width / 2, 105, "La facture referencee ci-dessus est annulee et ne peut plus etre utilisee comme facture valide.")
        pdf.setFillColor(colors.HexColor("#64748b"))
        pdf.setFont("Helvetica", 9)
        pdf.drawCentredString(width / 2, 78, "Document d’annulation généré par Cliniva")
        pdf.save()
        avoir_bytes = output.getvalue()

        cursor.execute("""
            UPDATE caisse_factures
            SET statut = 'ANNULEE', date_annulation = %s, utilisateur_annulation = %s,
                motif_annulation = %s, numero_avoir = %s, avoir_pdf_data = %s
            WHERE id = %s AND statut <> 'ANNULEE';
        """, (cancelled_at, operator, reason, avoir_number, avoir_bytes, invoice_id))

    _track_financial_action(request, "ANNULATION_FACTURE_CAISSE",
                            f"Facture {invoice['numero_facture']} annulée par {operator}; avoir {avoir_number}; motif: {reason}")
    return {"success": True, "numero_avoir": avoir_number, "statut": "ANNULEE"}


@caisse_router.get("/factures/registre/{invoice_id}/avoir.pdf")
async def api_get_invoice_credit_note(invoice_id: int, request: Request, download: bool = False):
    _require_any_cash_permission(request, "cash_reports", "cash_refund")
    _ensure_invoice_registry_schema()
    with get_db_cursor() as cursor:
        cursor.execute("SELECT numero_avoir, avoir_pdf_data FROM caisse_factures WHERE id = %s AND statut = 'ANNULEE';", (invoice_id,))
        row = cursor.fetchone()
    if not row or not row.get("avoir_pdf_data"):
        raise HTTPException(status_code=404, detail="Aucun avoir n’est enregistré pour cette facture.")
    disposition = "attachment" if download else "inline"
    return Response(content=bytes(row["avoir_pdf_data"]), media_type="application/pdf",
                    headers={"Content-Disposition": f'{disposition}; filename="avoir_{row["numero_avoir"]}.pdf"', "Cache-Control": "no-store"})


@caisse_router.post("/factures/{passage_id}/valider")
async def api_validate_cash_invoice(passage_id: int, request: Request):
    user = _require_any_cash_permission(request, "cash_reports", "cash_collect", "cash_collect_rest")
    _ensure_invoice_registry_schema()
    with get_db_cursor() as cursor:
        cursor.execute("SELECT p.id, p.date_passage, p.montant_total, p.remise, p.actes_json, pat.nom, pat.prenom FROM patient_passages p LEFT JOIN patients pat ON pat.id=p.id_patient WHERE p.id=%s;", (passage_id,))
        passage = cursor.fetchone()
        if not passage:
            raise HTTPException(status_code=404, detail="Passage introuvable.")
        cursor.execute("SELECT id, numero_facture FROM caisse_factures WHERE id_passage=%s;", (passage_id,))
        existing = cursor.fetchone()
    if existing:
        return {"success": True, "invoice_id": existing["id"], "numero_facture": existing["numero_facture"], "already_validated": True}

    # Generate the exact same PDF shown in the preview, then persist it atomically.
    preview = await api_view_cash_invoice_pdf(passage_id, request)
    raw_acts = passage.get("actes_json")
    if isinstance(raw_acts, str):
        try:
            raw_acts = json.loads(raw_acts or "[]")
        except (TypeError, ValueError):
            raw_acts = []
    if isinstance(raw_acts, dict):
        raw_acts = next((raw_acts.get(key) for key in ("actes", "prestations", "items", "lignes") if isinstance(raw_acts.get(key), list)), [])
    gross = Decimal("0.00")
    for act in raw_acts if isinstance(raw_acts, list) else []:
        if not isinstance(act, dict):
            continue
        try:
            quantity = max(1, int(act.get("quantite") or 1))
            amount = Decimal(str(act.get("sous_total"))) if act.get("sous_total") is not None else Decimal(str(act.get("tarif") or act.get("prix") or 0)) * quantity
            if amount.is_finite() and amount >= 0:
                gross += amount
        except (InvalidOperation, TypeError, ValueError):
            continue
    try:
        discount = max(Decimal("0"), Decimal(str(passage.get("remise") or 0)))
    except (InvalidOperation, TypeError, ValueError):
        discount = Decimal("0")
    ttc = max(Decimal("0"), gross - discount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    passage_date = passage.get("date_passage") or date.today()
    invoice_year = str(passage_date)[:4]
    invoice_number = f"{invoice_year}_{passage_id:06d}"
    patient_name = f"{passage.get('nom') or 'Patient'} {passage.get('prenom') or ''}".strip()
    login = str(user.get("login") or user.get("username") or user.get("nom_utilisateur") or "")
    with get_db_cursor() as cursor:
        cursor.execute("""
            INSERT INTO caisse_factures (numero_facture, id_passage, utilisateur, patient_nom, montant_ttc, pdf_data)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id_passage) DO NOTHING
            RETURNING id;
        """, (invoice_number, passage_id, login, patient_name, ttc, preview.body))
        saved = cursor.fetchone()
        cursor.execute("SELECT id, numero_facture FROM caisse_factures WHERE id_passage=%s;", (passage_id,))
        record = cursor.fetchone()
    if not record:
        raise HTTPException(status_code=500, detail="La facture n'a pas pu être enregistrée.")
    _track_financial_action(request, "VALIDATION_FACTURE_CAISSE", f"Facture {invoice_number} validée pour {patient_name} ({ttc} DA)")
    return {"success": True, "invoice_id": record["id"], "numero_facture": record["numero_facture"], "already_validated": not bool(saved)}


@caisse_router.get("/factures/{passage_id}/pdf")
async def api_view_cash_invoice_pdf(passage_id: int, request: Request, download: bool = False):
    """Génère une facture A4 à partir des prestations d'un passage de caisse."""
    user = _require_any_cash_permission(request, "cash_reports", "cash_collect", "cash_collect_rest")
    _ensure_invoice_registry_schema()
    with get_db_cursor() as cursor:
        cursor.execute("SELECT pdf_data, numero_facture FROM caisse_factures WHERE id_passage=%s;", (passage_id,))
        saved_invoice = cursor.fetchone()
        if saved_invoice:
            return Response(content=bytes(saved_invoice["pdf_data"]), media_type="application/pdf", headers={
                "Content-Disposition": f'{"attachment" if download else "inline"}; filename="facture_{saved_invoice["numero_facture"]}.pdf"',
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            })
        if download:
            raise HTTPException(status_code=409, detail="Validez la facture avant de la télécharger.")
        cursor.execute("""
            SELECT p.id AS id_passage, p.id_patient, p.date_passage, p.heure_passage,
                   p.actes_json, p.montant_total, p.remise,
                   COALESCE(pat.nom, 'Patient') AS nom,
                   COALESCE(pat.prenom, '') AS prenom,
                   COALESCE(pat.telephone_1, '') AS telephone,
                   COALESCE(pat.wilaya, '') AS wilaya,
                   COALESCE(pat.commune, '') AS commune,
                   COALESCE(TO_CHAR(pat.date_naissance, 'DD/MM/YYYY'), '') AS date_naissance
            FROM patient_passages p
            LEFT JOIN patients pat ON pat.id = p.id_patient
            WHERE p.id = %s;
        """, (passage_id,))
        passage_row = cursor.fetchone()
        if not passage_row:
            raise HTTPException(status_code=404, detail="Passage introuvable.")
        passage = dict(passage_row)
        cursor.execute("""
            SELECT nom_raison_sociale, telephone, adresse, nif, nis, rc, article_imposition
            FROM clinic_info WHERE id = 1;
        """)
        clinic_row = cursor.fetchone()

    raw_acts = passage.get("actes_json")
    if isinstance(raw_acts, dict):
        # Accommodate both the current JSON array and older wrapped payloads.
        raw_acts = next((raw_acts.get(key) for key in ("actes", "prestations", "items", "lignes")
                         if isinstance(raw_acts.get(key), list)), [])
    elif not isinstance(raw_acts, list):
        try:
            raw_acts = json.loads(raw_acts or "[]")
        except (TypeError, ValueError):
            raw_acts = []
        if isinstance(raw_acts, dict):
            raw_acts = next((raw_acts.get(key) for key in ("actes", "prestations", "items", "lignes")
                             if isinstance(raw_acts.get(key), list)), [])
    if not raw_acts:
        raise HTTPException(status_code=409, detail="Aucune prestation n'est enregistrée pour ce passage.")

    invoice_lines = []
    gross_total = Decimal("0.00")
    for act in raw_acts:
        if not isinstance(act, dict):
            continue
        quantity = max(1, int(act.get("quantite") or 1))
        try:
            line_total = Decimal(str(act.get("sous_total"))) if act.get("sous_total") is not None else (
                Decimal(str(act.get("tarif") or act.get("prix") or 0)) * quantity
            )
            if not line_total.is_finite() or line_total < 0:
                raise InvalidOperation
        except (InvalidOperation, TypeError, ValueError):
            raise HTTPException(status_code=409, detail="Une prestation contient un montant invalide.")
        line_total = line_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        gross_total += line_total
        invoice_lines.append({
            "label": str(act.get("nom") or act.get("designation") or act.get("libelle")
                         or act.get("prestation") or act.get("examen") or "Prestation"),
            "quantity": quantity,
            "unit_price": (line_total / quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            "amount": line_total,
        })
    if not invoice_lines:
        raise HTTPException(status_code=409, detail="Aucune prestation exploitable pour cette facture.")

    # The provided invoice model uses a 9% VAT rate. Its TTC is the sum of
    # prestation prices; we split that amount backwards into HT, VAT and stamp.
    try:
        discount = max(Decimal("0"), Decimal(str(passage.get("remise") or 0)))
    except (InvalidOperation, TypeError, ValueError):
        discount = Decimal("0")
    discount = min(discount, gross_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    ttc = (gross_total - discount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    vat = (ttc * Decimal("0.09")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if ttc <= Decimal("30000"):
        stamp_rate = Decimal("0.01")
    elif ttc <= Decimal("100000"):
        stamp_rate = Decimal("0.015")
    else:
        stamp_rate = Decimal("0.02")
    stamp = ((ttc + vat) * stamp_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    ht = ttc - vat - stamp

    def amount_words_fr(value: Decimal) -> str:
        units = ("zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf",
                 "dix", "onze", "douze", "treize", "quatorze", "quinze", "seize")

        def under_hundred(number: int) -> str:
            if number < len(units):
                return units[number]
            if number < 20:
                return "dix-" + units[number - 10]
            if number < 70:
                tens, remainder = divmod(number, 10)
                names = {2: "vingt", 3: "trente", 4: "quarante", 5: "cinquante", 6: "soixante"}
                return names[tens] + ("" if remainder == 0 else (" et un" if remainder == 1 else "-" + units[remainder]))
            if number < 80:
                return "soixante" + ("-et-onze" if number == 71 else "-" + under_hundred(number - 60))
            if number == 80:
                return "quatre-vingts"
            return "quatre-vingt" + ("" if number == 80 else "-" + under_hundred(number - 80))

        def under_thousand(number: int) -> str:
            if number < 100:
                return under_hundred(number)
            hundreds, remainder = divmod(number, 100)
            prefix = "cent" if hundreds == 1 else units[hundreds] + " cent"
            if remainder == 0 and hundreds > 1:
                prefix += "s"
            return prefix if remainder == 0 else prefix + " " + under_hundred(remainder)

        cents_total = int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        whole, cents = divmod(cents_total, 100)
        if whole >= 1_000_000:
            millions, rest = divmod(whole, 1_000_000)
            words = under_thousand(millions) + " million" + ("s" if millions > 1 else "")
            if rest:
                words += " " + under_thousand(rest // 1000) + " mille" if rest >= 1000 else " " + under_thousand(rest)
                if rest >= 1000 and rest % 1000:
                    words += " " + under_thousand(rest % 1000)
        elif whole >= 1000:
            thousands, rest = divmod(whole, 1000)
            words = ("" if thousands == 1 else under_thousand(thousands) + " ") + "mille"
            if rest:
                words += " " + under_thousand(rest)
        else:
            words = under_thousand(whole)
        result = words + " dinar" + ("s" if whole > 1 else "")
        if cents:
            result += " et " + under_hundred(cents) + " centime" + ("s" if cents > 1 else "")
        return result[:1].upper() + result[1:]

    clinic = dict(clinic_row) if clinic_row else {}
    invoice_year = str(passage.get("date_passage") or date.today().isoformat())[:4]
    invoice_number = f"{invoice_year}_{passage_id:06d}"
    background_path = Path(__file__).resolve().parents[1] / "uploads" / "model_templates" / "Fact.png"
    if not background_path.is_file():
        raise HTTPException(status_code=500, detail="Le fond de facture uploads/model_templates/Fact.png est introuvable.")

    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=A4)
    page_width, page_height = A4

    # Use the clinic's supplied stationery as the sole page background.
    pdf.drawImage(ImageReader(str(background_path)), 0, 0, width=page_width, height=page_height, preserveAspectRatio=False)

    def money(value: Decimal) -> str:
        value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if value == value.to_integral_value():
            rendered = str(int(value))
        else:
            rendered = f"{value:.2f}".replace(".", ",")
        return rendered + " DA"

    def draw_fit(text: str, x: float, y: float, max_width: float, font: str = "Helvetica", size: float = 8):
        text = str(text or "")
        while size > 5 and pdf.stringWidth(text, font, size) > max_width:
            size -= 0.25
        pdf.setFont(font, size)
        pdf.drawString(x, y, text)

    patient_name = f"{passage.get('nom') or ''} {passage.get('prenom') or ''}".strip()
    passage_date = str(passage.get("date_passage") or date.today().isoformat())
    try:
        passage_date = date.fromisoformat(passage_date[:10]).strftime("%d/%m/%Y")
    except ValueError:
        pass
    pdf.setFillColor(colors.black)
    fiscal_parts = [
        ("RC", clinic.get("rc")),
        ("ART", clinic.get("article_imposition")),
        ("NIF", clinic.get("nif")),
        ("NIS", clinic.get("nis")),
    ]
    fiscal_line = "   ".join(f"{label} {value}" for label, value in fiscal_parts if value not in (None, ""))
    if fiscal_line:
        draw_fit(fiscal_line, 48, 722, page_width - 96, "Helvetica-Bold", 12)
    pdf.setFont("Helvetica-Bold", 32)
    pdf.drawCentredString(page_width / 2, 676, "Facture")
    pdf.setFont("Helvetica-Bold", 12.5)
    pdf.drawString(48, 636, f"ID Passage : Pas-{passage_id}")
    draw_fit(f"N° Facture : {invoice_number}", 48, 616, 390, "Helvetica-Bold", 12.5)
    draw_fit(f"Date : {passage_date}", 48, 596, 340, "Helvetica-Bold", 12.5)
    draw_fit(f"Patient : {patient_name}", 48, 576, 390, "Helvetica-Bold", 12.5)

    # QR links the invoice to its passage without exposing additional patient data.
    try:
        qr = QrCodeWidget(f"CLINIVA|FACTURE:{invoice_number}|PASSAGE:{passage_id}")
        x1, y1, x2, y2 = qr.getBounds()
        qr_size = 84
        qr_drawing = Drawing(x2 - x1, y2 - y1)
        qr_drawing.add(qr)
        pdf.saveState()
        pdf.translate(462, 603)
        pdf.scale(qr_size / (x2 - x1), qr_size / (y2 - y1))
        renderPDF.draw(qr_drawing, pdf, -x1, -y1)
        pdf.restoreState()
    except Exception:
        logger.debug("Impossible de générer le QR de la facture", exc_info=True)

    # Preserve the previously approved invoice layout on the supplied clinic
    # stationery: broad prestation grid, totals at lower right, and amount in words.
    table_x, table_bottom, table_top, table_width = 48, 366, 530, 508
    divider_x = 452
    pdf.setStrokeColor(colors.black)
    pdf.setLineWidth(0.8)
    pdf.rect(table_x, table_bottom, table_width, table_top - table_bottom)
    pdf.line(divider_x, table_bottom, divider_x, table_top)
    pdf.rect(table_x, table_top, 153, 22)
    pdf.setFillColor(colors.black)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(table_x + 4, table_top + 7, "Liste D'examens")

    row_space = (table_top - table_bottom - 12) / max(1, len(invoice_lines))
    row_font = max(5.5, min(10, row_space * 0.62))
    for index, line in enumerate(invoice_lines):
        y = table_top - 12 - index * row_space
        label = f"{line['quantity']} × {line['label']}" if line["quantity"] > 1 else line["label"]
        draw_fit(label, table_x + 4, y, divider_x - table_x - 10, "Helvetica-Bold", row_font)
        pdf.setFont("Helvetica-Bold", row_font)
        pdf.drawRightString(table_x + table_width - 7, y, money(line["amount"]))

    total_rows = [("Total HT", ht), ("Remise", discount), ("TVA (9 %)", vat),
                  (f"Timbre Fiscal ({stamp_rate * 100:g} %)", stamp), ("Total TTC", ttc)]
    totals_x, totals_y, totals_width, totals_height = 347, 280, 209, 86
    totals_split = 452
    total_row_h = totals_height / len(total_rows)
    pdf.setStrokeColor(colors.black)
    pdf.setLineWidth(0.8)
    pdf.rect(totals_x, totals_y, totals_width, totals_height)
    pdf.line(totals_split, totals_y, totals_split, totals_y + totals_height)
    for index, (label, value) in enumerate(total_rows):
        row_top = totals_y + totals_height - index * total_row_h
        if index:
            pdf.line(totals_x, row_top, totals_x + totals_width, row_top)
        pdf.setFont("Helvetica-Bold", 8.5)
        pdf.drawString(totals_x + 7, row_top - 12, label)
        pdf.drawRightString(totals_x + totals_width - 7, row_top - 12, money(value))

    pdf.setFillColor(colors.black)
    draw_fit(f"Arrêtée à la somme de : {amount_words_fr(ttc)} Dinars", 48, 252, 500, "Helvetica-Bold", 9)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawRightString(530, 214, "La Direction")
    pdf.save()

    return Response(
        content=output.getvalue(),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="facture_{invoice_number}.pdf"',
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@caisse_router.post("/regulariser")
async def api_regulariser_impaye(payload: Dict[str, Any], request: Request):
    user = _require_any_cash_permission(request, "cash_collect", "cash_collect_rest")
    try:
        passage_id = int(payload.get("id_passage"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Identifiant de passage invalide.")
    with get_db_cursor() as cursor:
        cursor.execute("SELECT versement FROM patient_passages WHERE id = %s;", (passage_id,))
        payment_row = cursor.fetchone()
    if not payment_row:
        raise HTTPException(status_code=404, detail="Passage introuvable.")
    needed_permission = "cash_collect_rest" if float(payment_row.get("versement") or 0) > 0 else "cash_collect"
    if not _has_cash_permission(user, needed_permission):
        raise HTTPException(status_code=403, detail="Vous n'avez pas le droit d'encaisser un reste." if needed_permission == "cash_collect_rest" else "Vous n'avez pas le droit d'effectuer un encaissement initial.")
    res = caisse_manager.regulariser(payload, user)
    if res.get("success"):
        action_label = "Reste encaissé" if float(payment_row.get("versement") or 0) > 0 else "Paiement encaissé"
        _track_financial_action(request, "ENCAISSEMENT_CAISSE",
                                f"{action_label} #{payload.get('id_passage')} ({payload.get('montant_versement')} DA)")
    return res


@caisse_router.post("/annuler-paiement")
async def api_annuler_paiement(payload: Dict[str, Any], request: Request):
    user = _require_cash_permission(request, "cash_refund")
    res = caisse_manager.annuler_paiement(payload, user)
    if res.get("success"):
        _track_financial_action(request, "ANNULATION_PAIEMENT", f"Annulation paiement #{payload.get('id_passage')}")
    return res


@caisse_router.get("/mouvements/{mouvement_id}/ticket", response_class=HTMLResponse)
async def view_movement_receipt(mouvement_id: int, request: Request):
    """Affiche le reçu d'un seul mouvement, sans confondre le montant cumulé du passage."""
    _require_any_cash_permission(request, "cash_collect", "cash_collect_rest")
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT cm.id AS id_mouvement, cm.id_passage, cm.type_mouvement,
                   cm.montant AS montant_mouvement, cm.mode_paiement AS mode_mouvement,
                   TO_CHAR(cm.date_mouvement, 'YYYY-MM-DD') AS date_mouvement,
                   cm.heure_mouvement, cm.utilisateur AS operateur_mouvement, cm.motif,
                   p.id_patient, p.actes_json, p.montant_total, p.remise,
                   p.remise_type, p.remise_valeur,
                   GREATEST(COALESCE(p.montant_total, 0) - COALESCE(p.remise, 0) - COALESCE((
                       SELECT SUM(cm2.montant)
                       FROM caisse_mouvements cm2
                       WHERE cm2.id_passage = p.id
                         AND (
                             cm2.date_mouvement < cm.date_mouvement
                             OR (cm2.date_mouvement = cm.date_mouvement AND cm2.heure_mouvement < cm.heure_mouvement)
                             OR (cm2.date_mouvement = cm.date_mouvement AND cm2.heure_mouvement = cm.heure_mouvement AND cm2.id <= cm.id)
                         )
                   ), 0), 0) AS reste,
                   COALESCE(pat.nom, 'Patient') AS nom,
                   COALESCE(pat.prenom, '') AS prenom
            FROM caisse_mouvements cm
            JOIN patient_passages p ON p.id = cm.id_passage
            LEFT JOIN patients pat ON pat.id = p.id_patient
            WHERE cm.id = %s;
        """, (mouvement_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Mouvement de caisse introuvable.")
        movement = dict(row)
        cursor.execute("""
            SELECT cm.id AS id_mouvement, cm.id_passage, cm.type_mouvement,
                   cm.montant AS montant_mouvement,
                   TO_CHAR(cm.date_mouvement, 'YYYY-MM-DD') AS date_mouvement,
                   cm.heure_mouvement
            FROM caisse_mouvements cm
            WHERE cm.id_passage = %s
            ORDER BY cm.date_mouvement, cm.heure_mouvement, cm.id
        """, (movement.get("id_passage"),))
        history = [dict(item) for item in cursor.fetchall()]
        passage_acts = movement.get("actes_json")
        if not isinstance(passage_acts, list):
            try:
                passage_acts = json.loads(passage_acts or "[]")
            except (TypeError, ValueError):
                passage_acts = []
        allocations = allocate_movement_categories({
            movement.get("id_passage"): {
                "actes": passage_acts,
                "montant_total": movement.get("montant_total"),
                "remise": movement.get("remise"),
            }
        }, history)
        movement["allocations"] = allocations.get(movement.get("id_mouvement"), {})
        cursor.execute("SELECT nom_raison_sociale, adresse, telephone FROM clinic_info WHERE id = 1;")
        clinic_row = cursor.fetchone()

    acts = movement.get("actes_json")
    movement["actes"] = acts if isinstance(acts, list) else json.loads(acts or "[]")
    clinic = dict(clinic_row) if clinic_row else {}
    movement["clinic"] = {
        "clinic_name": clinic.get("nom_raison_sociale") or "Cliniva Medical Center",
        "clinic_adresse": clinic.get("adresse") or "",
        "clinic_tel": clinic.get("telephone") or "",
    }
    from modules.gestion_passages import render_ticket_html
    return render_ticket_html(movement, movement["clinic"], movement=movement)


@caisse_router.get("/cloture-pv-html", response_class=HTMLResponse)
async def view_cloture_pv_html(
        request: Request,
        date_debut: Optional[str] = None,
        date_fin: Optional[str] = None,
        operateur: Optional[str] = "TOUS",
        mode_paiement: Optional[str] = "TOUS",
        statut_paiement: Optional[str] = "TOUS"
):
    """Génère la feuille A4 officielle du PV de clôture de caisse."""
    _require_cash_permission(request, "cash_reports")
    filters = {
        "date_debut": date_debut,
        "date_fin": date_fin,
        "operateur": operateur,
        "mode_paiement": mode_paiement,
        "statut_paiement": statut_paiement
    }
    data = caisse_manager.get_stats(filters)
    k = data.get("kpis") or {}
    f = data.get("filtres_actifs") or {}
    clinic = data.get("clinic_info") or {}

    def f_da(n):
        try:
            value = float(n or 0)
            if not math.isfinite(value):
                value = 0.0
        except (TypeError, ValueError):
            value = 0.0
        return f"{value:,.2f}".replace(",", " ") + " DA"

    journal_extrait = (data.get("journal") or [])[:20]
    lignes_tr = ""
    for j in journal_extrait:
        m_val = float(j.get("montant_mouvement") or 0.0)
        color_val = "#15803d" if m_val >= 0 else "#dc2626"
        sign_val = "+" if m_val > 0 else ""
        movement_names = {
            "ENCAISSEMENT_TOTAL": "Encaissement total",
            "ENCAISSEMENT_PARTIEL": "Encaissement partiel",
            "ENCAISSEMENT_RESTE": "Encaissement du reste",
            "REGULARISATION": "Encaissement du reste",
            "ENCAISSEMENT": "Encaissement total",
            "ANNULATION": "Remboursement / annulation",
        }
        movement_name = movement_names.get(j.get("type_mouvement"), "Mouvement de caisse")
        actes = j.get("actes") or []
        if isinstance(actes, str):
            try:
                actes = json.loads(actes or "[]")
            except (TypeError, ValueError):
                actes = []
        prestations = ", ".join(
            str(acte.get("nom") or "").strip()
            for acte in actes
            if isinstance(acte, dict) and str(acte.get("nom") or "").strip()
        ) or "Prestation non renseignée"
        lignes_tr += f"""
        <tr style="border-bottom:1px solid #e2e8f0;">
            <td style="padding:5px;">#PASS-{j['id_passage']:04d}</td>
            <td style="padding:5px; font-weight:bold;">{html_escape(str(j.get('nom', '') or ''))} {html_escape(str(j.get('prenom', '') or ''))}</td>
            <td style="padding:5px; font-size:9pt;">{html_escape(movement_name)}</td>
            <td style="padding:5px; font-size:8pt;">{html_escape(prestations)}</td>
            <td style="padding:5px; font-family:monospace;">@{html_escape(str(j.get('operateur_mouvement', '') or ''))}</td>
            <td style="padding:5px; text-align:right; color:{color_val}; font-weight:bold; font-family:monospace;">{sign_val}{f_da(m_val)}</td>
            <td style="padding:5px; text-align:center;">{html_escape(str(j.get('mode_mouvement', '') or ''))}</td>
        </tr>
        """

    def special_sales_table(category):
        rows = []
        total = 0.0
        for movement in data.get("journal") or []:
            try:
                acts = movement.get("actes") or []
                if isinstance(acts, str):
                    acts = json.loads(acts or "[]")
                matching = [
                    act for act in acts
                    if isinstance(act, dict) and classify_prestation(act) == category
                ]
                category_payment = float(
                    movement.get("montant_contraste" if category == "contraste" else "montant_sedation") or 0
                )
            except (TypeError, ValueError):
                continue
            if abs(category_payment) < 0.005:
                continue
            weighted = []
            for act in matching:
                try:
                    weight = float(act.get("sous_total") or 0)
                    if weight <= 0:
                        weight = float(act.get("tarif") or act.get("prix") or act.get("prix_jour") or 0) * float(act.get("quantite") or 1)
                except (TypeError, ValueError):
                    weight = 0.0
                weighted.append((act, max(0.0, weight)))
            if not weighted:
                weighted = [({"nom": "Prestation non renseignée"}, 1.0)]
            weight_sum = sum(weight for _, weight in weighted)
            if weight_sum <= 0:
                weighted = [(act, 1.0) for act, _ in weighted]
                weight_sum = len(weighted)
            allocated_so_far = 0.0
            for index, (act, weight) in enumerate(weighted):
                payment = (
                    round(category_payment - allocated_so_far, 2)
                    if index == len(weighted) - 1
                    else round(category_payment * weight / weight_sum, 2)
                )
                allocated_so_far += payment
                total += payment
                rows.append(f"""
                    <tr>
                        <td>{html_escape(str(movement.get("nom") or ""))} {html_escape(str(movement.get("prenom") or ""))}</td>
                        <td>{html_escape(str(act.get("nom") or act.get("designation") or "Prestation non renseignée"))}</td>
                        <td class="text-right">{f_da(payment)}</td>
                    </tr>
                """)
        rows.append(f'<tr class="total-row"><td colspan="2">TOTAL</td><td class="text-right">{f_da(total)}</td></tr>')
        return "".join(rows), total

    lignes_contraste, total_contraste = special_sales_table("contraste")
    lignes_sedation, total_sedation = special_sales_table("sedation")

    return f"""
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="UTF-8">
        <title>PV de Clôture de Caisse A4</title>
        <style>
            @page {{ size: A4 portrait; margin: 15mm 20mm; }}
            * {{ box-sizing: border-box; }}
            body {{
                font-family: 'Times New Roman', serif; font-size: 11pt; color: black; line-height: 1.4;
                margin: 0; padding: 20px; background: #525659; display: flex; justify-content: center;
            }}
            .sheet {{
                width: 210mm; min-height: 297mm; background: white; padding: 20mm 25mm;
                box-shadow: 0 4px 15px rgba(0,0,0,0.2);
            }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 10pt; }}
            th, td {{ border: 1px solid #cbd5e1; padding: 6px 10px; text-align: left; }}
            th {{ background: #f8fafc; font-weight: bold; }}
            .text-right {{ text-align: right; }}
            .bold {{ font-weight: bold; }}
            .special-sales .total-row {{ background: #ecfdf5; font-weight: bold; }}
            .no-print {{ position: fixed; top: 15px; right: 15px; z-index: 9999; display: flex; gap: 10px; }}
            .btn {{ border: none; padding: 10px 18px; border-radius: 10px; font-weight: bold; cursor: pointer; }}
            .btn-print {{ background: #121c15; color: #8be140; }}
            .btn-close {{ background: #dc2626; color: white; }}
            @media print {{
                .no-print {{ display: none !important; }}
                body {{ background: white; padding: 0; }}
                .sheet {{ box-shadow: none; padding: 0; width: 100%; }}
            }}
        </style>
    </head>
    <body>
        <div class="no-print">
            <button class="btn btn-print" onclick="window.print()">🖨️ Imprimer le PV A4</button>
            <button class="btn btn-close" onclick="window.parent && window.parent.closeCloturePvModal ? window.parent.closeCloturePvModal() : window.close()">✕ Quitter</button>
        </div>

        <div class="sheet">
            <div style="border-bottom: 2px solid black; padding-bottom: 8px; margin-bottom: 20px; display:flex; justify-content:space-between; align-items:flex-end;">
                <div>
                    <h2 style="margin:0; text-transform:uppercase; font-size:15pt;">{html_escape(str(clinic.get('nom_raison_sociale', 'CLINIVA') or 'CLINIVA'))}</h2>
                    <p style="margin:2px 0 0 0; font-size:9.5pt; color:#64748b;">Service de Facturation & Caisse Centrale | Tél: {html_escape(str(clinic.get('telephone', '-') or '-'))}</p>
                </div>
                <div style="text-align:right; font-size:9.5pt;">
                    <div>Date d'édition : <strong>{datetime.now().strftime('%d/%m/%Y à %H:%M')}</strong></div>
                    <div>Période : Du {f.get('date_debut')} au {f.get('date_fin')}</div>
                </div>
            </div>

            <h2 style="text-align:center; text-decoration:underline; margin: 25px 0; font-size:16pt; letter-spacing:1px;">
                PROCÈS-VERBAL DE CLÔTURE DE CAISSE
            </h2>

            <table>
                <tr>
                    <td>Chiffre d'Affaires Brut de la Période :</td>
                    <td class="text-right bold">{f_da(k.get('ca_brut'))}</td>
                    <td>Nombre d'Admissions du jour :</td>
                    <td class="text-right bold">{k.get('count_passages', 0)}</td>
                </tr>
                <tr>
                    <td>Total Remises Accordées :</td>
                    <td class="text-right bold" style="color:red;">-{f_da(k.get('remises_total'))}</td>
                    <td>Dossiers Remisés :</td>
                    <td class="text-right bold">{k.get('count_remises', 0)}</td>
                </tr>
                <tr>
                    <td>Net Facturé Théorique :</td>
                    <td class="text-right bold">{f_da(k.get('net_du'))}</td>
                    <td>Dossiers avec Restes Dûs :</td>
                    <td class="text-right bold">{k.get('count_impayes', 0)}</td>
                </tr>
                <tr style="background:#ecfdf5; font-size:12pt;">
                    <td class="bold" style="color:#065f46;">TOTAL ENCAISSÉ RÉEL EN CAISSE :</td>
                    <td class="text-right bold" style="color:#065f46;" colspan="3">{f_da(k.get('encaisse_total'))}</td>
                </tr>
                <tr>
                    <td class="bold" style="color:#b45309;">Restes Globaux à Recouvrer :</td>
                    <td class="text-right bold" style="color:#b45309;" colspan="3">{f_da(k.get('restes_total'))}</td>
                </tr>
            </table>

            <div style="font-size:11pt; font-weight:bold; margin-top:25px; margin-bottom:6px;">
                Mouvements de Caisse Effectués sur la Période :
            </div>
            <table>
                <thead>
                    <tr style="background:#f1f5f9;">
                        <th>N°</th><th>Patient</th><th>Type Flux</th><th>Nature des prestations</th><th>Caissier</th><th class="text-right">Montant Réel</th><th style="text-align:center;">Mode</th>
                    </tr>
                </thead>
                <tbody>
                    {lignes_tr}
                </tbody>
            </table>

            <div style="font-size:11pt; font-weight:bold; margin-top:25px; margin-bottom:6px;">
                Ventes de produits de contraste :
            </div>
            <table class="special-sales">
                <thead><tr style="background:#f1f5f9;"><th>Nom et prénom du patient</th><th>Produit vendu</th><th class="text-right">Paiement</th></tr></thead>
                <tbody>{lignes_contraste}</tbody>
            </table>

            <div style="font-size:11pt; font-weight:bold; margin-top:25px; margin-bottom:6px;">
                Ventes de produits de sédation :
            </div>
            <table class="special-sales">
                <thead><tr style="background:#f1f5f9;"><th>Nom et prénom du patient</th><th>Produit vendu</th><th class="text-right">Paiement</th></tr></thead>
                <tbody>{lignes_sedation}</tbody>
            </table>

            <div style="margin-top: 50px; display:flex; justify-content:space-between;">
                <div style="text-align:center; width:200px;">
                    <div>Visa du Caissier</div>
                    <div style="font-size:9pt; color:#64748b; margin-top:5px;">(Signature & Heure)</div>
                </div>
                <div style="text-align:center; width:200px;">
                    <div>Visa & Approbation Direction</div>
                    <div style="font-size:9pt; color:#64748b; margin-top:5px;">(Cachet & Visa)</div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """


# ============================================================================
# RENDU DU COMPOSANT HTML ET JAVASCRIPT
# ============================================================================
def render_html(user: Optional[Dict[str, Any]] = None) -> str:
    """Génère l'HTML complet de la caisse avec capsules centrées et visionneur A4."""
    accordeurs_options = "".join([f'<option value="{a}">{a}</option>' for a in ACCORDEURS_REMISE_LIST])
    raisons_options = "".join([f'<option value="{r}">{r}</option>' for r in RAISONS_ANNULATION])
    raw_permissions = _permissions(user or {})
    caisse_access = {
        "cash_collect": bool(user and _has_cash_permission(user, "cash_collect")),
        "cash_collect_rest": bool(user and _has_cash_permission(user, "cash_collect_rest")),
        "cash_refund": bool(user and _has_cash_permission(user, "cash_refund")),
        "cash_remise": bool(user and _has_cash_permission(user, "cash_remise")),
        "cash_reports": bool(user and _has_cash_permission(user, "cash_reports")),
    }
    caisse_access_json = json.dumps(caisse_access, ensure_ascii=False)

    return f"""
    <div id="view-caisse" class="space-y-6">

        <!-- BANDEAU D'EN-TÊTE -->
        <div class="banner-glow rounded-3xl p-6 lg:p-8 text-white shadow-xl flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
            <div>
                <h1 class="text-2xl font-extrabold tracking-tight mb-1">Caisse & Clôture Financière</h1>
                <p class="text-xs text-slate-300">Journal des recettes en temps réel, encaissement direct des restes et contrôle des remboursements du jour.</p>
            </div>

            <div class="flex items-center gap-2.5">
                <button type="button" id="caisseDailyJournalButton" onclick="openCaisseUserReportPdf('TOUS', 'jour')" class="flex items-center gap-2 bg-amber-500 hover:bg-amber-600 text-white px-4 py-2.5 rounded-2xl font-bold text-xs shadow-md transition cursor-pointer">
                    <i class="ph-bold ph-sun text-base"></i><span>Journal du jour</span>
                </button>
                <button type="button" id="caisseGuardJournalButton" onclick="openCaisseUserReportPdf('TOUS', 'garde')" class="flex items-center gap-2 bg-violet-600 hover:bg-violet-700 text-white px-4 py-2.5 rounded-2xl font-bold text-xs shadow-md transition cursor-pointer">
                    <i class="ph-bold ph-moon text-base"></i><span>Journal de garde</span>
                </button>
                <button type="button" id="caisseInvoiceRegisterButton" onclick="openCaisseInvoiceRegister()" class="flex items-center gap-2 bg-emerald-600 hover:bg-emerald-700 text-white px-4 py-2.5 rounded-2xl font-bold text-xs shadow-md transition cursor-pointer">
                    <i class="ph-bold ph-files text-base"></i><span>Registre des factures</span>
                </button>
                <button type="button" id="caissePvButton" onclick="printClotureCaissePV()" class="flex items-center gap-2 bg-slate-900 hover:bg-black text-white px-4 py-2.5 rounded-2xl font-bold text-xs shadow-md border border-white/10 transition cursor-pointer">
                    <i class="ph-bold ph-printer text-brand-neon text-base"></i>
                    <span>PV de Clôture A4</span>
                </button>
            </div>
        </div>

        <!-- BARRE DE FILTRES TEMPORELS ET RECHERCHE -->
        <div id="caisseFiltersPanel" class="bg-white dark:bg-[#121c15] p-5 rounded-3xl shadow-sm border border-slate-100 dark:border-white/5 space-y-4 transition-colors">
            <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-slate-100 dark:border-white/5 pb-3">
                <span class="text-xs font-bold text-slate-700 dark:text-slate-300 uppercase tracking-wider flex items-center gap-1.5">
                    <i class="ph-bold ph-funnel text-brand-neon"></i>
                    <span>Filtres de Période & Caissiers</span>
                </span>
                <div class="flex items-center gap-1.5 flex-wrap">
                    <button type="button" onclick="setCaisseDateQuick('today')" class="px-3 py-1 rounded-xl text-[11px] font-bold bg-slate-100 dark:bg-black/30 hover:bg-brand-neon/20 hover:text-brand-neon text-slate-600 dark:text-slate-300 transition cursor-pointer">Aujourd'hui</button>
                    <button type="button" onclick="setCaisseDateQuick('yesterday')" class="px-3 py-1 rounded-xl text-[11px] font-bold bg-slate-100 dark:bg-black/30 hover:bg-brand-neon/20 hover:text-brand-neon text-slate-600 dark:text-slate-300 transition cursor-pointer">Hier</button>
                    <button type="button" onclick="setCaisseDateQuick('this_week')" class="px-3 py-1 rounded-xl text-[11px] font-bold bg-slate-100 dark:bg-black/30 hover:bg-brand-neon/20 hover:text-brand-neon text-slate-600 dark:text-slate-300 transition cursor-pointer">Cette Semaine</button>
                    <button type="button" onclick="setCaisseDateQuick('this_month')" class="px-3 py-1 rounded-xl text-[11px] font-bold bg-slate-100 dark:bg-black/30 hover:bg-brand-neon/20 hover:text-brand-neon text-slate-600 dark:text-slate-300 transition cursor-pointer">Ce Mois</button>
                </div>
            </div>

            <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-12 gap-3 items-end">
                <div class="md:col-span-2">
                    <label class="block text-[10px] font-bold text-slate-400 uppercase mb-1">Date Début</label>
                    <input type="date" id="caisseDateDebut" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white outline-none focus:border-brand-neon">
                </div>
                <div class="md:col-span-2">
                    <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Date Fin</label>
                    <input type="date" id="caisseDateFin" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white outline-none focus:border-brand-neon">
                </div>
                <div class="md:col-span-3">
                    <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Caissier / Opérateur</label>
                    <select id="caisseFilterOperateur" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white outline-none">
                        <option value="TOUS">-- Tous les Caissiers --</option>
                    </select>
                </div>
                <div class="md:col-span-2">
                    <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Mode Paiement</label>
                    <select id="caisseFilterMode" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-medium text-slate-800 dark:text-white outline-none">
                        <option value="TOUS">Tous Modes</option>
                        <option value="Espèces">💵 Espèces</option>
                        <option value="Carte CIB / Edahabia">💳 Carte CIB / Edahabia</option>
                        <option value="Chèque">📝 Chèque</option>
                        <option value="Prise en Charge (PEC)">📑 Prise en Charge (PEC)</option>
                        <option value="Autre">Autre</option>
                    </select>
                </div>
                <div class="md:col-span-2">
                    <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Statut Paiement</label>
                    <select id="caisseFilterStatut" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-medium text-slate-800 dark:text-white outline-none">
                        <option value="TOUS">Tous Statuts</option>
                        <option value="Payé">✅ Soldé / Payé</option>
                        <option value="Partiel">⚠️ Partiel</option>
                        <option value="Non payé">❌ Non payé</option>
                        <option value="Annulé">Remboursé / annulé</option>
                    </select>
                </div>
                <div class="md:col-span-1">
                    <button type="button" onclick="loadCaisseData()" class="w-full bg-brand-neon hover:bg-brand-neonDark text-black py-2.5 rounded-xl text-xs font-bold shadow-md transition cursor-pointer flex items-center justify-center">
                        <i class="ph-bold ph-arrows-clockwise text-base"></i>
                    </button>
                </div>
            </div>
        </div>

        <!-- CAPSULES KPIS FINANCIERS CENTRÉES -->
        <div id="caisseKpisPanel" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-8 gap-4">

            <!-- 1. Encaissé Réel en Caisse -->
            <div class="bg-gradient-to-br from-[#12281a] to-[#0a150e] rounded-3xl p-5 text-white shadow-md border border-brand-neon/30 flex flex-col items-center justify-center text-center">
                <div class="flex items-center justify-center gap-1.5 text-[11px] font-bold text-slate-300 mb-1">
                    <span>Total Encaissé (Période)</span>
                    <i class="ph-bold ph-wallet text-brand-neon text-base"></i>
                </div>
                <div class="my-1.5">
                    <span id="kpiEncaisseTotal" class="text-2xl font-black text-brand-neon font-mono">0.00 DA</span>
                </div>
                <span class="text-[10px] text-slate-400">Flux net réel en caisse</span>
            </div>

            <!-- 2. Brut -->
            <div class="bg-white dark:bg-[#121c15] rounded-3xl p-5 shadow-sm border border-slate-100 dark:border-white/5 flex flex-col items-center justify-center text-center">
                <span class="text-[11px] font-bold text-slate-400 mb-1">Chiffre d'Affaires Brut</span>
                <div class="my-1.5">
                    <span id="kpiCaBrut" class="text-xl font-extrabold text-slate-900 dark:text-white font-mono">0.00 DA</span>
                </div>
                <span class="text-[10px] text-slate-400">Admissions créées : <strong id="kpiPassagesCount" class="text-slate-800 dark:text-white font-bold">0</strong></span>
            </div>

            <!-- 3. Remises -->
            <div class="bg-white dark:bg-[#121c15] rounded-3xl p-5 shadow-sm border border-slate-100 dark:border-white/5 flex flex-col items-center justify-center text-center">
                <span class="text-[11px] font-bold text-slate-400 mb-1">Total Remises Accordées</span>
                <div class="my-1.5">
                    <span id="kpiRemisesTotal" class="text-xl font-extrabold text-rose-500 font-mono">-0.00 DA</span>
                </div>
                <span class="text-[10px] text-slate-400">Dossiers remisés : <strong id="kpiRemisesCount" class="text-slate-700 dark:text-slate-300 font-bold">0</strong></span>
            </div>

            <!-- Remboursements effectués -->
            <div class="bg-white dark:bg-[#121c15] rounded-3xl p-5 shadow-sm border border-rose-100 dark:border-rose-500/10 flex flex-col items-center justify-center text-center">
                <span class="text-[11px] font-bold text-slate-400 mb-1">Total Remboursé</span>
                <div class="my-1.5">
                    <span id="kpiRemboursementsTotal" class="text-xl font-extrabold text-rose-500 font-mono">0.00 DA</span>
                </div>
                <span class="text-[10px] text-slate-400">Remboursements de la période</span>
            </div>

            <!-- Prestations spéciales séparées -->
            <div class="bg-white dark:bg-[#121c15] rounded-3xl p-5 shadow-sm border border-violet-100 dark:border-violet-500/10 flex flex-col items-center justify-center text-center">
                <span class="text-[11px] font-bold text-slate-400 mb-1">Produits de contraste encaissés</span>
                <div class="my-1.5"><span id="kpiProduitsContraste" class="text-xl font-extrabold text-violet-600 dark:text-violet-400 font-mono">0.00 DA</span></div>
                <span class="text-[10px] text-slate-400">Net des remboursements</span>
            </div>
            <div class="bg-white dark:bg-[#121c15] rounded-3xl p-5 shadow-sm border border-cyan-100 dark:border-cyan-500/10 flex flex-col items-center justify-center text-center">
                <span class="text-[11px] font-bold text-slate-400 mb-1">Sédations encaissées</span>
                <div class="my-1.5"><span id="kpiSedations" class="text-xl font-extrabold text-cyan-600 dark:text-cyan-400 font-mono">0.00 DA</span></div>
                <span class="text-[10px] text-slate-400">Net des remboursements</span>
            </div>

            <!-- 4. Net Dû -->
            <div class="bg-white dark:bg-[#121c15] rounded-3xl p-5 shadow-sm border border-slate-100 dark:border-white/5 flex flex-col items-center justify-center text-center">
                <span class="text-[11px] font-bold text-slate-400 mb-1">Net Facturé Théorique</span>
                <div class="my-1.5">
                    <span id="kpiNetDu" class="text-xl font-extrabold text-sky-600 dark:text-sky-400 font-mono">0.00 DA</span>
                </div>
                <span class="text-[10px] text-slate-400">(Brut - Remises)</span>
            </div>

            <!-- 5. Restes Dûs / Impayés -->
            <div class="bg-white dark:bg-[#121c15] rounded-3xl p-5 shadow-sm border border-slate-100 dark:border-white/5 flex flex-col items-center justify-center text-center">
                <span class="text-[11px] font-bold text-slate-400 mb-1">Restes Dûs à Recouvrer</span>
                <div class="my-1.5">
                    <span id="kpiRestesTotal" class="text-xl font-extrabold text-amber-500 font-mono">0.00 DA</span>
                </div>
                <span class="text-[10px] text-slate-400">Dossiers en attente : <strong id="kpiImpayesCount" class="text-amber-500 font-bold">0</strong></span>
            </div>
        </div>

        <!-- SÉLECTEUR DES 5 ONGLETS -->
        <div id="caisseTabsBar" class="flex items-center gap-2 overflow-x-auto p-1.5 bg-white dark:bg-[#121c15] border border-slate-200/80 dark:border-white/10 rounded-2xl shadow-sm w-fit">
            <button id="caisse-tab-btn-journal" onclick="switchCaisseTab('journal')" class="caisse-tab-btn flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-xs bg-brand-neon text-black shadow-md shadow-brand-neon/20 transition-all cursor-pointer">
                <i class="ph-bold ph-book-open text-base"></i>
                <span>Journal des Flux Réels</span>
            </button>
            <button id="caisse-tab-btn-nonpayes" onclick="switchCaisseTab('nonpayes')" class="caisse-tab-btn flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-xs text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white transition-all cursor-pointer">
                <i class="ph-bold ph-hand-coins text-base"></i>
                <span>Patients pas encore encaissés</span>
                <span id="badgeNonPayesCount" class="ml-1 px-2 py-0.5 rounded-full text-[10px] bg-rose-500/20 text-rose-500 font-extrabold">0</span>
            </button>
            <button id="caisse-tab-btn-impayes" onclick="switchCaisseTab('impayes')" class="caisse-tab-btn flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-xs text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white transition-all cursor-pointer">
                <i class="ph-bold ph-hourglass-high text-base"></i>
                <span>Suivi des Impayés & Restes</span>
            </button>
            <button id="caisse-tab-btn-remises" onclick="switchCaisseTab('remises')" class="caisse-tab-btn flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-xs text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white transition-all cursor-pointer">
                <i class="ph-bold ph-tag text-base"></i>
                <span>Récapitulatif des Remises</span>
            </button>
            <button id="caisse-tab-btn-annules" onclick="switchCaisseTab('annules')" class="caisse-tab-btn flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-xs text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white transition-all cursor-pointer">
                <i class="ph-bold ph-x-circle text-base"></i>
                <span>Paiements Annulés</span>
            </button>
            <button id="caisse-tab-btn-rapport-utilisateurs" onclick="switchCaisseTab('rapport-utilisateurs')" class="caisse-tab-btn flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-xs text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white transition-all cursor-pointer">
                <i class="ph-bold ph-users-three text-base"></i>
                <span>Rapport par utilisateur</span>
            </button>
        </div>

        <!-- ================= ONGLET 1 : JOURNAL DES FLUX DE CAISSE RÉELS ================= -->
        <div id="caisse-tab-content-journal" class="caisse-tab-content bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 space-y-4">
            <div class="border-b border-slate-100 dark:border-white/5 pb-3 flex justify-between items-center">
                <div>
                    <h3 class="text-sm font-bold text-slate-800 dark:text-white">Journal des Flux Réels de Caisse</h3>
                    <p class="text-[11px] text-slate-400">Tous les paiements, régularisations et remboursements perçus ou déduits sur la période sélectionnée.</p>
                </div>
            </div>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                    <thead>
                        <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-3">
                            <th class="pb-3 pl-2">Passage</th>
                            <th class="pb-3">Date & Heure Flux</th>
                            <th class="pb-3">Patient</th>
                            <th class="pb-3">Type Mouvement</th>
                            <th class="pb-3">Caissier (@)</th>
                            <th class="pb-3 text-right font-bold">Montant Flux</th>
                            <th class="pb-3 text-center">Mode</th>
                            <th class="pb-3 pr-2 text-right">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="caisseJournalTableBody" class="divide-y divide-slate-100 dark:divide-white/5 font-medium"></tbody>
                </table>
            </div>
        </div>

        <!-- ================= ONGLET 2 : PATIENTS PAS ENCORE ENCAISSÉS ================= -->
        <div id="caisse-tab-content-nonpayes" class="caisse-tab-content hidden bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 space-y-4">
            <div class="border-b border-slate-100 dark:border-white/5 pb-3">
                <h3 class="text-sm font-bold text-slate-800 dark:text-white">Patients En Attente d'Encaissement</h3>
                <p class="text-[11px] text-slate-400">Dossiers avec 0 DA encaissé. Cliquez sur « Encaisser » pour enregistrer le règlement dans la caisse du jour.</p>
            </div>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                    <thead>
                        <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-3">
                            <th class="pb-3 pl-2">Passage</th>
                            <th class="pb-3">Patient</th>
                            <th class="pb-3">Date & Heure</th>
                            <th class="pb-3">Prestations Effectuées</th>
                            <th class="pb-3 font-bold text-rose-500">Net à Encaisser</th>
                            <th class="pb-3">Opérateur</th>
                            <th class="pb-3 pr-2 text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody id="caisseNonPayesTableBody" class="divide-y divide-slate-100 dark:divide-white/5 font-medium"></tbody>
                </table>
            </div>
        </div>

        <!-- ================= ONGLET 3 : SUIVI DES IMPAYÉS ================= -->
        <div id="caisse-tab-content-impayes" class="caisse-tab-content hidden bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 space-y-4">
            <div class="border-b border-slate-100 dark:border-white/5 pb-3">
                <h3 class="text-sm font-bold text-slate-800 dark:text-white">Dossiers avec Restes Dûs (Paiements Partiels)</h3>
                <p class="text-[11px] text-slate-400">Patients ayant versé un acompte avec un solde restant à recouvrer.</p>
            </div>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                    <thead>
                        <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-3">
                            <th class="pb-3 pl-2">Passage</th>
                            <th class="pb-3">Patient & Téléphone</th>
                            <th class="pb-3">Date Admission</th>
                            <th class="pb-3">Net Facturé</th>
                            <th class="pb-3">Déjà Versé</th>
                            <th class="pb-3 font-bold text-amber-500">Reste à Recouvrer</th>
                            <th class="pb-3">Statut</th>
                            <th class="pb-3 pr-2 text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody id="caisseImpayesTableBody" class="divide-y divide-slate-100 dark:divide-white/5 font-medium"></tbody>
                </table>
            </div>
        </div>

        <!-- ================= ONGLET 4 : RÉCAPITULATIF DES REMISES ================= -->
        <div id="caisse-tab-content-remises" class="caisse-tab-content hidden bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 space-y-4">
            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                    <thead>
                        <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-3">
                            <th class="pb-3 pl-2">N° Passage</th>
                            <th class="pb-3">Date / Heure</th>
                            <th class="pb-3">Patient</th>
                            <th class="pb-3">Montant Initial</th>
                            <th class="pb-3">Taux / Type</th>
                            <th class="pb-3 text-rose-500 font-bold">Remise Déduite</th>
                            <th class="pb-3">Accordée Par</th>
                            <th class="pb-3">Caissier (@)</th>
                        </tr>
                    </thead>
                    <tbody id="caisseRemisesTableBody" class="divide-y divide-slate-100 dark:divide-white/5 font-medium"></tbody>
                </table>
            </div>
        </div>

        <!-- ================= ONGLET 5 : PAIEMENTS ANNULÉS ================= -->
        <div id="caisse-tab-content-annules" class="caisse-tab-content hidden bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 space-y-4">
            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                    <thead>
                        <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 pb-3">
                            <th class="pb-3 pl-2">Passage</th>
                            <th class="pb-3">Patient</th>
                            <th class="pb-3">Date Passage</th>
                            <th class="pb-3 text-rose-500 font-bold">Montant Annulé</th>
                            <th class="pb-3">Raison d'Annulation</th>
                            <th class="pb-3">Annulé Par</th>
                            <th class="pb-3">Date Annulation</th>
                        </tr>
                    </thead>
                    <tbody id="caisseAnnulesTableBody" class="divide-y divide-slate-100 dark:divide-white/5 font-medium"></tbody>
                </table>
            </div>
        </div>

        <!-- ================= RAPPORT COMPLÉMENTAIRE PAR UTILISATEUR ================= -->
        <div id="caisse-tab-content-rapport-utilisateurs" class="caisse-tab-content hidden bg-white dark:bg-[#121c15] rounded-3xl p-6 shadow-sm border border-slate-100 dark:border-white/5 space-y-4">
            <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-slate-100 dark:border-white/5 pb-3">
                <div>
                    <h3 class="text-sm font-bold text-slate-800 dark:text-white">Rapport de caisse par utilisateur</h3>
                    <p class="text-[11px] text-slate-400">Synthèse des encaissements et remboursements enregistrés par chaque opérateur sur la période filtrée.</p>
                </div>
                <span id="caisseReportUserPeriod" class="text-[10px] font-bold text-slate-500 dark:text-slate-400"></span>
            </div>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                    <thead>
                        <tr class="text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5">
                            <th class="py-3 pl-2">Utilisateur / Caissier</th>
                            <th class="py-3 text-center">Nombre de mouvements</th>
                            <th class="py-3 text-right">Encaissements</th>
                            <th class="py-3 text-right">Remboursements</th>
                            <th class="py-3 text-right">Net en caisse</th>
                            <th class="py-3 pr-2 text-right">Détail</th>
                        </tr>
                    </thead>
                    <tbody id="caisseReportUserTableBody" class="divide-y divide-slate-100 dark:divide-white/5 font-medium"></tbody>
                    <tfoot id="caisseReportUserTableFoot" class="border-t-2 border-slate-200 dark:border-white/10 font-extrabold"></tfoot>
                </table>
            </div>
        </div>

    </div>

    <!-- ================= MODALE D'ENCAISSEMENT ================= -->
    <div id="caisseEncaissementModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center hidden z-[100] p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-2xl max-h-[96vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">

            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex items-center justify-between bg-slate-50 dark:bg-black/20">
                <div class="flex items-center gap-2">
                    <i class="ph-bold ph-hand-coins text-brand-neon text-xl"></i>
                    <h3 class="text-base font-bold text-slate-800 dark:text-white">Encaissement du Patient</h3>
                </div>
                <button type="button" onclick="closeCaisseEncaissementModal()" class="text-slate-400 hover:text-white p-1 rounded-lg">✕</button>
            </div>

            <form onsubmit="submitCaisseEncaissement(event)" class="p-6 overflow-y-auto space-y-4 text-xs">
                <input type="hidden" id="encPassageId">
                <input type="hidden" id="encMontantBrutCache" value="0">
                <input type="hidden" id="encRemiseMontantDACache" value="0">

                <div class="p-3.5 bg-slate-50 dark:bg-black/30 rounded-2xl border border-slate-200 dark:border-white/10 flex items-center justify-between">
                    <div>
                        <span class="text-slate-400 block text-[10px] uppercase font-bold">Patient :</span>
                        <strong id="encPatientNom" class="text-sm font-extrabold text-slate-900 dark:text-white">-</strong>
                    </div>
                    <div class="text-right">
                        <span class="text-slate-400 block text-[10px] uppercase font-bold">Passage :</span>
                        <span id="encPassageNum" class="font-mono font-bold text-brand-neon text-xs">#PASS-0000</span>
                    </div>
                </div>

                <div class="space-y-1.5">
                    <div class="flex items-center justify-between">
                        <label class="font-bold text-slate-700 dark:text-slate-300 uppercase tracking-wider text-[11px]">
                            Actes & Prestations Effectués :
                        </label>
                        <span id="encActesCountBadge" class="text-[10px] font-bold text-brand-neon bg-brand-neon/10 px-2 py-0.5 rounded-md">0 acte(s)</span>
                    </div>

                    <div class="border border-slate-200 dark:border-white/10 rounded-2xl overflow-hidden max-h-40 overflow-y-auto">
                        <table class="w-full text-left text-xs bg-white dark:bg-[#121c15]">
                            <thead class="bg-slate-50 dark:bg-black/30 text-slate-400 font-semibold border-b border-slate-100 dark:border-white/5 sticky top-0">
                                <tr>
                                    <th class="p-2.5">Prestation</th>
                                    <th class="p-2.5">Période</th>
                                    <th class="p-2.5 text-center">Qté</th>
                                    <th class="p-2.5 text-right font-bold">Sous-total</th>
                                </tr>
                            </thead>
                            <tbody id="encActesTableBody" class="divide-y divide-slate-100 dark:divide-white/5 font-medium"></tbody>
                        </table>
                    </div>
                </div>

                <div id="caisseRemiseFields" class="bg-brand-neon/10 dark:bg-brand-neon/5 p-4 rounded-2xl border border-brand-neon/20 space-y-3">
                    <div class="flex items-center justify-between">
                        <span class="font-bold text-emerald-950 dark:text-brand-neon text-[11px] uppercase tracking-wider">Remise & Négociation</span>
                        <div class="inline-flex rounded-lg border border-brand-neon/30 bg-white dark:bg-black/40 p-0.5">
                            <button type="button" onclick="setEncRemiseType('DA')" id="btnEncRemiseDA" class="px-2.5 py-0.5 rounded-md text-[10px] font-bold bg-brand-neon text-black">DA</button>
                            <button type="button" onclick="setEncRemiseType('%')" id="btnEncRemisePct" class="px-2.5 py-0.5 rounded-md text-[10px] font-bold text-slate-500 hover:text-white">%</button>
                        </div>
                    </div>

                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        <div>
                            <label class="block text-[10px] font-bold text-slate-500 mb-1 uppercase">Valeur de la Remise</label>
                            <div class="relative">
                                <input type="number" step="any" min="0" id="encRemiseValeur" value="0" oninput="calculateEncaissementFinancials()" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/40 font-mono font-bold text-rose-500 outline-none">
                                <span id="encRemiseUnitLabel" class="absolute right-3 top-2 text-xs font-bold text-slate-400">DA</span>
                            </div>
                        </div>

                        <div>
                            <label class="block text-[10px] font-bold text-slate-500 mb-1 uppercase">Remise Accordée Par *</label>
                            <select id="encAccordeurRemise" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/40 font-semibold text-slate-800 dark:text-white outline-none">
                                {accordeurs_options}
                            </select>
                        </div>
                    </div>
                </div>

                <div class="grid grid-cols-2 sm:grid-cols-4 gap-2.5 p-3 rounded-2xl bg-slate-50 dark:bg-black/30 border border-slate-200 dark:border-white/10 font-mono">
                    <div>
                        <span class="text-[10px] text-slate-400 uppercase block font-sans">Montant Brut :</span>
                        <strong id="encBrutDisplay" class="text-xs text-slate-700 dark:text-slate-300">0.00 DA</strong>
                    </div>
                    <div>
                        <span class="text-[10px] text-slate-400 uppercase block font-sans">Déduction Remise :</span>
                        <strong id="encRemiseDisplay" class="text-xs text-rose-500 font-bold">-0.00 DA</strong>
                    </div>
                    <div>
                        <span class="text-[10px] text-slate-400 uppercase block font-sans">Net à Payer :</span>
                        <strong id="encNetDisplay" class="text-sm font-black text-slate-900 dark:text-white">0.00 DA</strong>
                    </div>
                    <div>
                        <span class="text-[10px] text-amber-500 uppercase block font-sans">Reste Dû :</span>
                        <strong id="encResteDisplay" class="text-sm font-black text-amber-500">0.00 DA</strong>
                    </div>
                </div>

                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 items-end">
                    <div>
                        <label class="block text-[11px] font-bold text-slate-700 dark:text-slate-300 mb-1 uppercase">Montant Encaissé Aujourd'hui *</label>
                        <input type="number" step="100" id="encVersement" required oninput="calculateEncaissementFinancials()" class="w-full text-sm p-3 rounded-xl border border-brand-neon/50 bg-brand-neon/10 font-mono font-black text-emerald-800 dark:text-brand-neon outline-none">
                    </div>
                    <div>
                        <label class="block text-[11px] font-bold text-slate-700 dark:text-slate-300 mb-1 uppercase">Mode de Paiement</label>
                        <select id="encModePaiement" class="w-full text-xs p-3 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white outline-none">
                            <option value="Espèces">💵 Espèces</option>
                            <option value="Carte CIB / Edahabia">💳 Carte CIB / Edahabia</option>
                            <option value="Chèque">📝 Chèque</option>
                            <option value="Prise en Charge (PEC)">📑 Prise en Charge (PEC)</option>
                            <option value="Autre">Autre</option>
                        </select>
                    </div>
                </div>

                <div class="border-t border-slate-100 dark:border-white/5 pt-3">
                    <span class="text-[11px] font-bold text-slate-400 block mb-1.5">Signature & Contrôle Caissier :</span>
                    <div class="grid grid-cols-2 gap-3">
                        <input type="text" id="encCaissierLogin" required placeholder="Login caissier" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 outline-none">
                        <input type="password" id="encCaissierPassword" required placeholder="Mot de passe" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 outline-none">
                    </div>
                </div>

                <div class="flex items-center justify-end gap-3 pt-3 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closeCaisseEncaissementModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler</button>
                    <button type="submit" id="btnSubmitEncaissement" class="px-6 py-2.5 rounded-xl bg-brand-neon hover:bg-brand-neonDark text-black text-xs font-bold shadow-md cursor-pointer inline-flex items-center gap-2">
                        <i class="ph-bold ph-receipt text-base"></i>
                        <span>Encaisser & Imprimer Ticket 80mm</span>
                    </button>
                </div>
            </form>
        </div>
    </div>

    <!-- ================= MODALE D'ANNULATION DE PAIEMENT ================= -->
    <div id="annulerPaiementModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center hidden z-[105] p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-md shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-rose-50/50 dark:bg-rose-950/20">
                <div class="flex items-center space-x-2">
                    <span class="w-2.5 h-2.5 rounded-full bg-rose-500"></span>
                    <h3 class="text-sm font-bold text-slate-800 dark:text-white">Annuler un Paiement (Remboursement)</h3>
                </div>
                <button type="button" onclick="closeAnnulerPaiementModal()" class="text-slate-400 hover:text-white p-1">✕</button>
            </div>
            <form onsubmit="submitAnnulerPaiement(event)" class="p-6 space-y-4 text-xs">
                <input type="hidden" id="annulerPassageId">
                <div class="p-4 bg-rose-500/10 rounded-2xl border border-rose-500/20 text-xs space-y-1">
                    <div class="flex justify-between"><span class="text-slate-400">Patient :</span><strong id="annulerPatientNom" class="text-slate-800 dark:text-white uppercase">-</strong></div>
                    <div class="flex justify-between"><span class="text-slate-400">Passage :</span><strong id="annulerPassageNum" class="font-mono text-brand-neon">-</strong></div>
                    <div class="flex justify-between"><span class="text-slate-400">Montant à déduire de la caisse :</span><strong id="annulerMontant" class="font-mono text-rose-500">-</strong></div>
                </div>
                <p class="text-[11px] text-slate-400">Ce montant sortira immédiatement de la caisse d'aujourd'hui sous forme de remboursement.</p>
                <div>
                    <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Raison du remboursement *</label>
                    <select id="annulerRaison" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-semibold text-slate-800 dark:text-white outline-none">
                        <option value="" disabled selected>-- Sélectionnez un motif --</option>
                        {raisons_options}
                    </select>
                </div>
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Login Caissier *</label>
                        <input type="text" id="annulerLogin" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 outline-none">
                    </div>
                    <div>
                        <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Mot de passe *</label>
                        <input type="password" id="annulerPassword" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 outline-none">
                    </div>
                </div>
                <div class="flex items-center justify-end space-x-2 pt-3 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closeAnnulerPaiementModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white font-semibold">Annuler</button>
                    <button type="submit" class="px-5 py-2.5 rounded-xl bg-rose-500 hover:bg-rose-600 text-white font-bold shadow-md cursor-pointer">Confirmer le Remboursement</button>
                </div>
            </form>
        </div>
    </div>

    <!-- ================= MODALE VISIONNEUSE PV DE CLÔTURE A4 ================= -->
    <div id="cloturePvModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center hidden z-[110] p-2 sm:p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-4xl h-[95vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-5 py-3 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50 dark:bg-black/20 flex-shrink-0">
                <h3 class="text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5">
                    <i class="ph-bold ph-printer text-brand-neon"></i>
                    <span>Procès-Verbal Officiel de Clôture de Caisse (Format A4)</span>
                </h3>
                <div class="flex items-center space-x-2">
                    <button type="button" onclick="printCloturePvIframe()" class="px-3.5 py-1.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-sm inline-flex items-center gap-1 cursor-pointer">
                        <i class="ph-bold ph-printer"></i>
                        <span>Imprimer</span>
                    </button>
                    <button type="button" onclick="closeCloturePvModal()" class="px-3.5 py-1.5 rounded-xl bg-rose-500 hover:bg-rose-600 text-white text-xs font-bold shadow-sm inline-flex items-center gap-1 cursor-pointer">
                        <i class="ph-bold ph-x-circle"></i>
                        <span>Quitter</span>
                    </button>
                </div>
            </div>
            <div class="p-2 overflow-y-auto bg-slate-200 dark:bg-black/60 flex justify-center flex-1">
                <iframe id="cloturePvIframe" src="about:blank" class="w-full h-full rounded-2xl border-none shadow-md bg-white"></iframe>
            </div>
        </div>
    </div>

    <!-- ================= MODALE RAPPORT UTILISATEUR PDF INTÉGRÉE ================= -->
    <div id="caisseUserReportPdfModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center hidden z-[115] p-2 sm:p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-5xl h-[95vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-5 py-3 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50 dark:bg-black/20 flex-shrink-0">
                <h3 class="text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5">
                    <i class="ph-bold ph-file-pdf text-brand-neon"></i>
                    <span id="caisseUserReportPdfTitle">Rapport détaillé de caisse</span>
                </h3>
                <div class="flex items-center space-x-2">
                    <button type="button" onclick="printCaisseUserReportPdfIframe()" class="px-3.5 py-1.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-sm inline-flex items-center gap-1 cursor-pointer"><i class="ph-bold ph-printer"></i><span>Imprimer / Enregistrer en PDF</span></button>
                    <button type="button" onclick="closeCaisseUserReportPdf()" class="px-3.5 py-1.5 rounded-xl bg-rose-500 hover:bg-rose-600 text-white text-xs font-bold shadow-sm inline-flex items-center gap-1 cursor-pointer"><i class="ph-bold ph-x-circle"></i><span>Fermer</span></button>
                </div>
            </div>
            <div class="p-2 overflow-y-auto bg-slate-200 dark:bg-black/60 flex justify-center flex-1">
                <iframe id="caisseUserReportPdfIframe" src="about:blank" title="Rapport PDF par utilisateur" class="w-full h-full rounded-2xl border-none shadow-md bg-white"></iframe>
            </div>
        </div>
    </div>

    <!-- ================= MODALE FACTURE PATIENT ================= -->
    <div id="caisseInvoicePdfModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center hidden z-[9999] p-2 sm:p-4" style="z-index:99999">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-5xl h-[95vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-5 py-3 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50 dark:bg-black/20 flex-shrink-0">
                <h3 class="text-xs font-bold text-slate-800 dark:text-white flex items-center gap-2">
                    <i class="ph-bold ph-file-text text-emerald-600"></i>
                    <span id="caisseInvoicePdfTitle">Facture patient</span>
                </h3>
                <div class="flex items-center gap-2">
                    <button type="button" id="caisseInvoiceValidateButton" onclick="validateCaisseInvoice()" class="px-3.5 py-1.5 rounded-xl text-white text-xs font-bold shadow-sm inline-flex items-center gap-1.5 cursor-pointer hover:brightness-110" style="background-color:#15803d;color:#ffffff;border:1px solid #166534"><i class="ph-bold ph-check-circle text-base" aria-hidden="true"></i><span>Valider la facture</span></button>
                    <button type="button" id="caisseInvoiceDownloadButton" onclick="downloadCaisseInvoicePdf()" class="hidden px-3.5 py-1.5 rounded-xl bg-sky-700 hover:bg-sky-800 text-white text-xs font-bold shadow-sm inline-flex items-center gap-1.5 cursor-pointer"><i class="ph-bold ph-download-simple"></i><span>Télécharger</span></button>
                    <button type="button" id="caisseInvoicePrintButton" onclick="printCaisseInvoicePdfIframe()" class="hidden px-3.5 py-1.5 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-bold shadow-sm inline-flex items-center gap-1.5 cursor-pointer"><i class="ph-bold ph-printer"></i><span>Imprimer</span></button>
                    <button type="button" onclick="closeCaisseInvoicePdf()" class="px-3.5 py-1.5 rounded-xl bg-rose-500 hover:bg-rose-600 text-white text-xs font-bold shadow-sm inline-flex items-center gap-1.5 cursor-pointer"><i class="ph-bold ph-x-circle"></i><span>Fermer</span></button>
                </div>
            </div>
            <div class="p-2 overflow-y-auto bg-slate-200 dark:bg-black/60 flex justify-center flex-1">
                <iframe id="caisseInvoicePdfIframe" src="about:blank" title="Facture patient PDF" class="w-full h-full rounded-2xl border-none shadow-md bg-white"></iframe>
            </div>
        </div>
    </div>

    <!-- ================= REGISTRE DES FACTURES VALIDÉES ================= -->
    <div id="caisseInvoiceRegisterModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center hidden z-[117] p-2 sm:p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-6xl max-h-[95vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-5 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50 dark:bg-black/20 flex-shrink-0">
                <h3 class="text-sm font-bold text-slate-800 dark:text-white flex items-center gap-2"><i class="ph-bold ph-files text-emerald-600"></i>Registre des factures</h3>
                <button type="button" onclick="closeCaisseInvoiceRegister()" class="px-3 py-1.5 rounded-xl bg-rose-500 hover:bg-rose-600 text-white text-xs font-bold inline-flex items-center gap-1"><i class="ph-bold ph-x"></i>Fermer</button>
            </div>
            <div class="p-4 sm:p-5 space-y-4 overflow-y-auto">
                <form id="caisseInvoiceRegisterFilters" onsubmit="event.preventDefault(); loadCaisseInvoiceRegister();" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3 p-4 rounded-2xl bg-slate-50 dark:bg-black/20 border border-slate-200 dark:border-white/10">
                    <label class="text-[11px] font-bold text-slate-600 dark:text-slate-300">Du<input id="caisseInvoiceDateStart" type="date" class="mt-1 w-full p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/30 text-xs"></label>
                    <label class="text-[11px] font-bold text-slate-600 dark:text-slate-300">Au<input id="caisseInvoiceDateEnd" type="date" class="mt-1 w-full p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/30 text-xs"></label>
                    <label class="text-[11px] font-bold text-slate-600 dark:text-slate-300">Patient ou facture<input id="caisseInvoiceSearch" type="search" placeholder="Nom, passage, n° facture" class="mt-1 w-full p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/30 text-xs"></label>
                    <label class="text-[11px] font-bold text-slate-600 dark:text-slate-300">Utilisateur<input id="caisseInvoiceUserSearch" type="search" placeholder="Tous les utilisateurs" class="mt-1 w-full p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/30 text-xs"></label>
                    <button type="submit" class="self-end w-full h-[38px] px-3 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-bold inline-flex items-center justify-center gap-1.5 leading-none"><i class="ph-bold ph-funnel"></i>Recherche de facture</button>
                </form>
                <div id="caisseInvoiceRegisterSummary" class="text-xs font-semibold text-slate-500 dark:text-slate-400"></div>
                <div class="overflow-x-auto rounded-2xl border border-slate-200 dark:border-white/10">
                    <table class="w-full min-w-[760px] text-left text-xs">
                        <thead class="bg-slate-100 dark:bg-black/30 text-slate-600 dark:text-slate-300 uppercase tracking-wide"><tr><th class="p-3">Date / heure</th><th class="p-3">N° facture</th><th class="p-3">Statut</th><th class="p-3">Utilisateur</th><th class="p-3">Patient</th><th class="p-3 text-right">Total TTC</th><th class="p-3 text-center">Actions</th></tr></thead>
                        <tbody id="caisseInvoiceRegisterRows" class="divide-y divide-slate-100 dark:divide-white/5"><tr><td colspan="7" class="p-6 text-center text-slate-400">Choisissez une période puis filtrez.</td></tr></tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <div id="caisseInvoiceCancelModal" class="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center hidden z-[10000] p-4" style="z-index:100000">
        <div class="w-full max-w-lg rounded-3xl bg-white dark:bg-[#121c15] shadow-2xl border border-rose-200 dark:border-rose-900/40 overflow-hidden">
            <div class="px-5 py-4 flex items-center justify-between bg-rose-50 dark:bg-rose-950/30 border-b border-rose-100 dark:border-rose-900/40">
                <h3 class="font-bold text-slate-800 dark:text-white flex items-center gap-2"><i class="ph-bold ph-prohibit text-rose-600 text-lg"></i>Annuler la facture</h3>
                <button type="button" onclick="closeCaisseInvoiceCancelModal()" class="text-slate-400 hover:text-rose-600"><i class="ph-bold ph-x text-lg"></i></button>
            </div>
            <form onsubmit="submitCaisseInvoiceCancellation(event)" class="p-5 space-y-4">
                <input type="hidden" id="caisseInvoiceCancelId">
                <div class="rounded-2xl bg-slate-50 dark:bg-black/20 border border-slate-200 dark:border-white/10 p-3 text-xs space-y-1">
                    <div><span class="text-slate-500">Facture :</span> <strong id="caisseInvoiceCancelNumber"></strong></div>
                    <div><span class="text-slate-500">Patient :</span> <strong id="caisseInvoiceCancelPatient"></strong></div>
                </div>
                <p class="text-xs text-rose-700 dark:text-rose-300">La facture ne sera pas supprimée. Elle sera marquée comme annulée et un avoir sera conservé dans le registre.</p>
                <label class="block text-xs font-bold text-slate-700 dark:text-slate-200">Motif obligatoire<textarea id="caisseInvoiceCancelReason" required minlength="4" maxlength="500" rows="3" class="mt-1 w-full p-3 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/30 text-sm outline-none focus:ring-2 focus:ring-rose-400" placeholder="Expliquez la raison de l’annulation"></textarea></label>
                <div class="flex justify-end gap-2 pt-2">
                    <button type="button" onclick="closeCaisseInvoiceCancelModal()" class="px-4 py-2 rounded-xl text-slate-500 bg-slate-100 dark:bg-white/5 text-xs font-bold">Retour</button>
                    <button id="caisseInvoiceCancelSubmit" type="submit" class="px-4 py-2 rounded-xl bg-rose-600 hover:bg-rose-700 text-white text-xs font-bold inline-flex items-center gap-2"><i class="ph-bold ph-prohibit"></i>Confirmer l’annulation</button>
                </div>
            </form>
        </div>
    </div>

    <!-- MODALE TICKET 80MM -->
    <div id="caisseReceiptModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center hidden z-[110] p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-md max-h-[96vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-5 py-3.5 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50 dark:bg-black/20">
                <h3 class="text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5">
                    <i class="ph-bold ph-receipt text-brand-neon"></i>
                    <span>Ticket de Caisse Thermique (80mm)</span>
                </h3>
                <div class="flex items-center space-x-2">
                    <button type="button" onclick="printCaisseReceiptIframe()" class="px-3.5 py-1.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-sm inline-flex items-center gap-1 cursor-pointer">
                        <i class="ph-bold ph-printer"></i>
                        <span>Imprimer</span>
                    </button>
                    <button type="button" onclick="closeCaisseReceiptModal()" class="text-slate-400 hover:text-white p-1">✕</button>
                </div>
            </div>
            <div class="p-4 overflow-y-auto bg-slate-100 dark:bg-black/40 flex justify-center flex-1">
                <iframe id="caisseReceiptIframe" src="about:blank" class="rounded-2xl border border-slate-200 shadow-md" style="width: 82mm; min-height: 480px; background: white;"></iframe>
            </div>
        </div>
    </div>

    <!-- SCRIPT DU MODULE CAISSE -->
    <script>
        (function() {{
            const caisseAccess = {caisse_access_json};
            let caisseGlobalData = {{ kpis: {{}}, journal: [], remises: [], impayes: [], non_payes: [], annules: [] }};
            let currentEncRemiseType = 'DA';

            function applyCaissePermissions() {{
                const setVisible = (id, visible) => document.getElementById(id)?.classList.toggle('hidden', !visible);
                setVisible('caissePvButton', caisseAccess.cash_reports);
                setVisible('caisseDailyJournalButton', caisseAccess.cash_reports);
                setVisible('caisseGuardJournalButton', caisseAccess.cash_reports);
                setVisible('caisseInvoiceRegisterButton', caisseAccess.cash_reports || caisseAccess.cash_collect || caisseAccess.cash_collect_rest || caisseAccess.cash_refund);
                setVisible('caisseFiltersPanel', caisseAccess.cash_reports);
                setVisible('caisseKpisPanel', caisseAccess.cash_reports);
                setVisible('caisse-tab-btn-journal', caisseAccess.cash_reports);
                setVisible('caisse-tab-content-journal', caisseAccess.cash_reports);
                setVisible('caisse-tab-btn-nonpayes', caisseAccess.cash_collect);
                setVisible('caisse-tab-content-nonpayes', caisseAccess.cash_collect);
                setVisible('caisse-tab-btn-impayes', caisseAccess.cash_collect_rest);
                setVisible('caisse-tab-content-impayes', caisseAccess.cash_collect_rest);
                setVisible('caisse-tab-btn-remises', caisseAccess.cash_reports || caisseAccess.cash_remise);
                setVisible('caisse-tab-content-remises', caisseAccess.cash_reports || caisseAccess.cash_remise);
                setVisible('caisse-tab-btn-annules', caisseAccess.cash_reports || caisseAccess.cash_refund);
                setVisible('caisse-tab-content-annules', caisseAccess.cash_reports || caisseAccess.cash_refund);
                setVisible('caisse-tab-btn-rapport-utilisateurs', caisseAccess.cash_reports);
                setVisible('caisse-tab-content-rapport-utilisateurs', caisseAccess.cash_reports);
                setVisible('caisseRemiseFields', caisseAccess.cash_remise);
                if (caisseAccess.cash_reports) {{
                    switchCaisseTab('journal');
                }} else {{
                    const firstTab = caisseAccess.cash_collect ? 'nonpayes' : (caisseAccess.cash_collect_rest ? 'impayes' : (caisseAccess.cash_remise ? 'remises' : (caisseAccess.cash_refund ? 'annules' : null)));
                    if (firstTab) switchCaisseTab(firstTab);
                }}
            }}

            window.loadCaisseModule = async function() {{
                applyCaissePermissions();
                setCaisseDateQuick('today', false);
                await loadCaisseData();
            }};

            window.loadCaisseData = async function() {{
                const dDeb = document.getElementById('caisseDateDebut')?.value || '';
                const dFin = document.getElementById('caisseDateFin')?.value || '';
                const op = document.getElementById('caisseFilterOperateur')?.value || 'TOUS';
                const mode = document.getElementById('caisseFilterMode')?.value || 'TOUS';
                const statut = document.getElementById('caisseFilterStatut')?.value || 'TOUS';

                const q = new URLSearchParams({{
                    date_debut: dDeb,
                    date_fin: dFin,
                    operateur: op,
                    mode_paiement: mode,
                    statut_paiement: statut
                }}).toString();

                try {{
                    const res = await fetch('/api/finance/stats?' + q);
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || 'Accès refusé.');
                    caisseGlobalData = data;

                    renderCaisseKpis(data.kpis);
                    renderCaisseOperateurs(data.operateurs, data.filtres_actifs?.operateur);

                    renderJournalTable(data.journal || []);
                    renderNonPayesTable(data.non_payes || []);
                    renderImpayesTable(data.impayes || []);
                    renderRemisesTable(data.remises || []);
                    renderAnnulesTable(data.annules || []);
                    renderCaisseUserReport(data.journal || []);

                    const badgeNP = document.getElementById('badgeNonPayesCount');
                    if (badgeNP) badgeNP.innerText = (data.non_payes || []).length;
                }} catch (e) {{
                    console.error("Erreur de chargement caisse:", e);
                }}
            }};

            window.switchCaisseTab = function(tabName) {{
                const targetBtn = document.getElementById('caisse-tab-btn-' + tabName);
                if (!targetBtn || targetBtn.classList.contains('hidden')) return;
                document.querySelectorAll('.caisse-tab-content').forEach(el => el.classList.add('hidden'));
                document.querySelectorAll('.caisse-tab-btn').forEach(btn => {{
                    btn.className = "caisse-tab-btn flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-xs text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white transition-all cursor-pointer";
                }});

                const targetContent = document.getElementById('caisse-tab-content-' + tabName);
                if (targetContent) targetContent.classList.remove('hidden');

                if (targetBtn) {{
                    targetBtn.className = "caisse-tab-btn flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-xs bg-brand-neon text-black shadow-md shadow-brand-neon/20 transition-all cursor-pointer";
                }}
            }};

            function renderCaisseKpis(kpis) {{
                if (!kpis) return;
                const f = (n) => Number(n || 0).toLocaleString('fr-FR', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }}) + ' DA';

                document.getElementById('kpiEncaisseTotal').innerText = f(kpis.encaisse_total);
                document.getElementById('kpiCaBrut').innerText = f(kpis.ca_brut);
                document.getElementById('kpiRemisesTotal').innerText = '-' + f(kpis.remises_total);
                document.getElementById('kpiRemboursementsTotal').innerText = f(kpis.remboursements_total);
                document.getElementById('kpiProduitsContraste').innerText = f(kpis.produits_contraste_total);
                document.getElementById('kpiSedations').innerText = f(kpis.sedations_total);
                document.getElementById('kpiNetDu').innerText = f(kpis.net_du);
                document.getElementById('kpiRestesTotal').innerText = f(kpis.restes_total);

                document.getElementById('kpiPassagesCount').innerText = kpis.count_passages || 0;
                document.getElementById('kpiRemisesCount').innerText = kpis.count_remises || 0;
                document.getElementById('kpiImpayesCount').innerText = kpis.count_impayes || 0;
            }}

            function renderCaisseOperateurs(operateurs, selected) {{
                const sel = document.getElementById('caisseFilterOperateur');
                if (!sel) return;
                sel.innerHTML = '<option value="TOUS">-- Tous les Caissiers --</option>' +
                    (operateurs || []).map(o => `<option value="${{escapeCaisseHtml(o)}}" ${{o === selected ? 'selected' : ''}}>👤 @${{escapeCaisseHtml(o)}}</option>`).join('');
            }}

            function escapeCaisseHtml(value) {{
                return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
            }}

            // 1. JOURNAL DES FLUX RÉELS DU JOUR
            function renderJournalTable(items) {{
                const tbody = document.getElementById('caisseJournalTableBody');
                if (!tbody) return;
                tbody.innerHTML = '';

                if (items.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="8" class="py-8 text-center text-slate-400">Aucun flux de caisse sur cette période.</td></tr>';
                    return;
                }}

                items.forEach(j => {{
                    const mVal = parseFloat(j.montant_mouvement || 0);
                    const isEntree = mVal >= 0;
                    const colorClass = isEntree ? "text-emerald-600 dark:text-brand-neon font-black" : "text-rose-500 font-black";
                    const movementLabels = {{
                        ENCAISSEMENT_TOTAL: 'Encaissement total',
                        ENCAISSEMENT_PARTIEL: 'Encaissement partiel',
                        ENCAISSEMENT_RESTE: 'Encaissement du reste',
                        REGULARISATION: 'Encaissement du reste',
                        ENCAISSEMENT: 'Encaissement total',
                        ANNULATION: 'Remboursement / annulation'
                    }};
                    const movementLabel = movementLabels[j.type_mouvement] || 'Mouvement de caisse';
                    const movementBadgeClass = j.type_mouvement === 'ANNULATION'
                        ? 'bg-rose-500/20 text-rose-500'
                        : (j.type_mouvement === 'ENCAISSEMENT_RESTE' || j.type_mouvement === 'REGULARISATION'
                            ? 'bg-sky-500/20 text-sky-500 dark:text-sky-400'
                            : (j.type_mouvement === 'ENCAISSEMENT_PARTIEL'
                                ? 'bg-amber-500/20 text-amber-700 dark:text-amber-300'
                                : 'bg-brand-neon/20 text-emerald-800 dark:text-brand-neon'));
                    const prestationLabels = (j.actes || []).map(a => escapeCaisseHtml(a.nom)).filter(Boolean);
                    const typeDisplay = `<div><span class="px-2 py-0.5 rounded-lg text-[10px] font-bold ${{movementBadgeClass}}">${{movementLabel}}</span></div><div class="mt-1 text-[10px] text-slate-500">${{prestationLabels.join(', ') || 'Prestation non renseignée'}}</div>`;

                    const tr = document.createElement('tr');
                    tr.className = "hover:bg-slate-50/80 dark:hover:bg-white/[0.02] transition";
                    tr.innerHTML = `
                        <td class="py-3 pl-2 font-mono text-slate-400">#PASS-${{String(j.id_passage).padStart(4, '0')}}</td>
                        <td class="py-3 text-slate-500">${{escapeCaisseHtml(j.date_mouvement)}} (${{escapeCaisseHtml(j.heure_mouvement || '-')}})</td>
                        <td class="py-3 font-bold text-slate-900 dark:text-white">${{escapeCaisseHtml(j.nom)}} ${{escapeCaisseHtml(j.prenom)}}</td>
                        <td class="py-3">${{typeDisplay}}</td>
                        <td class="py-3 font-mono text-slate-500">@${{escapeCaisseHtml(j.operateur_mouvement || j.utilisateur)}}</td>
                        <td class="py-3 font-mono text-right ${{colorClass}}">${{isEntree && mVal > 0 ? '+' : ''}}${{Number(mVal).toLocaleString('fr-FR', {{minimumFractionDigits: 2}})}} DA</td>
                        <td class="py-3 text-center">${{escapeCaisseHtml(j.mode_mouvement || 'Espèces')}}</td>
                        <td class="py-3 pr-2 text-right">
                            <div class="inline-flex items-center gap-1.5">
                                ${{(caisseAccess.cash_collect || caisseAccess.cash_collect_rest) ? `<button type="button" onclick="openCaisseMovementReceiptModal(${{j.id_mouvement}})" title="Ticket de ce mouvement" class="p-1.5 rounded-lg bg-slate-100 dark:bg-black/30 text-slate-700 dark:text-slate-300 hover:text-brand-neon"><i class="ph-bold ph-receipt text-sm"></i></button>` : ''}}
                                ${{(caisseAccess.cash_reports || caisseAccess.cash_collect || caisseAccess.cash_collect_rest) && isEntree && mVal > 0 ? `<button type="button" onclick="openCaisseInvoicePdf(Number(this.dataset.passage), this.dataset.patient)" data-passage="${{Number(j.id_passage)}}" data-patient="${{escapeCaisseHtml(`${{j.nom || ''}} ${{j.prenom || ''}}`.trim())}}" title="Générer la facture du passage" class="p-1.5 rounded-lg bg-emerald-500/10 text-emerald-700 hover:bg-emerald-600 hover:text-white transition"><i class="ph-bold ph-file-text text-sm"></i></button>` : ''}}
                                ${{caisseAccess.cash_refund && isEntree && mVal > 0 ? `
                                <button type="button" onclick="openAnnulerPaiementModal(Number(this.dataset.id), this.dataset.patient, Number(this.dataset.amount))" data-id="${{Number(j.id_passage)}}" data-patient="${{escapeCaisseHtml(`${{j.nom || ''}} ${{j.prenom || ''}}`.trim())}}" data-amount="${{mVal}}" title="Annuler le paiement / Rembourser" class="p-1.5 rounded-lg bg-rose-500/10 hover:bg-rose-500 text-rose-500 hover:text-white transition">
                                    <i class="ph-bold ph-arrow-u-up-left text-sm"></i>
                                </button>
                                ` : ''}}
                            </div>
                        </td>
                    `;
                    tbody.appendChild(tr);
                }});
            }}

            function renderCaisseUserReport(items) {{
                const tbody = document.getElementById('caisseReportUserTableBody');
                const tfoot = document.getElementById('caisseReportUserTableFoot');
                const period = document.getElementById('caisseReportUserPeriod');
                if (!tbody || !tfoot) return;
                const startDate = document.getElementById('caisseDateDebut')?.value || '';
                const endDate = document.getElementById('caisseDateFin')?.value || '';
                const selectedOperator = document.getElementById('caisseFilterOperateur')?.value || 'TOUS';
                if (period) period.textContent = `${{startDate}} → ${{endDate}}${{selectedOperator !== 'TOUS' ? ` · @${{selectedOperator}}` : ''}}`;

                const operators = new Map();
                (items || []).forEach(movement => {{
                    const operator = String(movement.operateur_mouvement || movement.utilisateur || '').trim() || 'Utilisateur non renseigné';
                    const amount = Number(movement.montant_mouvement || 0);
                    const entry = operators.get(operator) || {{ count: 0, incoming: 0, refunds: 0, net: 0, movements: [] }};
                    entry.count += 1;
                    if (amount > 0) entry.incoming += amount;
                    if (amount < 0) entry.refunds += Math.abs(amount);
                    entry.net += amount;
                    entry.movements.push(movement);
                    operators.set(operator, entry);
                }});

                const rows = [...operators.entries()].sort((a, b) => a[0].localeCompare(b[0], 'fr'));
                if (!rows.length) {{
                    tbody.innerHTML = '<tr><td colspan="6" class="py-8 text-center text-slate-400">Aucun mouvement de caisse pour cette période et ces filtres.</td></tr>';
                    tfoot.innerHTML = '';
                    return;
                }}
                const money = value => `${{Number(value || 0).toLocaleString('fr-FR', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }})}} DA`;
                const totals = rows.reduce((sum, [, entry]) => ({{
                    count: sum.count + entry.count,
                    incoming: sum.incoming + entry.incoming,
                    refunds: sum.refunds + entry.refunds,
                    net: sum.net + entry.net
                }}), {{ count: 0, incoming: 0, refunds: 0, net: 0 }});

                tbody.innerHTML = rows.map(([operator, entry]) => `
                    <tr class="hover:bg-slate-50/80 dark:hover:bg-white/[0.02]">
                        <td class="py-3 pl-2 font-mono font-bold text-slate-800 dark:text-slate-100">@${{escapeCaisseHtml(operator)}}</td>
                        <td class="py-3 text-center">${{entry.count}}</td>
                        <td class="py-3 text-right font-mono font-bold text-emerald-700 dark:text-brand-neon">${{money(entry.incoming)}}</td>
                        <td class="py-3 text-right font-mono font-bold text-rose-500">-${{money(entry.refunds)}}</td>
                        <td class="py-3 text-right font-mono font-black ${{entry.net >= 0 ? 'text-emerald-700 dark:text-brand-neon' : 'text-rose-500'}}">${{money(entry.net)}}</td>
                        <td class="py-3 pr-2 text-right"><button type="button" data-caisse-user="${{escapeCaisseHtml(operator)}}" data-report-type="complet" class="caisse-user-report-open inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-900 dark:bg-brand-neon text-white dark:text-black text-[10px] font-bold hover:opacity-90"><i class="ph-bold ph-file-pdf"></i>Rapport PDF</button></td>
                    </tr>
                `).join('');
                tfoot.innerHTML = `<tr class="bg-slate-50 dark:bg-black/20"><td class="py-3 pl-2">Total période</td><td class="py-3 text-center">${{totals.count}}</td><td class="py-3 text-right font-mono">${{money(totals.incoming)}}</td><td class="py-3 text-right font-mono text-rose-500">-${{money(totals.refunds)}}</td><td class="py-3 text-right font-mono">${{money(totals.net)}}</td><td></td></tr>`;
                tbody.querySelectorAll('.caisse-user-report-open').forEach(button => {{
                    button.addEventListener('click', () => window.openCaisseUserReportPdf(button.dataset.caisseUser || '', button.dataset.reportType || 'jour'));
                }});
            }}

            // 2. PATIENTS NON ENCAISSÉS
            function renderNonPayesTable(items) {{
                const tbody = document.getElementById('caisseNonPayesTableBody');
                if (!tbody) return;
                tbody.innerHTML = '';

                if (items.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="7" class="py-8 text-center text-emerald-600 font-bold">Tous les patients sont encaissés ! Aucun dossier en attente.</td></tr>';
                    return;
                }}

                items.forEach(p => {{
                    const actesBadges = (p.actes || []).map(a => `<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-slate-100 dark:bg-black/30 border border-slate-200 dark:border-white/10 text-slate-700 dark:text-slate-300">${{escapeCaisseHtml(a.nom)}}</span>`).join(' ');

                    const tr = document.createElement('tr');
                    tr.className = "hover:bg-slate-50/80 dark:hover:bg-white/[0.02] transition";
                    tr.innerHTML = `
                        <td class="py-3 pl-2 font-mono text-slate-400">#PASS-${{String(p.id_passage).padStart(4, '0')}}</td>
                        <td class="py-3 font-bold text-slate-900 dark:text-white">${{escapeCaisseHtml(p.patient_nom)}}</td>
                        <td class="py-3 text-slate-500">${{escapeCaisseHtml(p.date_passage)}} (${{escapeCaisseHtml(p.heure_passage)}})</td>
                        <td class="py-3 max-w-xs flex flex-wrap gap-1">${{actesBadges || '-'}}</td>
                        <td class="py-3 font-black text-rose-500 font-mono">${{Number(p.montant_net).toLocaleString()}} DA</td>
                        <td class="py-3 text-slate-500 font-mono">@${{escapeCaisseHtml(p.utilisateur)}}</td>
                        <td class="py-3 pr-2 text-right">
                            <button type="button" onclick="openCaisseEncaissementModal(${{p.id_passage}})" class="px-3.5 py-1.5 bg-brand-neon hover:bg-brand-neonDark text-black font-extrabold text-[11px] rounded-xl shadow-md shadow-brand-neon/20 inline-flex items-center gap-1.5 transition cursor-pointer">
                                <i class="ph-bold ph-hand-coins"></i>
                                <span>Encaisser</span>
                            </button>
                        </td>
                    `;
                    tbody.appendChild(tr);
                }});
            }}

            // 3. IMPAYÉS ET RESTES
            function renderImpayesTable(items) {{
                const tbody = document.getElementById('caisseImpayesTableBody');
                if (!tbody) return;
                tbody.innerHTML = '';

                if (items.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="8" class="py-8 text-center text-emerald-600 font-bold">Aucun impayé trouvé.</td></tr>';
                    return;
                }}

                items.forEach(i => {{
                    const tr = document.createElement('tr');
                    tr.className = "hover:bg-slate-50/80 dark:hover:bg-white/[0.02] transition";
                    tr.innerHTML = `
                        <td class="py-3 pl-2 font-mono text-slate-400">#PASS-${{String(i.id_passage).padStart(4, '0')}}</td>
                        <td class="py-3 font-bold text-slate-900 dark:text-white">${{escapeCaisseHtml(i.patient_nom)}} <span class="text-[10px] text-slate-400 font-normal">(${{escapeCaisseHtml(i.telephone_1)}})</span></td>
                        <td class="py-3 text-slate-500">${{escapeCaisseHtml(i.date_passage)}}</td>
                        <td class="py-3 font-mono text-slate-700 dark:text-slate-300">${{Number(i.montant_net).toLocaleString()}} DA</td>
                        <td class="py-3 font-mono text-emerald-600 dark:text-brand-neon font-bold">${{Number(i.versement).toLocaleString()}} DA</td>
                        <td class="py-3 font-mono font-black text-amber-500">${{Number(i.reste_du).toLocaleString()}} DA</td>
                        <td class="py-3"><span class="px-2 py-0.5 rounded-lg text-[10px] font-bold bg-amber-50 text-amber-700 border border-amber-200">${{escapeCaisseHtml(i.statut_paiement)}}</span></td>
                        <td class="py-3 pr-2 text-right">
                            <button type="button" onclick="openCaisseEncaissementModal(${{i.id_passage}})" class="px-3 py-1 bg-brand-neon text-black font-extrabold text-[11px] rounded-xl shadow-sm inline-flex items-center gap-1 cursor-pointer">
                                <span>Régulariser</span>
                            </button>
                        </td>
                    `;
                    tbody.appendChild(tr);
                }});
            }}

            // 4. REMISES
            function renderRemisesTable(items) {{
                const tbody = document.getElementById('caisseRemisesTableBody');
                if (!tbody) return;
                tbody.innerHTML = items.map(r => `
                    <tr class="hover:bg-slate-50/80 dark:hover:bg-white/[0.02]">
                        <td class="py-3 pl-2 font-mono text-slate-400">#PASS-${{String(r.id_passage).padStart(4, '0')}}</td>
                        <td class="py-3 text-slate-500">${{escapeCaisseHtml(r.date_passage)}} (${{escapeCaisseHtml(r.heure_passage)}})</td>
                        <td class="py-3 font-bold">${{escapeCaisseHtml(r.patient_nom)}}</td>
                        <td class="py-3 font-mono">${{Number(r.montant_brut).toLocaleString()}} DA</td>
                        <td class="py-3"><span class="px-2 py-0.5 rounded bg-amber-50 text-amber-800 font-bold">${{escapeCaisseHtml(r.remise_valeur)}} ${{escapeCaisseHtml(r.remise_type)}}</span></td>
                        <td class="py-3 font-mono font-bold text-rose-500">-${{Number(r.remise_montant).toLocaleString()}} DA</td>
                        <td class="py-3 font-bold text-emerald-700 dark:text-brand-neon">${{escapeCaisseHtml(r.accordeur)}}</td>
                        <td class="py-3 font-mono text-slate-500">@${{escapeCaisseHtml(r.utilisateur)}}</td>
                    </tr>
                `).join('');
            }}

            // 5. ANNULATIONS
            function renderAnnulesTable(items) {{
                const tbody = document.getElementById('caisseAnnulesTableBody');
                if (!tbody) return;
                tbody.innerHTML = items.map(a => `
                    <tr class="hover:bg-slate-50/80 dark:hover:bg-white/[0.02]">
                        <td class="py-3 pl-2 font-mono text-slate-400">#PASS-${{String(a.id_passage).padStart(4, '0')}}</td>
                        <td class="py-3 font-bold">${{escapeCaisseHtml(a.nom)}} ${{escapeCaisseHtml(a.prenom)}}</td>
                        <td class="py-3 text-slate-500">${{escapeCaisseHtml(a.date_passage)}}</td>
                        <td class="py-3 font-mono font-bold text-rose-500">${{Number(a.montant_annule || 0).toLocaleString()}} DA</td>
                        <td class="py-3"><span class="px-2 py-0.5 rounded bg-rose-50 text-rose-700 font-bold">${{escapeCaisseHtml(a.raison_annulation || '-')}}</span></td>
                        <td class="py-3 font-mono text-slate-500">@${{escapeCaisseHtml(a.utilisateur_annulation || '-')}}</td>
                        <td class="py-3 text-slate-400 text-[11px]">${{escapeCaisseHtml(a.date_annulation || '-')}}</td>
                    </tr>
                `).join('');
            }}

            // =================================================================
            // FORMULAIRE D'ENCAISSEMENT
            // =================================================================
            window.openCaisseEncaissementModal = function(passageId) {{
                const p = (caisseGlobalData.non_payes || []).find(x => x.id_passage === passageId) ||
                          (caisseGlobalData.impayes || []).find(x => x.id_passage === passageId) ||
                          (caisseGlobalData.journal || []).find(x => x.id_passage === passageId);

                if (!p) return;

                document.getElementById('encPassageId').value = p.id_passage;
                document.getElementById('encPatientNom').innerText = p.patient_nom || `${{p.nom}} ${{p.prenom}}`;
                document.getElementById('encPassageNum').innerText = `#PASS-${{String(p.id_passage).padStart(4, '0')}}`;

                const actes = p.actes || [];
                const tbodyActes = document.getElementById('encActesTableBody');
                const badgeActes = document.getElementById('encActesCountBadge');

                if (badgeActes) badgeActes.innerText = `${{actes.length}} acte(s)`;

                if (tbodyActes) {{
                    if (actes.length === 0) {{
                        tbodyActes.innerHTML = '<tr><td colspan="4" class="p-3 text-center text-slate-400 italic">Aucun acte enregistré.</td></tr>';
                    }} else {{
                        tbodyActes.innerHTML = actes.map(a => `
                            <tr class="hover:bg-slate-50 dark:hover:bg-white/[0.02]">
                                <td class="p-2.5 font-bold text-slate-800 dark:text-slate-100">${{escapeCaisseHtml(a.nom)}}</td>
                                <td class="p-2.5"><span class="px-2 py-0.5 rounded text-[10px] font-bold ${{a.periode === 'Jour' ? 'bg-amber-100 text-amber-800' : 'bg-purple-100 text-purple-800'}}">${{escapeCaisseHtml(a.periode || 'Jour')}}</span></td>
                                <td class="p-2.5 text-center font-bold">${{Number(a.quantite || 1)}}</td>
                                <td class="p-2.5 text-right font-mono font-black text-slate-900 dark:text-white">${{Number(a.sous_total || 0).toLocaleString()}} DA</td>
                            </tr>
                        `).join('');
                    }}
                }}

                let brutTotal = 0;
                actes.forEach(a => {{ brutTotal += (parseFloat(a.sous_total) || 0); }});
                if (brutTotal === 0) brutTotal = parseFloat(p.montant_total || p.montant_net || 0);

                document.getElementById('encMontantBrutCache').value = brutTotal;

                currentEncRemiseType = p.remise_type || 'DA';
                setEncRemiseType(currentEncRemiseType);
                document.getElementById('encRemiseValeur').value = p.remise_valeur || 0;
                document.getElementById('encAccordeurRemise').value = p.accordeur_remise || 'Direction Générale';

                const netInit = Math.max(0, brutTotal - (parseFloat(p.remise) || 0));
                const resteInit = p.reste_du !== undefined ? p.reste_du : Math.max(0, netInit - (parseFloat(p.versement) || 0));

                document.getElementById('encVersement').value = resteInit;
                calculateEncaissementFinancials();

                document.getElementById('encCaissierLogin').value = '';
                document.getElementById('encCaissierPassword').value = '';

                document.getElementById('caisseEncaissementModal').classList.remove('hidden');
            }};

            window.closeCaisseEncaissementModal = function() {{
                document.getElementById('caisseEncaissementModal').classList.add('hidden');
            }};

            window.setEncRemiseType = function(type) {{
                currentEncRemiseType = type;
                const btnDA = document.getElementById('btnEncRemiseDA');
                const btnPct = document.getElementById('btnEncRemisePct');
                const label = document.getElementById('encRemiseUnitLabel');

                if (type === 'DA') {{
                    btnDA.className = "px-2.5 py-0.5 rounded-md text-[10px] font-bold bg-brand-neon text-black";
                    btnPct.className = "px-2.5 py-0.5 rounded-md text-[10px] font-bold text-slate-500 hover:text-white";
                    label.innerText = "DA";
                }} else {{
                    btnPct.className = "px-2.5 py-0.5 rounded-md text-[10px] font-bold bg-brand-neon text-black";
                    btnDA.className = "px-2.5 py-0.5 rounded-md text-[10px] font-bold text-slate-500 hover:text-white";
                    label.innerText = "%";
                }}
                calculateEncaissementFinancials();
            }};

            window.calculateEncaissementFinancials = function() {{
                const brut = parseFloat(document.getElementById('encMontantBrutCache')?.value || 0);
                const rVal = parseFloat(document.getElementById('encRemiseValeur')?.value || 0);

                let rMontantDA = 0;
                if (currentEncRemiseType === '%') {{
                    rMontantDA = (brut * Math.min(rVal, 100)) / 100;
                }} else {{
                    rMontantDA = Math.min(rVal, brut);
                }}

                document.getElementById('encRemiseMontantDACache').value = rMontantDA.toFixed(2);
                const net = Math.max(0, brut - rMontantDA);

                document.getElementById('encBrutDisplay').innerText = Number(brut).toLocaleString() + ' DA';
                document.getElementById('encRemiseDisplay').innerText = '-' + Number(rMontantDA).toLocaleString() + ' DA';
                document.getElementById('encNetDisplay').innerText = Number(net).toLocaleString() + ' DA';

                const vers = parseFloat(document.getElementById('encVersement')?.value || 0);
                const reste = Math.max(0, net - vers);

                document.getElementById('encResteDisplay').innerText = Number(reste).toLocaleString() + ' DA';
            }};

            window.submitCaisseEncaissement = async function(e) {{
                e.preventDefault();
                const passId = parseInt(document.getElementById('encPassageId').value);
                const montantVers = parseFloat(document.getElementById('encVersement').value || 0);
                const mode = document.getElementById('encModePaiement').value;
                const rType = currentEncRemiseType;
                const rVal = parseFloat(document.getElementById('encRemiseValeur').value || 0);
                const accordeur = document.getElementById('encAccordeurRemise').value;
                const login = document.getElementById('encCaissierLogin').value.trim();
                const pwd = document.getElementById('encCaissierPassword').value;

                if (montantVers <= 0) {{
                    alert("Veuillez saisir un montant valide.");
                    return;
                }}

                const btn = document.getElementById('btnSubmitEncaissement');
                const origHtml = btn.innerHTML;
                btn.disabled = true;
                btn.innerHTML = '<i class="ph-bold ph-spinner animate-spin text-base"></i> Validation...';

                try {{
                    const res = await fetch('/api/finance/regulariser', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{
                            id_passage: passId,
                            montant_versement: montantVers,
                            mode_paiement: mode,
                            remise_type: rType,
                            remise_valeur: rVal,
                            accordeur_remise: accordeur,
                            login: login,
                            password: pwd
                        }})
                    }}).then(r => r.json());

                    if (res.success) {{
                        closeCaisseEncaissementModal();
                        await loadCaisseData();
                        if (res.id_mouvement) openCaisseMovementReceiptModal(res.id_mouvement);
                    }} else {{
                        alert("❌ " + (res.message || res.detail || "Encaissement refusé."));
                    }}
                }} catch (err) {{
                    alert("Erreur lors de la validation de l'encaissement.");
                }} finally {{
                    btn.disabled = false;
                    btn.innerHTML = origHtml;
                }}
            }};

            // =================================================================
            // ANNULATION DU PAIEMENT (REMBOURSEMENT DU JOUR)
            // =================================================================
            window.openAnnulerPaiementModal = function(passageId, patientNom, montant) {{
                document.getElementById('annulerPassageId').value = passageId;
                document.getElementById('annulerPatientNom').innerText = patientNom;
                document.getElementById('annulerPassageNum').innerText = `#PASS-${{String(passageId).padStart(4, '0')}}`;
                document.getElementById('annulerMontant').innerText = `${{Number(montant).toLocaleString()}} DA`;
                document.getElementById('annulerRaison').value = '';
                document.getElementById('annulerLogin').value = '';
                document.getElementById('annulerPassword').value = '';
                document.getElementById('annulerPaiementModal').classList.remove('hidden');
            }};

            window.closeAnnulerPaiementModal = function() {{
                document.getElementById('annulerPaiementModal').classList.add('hidden');
            }};

            window.submitAnnulerPaiement = async function(e) {{
                e.preventDefault();
                const passId = parseInt(document.getElementById('annulerPassageId').value);
                const raison = document.getElementById('annulerRaison').value;
                const login = document.getElementById('annulerLogin').value.trim();
                const password = document.getElementById('annulerPassword').value;

                try {{
                    const res = await fetch('/api/finance/annuler-paiement', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{
                            id_passage: passId,
                            raison_annulation: raison,
                            login: login,
                            password: password
                        }})
                    }}).then(r => r.json());

                    if (res.success) {{
                        closeAnnulerPaiementModal();
                        await loadCaisseData();
                        alert("✅ " + res.message);
                    }} else {{
                        alert("❌ " + (res.message || res.detail || "Annulation refusée."));
                    }}
                }} catch (err) {{
                    alert("Erreur lors de l'annulation du paiement.");
                }}
            }};

            // =================================================================
            // VISIONNEURS PV DE CLÔTURE & TICKET 80MM
            // =================================================================
            window.printClotureCaissePV = function() {{
                const dDeb = document.getElementById('caisseDateDebut')?.value || '';
                const dFin = document.getElementById('caisseDateFin')?.value || '';
                const op = document.getElementById('caisseFilterOperateur')?.value || 'TOUS';
                const mode = document.getElementById('caisseFilterMode')?.value || 'TOUS';
                const statut = document.getElementById('caisseFilterStatut')?.value || 'TOUS';

                const q = new URLSearchParams({{
                    date_debut: dDeb,
                    date_fin: dFin,
                    operateur: op,
                    mode_paiement: mode,
                    statut_paiement: statut
                }}).toString();

                const modal = document.getElementById('cloturePvModal');
                const iframe = document.getElementById('cloturePvIframe');
                if (iframe) iframe.src = '/api/finance/cloture-pv-html?' + q;
                if (modal) modal.classList.remove('hidden');
            }};

            window.closeCloturePvModal = function() {{
                const modal = document.getElementById('cloturePvModal');
                const iframe = document.getElementById('cloturePvIframe');
                if (iframe) iframe.src = 'about:blank';
                if (modal) modal.classList.add('hidden');
            }};

            window.openCaisseMovementReceiptModal = function(movementId) {{
                const modal = document.getElementById('caisseReceiptModal');
                const iframe = document.getElementById('caisseReceiptIframe');
                if (!modal || !iframe || !movementId) return;
                iframe.src = '/api/finance/mouvements/' + encodeURIComponent(movementId) + '/ticket';
                modal.classList.remove('hidden');
            }};

            window.printCloturePvIframe = function() {{
                const iframe = document.getElementById('cloturePvIframe');
                if (iframe && iframe.contentWindow) {{
                    iframe.contentWindow.focus();
                    iframe.contentWindow.print();
                }}
            }};

            window.openCaisseUserReportPdf = function(operator, reportType = 'jour') {{
                const modal = document.getElementById('caisseUserReportPdfModal');
                const iframe = document.getElementById('caisseUserReportPdfIframe');
                const title = document.getElementById('caisseUserReportPdfTitle');
                if (!modal || !iframe || !operator) return;
                const query = new URLSearchParams({{
                    operateur: operator,
                    type_rapport: reportType,
                    date_debut: document.getElementById('caisseDateDebut')?.value || '',
                    date_fin: document.getElementById('caisseDateFin')?.value || '',
                    mode_paiement: document.getElementById('caisseFilterMode')?.value || 'TOUS',
                    statut_paiement: document.getElementById('caisseFilterStatut')?.value || 'TOUS'
                }});
                const reportLabel = reportType === 'garde' ? 'Journal de garde' : (reportType === 'jour' ? 'Journal du jour' : 'Rapport détaillé');
                const operatorLabel = operator === 'TOUS' ? 'Tous les caissiers' : `@${{operator}}`;
                if (title) title.textContent = `${{reportLabel}} — ${{operatorLabel}}`;
                iframe.src = '/api/finance/rapport-utilisateur/pdf?' + query.toString();
                modal.classList.remove('hidden');
            }};

            window.closeCaisseUserReportPdf = function() {{
                const modal = document.getElementById('caisseUserReportPdfModal');
                const iframe = document.getElementById('caisseUserReportPdfIframe');
                if (iframe) iframe.src = 'about:blank';
                if (modal) modal.classList.add('hidden');
            }};

            let currentCaisseInvoicePassageId = null;
            let currentCaisseRegisteredInvoiceId = null;
            let currentCaisseInvoiceDocumentType = 'facture';
            let caisseInvoiceReturnToRegister = false;
            const setCaisseInvoiceActions = (validated, cancelled = false, isAvoir = false) => {{
                const pending = validated === null;
                const validateButton = document.getElementById('caisseInvoiceValidateButton');
                if (validateButton) {{
                    validateButton.classList.toggle('hidden', pending);
                    validateButton.disabled = Boolean(validated) || pending || cancelled;
                    validateButton.style.backgroundColor = validated || cancelled ? '#64748b' : '#15803d';
                    validateButton.style.borderColor = validated || cancelled ? '#475569' : '#166534';
                    validateButton.innerHTML = validated
                        ? `<i class="ph-bold ph-check-circle text-base" aria-hidden="true"></i><span>${{isAvoir ? 'Avoir d’annulation' : (cancelled ? 'Facture annulée' : 'Facture déjà validée')}}</span>`
                        : '<i class="ph-bold ph-check-circle text-base" aria-hidden="true"></i><span>Valider la facture</span>';
                    validateButton.title = validated || cancelled ? 'Cette facture est déjà validée et ne peut pas être validée une seconde fois.' : '';
                }}
                document.getElementById('caisseInvoicePrintButton')?.classList.toggle('hidden', !validated || pending || (cancelled && !isAvoir));
                document.getElementById('caisseInvoiceDownloadButton')?.classList.toggle('hidden', !validated || pending || (cancelled && !isAvoir));
            }};

            window.openCaisseInvoicePdf = async function(passageId, patientName = '') {{
                const modal = document.getElementById('caisseInvoicePdfModal');
                const iframe = document.getElementById('caisseInvoicePdfIframe');
                const title = document.getElementById('caisseInvoicePdfTitle');
                if (!modal || !iframe || !passageId) return;
                caisseInvoiceReturnToRegister = false;
                currentCaisseInvoicePassageId = Number(passageId);
                currentCaisseRegisteredInvoiceId = null;
                currentCaisseInvoiceDocumentType = 'facture';
                setCaisseInvoiceActions(null);
                if (title) title.textContent = `Facture — ${{patientName || `Passage #${{passageId}}`}}`;
                modal.classList.remove('hidden');
                try {{
                    const response = await fetch('/api/finance/factures/' + encodeURIComponent(passageId) + '/status', {{ cache: 'no-store' }});
                    const state = await response.json();
                    if (!response.ok || !state.success) throw new Error(state.detail || 'Impossible de vérifier la facture.');
                    const cancelled = String(state.statut || '').toUpperCase() === 'ANNULEE';
                    setCaisseInvoiceActions(Boolean(state.validated), cancelled);
                    if (state.validated && title) title.textContent = `${{cancelled ? 'Facture annulée' : 'Facture'}} ${{state.numero_facture || ''}} — ${{patientName || `Passage #${{passageId}}`}}`;
                    iframe.src = '/api/finance/factures/' + encodeURIComponent(passageId) + '/pdf?v=' + Date.now() + '#toolbar=0&navpanes=0';
                }} catch (error) {{
                    iframe.src = 'about:blank';
                    if (typeof showToast === 'function') showToast(error.message || 'Erreur de chargement de la facture.', 'error');
                    else alert(error.message || 'Erreur de chargement de la facture.');
                    modal.classList.add('hidden');
                }}
            }};

            window.validateCaisseInvoice = async function() {{
                if (!currentCaisseInvoicePassageId) return;
                const button = document.getElementById('caisseInvoiceValidateButton');
                if (button) {{ button.disabled = true; button.classList.add('opacity-60'); }}
                try {{
                    const response = await fetch('/api/finance/factures/' + encodeURIComponent(currentCaisseInvoicePassageId) + '/valider', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: '{{}}' }});
                    const result = await response.json();
                    if (!response.ok || !result.success) throw new Error(result.detail || 'La validation de la facture a échoué.');
                    setCaisseInvoiceActions(true);
                    const title = document.getElementById('caisseInvoicePdfTitle');
                    if (title) title.textContent = `Facture ${{result.numero_facture || ''}}`;
                    const iframe = document.getElementById('caisseInvoicePdfIframe');
                    if (iframe) iframe.src = '/api/finance/factures/' + encodeURIComponent(currentCaisseInvoicePassageId) + '/pdf?v=' + Date.now() + '#toolbar=0&navpanes=0';
                    if (typeof showToast === 'function') showToast('Facture validée et enregistrée. Vous pouvez maintenant l’imprimer.', 'success');
                }} catch (error) {{
                    if (typeof showToast === 'function') showToast(error.message || 'Erreur de validation.', 'error');
                    else alert(error.message || 'Erreur de validation.');
                }} finally {{
                    if (button) {{ button.disabled = false; button.classList.remove('opacity-60'); }}
                }}
            }};

            window.openCaisseInvoiceRegister = function() {{
                const modal = document.getElementById('caisseInvoiceRegisterModal');
                if (!modal) return;
                const today = new Date();
                const localDate = new Date(today.getTime() - today.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
                const start = document.getElementById('caisseInvoiceDateStart');
                const end = document.getElementById('caisseInvoiceDateEnd');
                if (start && !start.value) start.value = localDate;
                if (end && !end.value) end.value = localDate;
                modal.classList.remove('hidden');
                loadCaisseInvoiceRegister();
            }};

            window.closeCaisseInvoiceRegister = function() {{
                document.getElementById('caisseInvoiceRegisterModal')?.classList.add('hidden');
            }};

            window.loadCaisseInvoiceRegister = async function() {{
                const rows = document.getElementById('caisseInvoiceRegisterRows');
                const summary = document.getElementById('caisseInvoiceRegisterSummary');
                if (!rows) return;
                rows.innerHTML = '<tr><td colspan="7" class="p-6 text-center text-slate-400">Chargement des factures…</td></tr>';
                const query = new URLSearchParams({{
                    date_debut: document.getElementById('caisseInvoiceDateStart')?.value || '',
                    date_fin: document.getElementById('caisseInvoiceDateEnd')?.value || '',
                    recherche: document.getElementById('caisseInvoiceSearch')?.value || '',
                    utilisateur: document.getElementById('caisseInvoiceUserSearch')?.value || ''
                }});
                try {{
                    const response = await fetch('/api/finance/factures/registre?' + query.toString(), {{ cache: 'no-store' }});
                    const result = await response.json();
                    if (!response.ok || !result.success) throw new Error(result.detail || 'Impossible de charger le registre.');
                    const invoices = result.factures || [];
                    const activeInvoices = invoices.filter(invoice => String(invoice.statut || 'VALIDEE').toUpperCase() !== 'ANNULEE');
                    const total = activeInvoices.reduce((sum, invoice) => sum + Number(invoice.montant_ttc || 0), 0);
                    if (summary) summary.textContent = `${{invoices.length}} facture(s) · ${{invoices.length - activeInvoices.length}} annulée(s) · Total TTC valide : ${{total.toLocaleString('fr-FR', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }})}} DA`;
                    if (!invoices.length) {{
                        rows.innerHTML = '<tr><td colspan="7" class="p-8 text-center text-slate-400"><i class="ph-bold ph-file-x text-2xl block mb-2"></i>Aucune facture validée pour ces filtres.</td></tr>';
                        return;
                    }}
                    rows.innerHTML = invoices.map(invoice => {{
                        const cancelled = String(invoice.statut || 'VALIDEE').toUpperCase() === 'ANNULEE';
                        const statusCell = cancelled
                            ? `<span title="${{escapeCaisseHtml(invoice.motif_annulation || '')}}" class="inline-flex rounded-full px-2 py-1 bg-rose-100 text-rose-700 font-bold">Annulée</span><span class="block mt-1 text-[10px] text-slate-400">Avoir ${{escapeCaisseHtml(invoice.numero_avoir || '')}}</span>`
                            : '<span class="inline-flex rounded-full px-2 py-1 bg-emerald-100 text-emerald-700 font-bold">Validée</span>';
                        const viewButton = `<button type="button" onclick="openCaisseRegisteredInvoice(Number(this.dataset.id), this.dataset.number, this.dataset.patient, this.dataset.status)" data-id="${{Number(invoice.id)}}" data-number="${{escapeCaisseHtml(invoice.numero_facture)}}" data-patient="${{escapeCaisseHtml(invoice.patient_nom || '')}}" data-status="${{cancelled ? 'ANNULEE' : 'VALIDEE'}}" class="px-2.5 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white font-bold inline-flex items-center gap-1"><i class="ph-bold ph-eye"></i>Voir</button>`;
                        let otherActions = '';
                        if (cancelled) {{
                            otherActions = `<button type="button" onclick="openCaisseInvoiceAvoir(Number(this.dataset.id), this.dataset.number, this.dataset.patient)" data-id="${{Number(invoice.id)}}" data-number="${{escapeCaisseHtml(invoice.numero_avoir || '')}}" data-patient="${{escapeCaisseHtml(invoice.patient_nom || '')}}" class="px-2.5 py-1.5 rounded-lg bg-violet-600 hover:bg-violet-700 text-white font-bold inline-flex items-center gap-1"><i class="ph-bold ph-file-pdf"></i>Avoir</button>`;
                        }} else if (caisseAccess.cash_refund) {{
                            otherActions = `<button type="button" onclick="openCaisseInvoiceCancelModal(Number(this.dataset.id), this.dataset.number, this.dataset.patient)" data-id="${{Number(invoice.id)}}" data-number="${{escapeCaisseHtml(invoice.numero_facture)}}" data-patient="${{escapeCaisseHtml(invoice.patient_nom || '')}}" class="px-2.5 py-1.5 rounded-lg bg-rose-50 hover:bg-rose-600 text-rose-700 hover:text-white border border-rose-200 font-bold inline-flex items-center gap-1"><i class="ph-bold ph-prohibit"></i>Annuler</button>`;
                        }}
                        return `<tr class="hover:bg-emerald-50/60 dark:hover:bg-white/5">
                        <td class="p-3 whitespace-nowrap">${{escapeCaisseHtml(invoice.date_generation)}}</td>
                        <td class="p-3 font-mono font-bold text-emerald-700 dark:text-emerald-400">${{escapeCaisseHtml(invoice.numero_facture)}}</td>
                        <td class="p-3">${{statusCell}}${{cancelled ? `<span class="block mt-1 text-[10px] text-slate-400">${{escapeCaisseHtml(invoice.date_annulation || '')}} · @${{escapeCaisseHtml(invoice.utilisateur_annulation || '')}}</span>` : ''}}</td>
                        <td class="p-3">@${{escapeCaisseHtml(invoice.utilisateur || '—')}}</td>
                        <td class="p-3 font-semibold">${{escapeCaisseHtml(invoice.patient_nom || '—')}} <span class="block text-[10px] text-slate-400">Passage #${{Number(invoice.id_passage)}}</span></td>
                        <td class="p-3 text-right font-mono font-bold">${{Number(invoice.montant_ttc || 0).toLocaleString('fr-FR', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }})}} DA</td>
                        <td class="p-3 text-center"><div class="flex flex-wrap justify-center gap-1.5">${{viewButton}}${{otherActions}}</div></td>
                    </tr>`;
                    }}).join('');
                }} catch (error) {{
                    rows.innerHTML = `<tr><td colspan="7" class="p-6 text-center text-rose-500">${{escapeCaisseHtml(error.message || 'Erreur de chargement.')}}</td></tr>`;
                }}
            }};

            window.openCaisseRegisteredInvoice = function(invoiceId, invoiceNumber, patientName = '', invoiceStatus = 'VALIDEE') {{
                const modal = document.getElementById('caisseInvoicePdfModal');
                const iframe = document.getElementById('caisseInvoicePdfIframe');
                const title = document.getElementById('caisseInvoicePdfTitle');
                if (!modal || !iframe || !invoiceId) return;
                caisseInvoiceReturnToRegister = true;
                currentCaisseInvoicePassageId = null;
                currentCaisseRegisteredInvoiceId = Number(invoiceId);
                currentCaisseInvoiceDocumentType = 'facture';
                const cancelled = String(invoiceStatus || '').toUpperCase() === 'ANNULEE';
                setCaisseInvoiceActions(true, cancelled);
                if (title) title.textContent = `${{cancelled ? 'Facture annulée' : 'Facture'}} ${{invoiceNumber || ''}} — ${{patientName || 'Patient'}}`;
                iframe.src = '/api/finance/factures/registre/' + encodeURIComponent(invoiceId) + '/pdf?v=' + Date.now() + (cancelled ? '#toolbar=0&navpanes=0' : '');
                document.getElementById('caisseInvoiceRegisterModal')?.classList.add('hidden');
                modal.classList.remove('hidden');
            }};

            window.openCaisseInvoiceAvoir = function(invoiceId, avoirNumber, patientName = '') {{
                const modal = document.getElementById('caisseInvoicePdfModal');
                const iframe = document.getElementById('caisseInvoicePdfIframe');
                const title = document.getElementById('caisseInvoicePdfTitle');
                if (!modal || !iframe || !invoiceId) return;
                caisseInvoiceReturnToRegister = true;
                currentCaisseInvoicePassageId = null;
                currentCaisseRegisteredInvoiceId = Number(invoiceId);
                currentCaisseInvoiceDocumentType = 'avoir';
                setCaisseInvoiceActions(true, true, true);
                if (title) title.textContent = `Avoir ${{avoirNumber || ''}} — ${{patientName || 'Patient'}}`;
                iframe.src = '/api/finance/factures/registre/' + encodeURIComponent(invoiceId) + '/avoir.pdf?v=' + Date.now();
                document.getElementById('caisseInvoiceRegisterModal')?.classList.add('hidden');
                modal.classList.remove('hidden');
            }};

            window.openCaisseInvoiceCancelModal = function(invoiceId, invoiceNumber, patientName) {{
                document.getElementById('caisseInvoiceCancelId').value = invoiceId;
                document.getElementById('caisseInvoiceCancelNumber').textContent = invoiceNumber || '';
                document.getElementById('caisseInvoiceCancelPatient').textContent = patientName || '';
                document.getElementById('caisseInvoiceCancelReason').value = '';
                document.getElementById('caisseInvoiceCancelModal').classList.remove('hidden');
            }};

            window.closeCaisseInvoiceCancelModal = function() {{
                document.getElementById('caisseInvoiceCancelModal')?.classList.add('hidden');
            }};

            window.submitCaisseInvoiceCancellation = async function(event) {{
                event.preventDefault();
                const invoiceId = Number(document.getElementById('caisseInvoiceCancelId')?.value);
                const reason = document.getElementById('caisseInvoiceCancelReason')?.value?.trim() || '';
                const button = document.getElementById('caisseInvoiceCancelSubmit');
                if (!invoiceId || reason.length < 4) return;
                if (button) {{ button.disabled = true; button.classList.add('opacity-60'); }}
                try {{
                    const response = await fetch('/api/finance/factures/registre/' + encodeURIComponent(invoiceId) + '/annuler', {{
                        method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify({{ motif: reason }})
                    }});
                    const result = await response.json();
                    if (!response.ok || !result.success) throw new Error(result.detail || 'L’annulation de la facture a échoué.');
                    closeCaisseInvoiceCancelModal();
                    await loadCaisseInvoiceRegister();
                    if (typeof showToast === 'function') showToast(`Facture annulée. Avoir ${{result.numero_avoir}} créé et conservé au registre.`, 'success');
                }} catch (error) {{
                    if (typeof showToast === 'function') showToast(error.message || 'Erreur lors de l’annulation.', 'error');
                    else alert(error.message || 'Erreur lors de l’annulation.');
                }} finally {{
                    if (button) {{ button.disabled = false; button.classList.remove('opacity-60'); }}
                }}
            }};

            window.closeCaisseInvoicePdf = function() {{
                const modal = document.getElementById('caisseInvoicePdfModal');
                const iframe = document.getElementById('caisseInvoicePdfIframe');
                if (iframe) iframe.src = 'about:blank';
                currentCaisseInvoicePassageId = null;
                currentCaisseRegisteredInvoiceId = null;
                currentCaisseInvoiceDocumentType = 'facture';
                if (modal) modal.classList.add('hidden');
                if (caisseInvoiceReturnToRegister) {{
                    document.getElementById('caisseInvoiceRegisterModal')?.classList.remove('hidden');
                    caisseInvoiceReturnToRegister = false;
                }}
            }};

            window.downloadCaisseInvoicePdf = function() {{
                let url = '';
                if (currentCaisseInvoicePassageId) {{
                    url = '/api/finance/factures/' + encodeURIComponent(currentCaisseInvoicePassageId) + '/pdf?download=true';
                }} else if (currentCaisseRegisteredInvoiceId) {{
                    const fileType = currentCaisseInvoiceDocumentType === 'avoir' ? 'avoir.pdf' : 'pdf';
                    url = '/api/finance/factures/registre/' + encodeURIComponent(currentCaisseRegisteredInvoiceId) + '/' + fileType + '?download=true';
                }}
                if (!url) return;
                const link = document.createElement('a');
                link.href = url;
                link.download = 'facture.pdf';
                document.body.appendChild(link);
                link.click();
                link.remove();
            }};

            window.printCaisseInvoicePdfIframe = function() {{
                const iframe = document.getElementById('caisseInvoicePdfIframe');
                if (iframe && iframe.contentWindow) {{
                    iframe.contentWindow.focus();
                    iframe.contentWindow.print();
                }}
            }};

            window.printCaisseUserReportPdfIframe = function() {{
                const iframe = document.getElementById('caisseUserReportPdfIframe');
                if (iframe && iframe.contentWindow) {{
                    iframe.contentWindow.focus();
                    iframe.contentWindow.print();
                }}
            }};

            window.openCaisseReceiptModal = function(passageId) {{
                const modal = document.getElementById('caisseReceiptModal');
                const iframe = document.getElementById('caisseReceiptIframe');
                if (iframe) iframe.src = '/api/reception/ticket/' + passageId;
                if (modal) modal.classList.remove('hidden');
            }};

            window.closeCaisseReceiptModal = function() {{
                const modal = document.getElementById('caisseReceiptModal');
                const iframe = document.getElementById('caisseReceiptIframe');
                if (iframe) iframe.src = 'about:blank';
                if (modal) modal.classList.add('hidden');
            }};

            window.printCaisseReceiptIframe = function() {{
                const iframe = document.getElementById('caisseReceiptIframe');
                if (iframe && iframe.contentWindow) {{
                    iframe.contentWindow.focus();
                    iframe.contentWindow.print();
                }}
            }};

            window.setCaisseDateQuick = function(preset, reload = true) {{
                const today = new Date();
                const dDeb = document.getElementById('caisseDateDebut');
                const dFin = document.getElementById('caisseDateFin');
                const toYmd = (d) => d.toISOString().split('T')[0];

                if (!dDeb || !dFin) return;

                if (preset === 'today') {{
                    dDeb.value = toYmd(today);
                    dFin.value = toYmd(today);
                }} else if (preset === 'yesterday') {{
                    const y = new Date();
                    y.setDate(today.getDate() - 1);
                    dDeb.value = toYmd(y);
                    dFin.value = toYmd(y);
                }} else if (preset === 'this_week') {{
                    const first = new Date(today.setDate(today.getDate() - today.getDay() + 1));
                    dDeb.value = toYmd(first);
                    dFin.value = toYmd(new Date());
                }} else if (preset === 'this_month') {{
                    const first = new Date(today.getFullYear(), today.getMonth(), 1);
                    dDeb.value = toYmd(first);
                    dFin.value = toYmd(new Date());
                }}

                if (reload) loadCaisseData();
            }};

            loadCaisseModule();
        }})();
    </script>
    """
