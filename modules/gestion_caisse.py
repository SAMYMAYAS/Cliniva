

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import logging
import math
from html import escape as html_escape
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from database import get_db_cursor
from app.auth import hash_password, track_action, get_current_user_optional

logger = logging.getLogger("CaisseModule")

caisse_router = APIRouter(prefix="/api/finance", tags=["Caisse & Finances"])

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
                "SELECT DISTINCT utilisateur FROM patient_passages WHERE utilisateur IS NOT NULL AND utilisateur != '' ORDER BY utilisateur ASC;")
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
        cursor.execute("SELECT nom_raison_sociale, adresse, telephone FROM clinic_info WHERE id = 1;")
        clinic_row = cursor.fetchone()

    movement = dict(row)
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
        <div id="caisseKpisPanel" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-4">

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
