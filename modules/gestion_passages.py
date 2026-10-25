
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import base64
import hashlib
from html import escape as html_escape
import json
import logging
import math
import os
import time
import unicodedata
from pathlib import Path
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from database import DOCS_DIR, get_db_cursor
from app.auth import hash_password, track_action, get_current_user_optional
from modules.gestion_patients import (
    ALGERIA_WILAYAS,
    GROUPES_SANGUINS,
    TYPES_PIECES_IDENTITE,
    render_html as render_patients_html,
)

logger = logging.getLogger("PassagesModule")

reception_router = APIRouter(prefix="/api/reception", tags=["Accueil & Passages"])


def _require_reception_permission(request: Request, permission: str) -> Dict[str, Any]:
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    raw_permissions = user.get("permissions") or {}
    if isinstance(raw_permissions, str):
        try:
            raw_permissions = json.loads(raw_permissions)
        except (TypeError, ValueError):
            raw_permissions = {}
    if user.get("status") != "Actif":
        raise HTTPException(status_code=403, detail="Ce compte utilisateur est inactif.")
    permission_granted = isinstance(raw_permissions, dict) and bool(raw_permissions.get(permission))
    if permission == "cash_collect_rest" and isinstance(raw_permissions, dict) and permission not in raw_permissions:
        permission_granted = bool(raw_permissions.get("cash_collect"))
    if permission.startswith("cash_"):
        if not permission_granted:
            raise HTTPException(status_code=403, detail="Vous n'avez pas l'autorisation pour cette action financière.")
    elif user.get("role") != "Administrateur" and not permission_granted:
        raise HTTPException(status_code=403, detail="Vous n'avez pas l'autorisation pour cette action.")
    return user

TYPES_PASSAGES: List[str] = [
    "Consultation",
    "Hospitalisation",
    "Radiologie",
    "Soins",
    "Rééducation",
    "Coloscopie",
]

DOCS_PASSAGE_TYPES: List[str] = [
    "Ordonnance",
    "Demande d'Examen (Face 1)",
    "Demande d'Examen (Face 2)",
    "Demande d'Examen (Face 3)",
    "Demande d'Examen (Face 4)",
    "Ancien Dossier",
    "Analyses Médicales",
    "Relecture",
    "Prise en Charge (PEC)",
    "Badge",
]


def is_radiology_category(category_name: Any) -> bool:
    normalized = "".join(
        char for char in unicodedata.normalize("NFKD", str(category_name or "").casefold())
        if not unicodedata.combining(char)
    )
    return "radiolog" in normalized and "medical" in normalized


def passage_has_radiology_exam(passage: Dict[str, Any], consultations_by_name: Dict[str, Dict[str, Any]]) -> bool:
    acts = passage.get("actes") or []
    for act in acts:
        if not isinstance(act, dict):
            continue
        category = act.get("categorie") or act.get("category") or ""
        if not category:
            designation = str(act.get("nom") or act.get("designation") or "").strip().casefold()
            category = (consultations_by_name.get(designation) or {}).get("categorie") or ""
        if is_radiology_category(category):
            return True
    return False


# ============================================================================
# LOGIQUE MÉTIER POSTGRESQL
# ============================================================================
class ReceptionManager:
    """Gestionnaire des passages, encaissements et pièces médicales."""

    @staticmethod
    def save_base64_doc(base64_data: str, prefix: str = "passage_doc") -> str:
        """Sauvegarde les documents scannés base64 sur disque."""
        if not base64_data or not base64_data.startswith("data:"):
            return base64_data
        try:
            import base64
            header, encoded = base64_data.split(",", 1)
            ext = "jpg"
            if "pdf" in header:
                ext = "pdf"
            elif "png" in header:
                ext = "png"
            filename = f"{prefix}_{int(time.time() * 1000)}.{ext}"
            filepath = os.path.join(DOCS_DIR, filename)
            with open(filepath, "wb") as f:
                f.write(base64.b64decode(encoded))
            return base64_data
        except Exception as err:
            logger.warning("Échec sauvegarde fichier passage : %s", err)
            return base64_data

    def get_all(self) -> List[Dict[str, Any]]:
        """Récupère tous les passages enregistrés avec détails patients et médecins."""
        with get_db_cursor() as cursor:
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
                                  p.raison_annulation,
                                  p.montant_annule,
                                  p.questionnaire_json,
                                  p.documents_scannes,
                                  p.statut,
                                  p.consultation_utilisateur,
                                  p.medecin_traitant_id,
                                  COALESCE(pat.nom, 'Patient')          as nom,
                                  COALESCE(pat.prenom, '')              as prenom,
                                  COALESCE(pat.telephone_1, '-')        as telephone_1,
                                  COALESCE(pat.telephone_2, '')          as telephone_2,
                                  COALESCE(pat.email, '')                as email,
                                  pat.avatar,
                                  pat.date_naissance,
                                  pat.genre,
                                  pat.groupe_sanguin,
                                  pat.wilaya,
                                  pat.commune,
                                  (
                                      COALESCE(p.statut, '') = 'En attente'
                                      AND COALESCE(p.statut_paiement, 'Non payé') = 'Non payé'
                                      AND COALESCE(p.versement, 0) <= 0
                                      AND NOT EXISTS (SELECT 1 FROM caisse_mouvements cm WHERE cm.id_passage = p.id)
                                      AND NOT EXISTS (SELECT 1 FROM patient_dme_records d WHERE d.id_passage = p.id)
                                  ) AS editable_pre_consultation,
                                  td.nom                                as medecin_traitant_nom,
                                  td.prenom                             as medecin_traitant_prenom,
                                  td.grade                              as medecin_traitant_grade,
                                  td.specialite                         as medecin_traitant_specialite,
                                  td.adresse                            as medecin_traitant_adresse
                           FROM patient_passages p
                                    LEFT JOIN patients pat ON p.id_patient = pat.id
                                    LEFT JOIN treating_doctors td ON p.medecin_traitant_id = td.id
                           ORDER BY p.id DESC;
                           """)
            rows = cursor.fetchall()

        passages = []
        for r in rows:
            item = dict(r)
            item["actes"] = item.get("actes_json") if isinstance(item.get("actes_json"), list) else json.loads(
                item.get("actes_json") or "[]")
            item["questionnaire"] = item.get("questionnaire_json") if isinstance(item.get("questionnaire_json"),
                                                                                 dict) else json.loads(
                item.get("questionnaire_json") or "{}")
            item["documents_scannes"] = item.get("documents_scannes") if isinstance(item.get("documents_scannes"),
                                                                                    list) else json.loads(
                item.get("documents_scannes") or "[]")
            item["pieces_scannees"] = item.get("pieces_scannees") if isinstance(item.get("pieces_scannees"), list) else json.loads(item.get("pieces_scannees") or "[]")
            passages.append(item)
        return passages

    def get_passage_by_id(self, passage_id: int) -> Optional[Dict[str, Any]]:
        """Récupère un passage spécifique avec l'ensemble des données patient et médecin."""
        with get_db_cursor() as cursor:
            cursor.execute("""
                           SELECT p.id                                                     as id_passage,
                                  p.id_patient,
                                  TO_CHAR(p.date_passage, 'YYYY-MM-DD')                    as date_passage,
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
                                  p.questionnaire_json,
                                  p.documents_scannes,
                                  p.statut,
                                  p.consultation_utilisateur,
                                  p.medecin_traitant_id,
                                  COALESCE(pat.nom, 'Patient')                             as nom,
                                  COALESCE(pat.prenom, '')                                 as prenom,
                                  COALESCE(pat.telephone_1, '-')                           as telephone_1,
                                  COALESCE(pat.telephone_2, '')                            as telephone_2,
                                  COALESCE(pat.email, '-')                                 as email,
                                  COALESCE(TO_CHAR(pat.date_naissance, 'DD/MM/YYYY'), '-') as date_naissance,
                                  COALESCE(pat.wilaya, '-')                                as wilaya,
                                  COALESCE(pat.commune, '-')                               as commune,
                                  pat.avatar,
                                  pat.genre,
                                  td.nom                                                   as medecin_traitant_nom,
                                  td.prenom                                                as medecin_traitant_prenom,
                                  td.grade                                                 as medecin_traitant_grade,
                                  td.specialite                                            as medecin_traitant_specialite,
                                  td.adresse                                               as medecin_traitant_adresse
                           FROM patient_passages p
                                    LEFT JOIN patients pat ON p.id_patient = pat.id
                                    LEFT JOIN treating_doctors td ON p.medecin_traitant_id = td.id
                           WHERE p.id = %s;
                           """, (passage_id,))
            row = cursor.fetchone()

        if not row:
            return None

        item = dict(row)
        item["actes"] = item.get("actes_json") if isinstance(item.get("actes_json"), list) else json.loads(
            item.get("actes_json") or "[]")
        item["questionnaire"] = item.get("questionnaire_json") if isinstance(item.get("questionnaire_json"),
                                                                             dict) else json.loads(
            item.get("questionnaire_json") or "{}")
        return item

    def count(self) -> int:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total FROM patient_passages;")
            row = cursor.fetchone()
            return row["total"] if row else 0

    def save(self, data: Dict[str, Any]) -> Dict[str, Any]:
        pass_id = data.get("id_passage")
        id_patient = data.get("id_patient")
        date_passage = data.get("date_passage") or datetime.now().strftime("%Y-%m-%d")
        heure_passage = data.get("heure_passage") or datetime.now().strftime("%H:%M")
        utilisateur = str(data.get("utilisateur") or "admin").strip()
        type_passage = str(data.get("type_passage") or "Consultation").strip()
        actes_list = data.get("actes", [])
        questionnaire = data.get("questionnaire", {})
        documents_scannes = data.get("documents_scannes", [])
        statut = str(data.get("statut") or "En attente").strip()
        medecin_traitant_id = data.get("medecin_traitant_id") or None

        try:
            montant_total = float(data.get("montant_total", 0.0))
        except (ValueError, TypeError):
            montant_total = 0.0

        remise_type = str(data.get("remise_type") or "DA").strip()
        try:
            remise_valeur = float(data.get("remise_valeur", 0.0))
            remise = float(data.get("remise", 0.0))
            versement = float(data.get("versement", 0.0))
        except (ValueError, TypeError):
            remise_valeur, remise, versement = 0.0, 0.0, 0.0

        if not all(math.isfinite(v) for v in (montant_total, remise_valeur, remise, versement)):
            return {"success": False, "message": "Les montants doivent être des nombres valides."}
        if montant_total < 0 or remise_valeur < 0 or remise < 0 or versement < 0:
            return {"success": False, "message": "Les montants ne peuvent pas être négatifs."}
        if remise_type not in {"DA", "%"}:
            return {"success": False, "message": "Type de remise invalide."}
        if remise_type == "%":
            if remise_valeur > 100:
                return {"success": False, "message": "La remise ne peut pas dépasser 100 %."}
            remise = round(montant_total * remise_valeur / 100, 2)
        else:
            if remise_valeur > montant_total:
                return {"success": False, "message": "La remise ne peut pas dépasser le montant total."}
            remise = round(remise_valeur, 2)

        accordeur_remise = str(data.get("accordeur_remise") or "").strip()
        net = max(0.0, montant_total - remise)
        if versement > net:
            return {"success": False, "message": f"Le versement dépasse le montant net dû ({net:.2f} DA)."}
        reste = max(0.0, net - versement)

        mode_paiement = str(data.get("mode_paiement") or "Espèces").strip()
        if mode_paiement not in {"Espèces", "Carte CIB / Edahabia", "Chèque", "Prise en Charge (PEC)", "Autre"}:
            return {"success": False, "message": "Mode de paiement invalide."}
        statut_paiement = "Payé" if reste == 0 and net > 0 else ("Partiel" if versement > 0 else "Non payé")

        if not id_patient or not medecin_traitant_id or not actes_list:
            return {"success": False,
                    "message": "Le patient, le médecin traitant et au moins une prestation sont obligatoires."}

        for doc in documents_scannes:
            if doc.get("data"):
                doc["data"] = self.save_base64_doc(doc["data"])

        actes_json = json.dumps(actes_list)
        questionnaire_json = json.dumps(questionnaire)
        docs_json = json.dumps(documents_scannes)

        with get_db_cursor() as cursor:
            if pass_id:
                cursor.execute(
                    "SELECT montant_total, remise, remise_type, remise_valeur, accordeur_remise, versement "
                    "FROM patient_passages WHERE id = %s FOR UPDATE;",
                    (pass_id,)
                )
                passage_existant = cursor.fetchone()
                if not passage_existant:
                    return {"success": False, "message": "Passage introuvable."}
                try:
                    montants_actuels = tuple(
                        Decimal(str(passage_existant.get(champ) or 0)).quantize(Decimal("0.01"))
                        for champ in ("montant_total", "remise", "remise_valeur", "versement")
                    )
                    montants_demandes = tuple(
                        Decimal(str(valeur)).quantize(Decimal("0.01"))
                        for valeur in (montant_total, remise, remise_valeur, versement)
                    )
                except (InvalidOperation, TypeError, ValueError):
                    return {"success": False, "message": "Impossible de vérifier les montants du passage."}
                metadata_remise_identique = (
                    str(passage_existant.get("remise_type") or "DA") == remise_type
                    and str(passage_existant.get("accordeur_remise") or "") == accordeur_remise
                )
                if montants_actuels != montants_demandes or not metadata_remise_identique:
                    return {
                        "success": False,
                        "message": "Les montants d'un passage existant ne peuvent pas être modifiés ici. Utilisez la caisse pour conserver une trace complète.",
                    }
                query = """
                        UPDATE patient_passages \
                        SET id_patient    = %s, \
                            date_passage  = %s, \
                            heure_passage = %s, \
                            utilisateur   = %s, \
                            type_passage  = %s, \
                            actes_json    = %s::jsonb, montant_total = %s, remise = %s, remise_type = %s, remise_valeur = %s, accordeur_remise = %s, versement = %s, reste = %s, mode_paiement = %s, statut_paiement = %s, questionnaire_json = %s::jsonb, documents_scannes = %s::jsonb, statut = %s, medecin_traitant_id = %s
                        WHERE id = %s; \
                        """
                cursor.execute(query, (
                    id_patient, date_passage, heure_passage, utilisateur, type_passage,
                    actes_json, montant_total, remise, remise_type, remise_valeur,
                    accordeur_remise, versement, reste, mode_paiement, statut_paiement,
                    questionnaire_json, docs_json, statut, medecin_traitant_id, pass_id
                ))
                saved_id = pass_id
            else:
                query = """
                        INSERT INTO patient_passages
                        (id_patient, date_passage, heure_passage, utilisateur, type_passage,
                         actes_json, montant_total, remise, remise_type, remise_valeur,
                         accordeur_remise, versement, reste, mode_paiement, statut_paiement,
                         questionnaire_json, documents_scannes, statut, medecin_traitant_id)
                        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, \
                                %s, %s) RETURNING id; \
                        """
                cursor.execute(query, (
                    id_patient, date_passage, heure_passage, utilisateur, type_passage,
                    actes_json, montant_total, remise, remise_type, remise_valeur,
                    accordeur_remise, versement, reste, mode_paiement, statut_paiement,
                    questionnaire_json, docs_json, statut, medecin_traitant_id
                ))
                res = cursor.fetchone()
                saved_id = res["id"] if res else None

        return {
            "success": True,
            "message": "Passage et encaissement enregistrés avec succès !",
            "id_passage": saved_id,
        }

    def encaisser(self, data: Dict[str, Any], session_user: Dict[str, Any]) -> Dict[str, Any]:
        """Encaisse un solde restant avec vérification du mot de passe."""
        try:
            passage_id = int(data.get("id_passage"))
            login = str(data.get("login") or "").strip().lower()
            password = str(data.get("password") or "")
            montant = Decimal(str(data.get("montant") or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            mode_paiement = str(data.get("mode_paiement") or "Espèces").strip()
        except (TypeError, ValueError, InvalidOperation):
            return {"success": False, "message": "Données d'encaissement invalides."}

        if not login or not password:
            return {"success": False, "message": "Identifiant et mot de passe requis."}
        if not montant.is_finite() or montant <= 0:
            return {"success": False, "message": "Le montant doit être supérieur à zéro."}
        if mode_paiement not in {"Espèces", "Carte CIB / Edahabia", "Chèque", "Prise en Charge (PEC)", "Autre"}:
            return {"success": False, "message": "Mode de paiement invalide."}
        if login != str(session_user.get("login") or "").strip().lower():
            return {"success": False, "message": "La confirmation doit correspondre à l'utilisateur connecté."}

        from modules.gestion_caisse import CaisseManager
        CaisseManager.ensure_caisse_schema()

        pwd_hash = hash_password(password)

        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT id, login, role, status FROM users WHERE id = %s AND login = %s AND password_hash = %s;",
                (session_user["id"], login, pwd_hash)
            )
            user = cursor.fetchone()
            if not user or user["status"] != "Actif":
                return {"success": False, "message": "Authentification refusée ou compte inactif."}

            cursor.execute(
                "SELECT id, montant_total, remise, versement, reste, statut_paiement FROM patient_passages WHERE id = %s FOR UPDATE;",
                (passage_id,)
            )
            passage = cursor.fetchone()
            if not passage:
                return {"success": False, "message": "Passage introuvable."}
            if passage.get("statut_paiement") == "Annulé":
                return {"success": False, "message": "Un passage annulé ne peut pas recevoir de paiement."}

            total = Decimal(str(passage["montant_total"] or 0))
            remise = Decimal(str(passage["remise"] or 0))
            net = max(Decimal("0"), total - remise)
            ancien_versement = Decimal(str(passage["versement"] or 0))
            reste_actuel = max(Decimal("0"), net - ancien_versement)

            if reste_actuel <= Decimal("0"):
                return {"success": False, "message": "Ce passage est déjà soldé."}
            if montant > reste_actuel:
                return {"success": False, "message": f"Le montant maximal est de {reste_actuel:.2f} DA."}

            nouveau_versement = ancien_versement + montant
            nouveau_reste = max(Decimal("0"), net - nouveau_versement)
            nouveau_statut = "Payé" if nouveau_reste == Decimal("0") else "Partiel"
            movement_type = (
                "ENCAISSEMENT_RESTE" if ancien_versement > 0
                else ("ENCAISSEMENT_TOTAL" if nouveau_versement >= net - Decimal("0.01") else "ENCAISSEMENT_PARTIEL")
            )

            cursor.execute(
                "UPDATE patient_passages SET versement = %s, reste = %s, mode_paiement = %s, statut_paiement = %s WHERE id = %s;",
                (nouveau_versement, nouveau_reste, mode_paiement, nouveau_statut, passage_id)
            )
            now_dt = datetime.now()
            cursor.execute("""
                INSERT INTO caisse_mouvements
                    (id_passage, type_mouvement, montant, mode_paiement, date_mouvement,
                     heure_mouvement, utilisateur, motif)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
            """, (passage_id, movement_type, montant, mode_paiement, now_dt.date(), now_dt.strftime("%H:%M:%S"),
                  login, "Reste encaissé depuis Accueil & Passages" if ancien_versement > 0 else "Premier encaissement depuis Accueil & Passages"))

        return {
            "success": True,
            "message": "Encaissement validé avec succès !",
            "id_passage": passage_id,
            "versement": float(nouveau_versement),
            "reste": float(nouveau_reste),
            "statut_paiement": nouveau_statut,
        }

    def delete(self, pass_id: int) -> Dict[str, Any]:
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM patient_passages WHERE id = %s;", (pass_id,))
        return {"success": True, "message": "Passage supprimé."}

    def magic_delete(self, pass_id: int) -> Dict[str, Any]:
        """Supprime le passage et toutes ses données liées, avec ses mouvements de caisse."""
        files_to_remove = set()
        scanned_document_hashes = set()
        counts = {}
        with get_db_cursor() as cursor:
            cursor.execute("SELECT id, id_patient, documents_scannes FROM patient_passages WHERE id = %s FOR UPDATE;", (pass_id,))
            passage = cursor.fetchone()
            if not passage:
                raise HTTPException(status_code=404, detail="Passage introuvable.")

            raw_documents = passage.get("documents_scannes") or []
            if isinstance(raw_documents, str):
                try:
                    raw_documents = json.loads(raw_documents)
                except (TypeError, ValueError):
                    raw_documents = []
            for document in raw_documents if isinstance(raw_documents, list) else []:
                data_uri = str(document.get("data") or "") if isinstance(document, dict) else ""
                if data_uri.startswith("data:") and "," in data_uri:
                    try:
                        encoded = data_uri.split(",", 1)[1]
                        scanned_document_hashes.add(hashlib.sha256(base64.b64decode(encoded)).hexdigest())
                    except Exception:
                        logger.warning("Document scanné illisible pendant la suppression du passage #%s", pass_id)

            cursor.execute("SELECT chemin_docx FROM patient_cr_records WHERE id_passage = %s;", (pass_id,))
            for row in cursor.fetchall():
                file_path = str(row.get("chemin_docx") or "").strip()
                if file_path:
                    files_to_remove.add(file_path)
                    if file_path.lower().endswith(".docx"):
                        files_to_remove.add(file_path[:-5] + ".pdf")

            cursor.execute("SELECT chemin_image FROM patient_echographies WHERE id_passage = %s;", (pass_id,))
            files_to_remove.update(str(row.get("chemin_image") or "").strip() for row in cursor.fetchall())

            # These records are not consistently declared with cascading foreign keys in older schemas.
            cursor.execute("DELETE FROM patient_prescriptions WHERE id_passage = %s;", (pass_id,))
            counts["ordonnances"] = cursor.rowcount
            cursor.execute("DELETE FROM patient_dme_records WHERE id_passage = %s;", (pass_id,))
            counts["consultations"] = cursor.rowcount
            cursor.execute("DELETE FROM patient_cr_records WHERE id_passage = %s;", (pass_id,))
            counts["documents"] = cursor.rowcount
            cursor.execute("DELETE FROM patient_echographies WHERE id_passage = %s;", (pass_id,))
            counts["echographies"] = cursor.rowcount
            cursor.execute("DELETE FROM appointments WHERE id_passage = %s;", (pass_id,))
            counts["rendez_vous"] = cursor.rowcount

            cursor.execute("DELETE FROM caisse_mouvements WHERE id_passage = %s;", (pass_id,))
            counts["mouvements_caisse"] = cursor.rowcount
            cursor.execute("DELETE FROM patient_passages WHERE id = %s;", (pass_id,))
            if cursor.rowcount != 1:
                raise HTTPException(status_code=409, detail="Le passage n'a pas pu être supprimé.")

        # Remove only generated files that resolve inside Cliniva's uploads directory.
        uploads_root = (Path(__file__).resolve().parent.parent / "uploads").resolve()
        for raw_path in files_to_remove:
            if not raw_path:
                continue
            try:
                candidate = Path(raw_path)
                if not candidate.is_absolute():
                    candidate = Path(__file__).resolve().parent.parent / candidate
                candidate = candidate.resolve()
                if uploads_root in candidate.parents and candidate.is_file():
                    candidate.unlink()
            except OSError:
                logger.exception("Impossible de supprimer un fichier lié au passage #%s : %s", pass_id, raw_path)
        if scanned_document_hashes:
            docs_root = Path(DOCS_DIR).resolve()
            for candidate in docs_root.glob("passage_doc_*"):
                try:
                    resolved = candidate.resolve()
                    if resolved.parent == docs_root and resolved.is_file():
                        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
                        if digest in scanned_document_hashes:
                            resolved.unlink()
                except OSError:
                    logger.exception("Impossible de supprimer une copie de document du passage #%s", pass_id)

        return {
            "success": True,
            "message": f"Passage #{pass_id} et toutes ses données liées ont été supprimés.",
            "details": counts,
        }

    def get_initial_data(self) -> Dict[str, Any]:
        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT id, nom, prenom, telephone_1, date_naissance, groupe_sanguin, avatar FROM patients ORDER BY nom ASC;")
            patients = [dict(r) for r in cursor.fetchall()]

            cursor.execute(
                "SELECT id, designation, categorie, type_consultation, transmettre_hl7, modalite, tarif, tarif_nuit, specialite FROM consultation_types ORDER BY designation ASC;")
            consultations = [dict(r) for r in cursor.fetchall()]

            cursor.execute("SELECT nom FROM ref_modalites ORDER BY nom ASC;")
            modalites = [str(r.get("nom") or "").strip() for r in cursor.fetchall() if str(r.get("nom") or "").strip()]

            cursor.execute(
                "SELECT id, grade, nom, prenom, telephone, email, adresse, wilaya, commune, specialite FROM treating_doctors ORDER BY nom ASC;")
            medecins_traitants = [dict(r) for r in cursor.fetchall()]

            cursor.execute(
                "SELECT nom_raison_sociale, telephone, adresse, horaires_tarification FROM clinic_info WHERE id = 1;")
            row_clinic = cursor.fetchone()

        clinic_name = "Cliniva Medical Center"
        clinic_tel = "+213 (0) 23 45 67 89"
        clinic_adresse = "Alger, Algérie"
        horaires = {}

        if row_clinic:
            clinic_name = row_clinic.get("nom_raison_sociale") or clinic_name
            clinic_tel = row_clinic.get("telephone") or clinic_tel
            clinic_adresse = row_clinic.get("adresse") or clinic_adresse
            h_raw = row_clinic.get("horaires_tarification")
            if isinstance(h_raw, dict):
                horaires = h_raw
            elif isinstance(h_raw, str):
                try:
                    horaires = json.loads(h_raw)
                except Exception:
                    horaires = {}

        return {
            "patients": patients,
            "medecins_traitants": medecins_traitants,
            "consultations": consultations,
            "modalites": modalites,
            "types_passages": TYPES_PASSAGES,
            "doc_types": DOCS_PASSAGE_TYPES,
            "horaires_tarification": horaires,
            "clinic_name": clinic_name,
            "clinic_tel": clinic_tel,
            "clinic_adresse": clinic_adresse,
        }


manager = ReceptionManager()


# ============================================================================
# POINTS D'ACCÈS FASTAPI (ROUTES JSON & TICKET)
# ============================================================================
@reception_router.get("/passages")
async def api_get_passages(request: Request, service: Optional[str] = None):
    try:
        from modules.gestion_caisse import CaisseManager
        CaisseManager.ensure_caisse_schema()
        user = get_current_user_optional(request)
        user_permissions = user.get("permissions") or {} if user else {}
        if isinstance(user_permissions, str):
            try:
                user_permissions = json.loads(user_permissions)
            except (TypeError, ValueError):
                user_permissions = {}
        can_edit_patient_info = bool(
            user and user.get("status") == "Actif"
            and (user.get("role") == "Administrateur"
                 or (isinstance(user_permissions, dict) and user_permissions.get("pat_edit")))
        )
        can_magic_delete = bool(
            user
            and user.get("status") == "Actif"
            and user.get("role") == "Administrateur"
            and str(user.get("login") or "").strip().upper() == "SYSTEM"
        )
        user_specs = []

        if user and user.get("id"):
            with get_db_cursor() as cursor:
                cursor.execute(
                    "SELECT specialites_autorisees FROM users WHERE id = %s;",
                    (user["id"],)
                )
                row = cursor.fetchone()
                if row and row.get("specialites_autorisees"):
                    raw = row["specialites_autorisees"]
                    if isinstance(raw, str):
                        try:
                            raw = json.loads(raw)
                        except Exception:
                            raw = []
                    if isinstance(raw, list):
                        user_specs = [str(s).strip() for s in raw if str(s).strip()]

        user_specs = sorted(list(set(user_specs)))

        passages = manager.get_all()
        init_data = manager.get_initial_data()
        if str(service or "").strip().lower() in {"radiologie", "autres"}:
            consultations_by_name = {
                str(item.get("designation") or "").strip().casefold(): item
                for item in init_data.get("consultations", [])
            }
            radiology_only = str(service).strip().lower() == "radiologie"
            passages = [
                passage for passage in passages
                if passage_has_radiology_exam(passage, consultations_by_name) == radiology_only
            ]
            if radiology_only:
                for passage in passages:
                    modalities = {
                        str(consultations_by_name.get(str(act.get("nom") or "").strip().casefold(), {}).get("modalite") or "").strip()
                        for act in (passage.get("actes") or [])
                        if is_radiology_category(consultations_by_name.get(str(act.get("nom") or "").strip().casefold(), {}).get("categorie"))
                    }
                    passage["modalites"] = sorted(mod for mod in modalities if mod)
                init_data["radiology_modalites"] = sorted(
                    set(init_data.get("modalites") or []), key=str.casefold
                )
        total = len(passages) if service else manager.count()
        init_data["user_specialites"] = user_specs

        return {
            "passages": passages,
            "total_count": total,
            "init_data": init_data,
            "user_specialites": user_specs,
            "can_magic_delete": can_magic_delete,
            "can_edit_patient_info": can_edit_patient_info,
        }
    except Exception as exc:
        logger.error("Erreur GET /api/reception/passages : %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@reception_router.post("/patient/update-from-passage")
async def api_update_patient_from_passage(payload: Dict[str, Any], request: Request):
    """Modifie les coordonnées du patient depuis un passage autorisé."""
    user = _require_reception_permission(request, "pat_edit")
    try:
        passage_id = int(payload.get("id_passage"))
        patient_id = int(payload.get("id_patient"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Identifiants patient ou passage invalides.")

    nom = str(payload.get("nom") or "").strip().upper()
    prenom = str(payload.get("prenom") or "").strip().title()
    date_naissance = str(payload.get("date_naissance") or "").strip()
    genre = str(payload.get("genre") or "").strip()
    groupe_sanguin = str(payload.get("groupe_sanguin") or "").strip()
    type_piece_identite = str(payload.get("type_piece_identite") or "Carte Chifa").strip()
    doc_identite = payload.get("doc_identite") or ""
    avatar = payload.get("avatar") or ""
    photo_face = payload.get("photo_face") or ""
    photo_profil_droit = payload.get("photo_profil_droit") or ""
    photo_profil_gauche = payload.get("photo_profil_gauche") or ""
    empreinte_index = payload.get("empreinte_index") or ""
    pieces_scannees = payload.get("pieces_scannees") or []
    telephone_1 = str(payload.get("telephone_1") or "").strip()
    telephone_2 = str(payload.get("telephone_2") or "").strip()
    email = str(payload.get("email") or "").strip()
    wilaya = str(payload.get("wilaya") or "").strip()
    commune = str(payload.get("commune") or "").strip()
    questionnaire = payload.get("questionnaire") or {}
    documents_scannes = payload.get("documents_scannes") or []
    actes = payload.get("actes") or []
    date_passage = str(payload.get("date_passage") or "").strip()
    heure_passage = str(payload.get("heure_passage") or "").strip()
    type_passage = str(payload.get("type_passage") or "").strip()
    try:
        medecin_traitant_id = int(payload.get("medecin_traitant_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Médecin traitant invalide.")

    if not all((nom, prenom, date_naissance, genre, groupe_sanguin, telephone_1, wilaya, commune)):
        raise HTTPException(status_code=400, detail="Nom, prénom, date de naissance, genre, groupe sanguin, téléphone, wilaya et commune sont obligatoires.")
    if len(nom) > 100 or len(prenom) > 100 or len(telephone_1) > 30 or len(telephone_2) > 30 or len(email) > 254 or len(wilaya) > 100 or len(commune) > 100:
        raise HTTPException(status_code=400, detail="Une ou plusieurs informations dépassent la longueur autorisée.")
    if genre not in {"Homme", "Femme", "Autre"}:
        raise HTTPException(status_code=400, detail="Genre invalide.")
    if groupe_sanguin not in set(GROUPES_SANGUINS):
        raise HTTPException(status_code=400, detail="Groupe sanguin invalide.")
    if type_piece_identite not in TYPES_PIECES_IDENTITE:
        raise HTTPException(status_code=400, detail="Type de pièce d’identité invalide.")
    if not isinstance(questionnaire, dict) or not isinstance(actes, list) or not actes or len(actes) > 100 or not isinstance(documents_scannes, list) or len(documents_scannes) > 30 or not isinstance(pieces_scannees, list) or len(pieces_scannees) > 30:
        raise HTTPException(status_code=400, detail="Informations médicales ou documents invalides.")
    try:
        date.fromisoformat(date_passage)
        datetime.strptime(heure_passage, "%H:%M")
    except ValueError:
        raise HTTPException(status_code=400, detail="Date ou heure du passage invalide.")
    if not type_passage:
        raise HTTPException(status_code=400, detail="La discipline ou le type de passage est obligatoire.")
    patient_files = [doc_identite, avatar, photo_face, photo_profil_droit, photo_profil_gauche, empreinte_index]
    if any(not isinstance(value, str) or len(value) > 18_000_000 for value in patient_files):
        raise HTTPException(status_code=413, detail="Une photo ou pièce d’identité dépasse la taille autorisée.")
    if any(not isinstance(doc, dict) or len(str(doc.get("data") or "")) > 18_000_000 for doc in documents_scannes + pieces_scannees):
        raise HTTPException(status_code=413, detail="Un document dépasse la taille autorisée.")
    try:
        date.fromisoformat(date_naissance)
    except ValueError:
        raise HTTPException(status_code=400, detail="Date de naissance invalide.")

    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT p.id_patient, p.versement, p.remise_type, p.remise_valeur,
                   COALESCE(p.statut, '') AS statut,
                   COALESCE(p.statut_paiement, 'Non payé') AS statut_paiement,
                   EXISTS (SELECT 1 FROM caisse_mouvements cm WHERE cm.id_passage = p.id) AS has_cash_history,
                   EXISTS (SELECT 1 FROM patient_dme_records d WHERE d.id_passage = p.id) AS has_consultation
            FROM patient_passages p
            WHERE p.id = %s
            FOR UPDATE OF p;
        """, (passage_id,))
        passage = cursor.fetchone()
        if not passage or int(passage.get("id_patient") or 0) != patient_id:
            raise HTTPException(status_code=404, detail="Le patient n'est pas associé à ce passage.")
        if passage.get("has_cash_history") or float(passage.get("versement") or 0) > 0 or passage.get("statut_paiement") != "Non payé":
            raise HTTPException(status_code=409, detail="La fiche ne peut plus être modifiée : un encaissement ou un mouvement de caisse existe déjà.")
        if passage.get("has_consultation") or passage.get("statut") != "En attente":
            raise HTTPException(status_code=409, detail="La fiche ne peut plus être modifiée : la consultation DME a commencé ou le passage a avancé.")
        act_totals = []
        try:
            for act in actes:
                if not isinstance(act, dict):
                    raise ValueError("prestation invalide")
                raw_total = act.get("sous_total")
                if raw_total is None:
                    raw_total = float(act.get("tarif") or 0) * int(act.get("quantite") or 1)
                act_total = float(raw_total)
                if not math.isfinite(act_total) or act_total < 0:
                    raise ValueError("montant invalide")
                act_totals.append(act_total)
            montant_total_corrige = round(sum(act_totals), 2)
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(status_code=400, detail="Une prestation contient un tarif ou une quantité invalide.")
        remise_type_existante = str(passage.get("remise_type") or "DA")
        remise_valeur_existante = float(passage.get("remise_valeur") or 0)
        remise_corrigee = (montant_total_corrige * remise_valeur_existante / 100) if remise_type_existante == "%" else remise_valeur_existante
        remise_corrigee = round(min(montant_total_corrige, max(0, remise_corrigee)), 2)
        reste_corrige = round(max(0, montant_total_corrige - remise_corrigee), 2)
        cursor.execute("""
            UPDATE patients
            SET nom = %s, prenom = %s, date_naissance = %s, genre = %s,
                groupe_sanguin = %s, telephone_1 = %s, telephone_2 = %s,
                email = %s, wilaya = %s, commune = %s, type_piece_identite = %s,
                doc_identite = %s, avatar = %s, photo_face = %s, photo_profil_droit = %s,
                photo_profil_gauche = %s, empreinte_index = %s, pieces_scannees = %s::jsonb
            WHERE id = %s;
        """, (nom, prenom, date_naissance, genre, groupe_sanguin, telephone_1, telephone_2, email, wilaya, commune,
              type_piece_identite, doc_identite, avatar, photo_face, photo_profil_droit, photo_profil_gauche, empreinte_index,
              json.dumps(pieces_scannees), patient_id))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Patient introuvable.")
        cursor.execute("""
            UPDATE patient_passages
            SET date_passage = %s, heure_passage = %s, type_passage = %s,
                medecin_traitant_id = %s, actes_json = %s::jsonb,
                montant_total = %s, remise = %s, reste = %s,
                questionnaire_json = %s::jsonb, documents_scannes = %s::jsonb
            WHERE id = %s AND id_patient = %s;
        """, (date_passage, heure_passage, type_passage, medecin_traitant_id, json.dumps(actes),
              montant_total_corrige, remise_corrigee, reste_corrige,
              json.dumps(questionnaire), json.dumps(documents_scannes), passage_id, patient_id))

    track_action(request, "MODIFICATION_DOSSIER_AVANT_CONSULTATION", f"Fiche patient et informations médicales du passage #{passage_id} mises à jour")
    return {"success": True, "message": "Informations du patient et du passage mises à jour.", "patient_id": patient_id, "id_passage": passage_id}


@reception_router.get("/patient/edit-data/{passage_id}")
async def api_get_patient_edit_data(passage_id: int, request: Request):
    _require_reception_permission(request, "pat_edit")
    with get_db_cursor() as cursor:
        cursor.execute("""
            SELECT p.id_patient, p.questionnaire_json, p.documents_scannes,
                   COALESCE(p.statut, '') AS statut, COALESCE(p.statut_paiement, 'Non payé') AS statut_paiement,
                   p.versement,
                   pat.type_piece_identite, pat.doc_identite, pat.avatar, pat.photo_face,
                   pat.photo_profil_droit, pat.photo_profil_gauche, pat.empreinte_index, pat.pieces_scannees,
                   EXISTS (SELECT 1 FROM caisse_mouvements cm WHERE cm.id_passage = p.id) AS has_cash_history,
                   EXISTS (SELECT 1 FROM patient_dme_records d WHERE d.id_passage = p.id) AS has_consultation
            FROM patient_passages p JOIN patients pat ON pat.id = p.id_patient
            WHERE p.id = %s;
        """, (passage_id,))
        row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Passage introuvable.")
    if (row.get("has_cash_history") or float(row.get("versement") or 0) > 0
            or row.get("statut_paiement") != "Non payé" or row.get("has_consultation")
            or row.get("statut") != "En attente"):
        raise HTTPException(status_code=409, detail="La fiche ne peut plus être modifiée : encaissement ou consultation déjà commencé.")
    result = dict(row)
    for key, default, expected in (("questionnaire_json", {}, dict), ("documents_scannes", [], list), ("pieces_scannees", [], list)):
        value = result.get(key)
        if not isinstance(value, expected):
            try:
                value = json.loads(value or ("{}" if expected is dict else "[]"))
            except (TypeError, ValueError):
                value = default
        result[key.replace("_json", "")] = value
    return result


@reception_router.post("/save")
async def api_save_passage(payload: Dict[str, Any], request: Request):
    user = _require_reception_permission(request, "reception_create")
    try:
        versement_initial = float(payload.get("versement") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Montant de versement invalide.")
    if not math.isfinite(versement_initial) or versement_initial < 0:
        raise HTTPException(status_code=400, detail="Montant de versement invalide.")
    try:
        remise_demandee = float(payload.get("remise_valeur") or 0)
    except (TypeError, ValueError):
        remise_demandee = 0
    permissions = user.get("permissions") or {}
    if isinstance(permissions, str):
        try:
            permissions = json.loads(permissions)
        except (TypeError, ValueError):
            permissions = {}
    if remise_demandee > 0 and (not isinstance(permissions, dict) or not permissions.get("cash_remise")):
        raise HTTPException(status_code=403, detail="Le droit d'accorder une remise est requis.")
    if versement_initial > 0 and (not isinstance(permissions, dict) or not permissions.get("cash_collect")):
        raise HTTPException(status_code=403, detail="Vous n'avez pas le droit d'effectuer cette opération financière.")
    if versement_initial > 0:
        from modules.gestion_caisse import CaisseManager
        CaisseManager.ensure_caisse_schema()
    payload["utilisateur"] = user.get("login") or ""
    res = manager.save(payload)
    if res.get("success"):
        p_id = res.get("id_passage")
        track_action(request, "ENCAISSEMENT", f"Passage et encaissement enregistrés (#{p_id})")
    return res


@reception_router.post("/encaisser")
async def api_encaisser_passage(payload: Dict[str, Any], request: Request):
    try:
        pass_id = int(payload.get("id_passage"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Identifiant de passage invalide.")
    with get_db_cursor() as cursor:
        cursor.execute("SELECT versement FROM patient_passages WHERE id = %s;", (pass_id,))
        payment_row = cursor.fetchone()
    if not payment_row:
        raise HTTPException(status_code=404, detail="Passage introuvable.")
    user = _require_reception_permission(
        request,
        "cash_collect_rest" if float(payment_row.get("versement") or 0) > 0 else "cash_collect",
    )
    res = manager.encaisser(payload, user)
    if res.get("success"):
        track_action(request, "ENCAISSEMENT_SOLDE", f"Reste encaissé pour le passage #{payload.get('id_passage')}")
    return res


@reception_router.post("/delete")
async def api_delete_passage(payload: Dict[str, Any], request: Request):
    _require_reception_permission(request, "reception_edit")
    if payload.get("code_suppression") != "AdminRak1458":
        raise HTTPException(status_code=403, detail="Code de suppression incorrect.")
    p_id = payload.get("id")
    try:
        result = manager.delete(p_id)
    except Exception as exc:
        if "foreign key" in str(exc).lower() or "restrict" in str(exc).lower():
            raise HTTPException(status_code=409, detail="Ce passage possède un historique financier; sa suppression est bloquée.") from exc
        raise
    if result.get("success"):
        track_action(request, "SUPPRESSION", f"Suppression du passage #{p_id}")
    return result


@reception_router.post("/magic-delete")
async def api_magic_delete_passage(payload: Dict[str, Any], request: Request):
    user = _require_reception_permission(request, "reception_edit")
    if user.get("role") != "Administrateur" or str(user.get("login") or "").strip().upper() != "SYSTEM":
        raise HTTPException(status_code=403, detail="Cette option est réservée au compte administrateur SYSTEM.")

    try:
        pass_id = int(payload.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Identifiant de passage invalide.")
    if payload.get("confirmation") != f"SUPPRIMER #PASS-{pass_id:04d}":
        raise HTTPException(status_code=400, detail="Confirmation de suppression incorrecte.")
    password = str(payload.get("password") or "")
    if not password:
        raise HTTPException(status_code=400, detail="Mot de passe administrateur requis.")

    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM users WHERE id = %s AND UPPER(login) = 'SYSTEM' AND role = 'Administrateur' "
            "AND status = 'Actif' AND password_hash = %s;",
            (user["id"], hash_password(password)),
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=403, detail="Mot de passe SYSTEM incorrect.")

    from modules.gestion_caisse import CaisseManager
    CaisseManager.ensure_caisse_schema()
    from modules.gestion_dme import dme_manager
    dme_manager.ensure_dme_schema()
    result = manager.magic_delete(pass_id)
    try:
        track_action(
            request,
            "SUPPRESSION_MAGIC_PASSAGE_CAISSE",
            f"Compte SYSTEM : suppression du passage #{pass_id}, données DME, documents et mouvements de caisse.",
        )
    except Exception:
        logger.exception("Échec de journalisation de la suppression magique du passage #%s", pass_id)
    return result


@reception_router.post("/treating-doctor/add")
async def api_add_treating_doctor_from_reception(payload: Dict[str, Any]):
    from modules.gestion_medecins_traitants import manager as treating_manager
    return treating_manager.save(payload)


@reception_router.get("/ticket/{passage_id}", response_class=HTMLResponse)
async def view_standalone_ticket(passage_id: int, request: Request):
    _require_reception_permission(request, "cash_collect")
    passage = manager.get_passage_by_id(passage_id)
    if not passage:
        raise HTTPException(status_code=404, detail="Ticket introuvable.")

    init_data = manager.get_initial_data()
    return render_ticket_html(passage, init_data)


def render_ticket_html(p: Dict[str, Any], clinic: Dict[str, Any], movement: Optional[Dict[str, Any]] = None) -> str:
    """Génération du ticket 80mm standard."""
    if movement:
        from modules.prestation_categories import CATEGORY_KEYS, classify_prestation

        acts = movement.get("actes") or []
        allocations = movement.get("allocations") or {"principal": movement.get("montant_mouvement", 0)}
        categories = {
            "principal": "TICKET PRINCIPAL — PRESTATIONS",
            "contraste": "TICKET SPÉCIAL — PRODUITS DE CONTRASTE",
            "sedation": "TICKET SPÉCIAL — SÉDATION",
        }
        grouped = {key: [] for key in CATEGORY_KEYS}
        for act in acts:
            if isinstance(act, dict):
                grouped[classify_prestation(act)].append(act)
        clinic_name = html_escape(str(clinic.get("clinic_name") or "Cliniva Medical Center"))
        clinic_address = html_escape(str(clinic.get("clinic_adresse") or ""))
        clinic_phone = html_escape(str(clinic.get("clinic_tel") or ""))
        patient_name = html_escape(f'{movement.get("nom") or "Patient"} {movement.get("prenom") or ""}'.strip())
        movement_id = int(movement.get("id_mouvement") or 0)
        passage_id = int(movement.get("id_passage") or 0)
        sign_label = "REMBOURSÉ" if float(movement.get("montant_mouvement") or 0) < 0 else "MONTANT ENCAISSÉ"
        tickets = []
        for category in CATEGORY_KEYS:
            category_amount = float(allocations.get(category) or 0)
            if abs(category_amount) < 0.005:
                continue
            lines = "".join(
                f'<div style="margin:2px 0;">• {html_escape(str(act.get("nom") or act.get("designation") or "Prestation"))} × {html_escape(str(act.get("quantite") or 1))}</div>'
                for act in grouped[category]
            ) or '<div>Aucune prestation détaillée</div>'
            amount_text = f'{"-" if category_amount < 0 else ""}{abs(category_amount):,.2f} DA'
            tickets.append(f"""
              <section class="ticket">
                <div class="center bold">{clinic_name}</div>
                <div class="center small">{clinic_address} | Tél: {clinic_phone}</div>
                <div class="line"></div><div class="center bold">{categories[category]}</div>
                <div class="center small">{html_escape(str(movement.get("type_mouvement") or ""))}</div>
                <div class="row"><span>Passage</span><strong>#PASS-{passage_id:04d}</strong></div>
                <div class="row"><span>Mouvement</span><strong>#{movement_id}</strong></div>
                <div class="row"><span>Date</span><strong>{html_escape(str(movement.get("date_mouvement") or ""))} {html_escape(str(movement.get("heure_mouvement") or ""))}</strong></div>
                <div>Patient : <strong>{patient_name}</strong></div>
                <div>Caissier : @{html_escape(str(movement.get("operateur_mouvement") or "-"))}</div>
                <div class="line"></div><strong>PRESTATIONS :</strong><div>{lines}</div>
                <div class="line"></div>
                <div class="row"><span>Mode de paiement</span><strong>{html_escape(str(movement.get("mode_mouvement") or "Espèces"))}</strong></div>
                <div class="row bold"><span>{sign_label}</span><span>{amount_text}</span></div>
                <div class="line"></div><div class="center small">Merci pour votre confiance.</div>
              </section>
            """)
        return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8"><title>Tickets #{passage_id}</title>
        <style>
          @page {{ size:80mm auto; margin:0; }} * {{ box-sizing:border-box; }}
          body {{ width:78mm; margin:0; padding:10px; color:#000; font:11px/1.3 'Courier New',monospace; }}
          .center {{ text-align:center; }} .bold {{ font-weight:bold; }} .small {{ font-size:9px; }}
          .line {{ border-top:1px dashed #000; margin:7px 0; }}
          .row {{ display:flex; justify-content:space-between; gap:6px; margin:3px 0; }}
          .ticket {{ padding-bottom:10px; }}
          @media print {{ .no-print {{ display:none!important; }} .ticket:not(:last-child) {{ page-break-after:always; }} }}
        </style></head><body><div class="no-print center"><button onclick="window.print()">Imprimer les tickets</button></div>
        {''.join(tickets)}</body></html>"""

        movement_type = movement.get("type_mouvement")
        labels = {
            "ENCAISSEMENT_RESTE": "ENCAISSEMENT DU RESTE",
            "REGULARISATION": "ENCAISSEMENT DU RESTE",
            "ENCAISSEMENT_TOTAL": "ENCAISSEMENT TOTAL",
            "ENCAISSEMENT_PARTIEL": "ENCAISSEMENT PARTIEL",
            "ENCAISSEMENT": "ENCAISSEMENT TOTAL",
            "ANNULATION": "REMBOURSEMENT / ANNULATION",
        }
        label = labels.get(movement_type, "MOUVEMENT DE CAISSE")
        acts = movement.get("actes") or []
        acts_html = "".join(
            f'<div style="margin:2px 0;">• {html_escape(str(a.get("nom") or "Prestation"))}</div>'
            for a in acts if isinstance(a, dict)
        ) or '<div style="color:#555;">Aucune prestation renseignée</div>'
        amount = float(movement.get("montant_mouvement") or 0)
        sign = "-" if amount < 0 else ""
        clinic_name = html_escape(str(clinic.get("clinic_name") or "Cliniva Medical Center"))
        clinic_address = html_escape(str(clinic.get("clinic_adresse") or ""))
        clinic_phone = html_escape(str(clinic.get("clinic_tel") or ""))
        patient_name = html_escape(f'{movement.get("nom") or "Patient"} {movement.get("prenom") or ""}'.strip())
        payment_mode = html_escape(str(movement.get("mode_mouvement") or "Espèces"))
        operator = html_escape(str(movement.get("operateur_mouvement") or "-"))
        passage_id = int(movement.get("id_passage") or 0)
        amount_text = f"{sign}{abs(amount):,.2f} DA"
        remaining_html = ""
        if movement_type in {"ENCAISSEMENT_PARTIEL", "ENCAISSEMENT_RESTE", "REGULARISATION"}:
            remaining_amount = float(movement.get("reste") or 0)
            remaining_html = f'<div class="row"><span>Reste dû après encaissement :</span><strong>{remaining_amount:,.2f} DA</strong></div>'
        return f"""
        <!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
        <title>Ticket {label} #{passage_id}</title>
        <style>
            @page {{ size: 80mm auto; margin: 0; }}
            * {{ box-sizing: border-box; }}
            body {{ margin:0; padding:10px; width:78mm; font-family:'Courier New',monospace; color:#000; font-size:11px; line-height:1.3; }}
            .line {{ border-top:1px dashed #000; margin:7px 0; }}
            .row {{ display:flex; justify-content:space-between; gap:6px; margin:3px 0; }}
            @media print {{ .no-print {{ display:none !important; }} }}
        </style></head><body>
          <div class="no-print" style="text-align:center;margin-bottom:8px;"><button onclick="window.print()">Imprimer</button></div>
          <div style="text-align:center;font-weight:bold;text-transform:uppercase;">{clinic_name}</div>
          <div style="text-align:center;font-size:9px;">{clinic_address} | Tél: {clinic_phone}</div>
          <div class="line"></div>
          <div style="text-align:center;font-weight:bold;font-size:13px;">{label}</div>
          <div class="row"><span>Passage :</span><strong>#PASS-{passage_id:04d}</strong></div>
          <div class="row"><span>Référence mouvement :</span><strong>#{int(movement.get('id_mouvement') or 0)}</strong></div>
          <div class="row"><span>Date :</span><strong>{html_escape(str(movement.get('date_mouvement') or ''))} {html_escape(str(movement.get('heure_mouvement') or ''))}</strong></div>
          <div>Patient : <strong>{patient_name}</strong></div>
          <div>Caissier : @{operator}</div>
          {remaining_html}
          <div class="line"></div>
          <strong>PRESTATIONS CONCERNÉES :</strong>
          <div style="margin-top:4px;">{acts_html}</div>
          <div class="line"></div>
          <div class="row"><span>Mode de paiement :</span><strong>{payment_mode}</strong></div>
          <div class="row" style="font-size:13px;font-weight:bold;"><span>{'REMBOURSÉ' if amount < 0 else 'MONTANT ENCAISSÉ'} :</span><span>{amount_text}</span></div>
          <div class="line"></div>
          <div style="text-align:center;font-size:9px;font-style:italic;">Merci pour votre confiance.</div>
        </body></html>
        """

    actes_html = ""
    for a in p.get("actes", []):
        nom = a.get("nom", "Prestation")
        qte = a.get("quantite", 1)
        st = a.get("sous_total", 0.0)
        actes_html += f"""
        <div style="display:flex; justify-content:space-between; margin-bottom:2px;">
            <span>{qte}x {nom}</span>
            <span style="font-weight:bold;">{st:,.2f} DA</span>
        </div>
        """

    remise_html = ""
    remise_val = float(p.get("remise") or 0)
    if remise_val > 0:
        remise_type = p.get("remise_type", "DA")
        remise_num = p.get("remise_valeur", 0)
        pct_label = f"(-{remise_num}%)" if remise_type == "%" else ""
        remise_html = f"""
        <div style="display:flex; justify-content:space-between; margin-bottom:2px;">
            <span>REMISE {pct_label} :</span>
            <strong>-{remise_val:,.2f} DA</strong>
        </div>
        """

    net = max(0.0, float(p.get("montant_total") or 0) - remise_val)
    versement_actuel = float(p.get("versement") or 0)
    type_encaissement = (
        "ENCAISSEMENT TOTAL" if versement_actuel >= net - 0.01 and versement_actuel > 0
        else ("ENCAISSEMENT PARTIEL" if versement_actuel > 0 else "FACTURE — NON ENCAISSÉE")
    )

    return f"""
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="UTF-8">
        <title>Ticket de Caisse #{p['id_passage']}</title>
        <style>
            @page {{ size: 80mm auto; margin: 0; }}
            * {{ box-sizing: border-box; }}
            body {{
                margin: 0; padding: 10px; font-family: 'Courier New', Courier, monospace;
                width: 78mm; background: white; color: black; font-size: 11px; line-height: 1.25;
            }}
            .dashed-line {{ border-top: 1px dashed black; margin: 6px 0; }}
            .solid-line {{ border-top: 1px solid black; margin: 6px 0; }}
            .flex-between {{ display: flex; justify-content: space-between; margin-bottom: 3px; }}
            @media print {{ .no-print {{ display: none !important; }} }}
        </style>
    </head>
    <body>
        <div class="no-print" style="display:flex; gap:6px; justify-content:center; margin-bottom:10px;">
            <button onclick="window.print()" style="background:#121c15; color:#8be140; border:none; padding:6px 12px; font-weight:bold; border-radius:6px; cursor:pointer;">🖨️ Imprimer</button>
        </div>

        <div style="text-align:center; font-weight:bold; font-size:13px; text-transform:uppercase;">
            {clinic.get('clinic_name')}
        </div>
        <div style="text-align:center; font-size:9.5px; color:#444; margin-top:2px;">
            {clinic.get('clinic_adresse')} | Tél: {clinic.get('clinic_tel')}
        </div>

        <div class="dashed-line"></div>

        <div class="flex-between" style="font-size:9.5px;">
            <span>Date: <strong>{p.get('date_passage')} {p.get('heure_passage')}</strong></span>
            <span>N°: <strong>#PASS-{p['id_passage']:04d}</strong></span>
        </div>
        <div style="font-size:11px; margin-top:2px;">
            Patient: <strong style="text-transform:uppercase;">{p.get('nom')} {p.get('prenom')}</strong>
        </div>
        <div style="font-size:9.5px; color:#444;">
            Dossier: #DOS-{p.get('id_patient', 0):04d} | Caissier: @{p.get('utilisateur')}
        </div>
        <div style="text-align:center; font-weight:bold; margin-top:5px;">{type_encaissement}</div>

        <div class="dashed-line"></div>
        <div style="font-weight:bold; margin-bottom:4px;">DÉSIGNATION DES PRESTATIONS :</div>
        {actes_html}
        <div class="dashed-line"></div>

        <div class="flex-between">
            <span>TOTAL BRUT :</span>
            <strong>{float(p.get('montant_total', 0)):,.2f} DA</strong>
        </div>
        {remise_html}

        <div class="solid-line"></div>
        <div class="flex-between" style="font-weight:bold; font-size:12px;">
            <span>NET À PAYER :</span>
            <span>{net:,.2f} DA</span>
        </div>
        <div class="solid-line"></div>

        <div class="flex-between">
            <span>VERSEMENT :</span>
            <strong>{float(p.get('versement', 0)):,.2f} DA</strong>
        </div>
        <div class="flex-between" style="font-weight:bold; font-size:12px; margin-top:2px;">
            <span>RESTE :</span>
            <strong>{float(p.get('reste', 0)):,.2f} DA</strong>
        </div>

        <div class="dashed-line"></div>
        <div style="text-align:center; font-size:9.5px; font-style:italic; margin-top:5px;">
            " Merci pour votre confiance.<br>Prompt rétablissement ! "
        </div>
    </body>
    </html>
    """


# ============================================================================
# RENDU DE LA DEMANDE D'EXAMEN A4 (DEM_MR_Valid.png)
# ============================================================================
@reception_router.get("/demande-examen/{passage_id}", response_class=HTMLResponse)
async def view_demande_examen(passage_id: int):
    """Génère la fiche Demande d'Examen A4 remplie et parfaitement calibrée."""
    passage = manager.get_passage_by_id(passage_id)
    if not passage:
        raise HTTPException(status_code=404, detail="Passage introuvable.")

    img_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads", "model_templates",
                            "DEM_MR_Valid.png")
    img_b64 = ""
    if os.path.exists(img_path):
        import base64
        with open(img_path, "rb") as f:
            img_b64 = f"data:image/png;base64,{base64.b64encode(f.read()).decode('utf-8')}"

    # Données Patient
    nom = (passage.get("nom") or "").upper()
    prenom = (passage.get("prenom") or "").upper()
    dob = passage.get("date_naissance") or "-"
    tel = passage.get("telephone_1") or ""
    if passage.get("telephone_2"):
        tel += f" / {passage.get('telephone_2')}"
    else:
        tel += " /"
    email = (passage.get("email") or "-").upper()

    wilaya = (passage.get("wilaya") or "").upper()
    commune = (passage.get("commune") or "").upper()
    if commune and wilaya and wilaya != "-":
        region = f"{wilaya} - {commune}"
    else:
        region = (wilaya or commune or "-").upper()

    date_p = passage.get("date_passage") or ""
    try:
        dt = datetime.strptime(date_p, "%Y-%m-%d")
        date_formatee = dt.strftime("%d/%m/%Y")
    except Exception:
        date_formatee = date_p
    heure_p = passage.get("heure_passage") or datetime.now().strftime("%H:%M:%S")
    auteur = (passage.get("utilisateur") or "admin").upper()
    date_enregistrement_txt = f"{date_formatee}  {heure_p}   Ajouter Par {auteur}"

    # 1. TYPE D'EXAMEN : FORCÉ À "EXPLORATION"
    type_examen = "EXPLORATION"

    # 2. FILTRAGE : EXCLUSION DU CONTRASTE ET DE LA SÉDATION DANS LE PROTOCOLE
    actes_list = passage.get("actes", [])
    has_contrast = False
    has_sedation = False
    total_pc = 0.0
    total_ps = 0.0
    exam_actes_names = []

    contrast_keywords = ["contrast", "contraste", "iode", "gadolinium", "p.contrast"]
    sedation_keywords = ["sedat", "sédation", "anesth"]

    for a in actes_list:
        a_nom = (a.get("nom") or "").strip()
        a_nom_lower = a_nom.lower()
        sous_total = float(a.get("sous_total") or 0.0)

        is_contrast = any(k in a_nom_lower for k in contrast_keywords)
        is_sedation = any(k in a_nom_lower for k in sedation_keywords)

        if is_contrast:
            has_contrast = True
            total_pc += sous_total
        elif is_sedation:
            has_sedation = True
            total_ps += sous_total
        else:
            exam_actes_names.append(a_nom.upper())

    protocol_text = ", ".join(exam_actes_names) if exam_actes_names else "EXPLORATION"

    net_total = float(passage.get("montant_total") or 0) - float(passage.get("remise") or 0)
    total_exam = max(0.0, net_total - total_pc - total_ps)

    med_traitant = f"{passage.get('medecin_traitant_grade') or 'Dr'} {passage.get('medecin_traitant_nom') or ''} {passage.get('medecin_traitant_prenom') or ''}".strip()
    if not passage.get("medecin_traitant_nom"):
        med_traitant = "Non spécifié"
    med_adresse = passage.get("medecin_traitant_adresse") or "-"

    # QR Code
    qr_b64 = ""
    try:
        import qrcode, io, base64
        qr_text = f"CLINIVA\\nPASS: Pas-{passage['id_passage']}\\nPATIENT: {nom} {prenom}\\nEXAMEN: {type_examen}\\nDATE: {date_formatee} {heure_p}"
        qr = qrcode.QRCode(version=1, box_size=4, border=0)
        qr.add_data(qr_text)
        qr.make(fit=True)
        img_qr = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img_qr.save(buf, format="PNG")
        qr_b64 = f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('utf-8')}"
    except Exception:
        pass

    # Questionnaire : Coordonnées des 'X'
    q = passage.get("questionnaire") or {}

    def is_oui(key):
        val = q.get(key)
        return val is True or str(val).upper() == "OUI"

    dots_config = [
        ("q_hospitalise", 66.5, 25.2, 33.6),
        ("q_dossier_interne", 69.6, 33.5, 43.8),
        ("q_dossier_externe", 72.3, 33.5, 43.8),
        ("q_urgence_vitale", 75.2, 23.6, 33.4),
        ("q_grossesse", 78.2, 23.6, 33.4),
        ("q_allergie", 81.3, 23.6, 33.4),
        ("q_diabete", 84.4, 23.6, 33.4),
        ("q_hta", 87.6, 23.6, 33.4),
        ("q_asthme", 90.8, 23.6, 33.4),
        ("q_insuffisance_renale", 94.0, 25.6, 35.6),
        ("q_pace_maker", 68.4, 75.3, 85.0),
        ("q_valve_cardiaque", 73.1, 75.3, 85.0),
        ("q_prothese_metallique", 77.8, 75.3, 85.0),
    ]

    x_marks_html = ""
    for key, top_pct, left_oui, left_non in dots_config:
        chosen_left = left_oui if is_oui(key) else left_non
        x_marks_html += (
            f'<div style="position:absolute; top:{top_pct}%; left:{chosen_left}%; '
            f'transform:translate(-50%, -50%); font-family:Arial, sans-serif; '
            f'font-size:16px; font-weight:900; color:#000000; z-index:25; line-height:1;">X</div>'
        )

    # Cocher les cases Produit de Contraste et Sédation
    if has_contrast:
        x_marks_html += '<div style="position:absolute; top:38.3%; left:44.4%; transform:translate(-50%, -50%); font-family:Arial, sans-serif; font-size:16px; font-weight:900; color:#000000; z-index:25;">X</div>'
    else:
        x_marks_html += '<div style="position:absolute; top:35.8%; left:44.4%; transform:translate(-50%, -50%); font-family:Arial, sans-serif; font-size:16px; font-weight:900; color:#000000; z-index:25;">X</div>'

    if has_sedation:
        x_marks_html += '<div style="position:absolute; top:35.8%; left:70.4%; transform:translate(-50%, -50%); font-family:Arial, sans-serif; font-size:16px; font-weight:900; color:#000000; z-index:25;">X</div>'

    return f"""
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="UTF-8">
        <title>Demande d'Examen - {nom} {prenom}</title>
        <style>
            @page {{ size: A4 portrait; margin: 0; }}
            * {{ box-sizing: border-box; }}
            body {{
                margin: 0; padding: 0; background: #525659;
                display: flex; justify-content: center;
                font-family: Arial, sans-serif;
            }}
            .page {{
                position: relative; width: 210mm; height: 297mm;
                background: white; overflow: hidden;
            }}
            .bg-template {{
                position: absolute; top: 0; left: 0;
                width: 100%; height: 100%; z-index: 1; pointer-events: none;
            }}
            .field-above-line {{
                position: absolute; z-index: 10;
                font-size: 14.5px; font-weight: 900; color: #000000;
                white-space: nowrap; line-height: 1;
            }}
            .qr-frame {{
                position: absolute; top: 4.2%; left: 73.2%;
                width: 17.5%; height: 12.5%; z-index: 15;
                display: flex; flex-direction: column;
                align-items: center; justify-content: center;
            }}
            .no-print {{
                position: fixed; top: 15px; right: 15px; z-index: 9999;
                display: flex; gap: 10px;
            }}
            .btn {{
                border: none; padding: 10px 18px; border-radius: 10px; font-weight: bold;
                cursor: pointer; font-size: 13px; box-shadow: 0 4px 10px rgba(0,0,0,0.3);
            }}
            .btn-print {{ background: #121c15; color: #8be140; }}
            .btn-close {{ background: #dc2626; color: white; }}
            @media print {{
                .no-print {{ display: none !important; }}
                body {{ background: white; }}
            }}
        </style>
    </head>
    <body>
        <div class="no-print">
            <button class="btn btn-print" onclick="window.print()">🖨️ Imprimer / Enregistrer PDF</button>
            <button class="btn btn-close" onclick="window.parent && window.parent.closeDemandeExamenModal ? window.parent.closeDemandeExamenModal() : window.close()">❌ Quitter le visionneur</button>
        </div>

        <div class="page">
            <img src="{img_b64}" class="bg-template">

            <!-- QR Code -->
            <div class="qr-frame">
                {f'<img src="{qr_b64}" style="max-width:85%; max-height:85%; object-fit:contain;">' if qr_b64 else ''}
                <span style="font-size: 12px; font-family: Arial, sans-serif; font-weight: 900; color: #000; margin-top: 3px;">
                    Pas-{passage['id_passage']:07d}
                </span>
            </div>

            <!-- TYPE D'EXAMEN (TOUJOURS EXPLORATION) -->
            <div class="field-above-line" style="top: 24.0%; left: 31%;">{type_examen}</div>
            <div class="field-above-line" style="top: 26.6%; left: 31%; font-size: 13px;">{date_enregistrement_txt}</div>

            <!-- PROTOCOLE D'EXAMEN -->
            <div class="field-above-line" style="top: 29.3%; left: 31%; max-width: 58%; overflow: hidden; text-overflow: ellipsis;">{protocol_text}</div>

            <!-- Identification patient -->
            <div class="field-above-line" style="top: 43.4%; left: 18%; font-size: 15px;">{nom}</div>
            <div class="field-above-line" style="top: 46.5%; left: 18%; font-size: 15px;">{prenom}</div>
            <div class="field-above-line" style="top: 49.6%; left: 18%;">{dob} -> ()</div>
            <div class="field-above-line" style="top: 52.8%; left: 18%; font-family: monospace;">{tel}</div>
            <div class="field-above-line" style="top: 56.0%; left: 18%;">{email}</div>
            <div class="field-above-line" style="top: 59.2%; left: 18%;">{region}</div>

            <!-- Questionnaire clinique avec grands 'X' -->
            {x_marks_html}

            <!-- MÉDECIN TRAITANT : POSITIONNÉ PLUS BAS QUE INSUFFISANCE RÉNALE (TOP: 96.0%) -->
            <div style="position:absolute; top: 96.0%; left: 6%; z-index: 20; font-size: 12px; font-weight: 900; color: #000; text-align: left; line-height: 1.25; max-width: 52%;">
                <div>Médecin Traitant : {med_traitant}</div>
                <div>Adresse : {med_adresse}</div>
            </div>

            <!-- TOTAUX FINANCIERS -->
            <div style="position:absolute; bottom: 2.2%; right: 6%; z-index: 20; font-size: 13.5px; font-weight: 900; color: #000; text-align: left; line-height: 1.35;">
                <div>Total Exam : {int(total_exam)} DZD</div>
                <div>Total P.C : {int(total_pc)} DZD</div>
                <div>Total P.S : {int(total_ps)} DZD</div>
            </div>
        </div>
    </body>
    </html>
    """


# ============================================================================
# RENDU DU COMPOSANT HTML ET JAVASCRIPT COMPLET
# ============================================================================
def render_html(mode: str = "general") -> str:
    """Génère l'HTML complet avec Dark Mode, Modales d'ajout rapide au premier plan et validation."""
    is_radiology_mode = str(mode or "").strip().lower() == "radiologie"
    doc_types_options = "".join([f'<option value="{dt}">{dt}</option>' for dt in DOCS_PASSAGE_TYPES])
    identity_types_options = "".join([f'<option value="{tp}">{tp}</option>' for tp in TYPES_PIECES_IDENTITE])
    wilayas_options = "".join([f'<option value="{w}">{w}</option>' for w in sorted(ALGERIA_WILAYAS.keys())])
    groupes_options = "".join([f'<option value="{g}">{g}</option>' for g in GROUPES_SANGUINS])
    pieces_options = "".join([f'<option value="{tp}">{tp}</option>' for tp in TYPES_PIECES_IDENTITE])
    wilayas_json = json.dumps(ALGERIA_WILAYAS)
    questionnaire_edit_items = [
        ("hospitalise", "Patient hospitalisé"), ("dossier_interne", "Ancien dossier interne"),
        ("dossier_externe", "Dossier externe"), ("urgence_vitale", "Urgence vitale"),
        ("grossesse", "Grossesse"), ("allergie", "Allergie"), ("diabete", "Diabète"),
        ("hta", "Hypertension (HTA)"), ("asthme", "Asthme"),
        ("insuffisance_renale", "Insuffisance rénale"), ("pace_maker", "Pace maker"),
        ("valve_cardiaque", "Valve cardiaque"), ("prothese_metallique", "Prothèse métallique"),
    ]
    questionnaire_edit_html = "".join(
        f'<label class="flex items-center justify-between gap-3 py-2 border-b border-slate-100 dark:border-white/5 text-[11px]">'
        f'<span class="font-semibold text-slate-700 dark:text-slate-300">{label}</span>'
        f'<select id="passageEditQuestionnaire_{key}" class="px-2.5 py-1.5 rounded-lg border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs text-slate-800 dark:text-white"><option value="NON">Non</option><option value="OUI">Oui</option></select></label>'
        for key, label in questionnaire_edit_items
    )
    patient_form_component = render_patients_html().replace(
        'id="view-patients" class="space-y-6"',
        'id="view-patients" class="hidden"',
    ).replace('z-50', 'z-[100]')
    table_headers_html = (
        """
        <th class="w-[27%] py-3 pl-3 text-left">Patient</th>
        <th class="w-[37%] py-3 px-2 text-left">Examen</th>
        <th class="w-[12%] py-3 px-2 text-center">État résultat</th>
        <th class="w-[10%] py-3 px-2 text-center">État passage</th>
        <th class="w-[8%] py-3 px-2 text-center">État paiement</th>
        <th class="w-[12%] py-3 pr-3 text-right">Détails</th>
        """
        if is_radiology_mode else
        """
        <th class="w-[14%] py-3 pl-3 pr-2 rounded-l-xl font-bold">Passage / horaire</th>
        <th class="w-[23%] py-3 px-2 font-bold">Patient</th>
        <th class="w-[17%] py-3 px-2 font-bold">Médecin / type</th>
        <th class="w-[22%] py-3 px-2 font-bold">Prestations / actes</th>
        <th class="w-[15%] py-3 px-2 font-bold">Règlement</th>
        <th class="w-[9%] py-3 pl-2 pr-3 rounded-r-xl font-bold text-right">Actions</th>
        """
    )

    return f"""
    <div id="view-reception" data-view-mode="{'radiologie' if is_radiology_mode else 'general'}" class="space-y-6">

        <!-- BANDEAU D'EN-TÊTE -->
        <div class="banner-glow rounded-3xl p-6 lg:p-8 text-white shadow-xl flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
            <div>
                <h1 class="text-2xl font-extrabold tracking-tight mb-1">{'Salle d’attente — Radiologie' if is_radiology_mode else 'Accueil & Passages des Patients'}</h1>
                <p class="text-xs text-slate-300">{'Suivi des passages de radiologie et création de nouvelles admissions.' if is_radiology_mode else 'Enregistrement des admissions, numérisation des ordonnances, questionnaire médical bilingue et accès direct au DME.'}</p>
            </div>

            <div class="flex items-center gap-3">
                <div class="bg-white/10 px-4 py-2 rounded-2xl backdrop-blur-sm border border-white/10 text-xs">
                    <span class="text-slate-300">Total Passages:</span> 
                    <strong id="totalPassagesCount" class="text-brand-neon font-black text-sm ml-1">0</strong>
                </div>

                <button type="button" onclick="openPassageModal()" class="flex items-center gap-2 bg-brand-neon hover:bg-brand-neonDark text-black px-5 py-2.5 rounded-2xl font-bold text-xs shadow-md shadow-brand-neon/20 transition cursor-pointer">
                    <i class="ph-bold ph-door-open text-base"></i>
                    <span>Nouveau Passage</span>
                </button>
            </div>
        </div>

        <!-- RECHERCHE ET FILTRES -->
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 bg-white dark:bg-[#121c15] p-4 rounded-2xl shadow-sm border border-slate-100 dark:border-white/5">
            <div>
                <div class="text-sm font-bold text-slate-800 dark:text-white">Historique des passages</div>
                <div id="passageFilterSummary" class="mt-1 text-[11px] text-slate-500 dark:text-slate-400">Tous les passages</div>
            </div>
            <div class="flex flex-wrap items-center gap-2">
                <button type="button" id="clearPassageFiltersBtn" onclick="clearPassageFilters()" class="hidden px-3 py-2 rounded-xl text-xs font-semibold text-slate-500 hover:bg-slate-100 dark:hover:bg-white/5 transition">Réinitialiser</button>
                <div class="passage-style-switch inline-flex items-center gap-1 p-1 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20" aria-label="Style du tableau des passages">
                    <button type="button" id="passageStyleClassicBtn" onclick="setPassageDisplayStyle('classic')" class="px-2.5 py-1.5 rounded-lg text-[10px] font-bold transition">Tableau</button>
                    <button type="button" id="passageStyleCardsBtn" onclick="setPassageDisplayStyle('cards')" class="px-2.5 py-1.5 rounded-lg text-[10px] font-bold transition">Lignes arrondies</button>
                    <button type="button" id="passageStyleWorklistBtn" onclick="setPassageDisplayStyle('worklist')" class="px-2.5 py-1.5 rounded-lg text-[10px] font-bold transition"><i class="ph-bold ph-list-dashes mr-1"></i>Style Worklist</button>
                    <button type="button" id="passageStyleEssentialBtn" onclick="setPassageDisplayStyle('essential')" class="px-2.5 py-1.5 rounded-lg text-[10px] font-bold transition"><i class="ph-bold ph-identification-card mr-1"></i>Vue d’ensemble</button>
                </div>
                <button type="button" onclick="openPassageFiltersModal()" class="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-slate-900 dark:bg-brand-neon text-white dark:text-black text-xs font-bold shadow-sm hover:opacity-90 transition">
                    <i class="ph-bold ph-sliders-horizontal text-sm"></i><span>Rechercher et filtrer</span><span id="passageFilterCount" class="hidden min-w-5 h-5 px-1 rounded-full bg-white/20 dark:bg-black/10 items-center justify-center text-[10px]">0</span>
                </button>
            </div>
        </div>

        <!-- MODALE RECHERCHE ET FILTRES -->
        <div id="passageFiltersModal" class="fixed inset-0 bg-slate-950/60 backdrop-blur-sm hidden items-center justify-center z-[70] p-4" onclick="if(event.target===this) closePassageFiltersModal()">
            <div class="passage-filter-modal-card w-full max-w-3xl max-h-[90vh] overflow-hidden rounded-3xl bg-white dark:bg-[#121c15] border border-slate-200 dark:border-white/10 shadow-2xl flex flex-col">
                <div class="passage-filter-modal-head px-6 py-5 flex items-center justify-between">
                    <div class="flex items-center gap-3"><div class="passage-filter-modal-icon w-11 h-11 rounded-2xl flex items-center justify-center"><i class="ph-bold ph-funnel text-xl"></i></div><div><div class="text-[9px] tracking-[.16em] font-extrabold text-emerald-100/80 uppercase">Recherche avancée</div><h2 class="font-extrabold text-white">Filtrer les passages</h2><p class="text-[11px] text-emerald-50/80">Combinez les critères pour retrouver rapidement un dossier.</p></div></div>
                    <button type="button" onclick="closePassageFiltersModal()" class="passage-filter-close w-9 h-9 rounded-xl text-white/80 hover:bg-white/15"><i class="ph-bold ph-x"></i></button>
                </div>
                <div class="passage-filter-modal-body p-6 overflow-y-auto">
                    <div class="passage-filter-grid grid grid-cols-1 sm:grid-cols-2 gap-3.5">
                        <label class="sm:col-span-2"><span class="passage-filter-label"><i class="ph-bold ph-magnifying-glass"></i>{'Nom et prénom du patient' if is_radiology_mode else 'Recherche libre'}</span><div class="passage-filter-search flex items-center gap-2 px-3 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20"><i class="ph ph-magnifying-glass text-slate-400"></i><input id="filterPassageSearch" type="search" placeholder="{'Nom ou prénom' if is_radiology_mode else 'Nom, prénom, téléphone, numéro, médecin ou acte...'}" class="w-full py-2.5 text-xs outline-none bg-transparent text-slate-800 dark:text-white"></div></label>
                        <label><span class="passage-filter-label"><i class="ph-bold ph-hash"></i>Numéro de passage</span><input id="filterPassageNumero" type="text" placeholder="Ex. 125" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                        <label><span class="passage-filter-label"><i class="ph-bold ph-phone"></i>Téléphone patient</span><input id="filterPassageTelephone" type="search" placeholder="Numéro de téléphone" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                        <label><span class="passage-filter-label"><i class="ph-bold ph-calendar-blank"></i>Date de naissance</span><input id="filterPassageDateNaissance" type="date" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                        <label><span class="passage-filter-label"><i class="ph-bold ph-stethoscope"></i>Médecin traitant</span><input id="filterPassageMedecin" type="search" placeholder="Nom du médecin" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                        <label><span class="passage-filter-label"><i class="ph-bold ph-first-aid-kit"></i>Acte ou prestation</span><input id="filterPassageActe" type="search" placeholder="Nom de l'acte" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                        <label><span class="passage-filter-label"><i class="ph-bold ph-list-bullets"></i>Type de passage</span><select id="filterPassageType" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"><option value="">Tous les types</option></select></label>
                        <label><span class="passage-filter-label"><i class="ph-bold ph-activity"></i>Statut</span><select id="filterPassageStatut" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"><option value="">Tous les statuts</option><option>En attente</option><option>En cours</option><option>Terminé</option></select></label>
                        <label class="sm:col-span-2"><span class="passage-filter-label"><i class="ph-bold ph-calendar-dots"></i>Raccourci de période</span><select id="filterPassageDateShortcut" onchange="applyPassageDateShortcut(this.value)" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"><option value="">Choisir une période rapide…</option><option value="today">Aujourd’hui</option><option value="yesterday">Hier</option><option value="day_before">Avant-hier</option><option value="3days">Depuis 3 jours</option><option value="7weeks">Depuis 7 semaines</option><option value="15days">Depuis 15 jours</option><option value="month">Depuis un mois</option></select></label>
                        <label><span class="passage-filter-label"><i class="ph-bold ph-calendar-plus"></i>Date à partir du</span><input id="filterPassageDateDebut" type="date" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                        <label><span class="passage-filter-label"><i class="ph-bold ph-calendar-check"></i>Date jusqu'au</span><input id="filterPassageDateFin" type="date" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                        {'<label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Modalité</span><select id="filterPassageModalite" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"><option value="">Toutes les modalités</option></select></label><label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">État du passage</span><select id="filterPassageEtat" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"><option value="">Tous</option><option value="fait">Fait</option><option value="non_fait">Pas encore fait</option></select></label><label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">État du résultat</span><select id="filterPassageResultat" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"><option value="">Tous</option><option value="disponible">Disponible</option><option value="non_disponible">Non disponible</option></select></label>' if is_radiology_mode else ''}
                    </div>
                </div>
                <div class="passage-filter-modal-footer px-6 py-4 border-t border-slate-100 dark:border-white/5 flex items-center justify-between gap-3">
                    <button type="button" onclick="clearPassageFilters()" class="passage-filter-clear inline-flex items-center gap-2 px-3.5 py-2.5 rounded-xl text-xs font-semibold"><i class="ph-bold ph-eraser"></i>Effacer les critères</button>
                    <div class="flex gap-2"><button type="button" onclick="closePassageFiltersModal()" class="px-4 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-semibold text-slate-600 dark:text-slate-300"><i class="ph-bold ph-x mr-1"></i>Annuler</button><button type="button" id="passageFilterApplyBtn" onclick="applyPassageFilters(event)" class="passage-filter-apply inline-flex items-center gap-2 px-4 py-2.5 rounded-xl text-xs font-bold"><i class="ph-bold ph-check-circle"></i><span>Afficher les résultats</span></button></div>
                </div>
            </div>
        </div>

        <!-- MODALE MODIFICATION DES INFORMATIONS PATIENT -->
        <div id="passagePatientEditModal" class="fixed inset-0 bg-slate-950/65 backdrop-blur-sm hidden items-center justify-center z-[85] p-4" onclick="if(event.target===this) closePassagePatientEditModal()">
            <form id="passagePatientEditForm" onsubmit="savePatientInfoFromPassage(event)" class="w-full max-w-2xl max-h-[92vh] overflow-y-auto rounded-3xl bg-white dark:bg-[#121c15] border border-slate-200 dark:border-white/10 shadow-2xl">
                <div class="sticky top-0 z-10 px-6 py-4 bg-white dark:bg-[#121c15] border-b border-slate-100 dark:border-white/5 flex items-center justify-between rounded-t-3xl">
                    <div class="flex items-center gap-3"><span class="w-10 h-10 rounded-2xl bg-brand-neon/20 text-emerald-700 dark:text-brand-neon flex items-center justify-center"><i class="ph-bold ph-identification-card text-lg"></i></span><div><h2 class="text-sm font-bold text-slate-800 dark:text-white">Corriger le dossier avant consultation</h2><p id="passagePatientEditSubtitle" class="text-[11px] text-slate-500 dark:text-slate-400">Identité, questionnaire médical et documents d’admission.</p></div></div>
                    <button type="button" onclick="closePassagePatientEditModal()" class="w-9 h-9 rounded-xl text-slate-400 hover:bg-slate-100 dark:hover:bg-white/5"><i class="ph-bold ph-x"></i></button>
                </div>
                <input type="hidden" id="passagePatientEditPassageId"><input type="hidden" id="passagePatientEditPatientId">
                <div class="p-6 grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Nom *</span><input id="passagePatientEditNom" required maxlength="100" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                    <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Prénom *</span><input id="passagePatientEditPrenom" required maxlength="100" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                    <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Date de naissance *</span><input id="passagePatientEditDob" type="date" required class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                    <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Genre *</span><select id="passagePatientEditGenre" required class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"><option>Homme</option><option>Femme</option><option>Autre</option></select></label>
                    <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Groupe sanguin *</span><select id="passagePatientEditBloodGroup" required class="w-full px-3 py-2.5 rounded-xl border border-rose-200 dark:border-rose-900/40 bg-rose-50/40 dark:bg-rose-950/20 text-xs font-bold text-rose-700 dark:text-rose-300">{groupes_options}</select></label>
                    <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Téléphone principal *</span><input id="passagePatientEditPhone1" type="tel" required maxlength="30" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                    <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Téléphone secondaire</span><input id="passagePatientEditPhone2" type="tel" maxlength="30" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                    <label class="sm:col-span-2"><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">E-mail</span><input id="passagePatientEditEmail" type="email" maxlength="254" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                    <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Wilaya *</span><select id="passagePatientEditWilaya" required class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"><option value="">Choisir une wilaya</option>{wilayas_options}</select></label>
                    <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Commune *</span><input id="passagePatientEditCommune" required maxlength="100" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                    <div class="sm:col-span-2 rounded-2xl border border-slate-200 dark:border-white/10 p-4">
                        <h3 class="text-xs font-bold text-slate-800 dark:text-white mb-3">Pièce d’identité et éléments biométriques</h3>
                        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                            <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Type de pièce</span><select id="passagePatientEditIdentityType" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs">{identity_types_options}</select></label>
                            <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Scan de la pièce</span><input id="passagePatientEditIdentityFile" type="file" accept="image/*,application/pdf" class="w-full text-xs" onchange="readPassagePatientFile(event, 'doc_identite')"></label>
                            <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Photo du dossier</span><input id="passagePatientEditAvatarFile" type="file" accept="image/*" class="w-full text-xs" onchange="readPassagePatientFile(event, 'avatar')"></label>
                            <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Photo de face</span><input id="passagePatientEditFaceFile" type="file" accept="image/*" class="w-full text-xs" onchange="readPassagePatientFile(event, 'photo_face')"></label>
                            <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Profil droit</span><input id="passagePatientEditRightFile" type="file" accept="image/*" class="w-full text-xs" onchange="readPassagePatientFile(event, 'photo_profil_droit')"></label>
                            <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Profil gauche</span><input id="passagePatientEditLeftFile" type="file" accept="image/*" class="w-full text-xs" onchange="readPassagePatientFile(event, 'photo_profil_gauche')"></label>
                            <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Empreinte digitale</span><input id="passagePatientEditFingerprintFile" type="file" accept="image/*" class="w-full text-xs" onchange="readPassagePatientFile(event, 'empreinte_index')"></label>
                        </div>
                        <p class="mt-2 text-[10px] text-slate-400">Choisir un nouveau fichier remplace le document actuel. Laisser vide conserve les données existantes.</p>
                        <div class="mt-3 border-t border-slate-100 dark:border-white/5 pt-3"><div class="flex items-center justify-between mb-2"><span class="text-[11px] font-semibold text-slate-600 dark:text-slate-300">Autres pièces du dossier patient</span><span id="passageEditPiecesCount" class="text-[10px] text-slate-400"></span></div><label class="cursor-pointer inline-flex px-3 py-2 rounded-lg bg-slate-100 dark:bg-white/5 text-[11px] font-semibold"><i class="ph-bold ph-upload-simple mr-1"></i>Ajouter une pièce<input type="file" accept="image/*,application/pdf" class="hidden" onchange="addPassagePatientPiece(event)"></label><div id="passageEditPiecesList" class="space-y-1.5 mt-2"></div></div>
                    </div>
                    <div class="sm:col-span-2 mt-2 rounded-2xl border border-sky-100 dark:border-sky-900/30 p-4">
                        <h3 class="text-xs font-bold text-slate-800 dark:text-white mb-3">Informations médicales de l’admission</h3>
                        <label class="block mb-4"><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Observation médicale</span><textarea id="passageEditObservation" rows="3" maxlength="5000" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></textarea></label>
                        <div class="grid grid-cols-1 md:grid-cols-2 gap-x-5">{questionnaire_edit_html}</div>
                    </div>
                    <div class="sm:col-span-2 rounded-2xl border border-slate-200 dark:border-white/10 p-4">
                        <div class="flex flex-wrap items-center justify-between gap-3 mb-3"><h3 class="text-xs font-bold text-slate-800 dark:text-white">Documents scannés du passage</h3><span id="passageEditDocsCount" class="text-[10px] text-slate-400"></span></div>
                        <div class="flex flex-wrap items-end gap-2 mb-3"><label class="min-w-40 flex-1"><span class="block mb-1 text-[10px] text-slate-500">Type de document</span><select id="passageEditDocType" class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs">{doc_types_options}</select></label><label class="cursor-pointer px-3 py-2 rounded-lg bg-slate-900 dark:bg-brand-neon text-white dark:text-black text-[11px] font-bold"><i class="ph-bold ph-upload-simple mr-1"></i>Ajouter un scan<input id="passageEditDocFile" type="file" accept="image/*,application/pdf" class="hidden" onchange="addPassageEditDocument(event)"></label></div>
                        <div id="passageEditDocsList" class="space-y-1.5"></div>
                    </div>
                </div>
                <div class="px-6 py-4 border-t border-slate-100 dark:border-white/5 bg-slate-50/70 dark:bg-black/10 flex justify-end gap-2 rounded-b-3xl">
                    <button type="button" onclick="closePassagePatientEditModal()" class="px-4 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-semibold text-slate-600 dark:text-slate-300">Annuler</button>
                    <button id="passagePatientEditSubmit" type="submit" class="inline-flex items-center gap-2 px-5 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold hover:opacity-90"><i class="ph-bold ph-floppy-disk"></i>Enregistrer et régénérer la demande</button>
                </div>
            </form>
        </div>

        <!-- TABLEAU DES PASSAGES -->
        <div class="bg-white dark:bg-[#121c15] rounded-3xl p-5 shadow-sm border border-slate-100 dark:border-white/5 flex flex-col transition-colors">
            {'<div class="flex items-center gap-2 mb-2 text-slate-700"><i class="ph-bold ph-list-bullets text-violet-600"></i><h2 class="font-extrabold text-sm">Passages de radiologie</h2></div>' if is_radiology_mode else ''}
            <div class="overflow-x-auto">
                <table id="passagesDataTable" data-style="classic" class="w-full min-w-[1080px] text-left text-xs table-fixed border-separate border-spacing-0">
                    <thead class="bg-slate-50 dark:bg-white/[0.035]">
                        <tr class="text-[10px] uppercase tracking-wider text-slate-500 dark:text-slate-400">
                            {table_headers_html}
                        </tr>
                    </thead>
                    <tbody id="passages-table-body" class="text-slate-700 dark:text-slate-300 font-medium"></tbody>
                </table>
                <style>
                    /* Le questionnaire reste immobile lorsque la souris passe dessus. */
                    #passageModal .passage-questionnaire-section:hover,
                    #passageModal .passage-questionnaire-section :hover {{ transform:none !important; translate:none !important; scale:1 !important; }}
                    #passageModal .passage-questionnaire-section {{ box-sizing:border-box; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable {{ min-width:1100px; border-collapse:collapse; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable thead {{ background:#fff; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable thead th {{ color:#94a3b8; font-size:9px; font-weight:600; letter-spacing:.04em; text-transform:uppercase; border-bottom:1px solid #e2e8f0; }}
                    #view-reception #passagesDataTable tbody tr.passage-menu-open {{ position:relative; z-index:80; }}
                    #view-reception #passagesDataTable details.passage-action-menu[open] {{ position:relative; z-index:100; }}
                    #view-reception #passagesDataTable .passage-action-menu-panel {{ z-index:9999; }}
                    .passage-action-menu-portal {{ display:block; min-width:205px; padding:5px; overflow:visible; border:1px solid #dbe2ea; border-radius:14px; background:#fff; color:#334155; box-shadow:0 16px 40px rgba(15,23,42,.24); }}
                    .passage-action-menu-portal .radio-menu-item {{ display:flex; width:100%; align-items:center; gap:8px; padding:9px 10px; border-radius:9px; text-align:left; font-size:11px; color:#334155; cursor:pointer; }}
                    .passage-action-menu-portal .radio-menu-item:hover {{ background:#f1f5f9; }}
                    .passage-action-menu-portal button {{ cursor:pointer; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable tbody tr:not(.passage-expand-details) {{ background:#fff; border-bottom:1px solid #e2e8f0; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable tbody tr:not(.passage-expand-details):hover {{ background:#f8fafc; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable tbody tr:not(.passage-expand-details) td {{ color:#111827; padding-top:12px; padding-bottom:12px; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable .radio-state {{ display:inline-flex; align-items:center; justify-content:center; padding:5px 10px; border-radius:9px; color:#fff; font-size:9px; line-height:1.1; font-weight:800; text-transform:uppercase; white-space:nowrap; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable .radio-state-result {{ background:linear-gradient(100deg,#22c7c0,#21c889); }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable .radio-state-muted {{ background:#f05245; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable .radio-state-passage {{ background:#7065df; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable details > summary {{ color:#fff; background:#7562e5; border:0; border-radius:9px; padding:9px 13px; box-shadow:0 3px 8px #7562e544; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable .radio-menu-item {{ display:flex; width:100%; align-items:center; gap:8px; padding:8px 10px; border-radius:8px; text-align:left; font-size:11px; color:#334155; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable .radio-menu-item:hover {{ background:#f1f5f9; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable[data-style="essential"] {{ min-width:1100px; border-spacing:0; border-collapse:collapse; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable[data-style="essential"] thead {{ background:#fff; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable[data-style="essential"] thead th {{ padding:11px 14px; border:0; border-bottom:1px solid #e3e7ea; color:#8793a0; font-size:9px; font-weight:800; letter-spacing:.06em; text-transform:uppercase; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable[data-style="essential"] tbody tr.radiology-passage-row {{ background:#fff; border-bottom:1px solid #e5e9ed; transition:background .15s ease; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable[data-style="essential"] tbody tr.radiology-passage-row:nth-of-type(4n+1) {{ background:#e9ecef; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable[data-style="essential"] tbody tr.radiology-passage-row:nth-of-type(4n+3) {{ background:#fff; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable[data-style="essential"] tbody tr.radiology-passage-row:hover {{ background:#e3eee8; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable[data-style="essential"] tbody tr.radiology-passage-row td {{ padding:9px 14px; border:0; background:transparent; color:#263849; vertical-align:middle; }}
                    #view-reception[data-view-mode="radiologie"] #passagesDataTable[data-style="essential"] tbody tr.passage-expand-details td {{ padding:8px 14px; border-bottom:1px solid #e5e9ed; background:#f8fafc; }}
                    #passagesDataTable[data-style="cards"] {{ border-spacing: 0 9px; }}
                    #passagesDataTable[data-style="cards"] thead {{ background: #334155; }}
                    #passagesDataTable[data-style="cards"] thead tr {{ color: #fff; }}
                    #passagesDataTable[data-style="cards"] tbody tr:not(.passage-expand-details) {{ background: #fff; box-shadow: 0 3px 12px rgba(15,23,42,.08); }}
                    #passagesDataTable[data-style="cards"] tbody tr:not(.passage-expand-details) td:first-child {{ border-left:1px solid #e2e8f0; border-radius:14px 0 0 14px; }}
                    #passagesDataTable[data-style="cards"] tbody tr:not(.passage-expand-details) td:last-child {{ border-right:1px solid #e2e8f0; border-radius:0 14px 14px 0; }}
                    #passagesDataTable[data-style="cards"] tbody tr:not(.passage-expand-details) td {{ border-top: 1px solid #e2e8f0; border-bottom: 1px solid #e2e8f0; color: #1e293b; }}
                    /* Garde les contenus à l'intérieur des deux bords arrondis de la ligne. */
                    #passagesDataTable[data-style="cards"] tbody tr:not(.passage-expand-details) td:first-child {{ box-sizing:border-box; padding-left:16px; overflow:hidden; }}
                    #passagesDataTable[data-style="cards"] tbody tr:not(.passage-expand-details) td:first-child > div:first-child {{ display:flex; flex-direction:column; align-items:flex-start; gap:4px; min-width:0; }}
                    #passagesDataTable[data-style="cards"] tbody tr:not(.passage-expand-details) td:first-child > div:first-child > span {{ max-width:100%; overflow-wrap:anywhere; line-height:1.35; }}
                    #passagesDataTable[data-style="cards"] tbody tr:not(.passage-expand-details) td:last-child {{ box-sizing:border-box; padding-right:16px; overflow:visible; }}
                    #passagesDataTable[data-style="cards"] tbody tr:not(.passage-expand-details) td:last-child > details {{ max-width:100%; }}
                    #passagesDataTable[data-style="cards"] tbody tr:not(.passage-expand-details) td:last-child > details > summary {{ max-width:100%; white-space:nowrap; }}
                    #passagesDataTable[data-style="cards"] tbody tr.passage-expand-details td {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; color: #334155; }}
                    #passagesDataTable[data-style="worklist"] {{ border-spacing: 0 8px; font-size:13px; }}
                    #passagesDataTable[data-style="worklist"] thead {{ background: transparent; }}
                    #passagesDataTable[data-style="worklist"] thead tr {{ color: #94a3b8; }}
                    #passagesDataTable[data-style="worklist"] thead th {{ padding: 8px 12px; border:0; color: #64748b; font-size: 11px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; }}
                    #passagesDataTable[data-style="worklist"] tbody tr:not(.passage-expand-details) {{ background: #f8fafc; transition: background-color .18s ease; }}
                    #passagesDataTable[data-style="worklist"] tbody tr:not(.passage-expand-details):hover {{ background: #ecfdf5; }}
                    #passagesDataTable[data-style="worklist"] tbody tr:not(.passage-expand-details) td {{ padding:12px; border:0; background:transparent; color: #1e293b; }}
                    #passagesDataTable[data-style="worklist"] tbody tr:not(.passage-expand-details) td:first-child {{ border-radius: 12px 0 0 12px; }}
                    #passagesDataTable[data-style="worklist"] tbody tr:not(.passage-expand-details) td:last-child {{ border-radius: 0 12px 12px 0; }}
                    #passagesDataTable[data-style="worklist"] tbody tr.passage-expand-details td {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; color: #334155; }}
                    #passagesDataTable[data-style="worklist"] .passage-worklist-name {{ color: #1e293b; font-size: 13px; font-weight: 800; line-height: 1.25; }}
                    #passagesDataTable[data-style="worklist"] .passage-worklist-avatar {{ width: 36px; height: 36px; }}
                    #passagesDataTable[data-style="worklist"] .passage-worklist-ref {{ display: inline-flex; align-items: center; padding: 3px 7px; border-radius: 7px; background: #f1f5f9; color: #64748b; font-size: 9px; font-weight: 700; }}
                    #passagesDataTable[data-style="worklist"] .passage-worklist-date-line {{ display:flex; align-items:center; flex-wrap:wrap; gap:3px 7px; }}
                    #passagesDataTable[data-style="worklist"] .passage-worklist-date-line .passage-worklist-ref {{ padding:2px 5px; }}
                    #passagesDataTable[data-style="worklist"] .passage-worklist-patient-primary {{ display:flex; align-items:center; flex-wrap:wrap; gap:4px 7px; }}
                    #passagesDataTable[data-style="worklist"] .passage-worklist-patient-primary > div, #passagesDataTable[data-style="worklist"] .passage-worklist-patient-primary > span {{ margin:0; }}
                    #passagesDataTable[data-style="worklist"] .passage-worklist-action {{ background:transparent; color:#4f46e5; border-color:transparent; padding:8px; font-size:12px; }}
                    #passagesDataTable[data-style="worklist"] .passage-worklist-action:hover {{ background:#e0e7ff; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] {{ min-width:1000px; border-spacing:0; border-collapse:collapse; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] thead {{ background:#fff; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] thead th {{ padding:11px 14px; border:0; border-bottom:1px solid #e3e7ea; color:#8a96a4; font-size:9px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] thead th:nth-child(6), #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] tbody tr:not(.passage-expand-details) td:nth-child(6) {{ display:none; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] thead th:nth-child(1), #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] tbody tr:not(.passage-expand-details) td:nth-child(1) {{ width:29%; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] thead th:nth-child(2), #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] tbody tr:not(.passage-expand-details) td:nth-child(2) {{ width:18%; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] thead th:nth-child(3), #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] tbody tr:not(.passage-expand-details) td:nth-child(3) {{ width:29%; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] thead th:nth-child(4), #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] tbody tr:not(.passage-expand-details) td:nth-child(4) {{ width:14%; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] thead th:nth-child(5), #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] tbody tr:not(.passage-expand-details) td:nth-child(5) {{ width:10%; text-align:center; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] tbody tr:not(.passage-expand-details) {{ background:#fff; border-bottom:1px solid #e5e9ed; transition:background .15s ease; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] tbody tr.passage-essential-row:nth-of-type(4n+1) {{ background:#e9ecef; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] tbody tr.passage-essential-row:nth-of-type(4n+3) {{ background:#fff; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] tbody tr:not(.passage-expand-details):hover {{ background:#e3eee8; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] tbody tr:not(.passage-expand-details) td {{ padding:8px 14px; border:0; background:transparent; color:#263849; vertical-align:middle; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] .passage-essential-status {{ display:inline-flex; align-items:center; gap:6px; padding:5px 9px; border-radius:7px; font-size:10px; font-weight:750; white-space:nowrap; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] .passage-essential-status.bg-amber-100 {{ background:#fff1c2; color:#946000; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] .passage-essential-status.bg-blue-100 {{ background:#e1edff; color:#235ca8; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] .passage-essential-status.bg-emerald-100 {{ background:#e4f6eb; color:#16814f; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] .passage-essential-status.bg-slate-100 {{ background:#eef1f4; color:#526173; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] .passage-essential-name {{ color:#294766; font-size:14px; font-weight:750; line-height:1.25; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] .passage-essential-avatar {{ width:42px; height:42px; border-radius:7px; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] .passage-essential-date-line {{ display:flex; align-items:center; flex-wrap:wrap; gap:5px 8px; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] .passage-essential-time {{ margin-top:3px; color:#718096; font-size:10px; font-weight:650; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] .passage-essential-date-icon {{ margin-right:5px; color:#8092a5; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] td:nth-child(3) > div > span:not(.passage-essential-status) {{ display:block; width:fit-content; max-width:100%; padding:0; border:0; border-radius:0; background:transparent; color:#334155; font-size:11px; font-weight:700; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] td:nth-child(3) > div > span:not(.passage-essential-status)::before {{ content:'◈'; display:inline-block; margin-right:8px; color:#6382a2; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] .passage-essential-action {{ display:inline-flex; align-items:center; justify-content:center; gap:6px; padding:8px 10px; border:0; border-radius:8px; background:#b7f34a; color:#102315; font-size:10px; font-weight:800; box-shadow:none; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] .passage-essential-action:hover {{ background:#a5e63b; }}
                    #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] .passage-essential-patient-status, #view-reception[data-view-mode="general"] #passagesDataTable[data-style="essential"] tbody tr.passage-expand-details {{ display:none; }}
                    #passageFiltersModal .passage-filter-modal-card {{ border-color:#d9e7df; box-shadow:0 28px 80px rgba(2,44,34,.35); }}
                    #passageFiltersModal .passage-filter-modal-head {{ background:linear-gradient(118deg,#0b3328 0%,#12664e 58%,#19a276 100%); border-bottom:1px solid rgba(255,255,255,.14); }}
                    #passageFiltersModal .passage-filter-modal-icon {{ color:#c7ff69; background:rgba(255,255,255,.13); border:1px solid rgba(255,255,255,.18); box-shadow:0 8px 22px rgba(0,0,0,.14); }}
                    #passageFiltersModal .passage-filter-modal-body {{ background:linear-gradient(180deg,#f8fbf9 0%,#f1f7f4 100%); }}
                    #passageFiltersModal .passage-filter-grid > label {{ display:block; padding:12px; border:1px solid #e2ebe5; border-radius:16px; background:rgba(255,255,255,.94); box-shadow:0 2px 7px rgba(15,50,35,.035); transition:border-color .16s ease,box-shadow .16s ease,transform .16s ease; }}
                    #passageFiltersModal .passage-filter-grid > label:focus-within {{ border-color:#67c99b; box-shadow:0 0 0 3px rgba(16,185,129,.12),0 6px 15px rgba(15,50,35,.06); transform:translateY(-1px); }}
                    #passageFiltersModal .passage-filter-label, #passageFiltersModal .passage-filter-grid > label > span:first-child {{ display:flex; align-items:center; gap:7px; margin-bottom:8px; color:#475569; font-size:10px; font-weight:800; letter-spacing:.025em; }}
                    #passageFiltersModal .passage-filter-label i {{ display:inline-flex; align-items:center; justify-content:center; width:23px; height:23px; border-radius:8px; background:#e8f7ef; color:#13835b; font-size:12px; }}
                    #passageFiltersModal .passage-filter-grid input, #passageFiltersModal .passage-filter-grid select, #passageFiltersModal .passage-filter-search {{ min-height:40px; border:1px solid #dce6df; border-radius:11px; background:#f8faf9; color:#1e293b; transition:border-color .15s ease,background .15s ease; }}
                    #passageFiltersModal .passage-filter-grid input:focus, #passageFiltersModal .passage-filter-grid select:focus {{ border-color:#34a879; background:#fff; outline:none; box-shadow:0 0 0 3px rgba(16,185,129,.1); }}
                    #passageFiltersModal .passage-filter-search {{ padding-left:12px; }}
                    #passageFiltersModal .passage-filter-search input {{ min-height:38px; border:0; box-shadow:none; background:transparent; }}
                    #passageFiltersModal .passage-filter-modal-footer {{ background:#fff; }}
                    #passageFiltersModal .passage-filter-clear {{ color:#64748b; background:#f1f5f9; }}
                    #passageFiltersModal .passage-filter-clear:hover {{ color:#334155; background:#e2e8f0; }}
                    #passageFiltersModal .passage-filter-apply {{ color:#102315; background:linear-gradient(110deg,#b7f34a,#7ed957); box-shadow:0 5px 14px rgba(113,191,48,.23); }}
                    #passageFiltersModal .passage-filter-apply:hover {{ filter:brightness(.97); transform:translateY(-1px); }}
                </style>
            </div>

            <!-- PAGINATION -->
            <div id="passagesPaginationWrapper" class="flex flex-col sm:flex-row items-center justify-between gap-3 pt-5 mt-3 border-t border-slate-100 dark:border-white/5 text-xs">
                <span id="passagesPaginationInfo" class="text-slate-500 dark:text-slate-400 font-medium">Affichage des passages...</span>
                <div class="flex items-center gap-1.5" id="passagesPaginationControls"></div>
            </div>
        </div>

    </div>

    <!-- ================= MODALE ENREGISTREMENT PASSAGE ================= -->
    <div id="passageModal" class="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center hidden z-[60] p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-5xl max-h-[96vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">

            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50/50 dark:bg-black/20">
                <div class="flex items-center space-x-2">
                    <div class="w-8 h-8 rounded-xl bg-brand-neon/20 text-brand-neon flex items-center justify-center font-bold text-sm">
                        <i class="ph-bold ph-door-open"></i>
                    </div>
                    <h3 id="passageModalTitle" class="text-base font-bold text-slate-800 dark:text-white">Enregistrer un Passage Patient</h3>
                </div>
                <button type="button" onclick="closePassageModal()" class="text-slate-400 hover:text-white p-1 rounded-lg">
                    <i class="ph-bold ph-x text-lg"></i>
                </button>
            </div>

            <form id="passageForm" onsubmit="handlePassageSubmit(event)" class="p-6 overflow-y-auto space-y-6">
                <input type="hidden" id="passageId">
                <input type="hidden" id="passagePatientId">
                <input type="hidden" id="passageMedecinTraitantId">
                <input type="hidden" id="passageRemiseType" value="DA">
                <input type="hidden" id="passageRemiseMontantDA" value="0">

                <!-- 1. IDENTIFICATION PATIENT ET MÉDECIN TRAITANT -->
                <div class="bg-slate-50 dark:bg-black/30 p-4 rounded-3xl border border-slate-200/80 dark:border-white/10 space-y-3">
                    <div class="flex items-center justify-between">
                        <label class="block text-xs font-bold text-slate-800 dark:text-white">1. Patient & Médecin Traitant *</label>
                        <div class="flex items-center gap-2">
                            <button type="button" onclick="openPassagePatientForm()" class="bg-brand-neon text-black px-3 py-1.5 rounded-xl text-xs font-bold shadow-sm inline-flex items-center gap-1.5 hover:opacity-90 transition cursor-pointer">
                                <i class="ph-bold ph-user-plus"></i>
                                <span>+ Nouveau Patient</span>
                            </button>
                            <button type="button" onclick="openFullDoctorModal()" class="bg-slate-800 hover:bg-black text-white px-3 py-1.5 rounded-xl text-xs font-bold shadow-sm inline-flex items-center gap-1.5 transition cursor-pointer">
                                <i class="ph-bold ph-stethoscope text-brand-neon"></i>
                                <span>+ Nouveau Médecin Traitant</span>
                            </button>
                        </div>
                    </div>

                    <!-- Recherche patient -->
                    <div class="relative">
                        <div class="flex items-center bg-white dark:bg-[#121c15] rounded-2xl px-3.5 py-2.5 border border-slate-200 dark:border-white/10 focus-within:border-brand-neon">
                            <i class="ph ph-magnifying-glass text-slate-400 mr-2 text-base"></i>
                            <input type="text" id="passagePatientSearchInput" oninput="searchPassagePatientLive()" onkeydown="handlePassagePatientSearchKeydown(event)" role="combobox" aria-autocomplete="list" aria-controls="passagePatientSearchResults" aria-expanded="false" placeholder="Nom, prénom, téléphone, dossier ou date de naissance..." class="w-full text-xs outline-none bg-transparent text-slate-800 dark:text-white font-bold">
                        </div>
                        <div id="passagePatientSearchResults" role="listbox" class="hidden absolute top-full left-0 right-0 mt-1 bg-white dark:bg-[#121c15] rounded-2xl shadow-xl border border-slate-200 dark:border-white/10 max-h-48 overflow-y-auto z-30 divide-y divide-slate-100 dark:divide-white/5"></div>
                    </div>

                    <div id="patientSelectedSummaryCard" class="hidden p-3 bg-white dark:bg-[#121c15] rounded-2xl border border-brand-neon/40 flex items-center justify-between">
                        <div class="flex items-center space-x-3">
                            <img id="selectedPatientAvatar" src="" class="w-10 h-10 rounded-full object-cover border border-brand-neon">
                            <div>
                                <span id="selectedPatientName" class="text-xs font-bold text-slate-800 dark:text-white block"></span>
                                <span id="selectedPatientDetails" class="text-[11px] text-slate-400"></span>
                            </div>
                        </div>
                        <button type="button" onclick="clearSelectedPatient()" class="text-rose-500 hover:text-rose-600 text-xs font-bold px-2 py-1">Changer</button>
                    </div>

                    <!-- Recherche médecin traitant -->
                    <div class="pt-1">
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Médecin Traitant * (Obligatoire)</label>
                        <div class="relative">
                            <div class="flex items-center bg-white dark:bg-[#121c15] rounded-2xl px-3.5 py-2 border border-slate-200 dark:border-white/10 focus-within:border-brand-neon">
                                <i class="ph ph-stethoscope text-slate-400 mr-2 text-base"></i>
                                <input type="text" id="passageMedecinTraitantSearchInput" oninput="searchPassageMedecinTraitantLive()" onkeydown="handlePassageMedecinTraitantSearchKeydown(event)" role="combobox" aria-autocomplete="list" aria-controls="passageMedecinTraitantSearchResults" aria-expanded="false" placeholder="Rechercher par nom du médecin traitant..." class="w-full text-xs outline-none bg-transparent text-slate-800 dark:text-white font-bold">
                            </div>
                            <div id="passageMedecinTraitantSearchResults" role="listbox" class="hidden absolute top-full left-0 right-0 mt-1 bg-white dark:bg-[#121c15] rounded-2xl shadow-xl border border-slate-200 dark:border-white/10 max-h-48 overflow-y-auto z-30 divide-y divide-slate-100 dark:divide-white/5"></div>
                        </div>

                        <div id="medecinTraitantSelectedCard" class="hidden mt-2 p-2.5 bg-brand-neon/10 rounded-2xl border border-brand-neon/30 flex items-center justify-between">
                            <div>
                                <span id="selectedMedecinTraitantName" class="text-xs font-bold text-emerald-800 dark:text-brand-neon block"></span>
                                <span id="selectedMedecinTraitantDetails" class="text-[10px] text-slate-400"></span>
                            </div>
                            <button type="button" onclick="clearSelectedMedecinTraitant()" class="text-rose-500 text-xs font-bold px-2 py-1">Retirer</button>
                        </div>
                    </div>
                </div>

                <div id="passagePersonalEditFields" class="hidden bg-white dark:bg-[#121c15] p-4 rounded-3xl border border-sky-200 dark:border-sky-900/40 space-y-3">
                    <div><h4 class="text-xs font-bold text-slate-800 dark:text-white">Informations personnelles du patient</h4><p class="text-[10px] text-slate-500 mt-1">Corrigez ici l’identité et les coordonnées avant de poursuivre le passage.</p></div>
                    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                        <label><span class="block mb-1 text-[10px] font-semibold text-slate-600 dark:text-slate-300">Nom *</span><input id="passageEditPatientNom" required disabled maxlength="100" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs text-slate-800 dark:text-white"></label>
                        <label><span class="block mb-1 text-[10px] font-semibold text-slate-600 dark:text-slate-300">Prénom *</span><input id="passageEditPatientPrenom" required disabled maxlength="100" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs text-slate-800 dark:text-white"></label>
                        <label><span class="block mb-1 text-[10px] font-semibold text-slate-600 dark:text-slate-300">Date de naissance *</span><input id="passageEditPatientDob" type="date" required disabled class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs text-slate-800 dark:text-white"></label>
                        <label><span class="block mb-1 text-[10px] font-semibold text-slate-600 dark:text-slate-300">Genre *</span><select id="passageEditPatientGenre" required disabled class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs text-slate-800 dark:text-white"><option>Homme</option><option>Femme</option><option>Autre</option></select></label>
                        <label><span class="block mb-1 text-[10px] font-semibold text-slate-600 dark:text-slate-300">Groupe sanguin *</span><select id="passageEditPatientBlood" required disabled class="w-full px-3 py-2.5 rounded-xl border border-rose-200 dark:border-rose-900/40 bg-rose-50/40 dark:bg-rose-950/20 text-xs font-bold text-rose-700 dark:text-rose-300">{groupes_options}</select></label>
                        <label><span class="block mb-1 text-[10px] font-semibold text-slate-600 dark:text-slate-300">Téléphone principal *</span><input id="passageEditPatientPhone1" type="tel" required disabled maxlength="30" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs text-slate-800 dark:text-white"></label>
                        <label><span class="block mb-1 text-[10px] font-semibold text-slate-600 dark:text-slate-300">Téléphone secondaire</span><input id="passageEditPatientPhone2" type="tel" disabled maxlength="30" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs text-slate-800 dark:text-white"></label>
                        <label><span class="block mb-1 text-[10px] font-semibold text-slate-600 dark:text-slate-300">E-mail</span><input id="passageEditPatientEmail" type="email" disabled maxlength="254" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs text-slate-800 dark:text-white"></label>
                        <label><span class="block mb-1 text-[10px] font-semibold text-slate-600 dark:text-slate-300">Wilaya *</span><select id="passageEditPatientWilaya" required disabled class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs text-slate-800 dark:text-white"><option value="">Choisir une wilaya</option>{wilayas_options}</select></label>
                        <label><span class="block mb-1 text-[10px] font-semibold text-slate-600 dark:text-slate-300">Commune *</span><input id="passageEditPatientCommune" required disabled maxlength="100" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs text-slate-800 dark:text-white"></label>
                    </div>
                </div>

                <!-- 2. DATE, HEURE & TYPE PASSAGE -->
                <div class="grid grid-cols-2 sm:grid-cols-4 gap-3">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Date *</label>
                        <input type="date" id="passageDate" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-medium outline-none text-slate-800 dark:text-white">
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Heure *</label>
                        <input type="time" id="passageHeure" required onchange="autoDetectTarifPeriod()" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-medium outline-none text-slate-800 dark:text-white">
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Discipline / Type de Passage *</label>
                        <select id="passageTypeSelect" required onchange="onPassageTypeChange()" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white outline-none focus:border-brand-neon">
                            <option value="" disabled selected>-- Sélectionnez votre spécialité --</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Statut *</label>
                        <select id="passageStatutSelect" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white outline-none">
                            <option value="En attente">⏳ En attente</option>
                            <option value="En cours">⚡ En cours</option>
                            <option value="Terminé">✅ Terminé</option>
                        </select>
                    </div>
                </div>

                <!-- 3. PRESTATIONS & ACTES -->
                <div class="bg-slate-50 dark:bg-black/30 p-4 rounded-3xl border border-slate-200/80 dark:border-white/10 space-y-3">
                    <div class="flex items-center justify-between">
                        <h4 class="text-xs font-bold text-slate-800 dark:text-white">2. Actes Médicaux & Prestations * (Au moins un acte requis)</h4>
                        <span id="autoTarifBadge" class="text-[10px] font-bold px-2.5 py-0.5 rounded-full bg-emerald-100 text-emerald-800">Tarif auto selon l'heure</span>
                    </div>

                    <div class="grid grid-cols-12 gap-2 bg-white dark:bg-[#121c15] p-3 rounded-2xl border border-slate-200 dark:border-white/10 items-end">
                        <div class="col-span-6 relative">
                            <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Rechercher une Prestation</label>
                            <input type="text" id="prestationSearchInput" oninput="searchPrestationsLive()" placeholder="Tapez le nom de l'acte ou consultation..." class="w-full text-xs p-2 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 font-medium text-slate-800 dark:text-white">
                            <div id="prestationSearchResultsTable" class="hidden absolute top-full left-0 right-0 mt-1 bg-white dark:bg-[#121c15] rounded-2xl shadow-xl border border-slate-200 dark:border-white/10 max-h-48 overflow-y-auto z-30 divide-y divide-slate-100 dark:divide-white/5"></div>
                        </div>

                        <div class="col-span-2">
                            <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Période</label>
                            <select id="selectAddPeriode" class="w-full text-xs p-2 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 font-bold text-slate-700 dark:text-slate-300">
                                <option value="Jour">☀️ Jour</option>
                                <option value="Garde">🌙 Nuit / Garde</option>
                            </select>
                        </div>
                        <div class="col-span-2">
                            <label class="block text-[10px] font-bold text-slate-400 mb-1 uppercase">Qté</label>
                            <input type="number" id="inputAddQuantite" value="1" min="1" class="w-full text-xs p-2 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-center font-bold text-slate-800 dark:text-white">
                        </div>
                        <div class="col-span-2">
                            <button type="button" onclick="addSelectedPrestationToTable()" class="w-full bg-brand-neon text-black py-2 rounded-xl text-xs font-bold shadow-sm inline-flex items-center justify-center gap-1 hover:opacity-90">
                                <i class="ph-bold ph-plus"></i>
                                <span>Ajouter</span>
                            </button>
                        </div>
                    </div>

                    <table class="w-full text-left text-xs bg-white dark:bg-[#121c15] rounded-2xl overflow-hidden border border-slate-200 dark:border-white/10">
                        <thead class="bg-slate-100 dark:bg-black/40 text-slate-500 font-semibold border-b border-slate-200 dark:border-white/10">
                            <tr>
                                <th class="p-2.5">Prestation</th>
                                <th class="p-2.5">Période</th>
                                <th class="p-2.5 text-center">Qté</th>
                                <th class="p-2.5">Tarif Unitaire</th>
                                <th class="p-2.5 font-bold">Sous-total</th>
                                <th class="p-2.5 text-right">Action</th>
                            </tr>
                        </thead>
                        <tbody id="passageActsTableBody" class="divide-y divide-slate-100 dark:divide-white/5 font-medium"></tbody>
                    </table>
                </div>

                <!-- 4. OBSERVATION MÉDICALE -->
                <div class="bg-white dark:bg-[#121c15] p-4 rounded-3xl border border-slate-200 dark:border-white/10 space-y-2">
                    <label class="block text-xs font-bold text-slate-800 dark:text-white">3. Observation Médicale & Renseignements Cliniques *</label>
                    <textarea id="passageObservation" rows="2" oninput="checkPassageFormValidity()" placeholder="Indiquez au moins une observation clinique, indication ou motif d'examen..." class="w-full text-xs p-3 rounded-2xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium"></textarea>
                </div>

                <!-- 5. ENCAISSEMENT & REMISE -->
                <div id="passageFinancialSection" class="bg-brand-neon/10 dark:bg-brand-neon/5 p-4 rounded-3xl border border-brand-neon/20 space-y-4">
                    <div class="flex items-center justify-between border-b border-brand-neon/20 pb-2">
                        <h4 class="text-xs font-bold text-emerald-950 dark:text-brand-neon uppercase tracking-wider flex items-center gap-1.5">
                            <i class="ph-bold ph-credit-card text-base"></i>
                            <span>4. Encaissement, Remise & Versement</span>
                        </h4>
                    </div>

                    <div class="grid grid-cols-12 gap-3 items-end">
                        <div class="col-span-3">
                            <label class="block text-[10px] font-bold text-slate-500 mb-1 uppercase">Montant Brut</label>
                            <input type="text" id="passageMontantBrut" readonly value="0.00" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/40 font-mono font-bold text-slate-800 dark:text-white">
                        </div>
                        <div class="col-span-3">
                            <div class="flex justify-between items-center mb-1">
                                <label class="block text-[10px] font-bold text-slate-500 uppercase">Remise</label>
                                <div class="inline-flex rounded-lg border border-slate-200 dark:border-white/10 bg-white dark:bg-black/40 p-0.5">
                                    <button type="button" onclick="setRemiseType('DA')" id="btnRemiseDA" class="px-2 py-0.5 rounded-md text-[10px] font-bold bg-brand-neon text-black">DA</button>
                                    <button type="button" onclick="setRemiseType('%')" id="btnRemisePct" class="px-2 py-0.5 rounded-md text-[10px] font-bold text-slate-500 hover:text-white">%</button>
                                </div>
                            </div>
                            <div class="relative">
                                <input type="number" step="any" id="passageRemiseValeur" value="0" oninput="calculatePassageFinancials()" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/40 font-mono font-bold text-rose-500 outline-none">
                                <span id="remiseUnitLabel" class="absolute right-3 top-2.5 text-xs font-bold text-slate-400">DA</span>
                            </div>
                        </div>
                        <div class="col-span-2">
                            <label class="block text-[10px] font-bold text-slate-500 mb-1 uppercase">Accordée Par</label>
                            <input type="text" id="passageAccordeurRemise" placeholder="Ex: Direction..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/40 font-medium text-slate-800 dark:text-white outline-none">
                        </div>
                        <div class="col-span-2">
                            <label class="block text-[10px] font-bold text-slate-500 mb-1 uppercase">Versement</label>
                            <input type="number" step="100" id="passageVersement" value="0" oninput="calculatePassageFinancials()" class="w-full text-xs p-2.5 rounded-xl border border-brand-neon/50 bg-white dark:bg-black/40 font-mono font-black text-emerald-600 dark:text-brand-neon outline-none">
                        </div>
                        <div class="col-span-2">
                            <label class="block text-[10px] font-bold text-slate-500 mb-1 uppercase">Reste (DA)</label>
                            <input type="text" id="passageReste" readonly value="0.00" class="w-full text-xs p-2.5 rounded-xl border border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/40 font-mono font-black text-amber-600 text-center">
                        </div>
                    </div>

                    <div class="flex items-center justify-between pt-1">
                        <div class="flex items-center space-x-3">
                            <label class="text-xs font-bold text-slate-700 dark:text-slate-300">Règlement :</label>
                            <select id="passageModePaiement" class="text-xs p-2 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-black/40 font-medium text-slate-800 dark:text-white outline-none">
                                <option value="Espèces">💵 Espèces</option>
                                <option value="Carte CIB / Edahabia">💳 Carte CIB / Edahabia</option>
                                <option value="Chèque">📝 Chèque</option>
                                <option value="Prise en Charge (PEC)">📑 Prise en Charge (PEC)</option>
                            </select>
                        </div>

                        <div class="text-right">
                            <span class="text-xs font-bold text-slate-400">Net à Payer : </span>
                            <strong id="passageNetAPayerDisplay" class="text-base font-black text-emerald-800 dark:text-brand-neon font-mono">0.00 DA</strong>
                        </div>
                    </div>
                </div>

                <!-- 6. NUMÉRISATION -->
                <div class="bg-slate-50 dark:bg-black/30 p-4 rounded-3xl border border-slate-200/80 dark:border-white/10 space-y-3">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                        <div>
                            <h4 class="text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5">
                                <i class="ph-bold ph-files text-brand-neon text-base"></i>
                                <span>5. Numérisation des Demandes d'Examen & Pièces</span>
                            </h4>
                        </div>
                        <div class="flex items-center space-x-2">
                            <label for="passageDocUploadInput" class="cursor-pointer bg-white dark:bg-[#121c15] text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10 hover:border-brand-neon px-3 py-1.5 rounded-xl text-xs font-bold shadow-sm inline-flex items-center gap-1.5 transition">
                                <i class="ph-bold ph-upload text-brand-neon"></i><span>Téléverser Scan</span>
                            </label>
                            <input type="file" id="passageDocUploadInput" accept="image/*,.pdf" class="hidden" onchange="previewPassageDoc(event)">

                            <button type="button" onclick="openPassageHardwareScannerModal()" class="bg-brand-neon text-black px-3.5 py-1.5 rounded-xl text-xs font-bold shadow-sm inline-flex items-center gap-1.5 hover:opacity-90 transition">
                                <i class="ph-bold ph-scan"></i><span>SCANNER DOC</span>
                            </button>
                        </div>
                    </div>
                    <div class="grid grid-cols-1 sm:grid-cols-3 gap-2 items-center bg-white dark:bg-[#121c15] p-3 rounded-2xl border border-slate-200 dark:border-white/10">
                        <label class="text-xs font-bold text-slate-700 dark:text-slate-300">Type de Document :</label>
                        <div class="sm:col-span-2">
                            <select id="passageDocTypeSelect" class="w-full text-xs p-2 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white outline-none">
                                {doc_types_options}
                            </select>
                        </div>
                    </div>
                    <div id="passageScannedDocsGallery" class="grid grid-cols-2 sm:grid-cols-4 gap-2.5 pt-1"></div>
                </div>

                <!-- 7. QUESTIONNAIRE MÉDICAL BILINGUE EXACT -->
                <div class="passage-questionnaire-section bg-white dark:bg-[#121c15] p-5 rounded-3xl border border-slate-200 dark:border-white/10 space-y-4">
                    <h4 class="text-xs font-bold text-slate-800 dark:text-white uppercase tracking-wider border-b border-slate-100 dark:border-white/5 pb-2">
                        📋 6. Questionnaire Médical & Antécédents
                    </h4>

                    <div class="grid grid-cols-1 md:grid-cols-12 gap-5 items-start">
                        <div class="md:col-span-8 space-y-2.5 text-xs font-bold text-slate-800 dark:text-slate-200">
                            <div class="flex items-center justify-between py-1.5 border-b border-slate-100 dark:border-white/5">
                                <span class="w-48 text-left">Patient Hospitalisé</span>
                                <div class="flex items-center space-x-4">
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_hospitalise" value="OUI" onchange="checkPassageFormValidity()" class="text-brand-neon"><span class="text-xs">OUI</span></label>
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_hospitalise" value="NON" checked onchange="checkPassageFormValidity()" class="text-brand-neon"><span class="text-xs">NON</span></label>
                                </div>
                                <span class="w-44 text-right text-slate-500 font-semibold" style="direction: rtl;">مريض في المشفى</span>
                            </div>

                            <div class="flex items-center justify-between py-1.5 border-b border-slate-100 dark:border-white/5">
                                <span class="w-48 text-left font-semibold">Ancien Dossier : <strong class="text-slate-900 dark:text-white">Chez Nous</strong></span>
                                <div class="flex items-center space-x-4">
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_dossier_interne" value="OUI" onchange="checkPassageFormValidity()" class="text-brand-neon"><span class="text-xs">OUI</span></label>
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_dossier_interne" value="NON" checked onchange="checkPassageFormValidity()" class="text-brand-neon"><span class="text-xs">NON</span></label>
                                </div>
                                <span class="w-44 text-right text-slate-500 font-semibold" style="direction: rtl;">مرفق بملف داخلي</span>
                            </div>

                            <div class="flex items-center justify-between py-1.5 border-b border-slate-100 dark:border-white/5">
                                <span class="w-48 text-left font-semibold pl-4">Dossier Externe</span>
                                <div class="flex items-center space-x-4">
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_dossier_externe" value="OUI" onchange="checkPassageFormValidity()" class="text-brand-neon"><span class="text-xs">OUI</span></label>
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_dossier_externe" value="NON" checked onchange="checkPassageFormValidity()" class="text-brand-neon"><span class="text-xs">NON</span></label>
                                </div>
                                <span class="w-44 text-right text-slate-500 font-semibold" style="direction: rtl;">مرفق بملف خارجي</span>
                            </div>

                            <div class="flex items-center justify-between py-1.5 border-b border-slate-100 dark:border-white/5 bg-rose-500/5 px-2 rounded-xl">
                                <span class="w-48 text-left text-rose-600 font-extrabold flex items-center gap-1">🚨 Urgence Vitale</span>
                                <div class="flex items-center space-x-4">
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_urgence_vitale" value="OUI" onchange="checkPassageFormValidity()" class="text-rose-600"><span class="text-xs text-rose-600 font-bold">OUI</span></label>
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_urgence_vitale" value="NON" checked onchange="checkPassageFormValidity()" class="text-slate-400"><span class="text-xs">NON</span></label>
                                </div>
                                <span class="w-44 text-right text-rose-600 font-bold" style="direction: rtl;">حالة مستعجلة</span>
                            </div>

                            <div class="flex items-center justify-between py-1.5 border-b border-slate-100 dark:border-white/5">
                                <span class="w-48 text-left">Grossesse</span>
                                <div class="flex items-center space-x-4">
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_grossesse" value="OUI" onchange="checkPassageFormValidity()" class="text-brand-neon"><span class="text-xs">OUI</span></label>
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_grossesse" value="NON" checked onchange="checkPassageFormValidity()" class="text-brand-neon"><span class="text-xs">NON</span></label>
                                </div>
                                <span class="w-44 text-right text-slate-500 font-semibold" style="direction: rtl;">الحمل</span>
                            </div>

                            <div class="flex items-center justify-between py-1.5 border-b border-slate-100 dark:border-white/5">
                                <span class="w-48 text-left">Allergie</span>
                                <div class="flex items-center space-x-4">
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_allergie" value="OUI" onchange="checkPassageFormValidity()" class="text-brand-neon"><span class="text-xs">OUI</span></label>
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_allergie" value="NON" checked onchange="checkPassageFormValidity()" class="text-brand-neon"><span class="text-xs">NON</span></label>
                                </div>
                                <span class="w-44 text-right text-slate-500 font-semibold" style="direction: rtl;">الحساسية</span>
                            </div>

                            <div class="flex items-center justify-between py-1.5 border-b border-slate-100 dark:border-white/5">
                                <span class="w-48 text-left">Diabète</span>
                                <div class="flex items-center space-x-4">
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_diabete" value="OUI" onchange="checkPassageFormValidity()" class="text-brand-neon"><span class="text-xs">OUI</span></label>
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_diabete" value="NON" checked onchange="checkPassageFormValidity()" class="text-brand-neon"><span class="text-xs">NON</span></label>
                                </div>
                                <span class="w-44 text-right text-slate-500 font-semibold" style="direction: rtl;">السكري</span>
                            </div>

                            <div class="flex items-center justify-between py-1.5 border-b border-slate-100 dark:border-white/5">
                                <span class="w-48 text-left">Hypertension (HTA)</span>
                                <div class="flex items-center space-x-4">
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_hta" value="OUI" onchange="checkPassageFormValidity()" class="text-brand-neon"><span class="text-xs">OUI</span></label>
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_hta" value="NON" checked onchange="checkPassageFormValidity()" class="text-brand-neon"><span class="text-xs">NON</span></label>
                                </div>
                                <span class="w-44 text-right text-slate-500 font-semibold" style="direction: rtl;">ارتفاع ضغط الدم</span>
                            </div>

                            <div class="flex items-center justify-between py-1.5 border-b border-slate-100 dark:border-white/5">
                                <span class="w-48 text-left">Asthme</span>
                                <div class="flex items-center space-x-4">
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_asthme" value="OUI" onchange="checkPassageFormValidity()" class="text-brand-neon"><span class="text-xs">OUI</span></label>
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_asthme" value="NON" checked onchange="checkPassageFormValidity()" class="text-brand-neon"><span class="text-xs">NON</span></label>
                                </div>
                                <span class="w-44 text-right text-slate-500 font-semibold" style="direction: rtl;">الربو</span>
                            </div>

                            <div class="flex items-center justify-between py-1.5">
                                <span class="w-48 text-left">Insuffisance Rénale</span>
                                <div class="flex items-center space-x-4">
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_insuffisance_renale" value="OUI" onchange="checkPassageFormValidity()" class="text-brand-neon"><span class="text-xs">OUI</span></label>
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_insuffisance_renale" value="NON" checked onchange="checkPassageFormValidity()" class="text-brand-neon"><span class="text-xs">NON</span></label>
                                </div>
                                <span class="w-44 text-right text-slate-500 font-semibold" style="direction: rtl;">قصور كلوي</span>
                            </div>
                        </div>

                        <!-- Colonne Implants -->
                        <div class="md:col-span-4 bg-sky-50/70 dark:bg-black/40 p-4 rounded-3xl border border-sky-100 dark:border-white/10 space-y-4">
                            <h5 class="text-xs font-black text-sky-950 dark:text-sky-300 uppercase tracking-tight flex items-center gap-1.5">
                                <i class="ph-bold ph-magnet"></i>
                                <span>Implants & Dispositifs</span>
                            </h5>

                            <div class="space-y-1.5 border-b border-sky-100 dark:border-white/5 pb-2">
                                <span class="text-xs font-bold text-slate-800 dark:text-slate-200 block">Pace Maker</span>
                                <div class="flex items-center space-x-4">
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_pace_maker" value="OUI" onchange="checkPassageFormValidity()" class="text-rose-600"><span class="text-xs font-bold text-rose-600">OUI</span></label>
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_pace_maker" value="NON" checked onchange="checkPassageFormValidity()" class="text-slate-400"><span class="text-xs font-bold">NON</span></label>
                                </div>
                            </div>

                            <div class="space-y-1.5 border-b border-sky-100 dark:border-white/5 pb-2">
                                <span class="text-xs font-bold text-slate-800 dark:text-slate-200 block">Valve Cardiaque</span>
                                <div class="flex items-center space-x-4">
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_valve_cardiaque" value="OUI" onchange="checkPassageFormValidity()" class="text-rose-600"><span class="text-xs font-bold text-rose-600">OUI</span></label>
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_valve_cardiaque" value="NON" checked onchange="checkPassageFormValidity()" class="text-slate-400"><span class="text-xs font-bold">NON</span></label>
                                </div>
                            </div>

                            <div class="space-y-1.5">
                                <span class="text-xs font-bold text-slate-800 dark:text-slate-200 block">Prothèse Métallique</span>
                                <div class="flex items-center space-x-4">
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_prothese_metallique" value="OUI" onchange="checkPassageFormValidity()" class="text-rose-600"><span class="text-xs font-bold text-rose-600">OUI</span></label>
                                    <label class="inline-flex items-center space-x-1.5 cursor-pointer"><input type="radio" name="q_prothese_metallique" value="NON" checked onchange="checkPassageFormValidity()" class="text-slate-400"><span class="text-xs font-bold">NON</span></label>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- ACTIONS DU FORMULAIRE : BOUTON VERROUILLÉ SELON LES 4 CONDITIONS -->
                <div class="flex items-center justify-between pt-4 border-t border-slate-100 dark:border-white/5">
                    <span id="passageValidationHint" class="text-[11px] text-amber-500 font-bold">
                        ⚠️ Requis : Patient, Médecin Traitant, au moins 1 acte et 1 observation.
                    </span>
                    <div class="flex items-center gap-3">
                        <button type="button" onclick="closePassageModal()" class="px-5 py-2.5 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler</button>
                        <button type="submit" id="btnSubmitPassage" disabled class="px-7 py-2.5 rounded-xl bg-slate-300 dark:bg-slate-700 text-slate-500 font-bold text-xs shadow-none cursor-not-allowed transition">
                            Enregistrer & Émettre Demande
                        </button>
                    </div>
                </div>
            </form>
        </div>
    </div>

    <!-- ================= MODALE PATIENT COMPLÈTE AU PREMIER PLAN (Z-[100]) ================= -->
    <div id="receptionFullPatientModal" class="fixed inset-0 bg-black/85 backdrop-blur-md flex items-center justify-center hidden z-[100] p-2 sm:p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-4xl max-h-[96vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex items-center justify-between bg-slate-50/50 dark:bg-black/20 flex-shrink-0">
                <h3 class="text-sm font-bold text-slate-800 dark:text-white flex items-center gap-2">
                    <i class="ph-bold ph-user-plus text-brand-neon"></i> <span id="fullPatientModalTitle">Fiche Patient Complète (Sélection Automatique dans le Passage)</span>
                </h3>
                <button type="button" onclick="closeFullPatientModal()" class="text-slate-400 hover:text-white p-1">✕</button>
            </div>
            <form id="receptionFullPatientForm" onsubmit="handleFullPatientSubmit(event)" class="p-6 overflow-y-auto space-y-5">
                <input type="hidden" id="recPatAvatar">
                <input type="hidden" id="recPatPhotoFace">
                <input type="hidden" id="recPatPhotoPDroit">
                <input type="hidden" id="recPatPhotoPGauche">
                <input type="hidden" id="recPatEmpreinte">

                <!-- 1. BIOMÉTRIE & WEBCAM -->
                <div class="bg-brand-neon/10 dark:bg-brand-neon/5 p-4 rounded-3xl border border-brand-neon/20 space-y-3">
                    <div class="flex items-center justify-between">
                        <span class="text-xs font-bold text-slate-800 dark:text-white">Photos Biométriques & Profils</span>
                        <div class="flex items-center gap-2">
                            <label for="recPatAvatarUpload" class="cursor-pointer bg-white dark:bg-[#121c15] text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10 px-3 py-1.5 rounded-xl text-xs font-bold shadow-sm inline-flex items-center gap-1.5 hover:bg-slate-50 transition">
                                <i class="ph-bold ph-upload text-brand-neon"></i><span>Charger...</span>
                            </label>
                            <input type="file" id="recPatAvatarUpload" accept="image/*" class="hidden" onchange="previewRecPatAvatar(event)">
                        </div>
                    </div>
                    <div class="grid grid-cols-3 gap-3">
                        <div class="bg-white dark:bg-black/30 p-2 rounded-2xl border border-slate-200 dark:border-white/10 flex items-center space-x-2.5">
                            <img id="recPreviewFace" src="/static/images/avatar-placeholder.svg?w=120" class="w-10 h-10 rounded-xl object-cover border border-brand-neon">
                            <span class="text-[11px] font-bold text-slate-800 dark:text-white">1. Face</span>
                        </div>
                        <div class="bg-white dark:bg-black/30 p-2 rounded-2xl border border-slate-200 dark:border-white/10 flex items-center space-x-2.5">
                            <img id="recPreviewPDroit" src="/static/images/avatar-placeholder.svg?w=120" class="w-10 h-10 rounded-xl object-cover border border-slate-200 opacity-40">
                            <span class="text-[11px] font-bold text-slate-400">2. Profil Droit</span>
                        </div>
                        <div class="bg-white dark:bg-black/30 p-2 rounded-2xl border border-slate-200 dark:border-white/10 flex items-center space-x-2.5">
                            <img id="recPreviewPGauche" src="/static/images/avatar-placeholder.svg?w=120" class="w-10 h-10 rounded-xl object-cover border border-slate-200 opacity-40">
                            <span class="text-[11px] font-bold text-slate-400">3. Profil Gauche</span>
                        </div>
                    </div>
                </div>

                <!-- 2. ÉTAT CIVIL & ÂGE -->
                <div class="grid grid-cols-2 sm:grid-cols-4 gap-3">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Nom *</label>
                        <input type="text" id="recPatNom" required placeholder="Ex: BENALI" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-bold uppercase text-slate-800 dark:text-white">
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Prénom *</label>
                        <input type="text" id="recPatPrenom" required placeholder="Ex: Amine" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-medium text-slate-800 dark:text-white">
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Date de Naissance *</label>
                        <input type="date" id="recPatDob" required onchange="calculateRecPatAge()" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none focus:border-brand-neon bg-slate-50 dark:bg-black/30 font-medium text-slate-800 dark:text-white">
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Âge Calculé</label>
                        <input type="text" id="recPatAgeDisplay" readonly placeholder="Auto" class="w-full text-xs p-2.5 rounded-xl border border-brand-neon/30 bg-brand-neon/10 text-emerald-800 dark:text-brand-neon font-bold text-center">
                    </div>
                </div>

                <!-- 3. GENRE & GROUPE SANGUIN -->
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Genre *</label>
                        <select id="recPatGenre" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium">
                            <option value="Homme">👨 Homme</option><option value="Femme">👩 Femme</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Groupe Sanguin *</label>
                        <select id="recPatGroupe" required class="w-full text-xs p-2.5 rounded-xl border border-rose-200 dark:border-rose-900/40 bg-rose-50/40 dark:bg-rose-950/20 text-rose-600 dark:text-rose-300 font-bold">
                            {groupes_options}
                        </select>
                    </div>
                </div>

                <!-- 4. COORDONNÉES -->
                <div class="grid grid-cols-3 gap-3">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Téléphone 1 *</label>
                        <input type="text" id="recPatTel1" required placeholder="0550 00 00 00" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Téléphone 2</label>
                        <input type="text" id="recPatTel2" placeholder="0660 00 00 00" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Email</label>
                        <input type="email" id="recPatEmail" placeholder="patient@mail.dz" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white">
                    </div>
                </div>

                <!-- 5. 58 WILAYAS ET COMMUNES -->
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Wilaya *</label>
                        <select id="recPatWilaya" required onchange="handleRecPatWilayaChange()" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-bold outline-none">
                            {wilayas_options}
                        </select>
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Commune *</label>
                        <select id="recPatCommune" required class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-medium outline-none"></select>
                    </div>
                </div>

                <div class="flex items-center justify-end space-x-3 pt-3 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closeFullPatientModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler</button>
                    <button id="fullPatientModalSubmit" type="submit" class="px-6 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md">Enregistrer & Sélectionner</button>
                </div>
            </form>
        </div>
    </div>

    <!-- ================= MODALE MÉDECIN COMPLÈTE AU PREMIER PLAN (Z-[100]) ================= -->
    <div id="receptionFullDoctorModal" class="fixed inset-0 bg-black/85 backdrop-blur-md flex items-center justify-center hidden z-[100] p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-2xl shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex items-center justify-between bg-slate-50/50 dark:bg-black/20">
                <h3 class="text-sm font-bold text-slate-800 dark:text-white flex items-center gap-2">
                    <i class="ph-bold ph-stethoscope text-brand-neon"></i> Fiche Médecin Traitant Complète (Auto-sélectionné)
                </h3>
                <button type="button" onclick="closeFullDoctorModal()" class="text-slate-400 hover:text-white p-1">✕</button>
            </div>
            <form onsubmit="handleFullDoctorSubmit(event)" class="p-6 space-y-4">
                <div class="grid grid-cols-4 gap-3">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Grade</label>
                        <select id="recDocGrade" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-bold">
                            <option value="Dr">Dr</option><option value="Pr">Pr</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Nom *</label>
                        <input type="text" id="recDocNom" required placeholder="Ex: BENMANSOUR" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-bold uppercase outline-none focus:border-brand-neon">
                    </div>
                    <div class="col-span-2">
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Prénom *</label>
                        <input type="text" id="recDocPrenom" required placeholder="Ex: Karim" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white outline-none focus:border-brand-neon">
                    </div>
                </div>

                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Téléphone Direct</label>
                        <input type="text" id="recDocTel" placeholder="0550 00 00 00" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white outline-none focus:border-brand-neon">
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Email</label>
                        <input type="email" id="recDocEmail" placeholder="dr@cabinet.dz" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white outline-none focus:border-brand-neon">
                    </div>
                </div>

                <div>
                    <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Adresse du Cabinet</label>
                    <input type="text" id="recDocAdresse" placeholder="Adresse complète..." class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white outline-none focus:border-brand-neon">
                </div>

                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Wilaya</label>
                        <select id="recDocWilaya" onchange="handleRecDocWilayaChange()" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white font-bold outline-none">
                            {wilayas_options}
                        </select>
                    </div>
                    <div>
                        <label class="block text-[11px] font-semibold text-slate-600 dark:text-slate-300 mb-1">Commune</label>
                        <select id="recDocCommune" class="w-full text-xs p-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/30 text-slate-800 dark:text-white outline-none"></select>
                    </div>
                </div>

                <div class="flex justify-end gap-2.5 pt-3 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closeFullDoctorModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler</button>
                    <button type="submit" class="px-5 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md">Enregistrer & Sélectionner</button>
                </div>
            </form>
        </div>
    </div>

    <!-- ================= MODALE SCANNEUR PHYSIQUE WIA ================= -->
    <div id="scannerDeviceModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center hidden z-[110] p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-md shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-6 py-4 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50 dark:bg-black/20">
                <div class="flex items-center space-x-2">
                    <div class="w-8 h-8 rounded-xl bg-brand-neon/20 text-brand-neon flex items-center justify-center font-bold text-sm">
                        <i class="ph-bold ph-printer"></i>
                    </div>
                    <h3 class="text-base font-bold text-slate-800 dark:text-white">Scanneur Physique Détecté</h3>
                </div>
                <button type="button" onclick="closeScannerDeviceModal()" class="text-slate-400 hover:text-white p-1 rounded-lg">
                    <i class="ph-bold ph-x text-lg"></i>
                </button>
            </div>

            <div class="p-6 space-y-4">
                <div id="scannerSelectionBox">
                    <label class="block text-xs font-bold text-slate-700 dark:text-slate-300 mb-1.5">Scanneur WIA Connecté</label>
                    <select id="scannerDeviceSelect" class="w-full text-xs p-3 rounded-2xl border border-slate-200 dark:border-white/10 outline-none bg-slate-50 dark:bg-black/30 font-bold text-slate-800 dark:text-white"></select>
                </div>

                <div id="noScannerAlert" class="hidden p-3.5 rounded-2xl bg-amber-500/10 border border-amber-500/20 text-amber-500 text-xs font-medium space-y-1">
                    <div class="font-bold flex items-center gap-1.5">⚠️ Aucun scanneur WIA détecté</div>
                    <p class="text-[11px] leading-snug">Vérifiez le câble USB de votre scanneur puis réessayez.</p>
                </div>

                <div id="scannerStatusMsg" class="hidden text-center py-2 text-xs font-bold text-brand-neon">
                    ⏳ Numérisation en cours... Veuillez patienter.
                </div>

                <div class="flex items-center justify-end space-x-2.5 pt-2 border-t border-slate-100 dark:border-white/5">
                    <button type="button" onclick="closeScannerDeviceModal()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler</button>
                    <button type="button" id="btnLaunchScan" onclick="executePassageHardwareScan()" class="px-5 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md inline-flex items-center gap-1.5 transition">
                        <i class="ph-bold ph-scan text-base"></i>
                        <span>Lancer le Scan</span>
                    </button>
                </div>
            </div>
        </div>
    </div>

    <!-- ================= MODALE APERÇU DOCUMENT AVANT VALIDATION ================= -->
    <div id="passageDocPreviewModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center hidden z-[10000] p-4" role="dialog" aria-modal="true" aria-labelledby="passageDocPreviewHeading" tabindex="-1">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-4xl max-h-[92vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-6 py-3.5 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50 dark:bg-black/20">
                <div>
                    <h3 id="passageDocPreviewHeading" class="text-sm font-bold text-slate-800 dark:text-white">Aperçu du Document Scanné</h3>
                    <p id="passageDocPreviewTitle" class="text-[11px] text-slate-400">Vérifiez la lisibilité avant validation définitive.</p>
                    <p id="passageDocPreviewSize" class="text-[11px] font-semibold text-slate-500 dark:text-slate-300 mt-1">Taille : — Mo</p>
                </div>
                <button type="button" onclick="cancelPassageDocPreview()" class="text-slate-400 hover:text-white p-1">
                    <i class="ph-bold ph-x text-lg"></i>
                </button>
            </div>
            <div class="p-6 overflow-auto flex-1 bg-slate-100 dark:bg-black/40 flex items-center justify-center">
                <iframe id="passageDocPreviewPdf" class="hidden w-full h-[65vh] rounded-2xl border border-slate-200 dark:border-white/10 bg-white"></iframe>
                <img id="passageDocPreviewImg" class="hidden max-w-full max-h-[65vh] rounded-2xl shadow-lg object-contain bg-white">
            </div>
            <div class="px-6 py-4 border-t border-slate-100 dark:border-white/5 bg-white dark:bg-[#121c15] flex justify-end gap-3">
                <button type="button" onclick="cancelPassageDocPreview()" class="px-4 py-2 rounded-xl text-slate-400 hover:text-white text-xs font-semibold">Annuler / Supprimer</button>
                <button type="button" onclick="validatePassageDocPreview()" class="px-5 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md">✓ Valider et Ajouter</button>
            </div>
        </div>
    </div>

    <!-- ================= MODALE VISIONNEUSE DOCUMENT SCANNÉ (LIGHTBOX) ================= -->
    <div id="passageDocLightboxModal" class="fixed inset-0 bg-black/85 backdrop-blur-md flex items-center justify-center hidden z-[110] p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-4xl max-h-[94vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-6 py-3.5 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50 dark:bg-black/20">
                <div class="flex items-center space-x-2">
                    <span class="w-2.5 h-2.5 rounded-full bg-brand-neon"></span>
                    <div class="min-w-0">
                        <h3 id="lightboxDocTitle" class="text-sm font-bold text-slate-800 dark:text-white">Document Scanné</h3>
                        <p id="lightboxDocSize" class="text-[10px] text-slate-500 dark:text-slate-400">Taille : — Mo</p>
                    </div>
                </div>
                <div class="flex flex-wrap items-center justify-end gap-1.5">
                    <span id="lightboxDocCounter" class="px-2 text-[10px] font-mono text-slate-500"></span>
                    <button type="button" id="lightboxPrevBtn" onclick="navigatePassageDoc(-1)" class="px-2 py-1.5 rounded-lg bg-slate-100 dark:bg-white/5 text-xs font-bold text-slate-700 dark:text-slate-300" title="Document précédent">◀</button>
                    <button type="button" id="lightboxNextBtn" onclick="navigatePassageDoc(1)" class="px-2 py-1.5 rounded-lg bg-slate-100 dark:bg-white/5 text-xs font-bold text-slate-700 dark:text-slate-300" title="Document suivant">▶</button>
                    <button type="button" onclick="changePassageDocZoom(-0.2)" class="w-8 h-8 rounded-lg bg-slate-100 dark:bg-white/5 text-slate-700 dark:text-slate-200 font-black" title="Zoom arrière">−</button>
                    <button type="button" onclick="resetPassageDocZoom()" id="lightboxZoomLabel" class="px-2 h-8 rounded-lg bg-slate-100 dark:bg-white/5 text-[10px] font-bold text-slate-700 dark:text-slate-200">100%</button>
                    <button type="button" onclick="changePassageDocZoom(0.2)" class="w-8 h-8 rounded-lg bg-slate-100 dark:bg-white/5 text-slate-700 dark:text-slate-200 font-black" title="Zoom avant">+</button>
                    <button type="button" onclick="rotateLightboxDoc()" class="px-2 py-1.5 rounded-lg bg-slate-100 dark:bg-white/5 text-xs font-bold text-slate-700 dark:text-slate-300" title="Pivoter 90°"><i class="ph-bold ph-arrows-clockwise text-brand-neon"></i></button>
                    <button type="button" onclick="printLightboxDoc()" class="px-2.5 py-1.5 rounded-xl bg-slate-900 text-white text-xs font-bold inline-flex items-center gap-1.5 hover:opacity-90 transition">
                        <i class="ph-bold ph-printer text-brand-neon"></i><span>Imprimer</span>
                    </button>
                    <button type="button" onclick="closePassageDocLightbox()" class="text-slate-400 hover:text-white p-1">
                        <i class="ph-bold ph-x text-lg"></i>
                    </button>
                </div>
            </div>
            <div id="passageDocViewerStage" class="p-4 overflow-auto bg-slate-200 dark:bg-black/60 min-h-[420px] flex justify-center">
                <iframe id="lightboxDocPdf" src="about:blank" class="hidden w-full h-[78vh] rounded-xl border border-slate-300 bg-white shadow-lg"></iframe>
                <img id="lightboxDocImg" src="" style="transform: rotate(0deg); width:100%; max-width:100%; height:auto; object-fit:contain;" class="hidden rounded-xl shadow-lg self-start transition-transform duration-200">
            </div>
        </div>
    </div>

    <!-- ================= MODALE VISUALISEUR DEMANDE D'EXAMEN A4 / PDF ================= -->
    <div id="demandeExamenModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center hidden z-[100] p-2 sm:p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-4xl h-[95vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-5 py-3 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50 dark:bg-black/20 flex-shrink-0">
                <h3 class="text-xs font-bold text-slate-800 dark:text-white flex items-center gap-1.5">
                    <i class="ph-bold ph-file-text text-brand-neon"></i>
                    <span>Fiche Demande d'Examen & Questionnaire (A4)</span>
                </h3>
                <div class="flex items-center space-x-2">
                    <button type="button" onclick="printDemandeExamenIframe()" class="px-3.5 py-1.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-sm inline-flex items-center gap-1 cursor-pointer">
                        <i class="ph-bold ph-printer"></i>
                        <span>Imprimer / PDF</span>
                    </button>
                    <button type="button" onclick="closeDemandeExamenModal()" class="px-3.5 py-1.5 rounded-xl bg-rose-500 hover:bg-rose-600 text-white text-xs font-bold shadow-sm inline-flex items-center gap-1 cursor-pointer">
                        <i class="ph-bold ph-x-circle"></i>
                        <span>Quitter le visionneur</span>
                    </button>
                </div>
            </div>
            <div class="p-2 overflow-y-auto bg-slate-200 dark:bg-black/60 flex justify-center flex-1">
                <iframe id="demandeExamenIframe" src="about:blank" class="w-full h-full rounded-2xl border-none shadow-md bg-white"></iframe>
            </div>
        </div>
    </div>

    {patient_form_component}

    <!-- SCRIPT DU MODULE ACCUEIL & PASSAGES -->
    <script>
        (function() {{
            const ALGERIA_WILAYAS_DATA = {wilayas_json};
            const IS_RADIOLOGY_MODE = {str(is_radiology_mode).lower()};
            let passagesData = [];
            let passageActionMenuCloseTimer = null;
            let canMagicDeletePassages = false;
            let canEditPatientInfo = false;
            let currentPatientEditDocs = [];
            let currentPatientEditQuestionnaire = {{}};
            let currentPatientEditFiles = {{}};
            let currentPatientEditPieces = [];
            let passageEditMode = false;
            let passageEditPatientData = null;
            let passageEditPatientDraft = null;
            let filteredPassagesList = [];
            let receptionInitData = {{ patients: [], consultations: [], medecins_traitants: [], horaires_tarification: {{}}, clinic_name: '' }};
            let passagePatientSearchMatches = [];
            let passagePatientSearchActiveIndex = -1;
            let passageMedecinSearchMatches = [];
            let passageMedecinSearchActiveIndex = -1;
            const normalizePassageSpecialite = value => String(value || '').normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/\\s+/g, ' ').trim().toLowerCase();
            let currentPassageActs = [];
            let currentPassageTypeValue = '';
            let currentPassageScannedDocs = [];
            let currentMatchingPrestations = [];
            let selectedPrestationTemp = null;
            let pendingPassageDocPreview = null;
            let currentLightboxRotation = 0;
            let passageViewerDocs = [];
            let passageViewerDocIndex = 0;
            let passageViewerZoom = 1;
            let passageDisplayStyle = 'worklist';
            try {{
                const savedPassageStyle = localStorage.getItem('clinivaPassagesDisplayStyleV2');
                passageDisplayStyle = ['classic', 'cards', 'worklist', 'essential'].includes(savedPassageStyle) ? savedPassageStyle : 'worklist';
            }} catch (_) {{}}

            let currentPassagePage = 1;
            const PASSAGES_PER_PAGE = 15;

            // =================================================================
            // 1. CHARGEMENT ET FILTRAGE
            // =================================================================
            window.loadReceptionPassagesModule = async function() {{
                try {{
                    const res = await fetch('/api/reception/passages?service=' + (IS_RADIOLOGY_MODE ? 'radiologie' : 'autres'));
                    const data = await res.json();
                    passagesData = data.passages || [];
                    canMagicDeletePassages = data.can_magic_delete === true;
                    canEditPatientInfo = data.can_edit_patient_info === true;
                    receptionInitData = data.init_data || {{}};

                    if (IS_RADIOLOGY_MODE) {{
                        document.querySelectorAll('#passageFiltersModal label:has(#filterPassageNumero), #passageFiltersModal label:has(#filterPassageTelephone), #passageFiltersModal label:has(#filterPassageDateNaissance), #passageFiltersModal label:has(#filterPassageMedecin), #passageFiltersModal label:has(#filterPassageActe), #passageFiltersModal label:has(#filterPassageType), #passageFiltersModal label:has(#filterPassageStatut)').forEach(label => label.style.display = 'none');
                        const modalityFilter = document.getElementById('filterPassageModalite');
                        if (modalityFilter) {{
                            const selected = modalityFilter.value;
                            modalityFilter.replaceChildren(new Option('Toutes les modalités', ''));
                            (receptionInitData.radiology_modalites || []).forEach(modality => modalityFilter.add(new Option(modality, modality)));
                            modalityFilter.value = selected;
                        }}
                    }}

                    const typeFilter = document.getElementById('filterPassageType');
                    if (typeFilter) {{
                        const selectedType = typeFilter.value;
                        const types = [...new Set(passagesData.map(p => p.type_passage).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'fr'));
                        typeFilter.replaceChildren(new Option('Tous les types', ''));
                        types.forEach(type => typeFilter.add(new Option(type, type)));
                        typeFilter.value = selectedType;
                    }}

                    const counter = document.getElementById('totalPassagesCount');
                    if (counter) counter.innerText = data.total_count || passagesData.length;
                    const dateStartFilter = document.getElementById('filterPassageDateDebut');
                    const dateEndFilter = document.getElementById('filterPassageDateFin');
                    if (dateStartFilter && dateEndFilter && !dateStartFilter.value && !dateEndFilter.value) {{
                        const today = new Date();
                        const todayLocal = `${{today.getFullYear()}}-${{String(today.getMonth() + 1).padStart(2, '0')}}-${{String(today.getDate()).padStart(2, '0')}}`;
                        dateStartFilter.value = todayLocal;
                        dateEndFilter.value = todayLocal;
                    }}
                    filterPassages();
                }} catch (err) {{
                    console.error("Erreur chargement passages:", err);
                }}
            }};

            // PASSERELLE VERS LE DME DIRECT
            window.openDmeFromPassage = async function(patientId, passageId) {{
                try {{
                    const moduleLoaded = await loadModule('gestion_dme');
                    if (moduleLoaded === false) {{
                        const moduleError = document.getElementById('dynamic-content-area')?.innerText?.trim();
                        throw new Error(moduleError || "Le serveur n'a pas pu charger le module DME.");
                    }}
                    if (typeof window.openPatientDme !== 'function') {{
                        throw new Error("Le module DME est chargé, mais son script d'ouverture n'a pas été initialisé. Consultez la console du navigateur pour le détail.");
                    }}
                    await window.openPatientDme(patientId, passageId);
                }} catch (err) {{
                    console.error('Erreur ouverture DME depuis les passages :', err);
                    if (typeof window.showToast === 'function') window.showToast(err.message || 'Impossible d’ouvrir le DME.', 'error');
                    else alert(err.message || 'Impossible d’ouvrir le DME.');
                }}
            }};

            window.openPassageFiltersModal = function() {{
                const modal = document.getElementById('passageFiltersModal');
                if (modal) {{ modal.classList.remove('hidden'); modal.classList.add('flex'); }}
            }};

            window.closePassageFiltersModal = function() {{
                const modal = document.getElementById('passageFiltersModal');
                if (modal) {{ modal.classList.add('hidden'); modal.classList.remove('flex'); }}
            }};

            window.applyPassageDateShortcut = function(shortcut) {{
                if (!shortcut) return;
                const today = new Date();
                today.setHours(0, 0, 0, 0);
                const start = new Date(today);
                const end = new Date(today);
                if (shortcut === 'yesterday') {{ start.setDate(start.getDate() - 1); end.setDate(end.getDate() - 1); }}
                else if (shortcut === 'day_before') {{ start.setDate(start.getDate() - 2); end.setDate(end.getDate() - 2); }}
                else if (shortcut === '3days') start.setDate(start.getDate() - 2);
                else if (shortcut === '7weeks') start.setDate(start.getDate() - 48);
                else if (shortcut === '15days') start.setDate(start.getDate() - 14);
                else if (shortcut === 'month') {{
                    const dayOfMonth = start.getDate();
                    start.setDate(1);
                    start.setMonth(start.getMonth() - 1);
                    const lastDay = new Date(start.getFullYear(), start.getMonth() + 1, 0).getDate();
                    start.setDate(Math.min(dayOfMonth, lastDay));
                }}
                const toISODate = date => [date.getFullYear(), String(date.getMonth() + 1).padStart(2, '0'), String(date.getDate()).padStart(2, '0')].join('-');
                const startInput = document.getElementById('filterPassageDateDebut');
                const endInput = document.getElementById('filterPassageDateFin');
                if (startInput) startInput.value = toISODate(start);
                if (endInput) endInput.value = toISODate(end);
            }};

            window.filterPassages = function() {{
                const value = id => (document.getElementById(id)?.value || '').trim();
                const q = value('filterPassageSearch').toLocaleLowerCase('fr');
                const numero = value('filterPassageNumero').replace(/^#?PASS-?/i, '').replace(/^0+/, '');
                const telephone = value('filterPassageTelephone').replace(/\\s+/g, '');
                const medecin = value('filterPassageMedecin').toLocaleLowerCase('fr');
                const acte = value('filterPassageActe').toLocaleLowerCase('fr');
                const type = value('filterPassageType').toLocaleLowerCase('fr');
                const statut = value('filterPassageStatut').toLocaleLowerCase('fr');
                const dateNaissance = value('filterPassageDateNaissance');
                const dateDebut = value('filterPassageDateDebut');
                const dateFin = value('filterPassageDateFin');
                const modalite = value('filterPassageModalite').toLocaleLowerCase('fr');
                const etat = value('filterPassageEtat');
                const resultat = value('filterPassageResultat');

                filteredPassagesList = passagesData.filter(p => {{
                    const patient = `${{p.nom || ''}} ${{p.prenom || ''}}`.toLocaleLowerCase('fr');
                    const dobRaw = String(p.date_naissance || '');
                    const dobMatch = dobRaw.match(/^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})/) || dobRaw.match(/^(\\d{{2}})\\/(\\d{{2}})\\/(\\d{{4}})$/);
                    const dobISO = dobMatch ? (dobMatch[1].length === 4 ? `${{dobMatch[1]}}-${{dobMatch[2]}}-${{dobMatch[3]}}` : `${{dobMatch[3]}}-${{dobMatch[2]}}-${{dobMatch[1]}}`) : '';
                    const dobDisplay = dobISO ? `${{dobISO.slice(8, 10)}}/${{dobISO.slice(5, 7)}}/${{dobISO.slice(0, 4)}}` : dobRaw;
                    const actes = (p.actes || []).map(a => a.nom || '').join(' ').toLocaleLowerCase('fr');
                    const medecinNom = `${{p.medecin_traitant_grade || ''}} ${{p.medecin_traitant_nom || ''}}`.toLocaleLowerCase('fr');
                    const montantNet = Math.max(0, Number(p.montant_total || 0) - Number(p.remise || 0));
                    const reste = Math.max(0, montantNet - Number(p.versement || 0));
                    const financialValues = [p.montant_total, p.remise, p.versement, montantNet, reste, p.documents_scannes?.length]
                        .filter(value => value !== undefined && value !== null)
                        .flatMap(value => [String(value), Number(value).toLocaleString('fr-FR')]).join(' ');
                    const searchable = [patient, p.telephone_1 || '', dobRaw, dobISO, dobDisplay, p.type_passage || '', p.statut || '', p.date_passage || '', p.heure_passage || '', p.id_passage || '', medecinNom, actes, financialValues].join(' ').toLocaleLowerCase('fr');
                    const passageDate = String(p.date_passage || '').slice(0, 10);
                    const resultAvailable = String(p.statut || '').toLocaleLowerCase('fr') === 'terminé'.toLocaleLowerCase('fr') || (p.documents_scannes || []).some(doc => /résultat|resultat|compte.?rendu|livret/i.test(String(doc.type || '')));
                    const idMatch = !numero || String(p.id_passage || '').replace(/^0+/, '').includes(numero);
                    const patientSearchMatch = !q || patient.includes(q);
                    const modalityMatch = !modalite || (p.modalites || []).some(item => String(item).toLocaleLowerCase('fr') === modalite);
                    const doneMatch = !etat || (etat === 'fait' ? String(p.statut || '').toLocaleLowerCase('fr') === 'terminé' : String(p.statut || '').toLocaleLowerCase('fr') !== 'terminé');
                    const resultMatch = !resultat || (resultat === 'disponible' ? resultAvailable : !resultAvailable);
                    return (IS_RADIOLOGY_MODE ? patientSearchMatch : (!q || searchable.includes(q))) && (IS_RADIOLOGY_MODE || idMatch) &&
                        (IS_RADIOLOGY_MODE ||
                        (!telephone || String(p.telephone_1 || '').replace(/\\s+/g, '').includes(telephone)) &&
                        (!dateNaissance || dobISO === dateNaissance) &&
                        (!medecin || medecinNom.includes(medecin)) &&
                        (!acte || actes.includes(acte)) &&
                        (!type || String(p.type_passage || '').toLocaleLowerCase('fr') === type) &&
                        (!statut || String(p.statut || '').toLocaleLowerCase('fr') === statut)) &&
                        (!dateDebut || passageDate >= dateDebut) && (!dateFin || passageDate <= dateFin) &&
                        (!IS_RADIOLOGY_MODE || (modalityMatch && doneMatch && resultMatch));
                }});

                const activeCount = (IS_RADIOLOGY_MODE ? [q, dateDebut, dateFin, modalite, etat, resultat] : [q, numero, telephone, dateNaissance, medecin, acte, type, statut, dateDebut, dateFin]).filter(Boolean).length;
                const countBadge = document.getElementById('passageFilterCount');
                const clearButton = document.getElementById('clearPassageFiltersBtn');
                const summary = document.getElementById('passageFilterSummary');
                if (countBadge) {{ countBadge.textContent = activeCount; countBadge.style.display = activeCount ? 'inline-flex' : 'none'; }}
                if (clearButton) clearButton.classList.toggle('hidden', activeCount === 0);
                if (summary) summary.textContent = activeCount
                    ? `${{activeCount}} critère(s) actif(s) · ${{filteredPassagesList.length}} résultat(s)`
                    : `Tous les passages · ${{filteredPassagesList.length}} résultat(s)`;
                currentPassagePage = 1;
                renderPassagesTable();
            }};

            window.applyPassageFilters = function(event) {{
                event?.preventDefault?.();
                const dateDebut = document.getElementById('filterPassageDateDebut')?.value;
                const dateFin = document.getElementById('filterPassageDateFin')?.value;
                if (dateDebut && dateFin && dateDebut > dateFin) {{
                    alert("La date de début doit être antérieure ou égale à la date de fin.");
                    return;
                }}
                try {{
                    filterPassages();
                    const resultCount = filteredPassagesList.length;
                    closePassageFiltersModal();
                    if (resultCount === 0) {{
                        if (typeof window.showToast === 'function') window.showToast('Aucun passage ne correspond aux critères sélectionnés.', 'warning');
                    }}
                }} catch (error) {{
                    console.error('Erreur lors de l’application des filtres des passages :', error);
                    if (typeof window.showToast === 'function') window.showToast(error.message || 'Le filtre n’a pas pu être appliqué.', 'error');
                    else alert(error.message || 'Le filtre n’a pas pu être appliqué.');
                }}
            }};

            window.clearPassageFilters = function() {{
                ['filterPassageSearch', 'filterPassageNumero', 'filterPassageTelephone', 'filterPassageDateNaissance', 'filterPassageMedecin', 'filterPassageActe', 'filterPassageType', 'filterPassageStatut', 'filterPassageDateShortcut', 'filterPassageDateDebut', 'filterPassageDateFin', 'filterPassageModalite', 'filterPassageEtat', 'filterPassageResultat'].forEach(id => {{
                    const field = document.getElementById(id);
                    if (field) field.value = '';
                }});
                filterPassages();
            }};

            function escapePassageDisplay(value) {{
                return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
            }}

            window.openPassagePatientEditModal = async function(passageId) {{
                if (!canEditPatientInfo) return;
                const patient = passagesData.find(p => Number(p.id_passage) === Number(passageId));
                if (!patient) return;
                if (!patient.editable_pre_consultation) {{
                    if (typeof window.showToast === 'function') window.showToast('Modification verrouillée : le passage a été encaissé ou la consultation DME a commencé.', 'warning');
                    return;
                }}
                try {{
                    const response = await fetch(`/api/reception/patient/edit-data/${{passageId}}`);
                    const editData = await response.json();
                    if (!response.ok) throw new Error(editData.detail || 'La fiche ne peut plus être modifiée.');
                    Object.assign(patient, editData);
                }} catch (error) {{
                    if (typeof window.showToast === 'function') window.showToast(error.message, 'error');
                    return;
                }}
                passageEditMode = true;
                passageEditPatientData = {{ ...patient }};
                passageEditPatientDraft = {{ ...patient }};
                passageEditPatientData.id_passage = Number(passageId);
                passageEditPatientDraft.id_passage = Number(passageId);
                openPassageModal(true);
                const setValue = (id, value) => {{ const field = document.getElementById(id); if (field) field.value = value ?? ''; }};
                setValue('passageId', patient.id_passage);
                selectPassagePatient(patient.id_patient);
                selectPassageMedecinTraitant(Number(patient.medecin_traitant_id));
                setValue('passageEditPatientNom', patient.nom);
                setValue('passageEditPatientPrenom', patient.prenom);
                setValue('passageEditPatientDob', String(patient.date_naissance || '').slice(0, 10));
                setValue('passageEditPatientGenre', patient.genre || 'Homme');
                setValue('passageEditPatientBlood', patient.groupe_sanguin || 'O+');
                setValue('passageEditPatientPhone1', patient.telephone_1 === '-' ? '' : patient.telephone_1);
                setValue('passageEditPatientPhone2', patient.telephone_2);
                setValue('passageEditPatientEmail', patient.email === '-' ? '' : patient.email);
                const patientWilaya = document.getElementById('passageEditPatientWilaya');
                if (patientWilaya) {{
                    if (patient.wilaya && !Array.from(patientWilaya.options).some(option => option.value === patient.wilaya)) patientWilaya.add(new Option(patient.wilaya, patient.wilaya));
                    patientWilaya.value = patient.wilaya || '';
                }}
                setValue('passageEditPatientCommune', patient.commune);
                populatePassageTypeSelect(patient.type_passage || '');
                setValue('passageDate', String(patient.date_passage || '').slice(0, 10));
                setValue('passageHeure', String(patient.heure_passage || '').slice(0, 5));
                setValue('passageStatutSelect', 'En attente');
                currentPassageActs = JSON.parse(JSON.stringify(patient.actes || []));
                currentPassageScannedDocs = JSON.parse(JSON.stringify(patient.documents_scannes || []));
                renderPassageActsTable();
                renderPassageScannedDocsGallery();
                setValue('passageObservation', patient.questionnaire?.observation_medicale || patient.questionnaire?.observation || '');
                const qMap = {{ hospitalise: 'q_hospitalise', dossier_interne: 'q_dossier_interne', dossier_externe: 'q_dossier_externe', urgence_vitale: 'q_urgence_vitale', grossesse: 'q_grossesse', allergie: 'q_allergie', diabete: 'q_diabete', hta: 'q_hta', asthme: 'q_asthme', insuffisance_renale: 'q_insuffisance_renale', pace_maker: 'q_pace_maker', valve_cardiaque: 'q_valve_cardiaque', prothese_metallique: 'q_prothese_metallique' }};
                Object.entries(qMap).forEach(([key, radioName]) => setRadioVal(radioName, patient.questionnaire?.[key] || 'NON'));
                setValue('passageRemiseValeur', patient.remise_valeur ?? patient.remise ?? 0);
                setRemiseType(patient.remise_type || 'DA');
                setValue('passageAccordeurRemise', patient.accordeur_remise || '');
                setValue('passageVersement', patient.versement || 0);
                setValue('passageModePaiement', patient.mode_paiement || 'Espèces');
                calculatePassageFinancials();
                const title = document.getElementById('passageModalTitle');
                if (title) title.textContent = `Corriger le passage #PASS-${{String(passageId).padStart(4, '0')}}`;
                const saveLabel = document.querySelector('#btnSubmitPassage');
                if (saveLabel) saveLabel.textContent = 'Enregistrer les corrections';
                const newPatientButton = document.querySelector('#passageForm button[onclick="openPassagePatientForm()"] span');
                if (newPatientButton) newPatientButton.textContent = 'Modifier la fiche patient';
                const financialSection = document.getElementById('passageFinancialSection');
                financialSection?.querySelectorAll('input,select,button').forEach(control => control.disabled = true);
                const statusSelect = document.getElementById('passageStatutSelect');
                if (statusSelect) statusSelect.disabled = true;
                const changePatientButton = document.querySelector('#patientSelectedSummaryCard button[onclick="clearSelectedPatient()"]');
                if (changePatientButton) changePatientButton.classList.add('hidden');
                checkPassageFormValidity();
            }};

            window.closePassagePatientEditModal = function() {{
                const modal = document.getElementById('passagePatientEditModal');
                modal?.classList.add('hidden');
                modal?.classList.remove('flex');
            }};

            function renderPassageEditDocs() {{
                const list = document.getElementById('passageEditDocsList');
                const count = document.getElementById('passageEditDocsCount');
                if (!list) return;
                if (count) count.textContent = `${{currentPatientEditDocs.length}} document(s)`;
                list.innerHTML = currentPatientEditDocs.length
                    ? currentPatientEditDocs.map((doc, index) => `<div class="flex items-center justify-between gap-2 px-3 py-2 rounded-xl bg-slate-50 dark:bg-black/20 text-[11px]"><span class="min-w-0 truncate"><i class="ph-bold ph-file text-brand-neon mr-1"></i>${{escapePassageDisplay(doc.type || `Document ${{index + 1}}`)}}</span><span class="flex items-center gap-1"><button type="button" onclick="viewPassageEditDocument(${{index}})" class="px-2 py-1 rounded-lg text-sky-600 hover:bg-sky-50 dark:hover:bg-white/5">Voir</button><button type="button" onclick="removePassageEditDocument(${{index}})" class="px-2 py-1 rounded-lg text-rose-600 hover:bg-rose-50 dark:hover:bg-white/5">Retirer</button></span></div>`).join('')
                    : '<div class="py-3 text-center text-[10px] text-slate-400">Aucun document scanné.</div>';
            }}

            window.viewPassageEditDocument = function(index) {{
                const doc = currentPatientEditDocs[index];
                if (doc) openPassageDocCollection(currentPatientEditDocs, index);
            }};

            window.removePassageEditDocument = function(index) {{
                currentPatientEditDocs.splice(index, 1);
                renderPassageEditDocs();
            }};

            window.addPassageEditDocument = function(event) {{
                const file = event.target.files?.[0];
                if (!file) return;
                if (file.size > 12 * 1024 * 1024) {{
                    alert('Chaque document doit faire moins de 12 Mo.');
                    event.target.value = '';
                    return;
                }}
                const reader = new FileReader();
                reader.onload = () => {{
                    currentPatientEditDocs.push({{
                        type: document.getElementById('passageEditDocType')?.value || 'Document scanné',
                        heure: new Date().toLocaleTimeString('fr-FR', {{ hour: '2-digit', minute: '2-digit' }}),
                        data: reader.result
                    }});
                    renderPassageEditDocs();
                    event.target.value = '';
                }};
                reader.readAsDataURL(file);
            }};

            window.readPassagePatientFile = function(event, field) {{
                const file = event.target.files?.[0];
                if (!file) return;
                if (file.size > 12 * 1024 * 1024) {{
                    alert('Chaque fichier doit faire moins de 12 Mo.');
                    event.target.value = '';
                    return;
                }}
                const reader = new FileReader();
                reader.onload = () => {{ currentPatientEditFiles[field] = reader.result; }};
                reader.readAsDataURL(file);
            }};

            function renderPassageEditPieces() {{
                const list = document.getElementById('passageEditPiecesList');
                const count = document.getElementById('passageEditPiecesCount');
                if (count) count.textContent = `${{currentPatientEditPieces.length}} pièce(s)`;
                if (!list) return;
                list.innerHTML = currentPatientEditPieces.map((piece, index) => `<div class="flex items-center justify-between gap-2 px-3 py-2 rounded-xl bg-slate-50 dark:bg-black/20 text-[11px]"><span class="truncate">${{escapePassageDisplay(piece.type || `Pièce ${{index + 1}}`)}}</span><span class="flex gap-1"><button type="button" onclick="viewPassagePatientPiece(${{index}})" class="px-2 py-1 text-sky-600">Voir</button><button type="button" onclick="removePassagePatientPiece(${{index}})" class="px-2 py-1 text-rose-600">Retirer</button></span></div>`).join('') || '<div class="text-[10px] text-slate-400">Aucune autre pièce.</div>';
            }}
            window.addPassagePatientPiece = function(event) {{
                const file = event.target.files?.[0]; if (!file) return;
                if (file.size > 12 * 1024 * 1024) {{ alert('Chaque fichier doit faire moins de 12 Mo.'); event.target.value = ''; return; }}
                const reader = new FileReader();
                reader.onload = () => {{ currentPatientEditPieces.push({{ type: file.name, data: reader.result }}); renderPassageEditPieces(); event.target.value = ''; }};
                reader.readAsDataURL(file);
            }};
            window.viewPassagePatientPiece = index => openPassageDocCollection(currentPatientEditPieces, index);
            window.removePassagePatientPiece = index => {{ currentPatientEditPieces.splice(index, 1); renderPassageEditPieces(); }};

            window.savePatientInfoFromPassage = async function(event) {{
                event.preventDefault();
                if (!canEditPatientInfo) return;
                const button = document.getElementById('passagePatientEditSubmit');
                const originalHtml = button?.innerHTML;
                if (button) {{ button.disabled = true; button.innerHTML = '<i class="ph-bold ph-spinner animate-spin"></i> Enregistrement…'; }}
                const value = id => document.getElementById(id)?.value?.trim() || '';
                const passageId = Number(value('passagePatientEditPassageId'));
                try {{
                    const response = await fetch('/api/reception/patient/update-from-passage', {{
                        method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{
                            id_passage: passageId,
                            id_patient: Number(value('passagePatientEditPatientId')),
                            nom: value('passagePatientEditNom'), prenom: value('passagePatientEditPrenom'),
                            date_naissance: value('passagePatientEditDob'), genre: value('passagePatientEditGenre'),
                            groupe_sanguin: value('passagePatientEditBloodGroup'),
                            telephone_1: value('passagePatientEditPhone1'), telephone_2: value('passagePatientEditPhone2'),
                            email: value('passagePatientEditEmail'), wilaya: value('passagePatientEditWilaya'), commune: value('passagePatientEditCommune'),
                            type_piece_identite: value('passagePatientEditIdentityType'),
                            ...currentPatientEditFiles,
                            pieces_scannees: currentPatientEditPieces,
                            questionnaire: {{
                                ...currentPatientEditQuestionnaire,
                                observation_medicale: value('passageEditObservation'),
                                ...Object.fromEntries(Array.from(document.querySelectorAll('[id^="passageEditQuestionnaire_"]')).map(select => [select.id.replace('passageEditQuestionnaire_', ''), select.value]))
                            }},
                            documents_scannes: currentPatientEditDocs
                        }})
                    }});
                    const result = await response.json();
                    if (!response.ok || result.success === false) throw new Error(result.detail || result.message || 'La fiche patient n’a pas pu être mise à jour.');
                    closePassagePatientEditModal();
                    if (typeof window.showToast === 'function') window.showToast('Dossier corrigé. La demande d’examen est régénérée avec les nouvelles informations.', 'success');
                    await loadReceptionPassagesModule();
                    openDemandeExamenModal(passageId);
                }} catch (error) {{
                    if (typeof window.showToast === 'function') window.showToast(error.message || 'Erreur lors de la mise à jour du patient.', 'error');
                    else alert(error.message || 'Erreur lors de la mise à jour du patient.');
                }} finally {{
                    if (button) {{ button.disabled = false; button.innerHTML = originalHtml; }}
                }}
            }};

            window.setPassageDisplayStyle = function(style) {{
                passageDisplayStyle = ['classic', 'cards', 'worklist', 'essential'].includes(style) ? style : 'classic';
                try {{ localStorage.setItem('clinivaPassagesDisplayStyleV2', passageDisplayStyle); }} catch (_) {{}}
                renderPassagesTable();
            }};

            window.togglePassageDetails = function(passageId) {{
                const row = document.getElementById(`passageDetails-${{passageId}}`);
                const button = document.getElementById(`passageExpandBtn-${{passageId}}`);
                if (!row) return;
                const open = row.classList.toggle('hidden') === false;
                if (button) button.textContent = open ? '−' : '+';
            }};

            window.onPassageActionMenuToggle = function(menu) {{
                if (!menu) return;
                if (menu.open) {{
                    document.querySelectorAll('#view-reception details.passage-action-menu[open]').forEach(other => {{
                        if (other !== menu) other.open = false;
                    }});
                    const panel = menu.querySelector('.passage-action-menu-panel');
                    if (panel) {{
                        if (menu._passagePortal) menu._passagePortal.remove();
                        const portal = panel.cloneNode(true);
                        portal.classList.add('passage-action-menu-portal');
                        portal.style.position = 'fixed';
                        portal.style.zIndex = '100000';
                        portal.style.right = 'auto';
                        portal.style.display = 'block';
                        menu._passagePortal = portal;
                        portal._passageOwner = menu;
                        panel.style.visibility = 'hidden';
                        const sourceButtons = Array.from(panel.querySelectorAll('button'));
                        const portalButtons = Array.from(portal.querySelectorAll('button'));
                        portalButtons.forEach((button, index) => {{
                            button.removeAttribute('onclick');
                            button.addEventListener('click', event => {{
                                event.preventDefault();
                                sourceButtons[index]?.click();
                                if (!menu.open) {{
                                    portal.remove();
                                    panel.style.visibility = '';
                                    menu._passagePortal = null;
                                }}
                            }});
                        }});
                        portal.addEventListener('mouseenter', window.keepPassageActionMenuOpen);
                        portal.addEventListener('mouseleave', () => window.schedulePassageActionMenuClose(menu));
                        document.body.appendChild(portal);
                        const anchor = menu.querySelector('summary').getBoundingClientRect();
                        const width = Math.max(portal.offsetWidth || 192, 192);
                        const height = portal.offsetHeight || 180;
                        const left = Math.max(8, Math.min(anchor.right - width, window.innerWidth - width - 8));
                        const top = anchor.bottom + height + 8 <= window.innerHeight
                            ? anchor.bottom : Math.max(8, anchor.top - height);
                        portal.style.left = left + 'px';
                        portal.style.top = top + 'px';
                    }}
                }} else {{
                    const panel = menu.querySelector('.passage-action-menu-panel');
                    if (panel) panel.style.visibility = '';
                    if (menu._passagePortal) {{
                        menu._passagePortal.remove();
                        menu._passagePortal = null;
                    }}
                }}
                menu.closest('tr')?.classList.toggle('passage-menu-open', menu.open);
            }};

            window.keepPassageActionMenuOpen = function() {{
                if (passageActionMenuCloseTimer) clearTimeout(passageActionMenuCloseTimer);
                passageActionMenuCloseTimer = null;
            }};

            window.schedulePassageActionMenuClose = function(menu) {{
                if (passageActionMenuCloseTimer) clearTimeout(passageActionMenuCloseTimer);
                passageActionMenuCloseTimer = setTimeout(() => {{
                    const owner = menu?.matches?.('details.passage-action-menu') ? menu : menu?._passageOwner;
                    if (owner?.open) owner.open = false;
                    passageActionMenuCloseTimer = null;
                }}, 220);
            }};

            window.closePassageActionMenu = function(element) {{
                const panel = element?.closest?.('.passage-action-menu-panel');
                const owner = panel?._passageOwner || element?.closest?.('details.passage-action-menu');
                if (owner && owner.open) {{
                    owner.open = false;
                    window.onPassageActionMenuToggle(owner);
                }}
            }};

            function renderRadiologyPassageRow(p, patientIdParam, passageIdParam, docsCount, actesHtml) {{
                const status = String(p.statut || '');
                const refund = String(p.statut_paiement || '').toLowerCase() === 'annulé';
                const net = Math.max(0, Number(p.montant_total || 0) - Number(p.remise || 0));
                const paid = Number(p.versement || 0);
                const due = Math.max(0, net - paid);
                const resultFound = status === 'Terminé' || (p.documents_scannes || []).some(doc => /résultat|resultat|compte.?rendu|livret/i.test(String(doc.type || '')));
                const resultText = resultFound ? 'Résultat disponible' : 'Non disponible';
                const resultClass = resultFound ? 'radio-state-result' : 'radio-state-muted';
                const passageText = status === 'En attente' ? 'En attente' : (status === 'En cours' ? 'En cours' : 'Passage fait');
                const paymentText = refund ? 'Remboursé' : (due > 0.009 ? (paid > 0 ? 'Régler' : 'Non payé') : 'Réglé');
                const paymentClass = refund || paymentText === 'Non payé' ? 'radio-state-muted' : (paymentText === 'Régler' ? 'radio-state-passage' : 'radio-state-result');
                const consultationOwner = status === 'En cours' ? String(p.consultation_utilisateur || '').trim() : '';
                const consultationOwnerHtml = consultationOwner
                    ? '<div class="mt-1 text-[10px] font-semibold text-sky-700"><i class="ph-bold ph-user-circle"></i> Consultation par ' + escapePassageDisplay(consultationOwner) + '</div>'
                    : '';
                const birth = String(p.date_naissance || '').slice(0, 10);
                const birthText = birth && birth !== '-' ? new Date(birth + 'T00:00:00').toLocaleDateString('fr-FR') : 'Date de naissance inconnue';
                const date = p.date_passage ? new Date(String(p.date_passage).slice(0, 10) + 'T00:00:00').toLocaleDateString('fr-FR') : '';
                const visitText = [birthText, date, String(p.heure_passage || '').slice(0, 5)].filter(Boolean).join(' / ');
                const doctor = [p.medecin_traitant_grade || 'Dr', p.medecin_traitant_nom || '', p.medecin_traitant_prenom || ''].filter(Boolean).join(' ');
                const exam = (p.actes || []).map(act => act.nom || '').filter(Boolean).join(' · ') || p.type_passage || 'Examen non précisé';
                const doctorSpecialty = String(p.medecin_traitant_specialite || '').trim();
                const actions = [];
                if (canEditPatientInfo && p.editable_pre_consultation) actions.push('<button type="button" onclick="closePassageActionMenu(this); openPassagePatientEditModal(' + passageIdParam + ')" class="radio-menu-item"><i class="ph-bold ph-identification-card"></i>Modifier le passage</button>');
                if (docsCount > 0) actions.push('<button type="button" onclick="closePassageActionMenu(this); viewPassageDocsFromTable(' + passageIdParam + ')" class="radio-menu-item"><i class="ph-bold ph-folder-open"></i>Documents scannés (' + docsCount + ')</button>');
                actions.push('<button type="button" onclick="closePassageActionMenu(this); openDmeFromPassage(' + patientIdParam + ',' + passageIdParam + ')" class="radio-menu-item"><i class="ph-bold ph-stethoscope"></i>Ouvrir le DME</button>');
                actions.push('<button type="button" onclick="closePassageActionMenu(this); openDemandeExamenModal(' + passageIdParam + ')" class="radio-menu-item"><i class="ph-bold ph-file-text"></i>Demande d\\'examen</button>');
                actions.push('<button type="button" onclick="closePassageActionMenu(this); deletePassage(' + passageIdParam + ')" class="radio-menu-item text-rose-600"><i class="ph-bold ph-trash"></i>Supprimer le passage</button>');
                if (canMagicDeletePassages) actions.push('<button type="button" onclick="closePassageActionMenu(this); magicDeletePassage(' + passageIdParam + ')" class="radio-menu-item text-rose-700"><i class="ph-bold ph-magic-wand"></i>Suppression magique</button>');
                return '<td class="radio-patient-cell"><div class="flex items-center gap-3">' +
                    '<img src="' + escapePassageDisplay(p.avatar || '/static/images/avatar-placeholder.svg?w=120&auto=format&fit=crop&q=80') + '" class="w-12 h-12 shrink-0 rounded-xl object-cover border border-slate-200 shadow-sm">' +
                    '<div class="min-w-0"><button type="button" onclick="openDmeFromPassage(' + patientIdParam + ',' + passageIdParam + ')" class="block text-left font-extrabold text-[15px] text-slate-900 uppercase leading-tight hover:text-violet-700">' +
                    escapePassageDisplay((p.nom || '') + ' ' + (p.prenom || '')) + '</button><div class="mt-1 text-[11px] text-slate-500">' + escapePassageDisplay(visitText) + '</div></div></div></td>' +
                    '<td class="radio-exam-cell"><div class="font-extrabold text-[15px] uppercase leading-tight text-slate-900">' + escapePassageDisplay(exam) +
                    '</div><div class="mt-1 text-xs font-bold text-red-600"><i class="ph-bold ph-user-circle"></i> ' + escapePassageDisplay(doctor) + (doctorSpecialty ? ' | Sp: ' + escapePassageDisplay(doctorSpecialty) : '') + '</div></td>' +
                    '<td class="text-center"><span class="radio-state ' + resultClass + '">' + resultText + '</span></td>' +
                    '<td class="text-center"><span class="radio-state radio-state-passage">' + escapePassageDisplay(passageText) + '</span>' + consultationOwnerHtml + '</td>' +
                    '<td class="text-center"><span class="radio-state ' + paymentClass + '">' + escapePassageDisplay(paymentText) + '</span></td>' +
                    '<td class="text-right pr-3"><details class="passage-action-menu relative z-10 inline-block text-left" onclick="event.stopPropagation()" ontoggle="onPassageActionMenuToggle(this)" onmouseenter="keepPassageActionMenuOpen()" onmouseleave="schedulePassageActionMenuClose(this)"><summary class="list-none cursor-pointer select-none inline-flex items-center gap-2 text-[11px] font-bold"><span>Détails</span><i class="ph-bold ph-identification-card"></i><i class="ph-bold ph-caret-down text-[9px]"></i></summary><div class="passage-action-menu-panel absolute right-0 top-full z-[9999] min-w-48 p-1 rounded-xl border border-slate-200 bg-white shadow-xl" onmouseenter="keepPassageActionMenuOpen()" onmouseleave="schedulePassageActionMenuClose(this)">' + actions.join('') + '</div></details></td>';
            }}

            window.renderPassagesTable = function() {{
                const tbody = document.getElementById('passages-table-body');
                if (!tbody) return;
                tbody.innerHTML = '';
                const dataTable = document.getElementById('passagesDataTable');
                if (dataTable) dataTable.dataset.style = passageDisplayStyle;
                if (dataTable && !IS_RADIOLOGY_MODE) {{
                    const headerRow = dataTable.querySelector('thead tr');
                    if (headerRow) {{
                        if (!headerRow._passageHeaderCells) {{
                            headerRow._passageHeaderCells = Array.from(headerRow.children);
                            headerRow._passageHeaderHtml = headerRow._passageHeaderCells.map(cell => cell.innerHTML);
                        }}
                        const headerCells = headerRow._passageHeaderCells;
                        headerCells.forEach((cell, index) => {{ cell.innerHTML = headerRow._passageHeaderHtml[index]; cell.style.width = ''; }});
                        const essentialOrder = passageDisplayStyle === 'essential' ? [1, 0, 3, 2, 5, 4] : [0, 1, 2, 3, 4, 5];
                        headerRow.replaceChildren(...essentialOrder.map(index => headerCells[index]));
                        if (passageDisplayStyle === 'essential') {{
                            headerRow.children[0].style.width = '29%';
                            headerRow.children[1].style.width = '18%';
                            headerRow.children[2].style.width = '29%';
                            headerRow.children[3].style.width = '14%';
                            headerRow.children[4].style.width = '10%';
                            headerRow.children[3].textContent = 'État du passage';
                        }}
                    }}
                }}
                document.getElementById('passageStyleClassicBtn')?.classList.toggle('bg-white', passageDisplayStyle === 'classic');
                document.getElementById('passageStyleClassicBtn')?.classList.toggle('text-slate-900', passageDisplayStyle === 'classic');
                document.getElementById('passageStyleCardsBtn')?.classList.toggle('bg-white', passageDisplayStyle === 'cards');
                document.getElementById('passageStyleCardsBtn')?.classList.toggle('text-slate-900', passageDisplayStyle === 'cards');
                document.getElementById('passageStyleWorklistBtn')?.classList.toggle('bg-white', passageDisplayStyle === 'worklist');
                document.getElementById('passageStyleWorklistBtn')?.classList.toggle('text-slate-900', passageDisplayStyle === 'worklist');
                document.getElementById('passageStyleEssentialBtn')?.classList.toggle('bg-white', passageDisplayStyle === 'essential');
                document.getElementById('passageStyleEssentialBtn')?.classList.toggle('text-slate-900', passageDisplayStyle === 'essential');

                const totalItems = filteredPassagesList.length;
                const totalPages = Math.ceil(totalItems / PASSAGES_PER_PAGE) || 1;

                if (currentPassagePage > totalPages) currentPassagePage = totalPages;
                if (currentPassagePage < 1) currentPassagePage = 1;

                const startIndex = (currentPassagePage - 1) * PASSAGES_PER_PAGE;
                const endIndex = startIndex + PASSAGES_PER_PAGE;
                const pageItems = filteredPassagesList.slice(startIndex, endIndex);

                if (pageItems.length === 0) {{
                    const emptyMessage = passagesData.length ? 'Aucun passage ne correspond aux critères. Modifiez ou effacez les filtres.' : 'Aucun passage enregistré.';
                    tbody.innerHTML = `<tr><td colspan="6" class="text-center py-8 text-slate-400">${{emptyMessage}}</td></tr>`;
                }} else {{
                    const statutBadges = {{
                        'En attente': 'bg-amber-50 dark:bg-amber-950/40 text-amber-700 dark:text-amber-300 border-amber-200 dark:border-amber-800/40',
                        'En cours': 'bg-sky-50 dark:bg-sky-950/40 text-sky-700 dark:text-sky-300 border-sky-200 dark:border-sky-800/40',
                        'Terminé': 'bg-brand-neon/20 text-emerald-800 dark:text-brand-neon border-brand-neon/30'
                    }};

                    pageItems.forEach((p, rowIndex) => {{
                        const tr = document.createElement('tr');
                        tr.className = "align-top odd:bg-white even:bg-slate-50/50 dark:odd:bg-transparent dark:even:bg-white/[0.018] border-b border-slate-100 dark:border-white/5 hover:bg-emerald-50/70 dark:hover:bg-brand-neon/[0.045] transition-colors" + (passageDisplayStyle === 'essential' ? ' passage-essential-row' : '');

                        const actesBadges = (p.actes && p.actes.length > 0) 
                            ? p.actes.map(a => `<span class="block w-fit max-w-full px-2 py-1 rounded-lg text-[10px] font-bold bg-slate-100 dark:bg-black/30 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10 break-words">${{escapePassageDisplay(a.nom || a.designation || a.libelle || 'Prestation non précisée')}}</span>`).join('')
                            : `<span class="text-slate-400 italic text-[11px]">Aucun acte</span>`;

                        const montantTotal = parseFloat(p.montant_total || 0);
                        const remise = parseFloat(p.remise || 0);
                        const netAPayer = Math.max(0, montantTotal - remise);
                        const versement = parseFloat(p.versement || 0);
                        const reste = Math.max(0, netAPayer - versement);

                        const remboursement = String(p.statut_paiement || '').toLocaleLowerCase('fr') === 'annulé';
                        const versementResteHtml = remboursement
                            ? `<div class="space-y-1">
                                   <span class="inline-flex px-2 py-0.5 rounded-full text-[10px] font-extrabold bg-rose-50 dark:bg-rose-950/40 text-rose-700 dark:text-rose-300 border border-rose-200 dark:border-rose-800/50">Remboursé</span>
                                   <div class="text-[10px] text-rose-600 dark:text-rose-300 font-mono font-bold">${{Number(p.montant_annule ?? versement).toLocaleString('fr-FR')}} DA remboursés</div>
                                   <div class="text-[10px] text-slate-500 dark:text-slate-400 whitespace-normal break-words">Cause : ${{escapePassageDisplay(p.raison_annulation || 'Non renseignée')}}</div>
                               </div>`
                            : `<div>
                                   <span class="text-emerald-600 dark:text-brand-neon font-mono font-bold">${{Number(versement).toLocaleString('fr-FR')}} DA</span>
                                   ${{reste > 0
                                       ? `<div class="text-[10px] text-rose-500 font-bold font-mono">Reste: ${{Number(reste).toLocaleString('fr-FR')}} DA</div>`
                                       : '<div class="text-[10px] text-emerald-500 font-bold">Soldé ✓</div>'}}
                               </div>`;

                        const medTraitantBadge = p.medecin_traitant_nom 
                            ? `<span class="inline-flex max-w-full whitespace-normal break-words items-center gap-1 px-2 py-0.5 rounded-lg bg-sky-50 dark:bg-sky-950/40 text-sky-700 dark:text-sky-300 border border-sky-200 dark:border-sky-800/40 text-[10px] font-bold">🩺 ${{p.medecin_traitant_grade || 'Dr'}} ${{p.medecin_traitant_nom}}</span>` 
                            : '<span class="text-slate-400 italic text-[11px]">-</span>';

                        const docsCount = p.documents_scannes ? p.documents_scannes.length : 0;
                        const patientIdParam = Number(p.id_patient || 0);
                        const passageIdParam = Number(p.id_passage || 0);
                        const essentialState = String(p.statut || 'Non renseigné');
                        const consultationOwner = essentialState === 'En cours' ? String(p.consultation_utilisateur || '').trim() : '';
                        const consultationOwnerHtml = consultationOwner
                            ? `<div class="mt-1 text-[10px] font-semibold text-sky-700 dark:text-sky-300"><i class="ph-bold ph-user-circle"></i> Consultation par ${{escapePassageDisplay(consultationOwner)}}</div>`
                            : '';
                        const essentialStateTone = essentialState === 'En attente' ? 'bg-amber-100 text-amber-800' : (essentialState === 'En cours' ? 'bg-blue-100 text-blue-800' : (essentialState === 'Terminé' ? 'bg-emerald-100 text-emerald-800' : 'bg-slate-100 text-slate-700'));
                        const essentialStateIcon = essentialState === 'En attente' ? 'ph-clock' : (essentialState === 'En cours' ? 'ph-spinner-gap' : (essentialState === 'Terminé' ? 'ph-check-circle' : 'ph-info'));

                        tr.innerHTML = `
                            <td class="py-4 pl-3 pr-2">
                                <div class="${{passageDisplayStyle === 'worklist' ? 'passage-worklist-date-line' : (passageDisplayStyle === 'essential' ? 'passage-essential-date-line' : '')}}"><span class="${{passageDisplayStyle === 'worklist' ? 'passage-worklist-ref' : 'text-slate-400 font-mono text-[10px]'}}">#PASS-${{String(p.id_passage || '').padStart(4, '0')}}</span><span class="font-semibold text-slate-700 dark:text-slate-200">${{passageDisplayStyle === 'essential' ? '<i class="ph-bold ph-calendar-blank passage-essential-date-icon"></i>' : ''}}${{escapePassageDisplay(p.date_passage || 'Date inconnue')}}</span></div>
                                <div class="${{passageDisplayStyle === 'worklist' ? 'mt-0.5' : (passageDisplayStyle === 'essential' ? 'passage-essential-time' : 'mt-2')}} text-[11px] text-slate-500 dark:text-slate-400">${{passageDisplayStyle === 'essential' ? '<i class="ph-bold ph-clock passage-essential-date-icon"></i>' : ''}}${{escapePassageDisplay(p.heure_passage || 'Heure inconnue')}}</div>
                            </td>
                            <td class="py-4 px-2">
                                <div class="flex items-start gap-2.5 cursor-pointer group min-w-0" onclick="openDmeFromPassage(${{patientIdParam}}, ${{passageIdParam}})" title="Cliquer pour ouvrir le Dossier Médical Électronique (DME)">
                                <button type="button" onclick="event.stopPropagation(); togglePassageDetails(${{passageIdParam}})" id="passageExpandBtn-${{passageIdParam}}" class="${{['cards', 'worklist'].includes(passageDisplayStyle) ? '' : 'hidden'}} w-5 h-5 shrink-0 mt-1 rounded-full text-emerald-600 font-black border border-emerald-200 hover:bg-emerald-50" title="Afficher les détails">+</button>
                                <img src="${{p.avatar || '/static/images/avatar-placeholder.svg?w=120&auto=format&fit=crop&q=80'}}" class="${{passageDisplayStyle === 'worklist' ? 'passage-worklist-avatar' : (passageDisplayStyle === 'essential' ? 'passage-essential-avatar' : 'w-8 h-8')}} shrink-0 rounded-full object-cover border border-slate-200 dark:border-white/10 shadow-sm">
                                <div class="min-w-0">
                                    <div class="${{passageDisplayStyle === 'worklist' ? 'passage-worklist-patient-primary' : ''}}"><div class="${{passageDisplayStyle === 'worklist' ? 'passage-worklist-name' : (passageDisplayStyle === 'essential' ? 'passage-essential-name' : 'font-bold text-slate-800 dark:text-slate-100 text-xs')}} group-hover:text-brand-neon transition flex items-center gap-1">
                                        <span class="break-words">${{escapePassageDisplay([p.nom, p.prenom].filter(Boolean).join(' ') || 'Patient sans nom')}}</span>
                                    </div><div class="${{passageDisplayStyle === 'worklist' ? '' : 'mt-1.5'}} passage-essential-patient-status"><span class="inline-flex px-2 py-0.5 rounded-full text-[9px] font-bold border ${{statutBadges[p.statut] || 'bg-slate-100 text-slate-600 border-slate-200'}}">${{p.statut || 'Statut inconnu'}}</span></div>${{consultationOwnerHtml}}</div>
                                    <div class="${{passageDisplayStyle === 'worklist' ? 'mt-0.5' : 'mt-1'}} text-[10px] text-slate-500 font-mono break-all">${{p.telephone_1 || '-'}}</div>
                                </div>
                                </div>
                            </td>
                            <td class="py-4 px-2">
                                ${{passageDisplayStyle === 'essential' ? `<span class="passage-essential-status ${{essentialStateTone}}"><i class="ph-bold ${{essentialStateIcon}}"></i>${{escapePassageDisplay(essentialState)}}</span>` : `<div>${{medTraitantBadge}}</div><div class="mt-2"><span class="inline-block max-w-full px-2 py-1 rounded-xl text-[10px] font-bold bg-slate-100 dark:bg-black/30 border border-slate-200 dark:border-white/10 text-slate-700 dark:text-slate-300 break-words">${{escapePassageDisplay(p.type_passage || 'Type non précisé')}}</span></div>`}}
                            </td>
                            <td class="py-4 px-2"><div class="space-y-1.5">${{actesBadges}}</div></td>
                            <td class="py-4 px-2">
                                <div class="font-black text-slate-900 dark:text-white font-mono text-xs">${{Number(netAPayer).toLocaleString()}} DA</div>
                                <div class="mt-2 border-l-2 border-slate-200 dark:border-white/10 pl-2">${{versementResteHtml}}</div>
                            </td>
                            <td class="py-4 pl-2 pr-3 text-right">
                                        <details class="passage-action-menu relative z-10 inline-block text-left" onclick="event.stopPropagation()" ontoggle="onPassageActionMenuToggle(this)" onmouseenter="keepPassageActionMenuOpen()" onmouseleave="schedulePassageActionMenuClose(this)">
                                    <summary class="list-none cursor-pointer select-none inline-flex items-center gap-1 px-2 py-1.5 rounded-lg border border-slate-200 dark:border-white/10 bg-white dark:bg-black/20 hover:bg-slate-50 dark:hover:bg-white/5 text-[10px] font-bold text-slate-700 dark:text-slate-200 ${{passageDisplayStyle === 'worklist' ? 'passage-worklist-action' : (passageDisplayStyle === 'essential' ? 'passage-essential-action' : '')}}" title="Actions du patient">
                                        <i class="ph-bold ph-dots-three-outline-vertical"></i><span>Actions</span><i class="ph-bold ph-caret-down text-[9px]"></i>
                                    </summary>
                                    <div class="passage-action-menu-panel absolute right-0 top-full z-[9999] min-w-48 p-1 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-[#121c15] shadow-xl">
                                        ${{canEditPatientInfo && p.editable_pre_consultation ? `<button type="button" onclick="this.closest('details').open=false; openPassagePatientEditModal(${{passageIdParam}})" class="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-left text-[11px] text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-white/5"><i class="ph-bold ph-identification-card text-sky-500"></i>Modifier avec le formulaire passage</button>` : ''}}
                                        ${{docsCount > 0 ? `<button type="button" onclick="this.closest('details').open=false; viewPassageDocsFromTable(${{passageIdParam}})" class="w-full flex items-center justify-between gap-2 px-2.5 py-2 rounded-lg text-left text-[11px] text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-white/5"><span><i class="ph-bold ph-folder-open mr-1.5 text-brand-neon"></i>Documents scannés</span><span class="text-[10px] font-bold">${{docsCount}}</span></button>` : `<div class="px-2.5 py-2 text-[10px] text-slate-400 italic">Aucun document scanné</div>`}}
                                        <button type="button" onclick="this.closest('details').open=false; openDmeFromPassage(${{patientIdParam}}, ${{passageIdParam}})" class="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-left text-[11px] text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-white/5"><i class="ph-bold ph-stethoscope text-brand-neon"></i>Ouvrir le DME</button>
                                        <button type="button" onclick="this.closest('details').open=false; openDemandeExamenModal(${{passageIdParam}})" class="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-left text-[11px] text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-white/5"><i class="ph-bold ph-file-text text-brand-neon"></i>Demande d'examen</button>
                                        <button type="button" onclick="this.closest('details').open=false; deletePassage(${{passageIdParam}})" class="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-left text-[11px] text-rose-600 dark:text-rose-400 hover:bg-rose-50 dark:hover:bg-rose-500/10"><i class="ph-bold ph-trash"></i>Supprimer le passage</button>
                                        ${{canMagicDeletePassages ? `<div class="my-1 border-t border-rose-200 dark:border-rose-900/50"></div><button type="button" onclick="this.closest('details').open=false; magicDeletePassage(${{passageIdParam}})" title="Supprime le passage et ses mouvements de caisse" class="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-left text-[11px] font-bold text-white bg-rose-700 hover:bg-rose-800"><i class="ph-bold ph-magic-wand"></i>Suppression magique</button>` : ''}}
                                    </div>
                                </details>
                            </td>
                        `;
                        if (passageDisplayStyle === 'essential' && !IS_RADIOLOGY_MODE) {{
                            const cells = Array.from(tr.children);
                            [1, 0, 3, 2, 5, 4].forEach(index => tr.appendChild(cells[index]));
                        }}
                        if (IS_RADIOLOGY_MODE) {{
                            tr.className = 'radiology-passage-row';
                            tr.innerHTML = renderRadiologyPassageRow(p, patientIdParam, passageIdParam, docsCount, actesBadges);
                        }}
                        tbody.appendChild(tr);
                        const detailRow = document.createElement('tr');
                        detailRow.id = `passageDetails-${{passageIdParam}}`;
                        detailRow.className = 'passage-expand-details hidden';
                        const dobValue = String(p.date_naissance || '-');
                        const scannedTypes = (p.documents_scannes || []).map(d => escapePassageDisplay(d.type || 'Document scanné')).join(' · ') || 'Aucun';
                        detailRow.innerHTML = `<td colspan="6" class="px-5 py-3"><div class="grid grid-cols-1 sm:grid-cols-3 gap-3 text-[11px]"><div><span class="text-slate-400">Date de naissance</span><div class="font-bold">${{escapePassageDisplay(dobValue)}}</div></div><div><span class="text-slate-400">Documents scannés</span><div class="font-bold">${{scannedTypes}}</div></div><div><span class="text-slate-400">Médecin traitant</span><div class="font-bold">${{escapePassageDisplay(`${{p.medecin_traitant_grade || 'Dr'}} ${{p.medecin_traitant_nom || ''}}`)}}</div></div></div></td>`;
                        tbody.appendChild(detailRow);
                    }});
                }}

                // Pagination
                const paginationWrapper = document.getElementById('passagesPaginationWrapper');
                const info = document.getElementById('passagesPaginationInfo');
                const controls = document.getElementById('passagesPaginationControls');

                if (totalItems > 0) {{
                    if (paginationWrapper) paginationWrapper.classList.remove('hidden');
                    if (info) info.innerHTML = `Affichage de <strong>${{startIndex + 1}}</strong> à <strong>${{Math.min(endIndex, totalItems)}}</strong> sur <strong>${{totalItems}}</strong> passages`;

                    if (totalItems > PASSAGES_PER_PAGE && controls) {{
                        let html = '';
                        const prevDisabled = currentPassagePage <= 1 ? 'opacity-40 cursor-not-allowed' : 'hover:bg-slate-100 dark:hover:bg-white/10 cursor-pointer';
                        html += `<button onclick="setPassagePage(${{currentPassagePage - 1}})" ${{currentPassagePage <= 1 ? 'disabled' : ''}} class="px-3 py-1 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-semibold ${{prevDisabled}}">◀ Précédent</button>`;

                        for (let i = 1; i <= totalPages; i++) {{
                            if (i === 1 || i === totalPages || (i >= currentPassagePage - 1 && i <= currentPassagePage + 1)) {{
                                const activeClass = i === currentPassagePage 
                                    ? 'bg-brand-neon text-black font-bold shadow-sm' 
                                    : 'border border-slate-200 dark:border-white/10 hover:bg-slate-100 dark:hover:bg-white/5 text-slate-700 dark:text-slate-300';
                                html += `<button onclick="setPassagePage(${{i}})" class="w-7 h-7 rounded-xl text-xs flex items-center justify-center transition ${{activeClass}}">${{i}}</button>`;
                            }} else if (i === currentPassagePage - 2 || i === currentPassagePage + 2) {{
                                html += `<span class="px-1 text-slate-400">...</span>`;
                            }}
                        }}

                        const nextDisabled = currentPassagePage >= totalPages ? 'opacity-40 cursor-not-allowed' : 'hover:bg-slate-100 dark:hover:bg-white/10 cursor-pointer';
                        html += `<button onclick="setPassagePage(${{currentPassagePage + 1}})" ${{currentPassagePage >= totalPages ? 'disabled' : ''}} class="px-3 py-1 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-semibold ${{nextDisabled}}">Suivant ▶</button>`;
                        controls.innerHTML = html;
                    }} else if (controls) {{
                        controls.innerHTML = '';
                    }}
                }} else if (paginationWrapper) {{
                    paginationWrapper.classList.add('hidden');
                }}
            }};

            window.setPassagePage = function(p) {{
                currentPassagePage = p;
                renderPassagesTable();
            }};

            // =================================================================
            // 2. VÉRIFICATION DE VALIDITÉ (VERROUILLAGE DU BOUTON)
            // =================================================================
            window.checkPassageFormValidity = function() {{
                const patientId = document.getElementById('passagePatientId')?.value;
                const doctorId = document.getElementById('passageMedecinTraitantId')?.value;
                const hasActs = currentPassageActs && currentPassageActs.length > 0;

                const obsText = (document.getElementById('passageObservation')?.value || '').trim();
                let hasObservation = obsText.length > 0;

                const qRadios = document.querySelectorAll('input[type="radio"]:checked');
                qRadios.forEach(r => {{
                    if (r.value === 'OUI') hasObservation = true;
                }});

                const isValid = Boolean(patientId) && Boolean(doctorId) && hasActs && hasObservation;
                const btn = document.getElementById('btnSubmitPassage');
                const hint = document.getElementById('passageValidationHint');

                if (btn) {{
                    if (isValid) {{
                        btn.disabled = false;
                        btn.className = "px-7 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md shadow-brand-neon/20 hover:opacity-90 cursor-pointer transition";
                        if (hint) hint.className = "text-[11px] text-emerald-500 font-bold";
                        if (hint) hint.innerText = "✓ Tous les critères obligatoires sont validés.";
                    }} else {{
                        btn.disabled = true;
                        btn.className = "px-7 py-2.5 rounded-xl bg-slate-300 dark:bg-slate-700 text-slate-500 font-bold text-xs shadow-none cursor-not-allowed transition";
                        if (hint) hint.className = "text-[11px] text-amber-500 font-bold";
                        if (hint) hint.innerText = "⚠️ Requis : Patient, Médecin Traitant, au moins 1 acte et 1 observation.";
                    }}
                }}
            }};

            // =================================================================
            // 3. AUTO-SÉLECTION NOUVEAU PATIENT COMPLET (Z-[100])
            // =================================================================
            window.openPassagePatientForm = function() {{
                if (passageEditMode) {{
                    window.openFullPatientModal();
                    return;
                }}
                if (typeof window.openPatientModal !== 'function') {{
                    alert("Le formulaire complet du module Patients n'a pas pu être initialisé. Rechargez le module puis réessayez.");
                    return;
                }}
                window.onPatientCreatedForPassage = async function(patient) {{
                    try {{
                        await window.loadReceptionPassagesModule();
                        window.selectPassagePatient(Number(patient.id));
                        window.checkPassageFormValidity();
                    }} catch (err) {{
                        console.error('Patient créé, mais impossible de le sélectionner dans le passage :', err);
                        alert('Le patient a été créé. Recherchez-le dans le formulaire pour le sélectionner.');
                    }} finally {{
                        window.onPatientCreatedForPassage = null;
                    }}
                }};
                const patientModal = document.getElementById('patientModal');
                if (patientModal) patientModal.classList.add('z-[120]');
                window.openPatientModal();
            }};

            window.openFullPatientModal = function() {{
                if (passageEditMode && passageEditPatientDraft) {{
                    passageEditPatientDraft = {{
                        ...passageEditPatientDraft,
                        nom: document.getElementById('passageEditPatientNom')?.value || passageEditPatientDraft.nom,
                        prenom: document.getElementById('passageEditPatientPrenom')?.value || passageEditPatientDraft.prenom,
                        date_naissance: document.getElementById('passageEditPatientDob')?.value || passageEditPatientDraft.date_naissance,
                        genre: document.getElementById('passageEditPatientGenre')?.value || passageEditPatientDraft.genre,
                        groupe_sanguin: document.getElementById('passageEditPatientBlood')?.value || passageEditPatientDraft.groupe_sanguin,
                        telephone_1: document.getElementById('passageEditPatientPhone1')?.value || passageEditPatientDraft.telephone_1,
                        telephone_2: document.getElementById('passageEditPatientPhone2')?.value || '',
                        email: document.getElementById('passageEditPatientEmail')?.value || '',
                        wilaya: document.getElementById('passageEditPatientWilaya')?.value || passageEditPatientDraft.wilaya,
                        commune: document.getElementById('passageEditPatientCommune')?.value || passageEditPatientDraft.commune
                    }};
                }}
                const form = document.getElementById('receptionFullPatientForm');
                if (form) form.reset();

                document.getElementById('recPatAvatar').value = '';
                document.getElementById('recPatPhotoFace').value = '';
                document.getElementById('recPatPhotoPDroit').value = '';
                document.getElementById('recPatPhotoPGauche').value = '';
                document.getElementById('recPatEmpreinte').value = '';
                document.getElementById('recPatAgeDisplay').value = '';

                const def = '/static/images/avatar-placeholder.svg?w=120';
                document.getElementById('recPreviewFace').src = def;
                document.getElementById('recPreviewPDroit').src = def;
                document.getElementById('recPreviewPGauche').src = def;

                handleRecPatWilayaChange();
                const title = document.getElementById('fullPatientModalTitle');
                const submit = document.getElementById('fullPatientModalSubmit');
                if (passageEditMode && passageEditPatientDraft) {{
                    const p = passageEditPatientDraft;
                    document.getElementById('recPatNom').value = p.nom || '';
                    document.getElementById('recPatPrenom').value = p.prenom || '';
                    document.getElementById('recPatDob').value = String(p.date_naissance || '').slice(0, 10);
                    document.getElementById('recPatGenre').value = p.genre || 'Homme';
                    document.getElementById('recPatGroupe').value = p.groupe_sanguin || 'O+';
                    document.getElementById('recPatTel1').value = p.telephone_1 === '-' ? '' : (p.telephone_1 || '');
                    document.getElementById('recPatTel2').value = p.telephone_2 || '';
                    document.getElementById('recPatEmail').value = p.email === '-' ? '' : (p.email || '');
                    document.getElementById('recPatWilaya').value = p.wilaya || '';
                    handleRecPatWilayaChange();
                    document.getElementById('recPatCommune').value = p.commune || '';
                    document.getElementById('recPatAvatar').value = p.avatar || '';
                    document.getElementById('recPatPhotoFace').value = p.photo_face || '';
                    document.getElementById('recPatPhotoPDroit').value = p.photo_profil_droit || '';
                    document.getElementById('recPatPhotoPGauche').value = p.photo_profil_gauche || '';
                    document.getElementById('recPatEmpreinte').value = p.empreinte_index || '';
                    document.getElementById('recPreviewFace').src = p.photo_face || p.avatar || def;
                    document.getElementById('recPreviewPDroit').src = p.photo_profil_droit || def;
                    document.getElementById('recPreviewPGauche').src = p.photo_profil_gauche || def;
                    calculateRecPatAge();
                    if (title) title.textContent = 'Modifier la fiche patient';
                    if (submit) submit.textContent = 'Valider les informations patient';
                }} else {{
                    if (title) title.textContent = 'Fiche Patient Complète (Sélection Automatique dans le Passage)';
                    if (submit) submit.textContent = 'Enregistrer & Sélectionner';
                }}
                document.getElementById('receptionFullPatientModal').classList.remove('hidden');
            }};

            window.closeFullPatientModal = function() {{
                document.getElementById('receptionFullPatientModal').classList.add('hidden');
            }};

            window.handleRecPatWilayaChange = function() {{
                const wSelect = document.getElementById('recPatWilaya');
                const cSelect = document.getElementById('recPatCommune');
                if (!wSelect || !cSelect) return;
                const communes = ALGERIA_WILAYAS_DATA[wSelect.value] || ["Centre-Ville"];
                cSelect.innerHTML = communes.map(c => `<option value="${{c}}">${{c}}</option>`).join('');
            }};

            window.calculateRecPatAge = function() {{
                const dob = document.getElementById('recPatDob').value;
                const display = document.getElementById('recPatAgeDisplay');
                if (!dob || !display) return;
                const born = new Date(dob);
                const today = new Date();
                let years = today.getFullYear() - born.getFullYear();
                let months = today.getMonth() - born.getMonth();
                if (today.getDate() < born.getDate()) months--;
                if (months < 0) {{ years--; months += 12; }}
                display.value = years > 0 ? `${{years}} ans` : `${{months}} mois`;
            }};

            window.previewRecPatAvatar = function(event) {{
                const file = event.target.files[0];
                if (file) {{
                    const reader = new FileReader();
                    reader.onload = function(e) {{
                        document.getElementById('recPatAvatar').value = e.target.result;
                        document.getElementById('recPatPhotoFace').value = e.target.result;
                        document.getElementById('recPreviewFace').src = e.target.result;
                    }};
                    reader.readAsDataURL(file);
                }}
            }};

            window.handleFullPatientSubmit = async function(e) {{
                e.preventDefault();
                const payload = {{
                    ...(passageEditPatientDraft || {{}}),
                    nom: document.getElementById('recPatNom').value.trim(),
                    prenom: document.getElementById('recPatPrenom').value.trim(),
                    date_naissance: document.getElementById('recPatDob').value,
                    genre: document.getElementById('recPatGenre').value,
                    telephone_1: document.getElementById('recPatTel1').value.trim(),
                    telephone_2: document.getElementById('recPatTel2').value.trim(),
                    email: document.getElementById('recPatEmail').value.trim(),
                    wilaya: document.getElementById('recPatWilaya').value,
                    commune: document.getElementById('recPatCommune').value,
                    groupe_sanguin: document.getElementById('recPatGroupe').value,
                    type_piece_identite: passageEditPatientDraft?.type_piece_identite || "Carte Chifa",
                    avatar: document.getElementById('recPatAvatar').value,
                    photo_face: document.getElementById('recPatPhotoFace').value,
                    photo_profil_droit: document.getElementById('recPatPhotoPDroit').value,
                    photo_profil_gauche: document.getElementById('recPatPhotoPGauche').value,
                    empreinte_index: document.getElementById('recPatEmpreinte').value,
                    pieces_scannees: passageEditPatientDraft?.pieces_scannees || []
                }};

                if (passageEditMode) {{
                    passageEditPatientDraft = payload;
                    passageEditPatientData = payload;
                    document.getElementById('passageEditPatientNom').value = payload.nom;
                    document.getElementById('passageEditPatientPrenom').value = payload.prenom;
                    document.getElementById('passageEditPatientDob').value = payload.date_naissance;
                    document.getElementById('passageEditPatientGenre').value = payload.genre;
                    document.getElementById('passageEditPatientBlood').value = payload.groupe_sanguin;
                    document.getElementById('passageEditPatientPhone1').value = payload.telephone_1;
                    document.getElementById('passageEditPatientPhone2').value = payload.telephone_2;
                    document.getElementById('passageEditPatientEmail').value = payload.email;
                    const inlineWilaya = document.getElementById('passageEditPatientWilaya');
                    if (inlineWilaya && payload.wilaya && !Array.from(inlineWilaya.options).some(option => option.value === payload.wilaya)) inlineWilaya.add(new Option(payload.wilaya, payload.wilaya));
                    if (inlineWilaya) inlineWilaya.value = payload.wilaya;
                    document.getElementById('passageEditPatientCommune').value = payload.commune;
                    closeFullPatientModal();
                    const selected = (receptionInitData.patients || []).find(p => Number(p.id) === Number(payload.id_patient || payload.id));
                    if (selected) Object.assign(selected, payload);
                    selectPassagePatient(Number(payload.id_patient || payload.id));
                    if (typeof window.showToast === 'function') window.showToast('Les informations patient seront enregistrées avec la correction du passage.', 'success');
                    return;
                }}

                try {{
                    const res = await fetch('/api/patients-module/save', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify(payload)
                    }}).then(r => r.json());

                    if (res.success && res.patient_id) {{
                        closeFullPatientModal();
                        await loadReceptionPassagesModule();
                        selectPassagePatient(res.patient_id);
                        checkPassageFormValidity();
                    }} else {{
                        alert(res.message);
                    }}
                }} catch(err) {{
                    alert("Erreur lors de l'enregistrement du patient.");
                }}
            }};

            // =================================================================
            // 4. AUTO-SÉLECTION NOUVEAU MÉDECIN COMPLET (Z-[100])
            // =================================================================
            window.openFullDoctorModal = function() {{
                const form = document.getElementById('receptionFullDoctorModal')?.querySelector('form');
                if (form) form.reset();
                handleRecDocWilayaChange();
                document.getElementById('receptionFullDoctorModal').classList.remove('hidden');
            }};

            window.closeFullDoctorModal = function() {{
                document.getElementById('receptionFullDoctorModal').classList.add('hidden');
            }};

            window.handleRecDocWilayaChange = function() {{
                const wSelect = document.getElementById('recDocWilaya');
                const cSelect = document.getElementById('recDocCommune');
                if (!wSelect || !cSelect) return;
                const communes = ALGERIA_WILAYAS_DATA[wSelect.value] || ["Centre-Ville"];
                cSelect.innerHTML = communes.map(c => `<option value="${{c}}">${{c}}</option>`).join('');
            }};

            window.handleFullDoctorSubmit = async function(e) {{
                e.preventDefault();
                const payload = {{
                    grade: document.getElementById('recDocGrade').value,
                    nom: document.getElementById('recDocNom').value.trim(),
                    prenom: document.getElementById('recDocPrenom').value.trim(),
                    telephone: document.getElementById('recDocTel').value.trim(),
                    email: document.getElementById('recDocEmail').value.trim(),
                    adresse: document.getElementById('recDocAdresse').value.trim(),
                    wilaya: document.getElementById('recDocWilaya').value,
                    commune: document.getElementById('recDocCommune').value
                }};

                try {{
                    const res = await fetch('/api/treating-doctors/save', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify(payload)
                    }}).then(r => r.json());

                    if (res.success && res.doctor) {{
                        closeFullDoctorModal();
                        await loadReceptionPassagesModule();
                        selectPassageMedecinTraitant(res.doctor.id);
                        checkPassageFormValidity();
                    }} else {{
                        alert(res.message);
                    }}
                }} catch(err) {{
                    alert("Erreur lors de l'enregistrement du médecin.");
                }}
            }};

            // =================================================================
            // 5. GESTION DES RECHERCHES ET SÉLECTIONS
            // =================================================================
            window.populatePassageTypeSelect = function(selectedVal = '') {{
                const select = document.getElementById('passageTypeSelect');
                if (!select) return;

                const specs = (receptionInitData && Array.isArray(receptionInitData.user_specialites))
                    ? receptionInitData.user_specialites
                    : [];

                if (IS_RADIOLOGY_MODE && specs.length > 0) {{
                    const preferred = specs.find(s => /radiolog|imagerie/i.test(String(s))) || specs[0];
                    const selected = selectedVal || preferred;
                    select.innerHTML = specs.map(s => {{
                        const isSel = String(selected).trim().toLowerCase() === String(s).trim().toLowerCase() ? 'selected' : '';
                        return '<option value="' + s + '" ' + isSel + '>🩺 ' + s + '</option>';
                    }}).join('');
                    select.value = selected;
                    currentPassageTypeValue = select.value;
                    return;
                }}

                if (IS_RADIOLOGY_MODE) {{
                    select.innerHTML = '<option value="Radiologie" selected>🩻 Radiologie</option>';
                    select.value = 'Radiologie';
                    currentPassageTypeValue = select.value;
                    return;
                }}

                if (specs.length === 0) {{
                    select.innerHTML = '<option value="" disabled selected>⚠️ Aucune spécialité cochée dans votre profil</option>';
                    currentPassageTypeValue = '';
                    return;
                }}

                select.innerHTML = specs.map(s => {{
                    const isSel = (selectedVal && selectedVal.trim().toLowerCase() === s.trim().toLowerCase()) ? 'selected' : '';
                    return `<option value="${{s}}" ${{isSel}}>🩺 ${{s}}</option>`;
                }}).join('');

                if (selectedVal && !specs.some(s => s.trim().toLowerCase() === selectedVal.trim().toLowerCase())) {{
                    select.add(new Option(`🩺 ${{selectedVal}}`, selectedVal, true, true));
                }}

                if (selectedVal && specs.includes(selectedVal)) {{
                    select.value = selectedVal;
                }} else if (specs.length > 0) {{
                    select.value = specs[0];
                }}
                currentPassageTypeValue = select.value;
            }};

            window.onPassageTypeChange = function() {{
                const select = document.getElementById('passageTypeSelect');
                const input = document.getElementById('prestationSearchInput');
                const selectedType = select?.value || '';
                // Le type filtre les prochains actes; les actes déjà ajoutés restent dans le passage.
                currentPassageTypeValue = selectedType;
                selectedPrestationTemp = null;
                if (input) input.value = '';
                searchPrestationsLive();
            }};

            window.searchPassagePatientLive = function() {{
                const q = (document.getElementById('passagePatientSearchInput')?.value || '').toLowerCase().trim();
                const dropdown = document.getElementById('passagePatientSearchResults');
                const input = document.getElementById('passagePatientSearchInput');
                if (!dropdown) return;

                if (!q) {{
                    dropdown.classList.add('hidden');
                    input?.setAttribute('aria-expanded', 'false');
                    input?.removeAttribute('aria-activedescendant');
                    passagePatientSearchMatches = [];
                    passagePatientSearchActiveIndex = -1;
                    return;
                }}

                const qDigits = q.replace(/\\D/g, '');
                const matches = (receptionInitData.patients || []).filter(p => {{
                    const dob = String(p.date_naissance || '');
                    const dobDigits = dob.replace(/\\D/g, '');
                    const dobDisplay = dob.match(/^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})/)
                        ? `${{dob.slice(8, 10)}}/${{dob.slice(5, 7)}}/${{dob.slice(0, 4)}}`
                        : dob;
                    return (p.nom && p.nom.toLowerCase().includes(q)) ||
                        (p.prenom && p.prenom.toLowerCase().includes(q)) ||
                        (p.telephone_1 && p.telephone_1.includes(q)) ||
                        String(p.id).includes(q) ||
                        dobDisplay.toLowerCase().includes(q) ||
                        (qDigits.length >= 4 && dobDigits.includes(qDigits));
                }});
                passagePatientSearchMatches = matches;
                passagePatientSearchActiveIndex = matches.length ? 0 : -1;

                if (matches.length === 0) {{
                    dropdown.innerHTML = '<div class="p-3 text-xs text-slate-400 italic text-center">Aucun patient trouvé.</div>';
                }} else {{
                    dropdown.innerHTML = matches.map((p, index) => `
                        <button type="button" id="passagePatientOption-${{p.id}}" role="option" aria-selected="${{index === 0}}" onclick="selectPassagePatient(${{p.id}})" onmouseenter="setPassagePatientSearchActive(${{index}})" class="w-full text-left p-2.5 ${{index === 0 ? 'bg-brand-neon/10' : ''}} hover:bg-brand-neon/10 flex items-center justify-between cursor-pointer transition">
                            <div class="flex items-center space-x-2.5">
                                <img src="${{p.avatar || '/static/images/avatar-placeholder.svg?w=120&auto=format&fit=crop&q=80'}}" class="w-8 h-8 rounded-full object-cover border border-slate-200">
                                <div>
                                    <span class="text-xs font-bold text-slate-800 dark:text-slate-100">${{p.nom}} ${{p.prenom}}</span>
                                    <span class="text-[10px] text-slate-400 block font-mono">#DOS-${{String(p.id).padStart(4, '0')}} | Tél: ${{p.telephone_1}}</span>
                                    <span class="text-[10px] text-slate-400 block">Né(e) le : ${{p.date_naissance || 'Non renseignée'}}</span>
                                </div>
                            </div>
                            <span class="px-2 py-0.5 rounded text-[10px] font-black bg-rose-50 text-rose-600 border border-rose-200">${{p.groupe_sanguin || 'O+'}}</span>
                        </button>
                    `).join('');
                }}
                dropdown.classList.remove('hidden');
                input?.setAttribute('aria-expanded', 'true');
                window.setPassagePatientSearchActive(passagePatientSearchActiveIndex);
            }};

            window.setPassagePatientSearchActive = function(index) {{
                const dropdown = document.getElementById('passagePatientSearchResults');
                const input = document.getElementById('passagePatientSearchInput');
                if (!dropdown || index < 0 || index >= passagePatientSearchMatches.length) return;
                passagePatientSearchActiveIndex = index;
                dropdown.querySelectorAll('[role="option"]').forEach((option, optionIndex) => {{
                    const active = optionIndex === index;
                    option.setAttribute('aria-selected', String(active));
                    option.classList.toggle('bg-brand-neon/10', active);
                    if (active) {{
                        input?.setAttribute('aria-activedescendant', option.id);
                        option.scrollIntoView({{ block: 'nearest' }});
                    }}
                }});
            }};

            window.handlePassagePatientSearchKeydown = function(event) {{
                const dropdown = document.getElementById('passagePatientSearchResults');
                if (!dropdown || dropdown.classList.contains('hidden')) return;
                if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {{
                    if (!passagePatientSearchMatches.length) return;
                    event.preventDefault();
                    const direction = event.key === 'ArrowDown' ? 1 : -1;
                    const count = passagePatientSearchMatches.length;
                    const next = (passagePatientSearchActiveIndex + direction + count) % count;
                    window.setPassagePatientSearchActive(next);
                }} else if (event.key === 'Enter' && passagePatientSearchMatches.length) {{
                    event.preventDefault();
                    const index = passagePatientSearchActiveIndex >= 0 ? passagePatientSearchActiveIndex : 0;
                    window.selectPassagePatient(passagePatientSearchMatches[index].id);
                }} else if (event.key === 'Escape') {{
                    dropdown.classList.add('hidden');
                    event.currentTarget.setAttribute('aria-expanded', 'false');
                    event.currentTarget.removeAttribute('aria-activedescendant');
                }}
            }};

            window.selectPassagePatient = function(patientId) {{
                const patient = (receptionInitData.patients || []).find(p => p.id === patientId);
                if (!patient) return;

                document.getElementById('passagePatientId').value = patient.id;
                document.getElementById('passagePatientSearchInput').value = `${{patient.nom}} ${{patient.prenom}}`;
                document.getElementById('passagePatientSearchResults').classList.add('hidden');
                document.getElementById('passagePatientSearchInput')?.setAttribute('aria-expanded', 'false');
                document.getElementById('passagePatientSearchInput')?.removeAttribute('aria-activedescendant');
                passagePatientSearchMatches = [];
                passagePatientSearchActiveIndex = -1;

                const card = document.getElementById('patientSelectedSummaryCard');
                document.getElementById('selectedPatientAvatar').src = patient.avatar || '/static/images/avatar-placeholder.svg?w=120&auto=format&fit=crop&q=80';
                document.getElementById('selectedPatientName').innerText = `${{patient.nom}} ${{patient.prenom}}`;
                document.getElementById('selectedPatientDetails').innerText = `#DOS-${{String(patient.id).padStart(4, '0')}} | Tél: ${{patient.telephone_1}} | Groupe: ${{patient.groupe_sanguin || 'O+'}}`;
                card.classList.remove('hidden');

                checkPassageFormValidity();
            }};

            window.clearSelectedPatient = function() {{
                document.getElementById('passagePatientId').value = '';
                document.getElementById('passagePatientSearchInput').value = '';
                document.getElementById('patientSelectedSummaryCard').classList.add('hidden');
                passagePatientSearchMatches = [];
                passagePatientSearchActiveIndex = -1;
                checkPassageFormValidity();
            }};

            window.searchPassageMedecinTraitantLive = function() {{
                const q = (document.getElementById('passageMedecinTraitantSearchInput')?.value || '').toLowerCase().trim();
                const dropdown = document.getElementById('passageMedecinTraitantSearchResults');
                const input = document.getElementById('passageMedecinTraitantSearchInput');
                if (!dropdown) return;

                if (!q) {{
                    dropdown.classList.add('hidden');
                    input?.setAttribute('aria-expanded', 'false');
                    input?.removeAttribute('aria-activedescendant');
                    passageMedecinSearchMatches = [];
                    passageMedecinSearchActiveIndex = -1;
                    return;
                }}

                const matches = (receptionInitData.medecins_traitants || []).filter(d =>
                    (`${{d.grade || ''}} ${{d.nom || ''}} ${{d.prenom || ''}}`).toLowerCase().includes(q) ||
                    (d.telephone && d.telephone.includes(q))
                );
                passageMedecinSearchMatches = matches;
                passageMedecinSearchActiveIndex = matches.length ? 0 : -1;

                if (!matches.length) {{
                    dropdown.innerHTML = '<div class="p-3 text-center text-xs text-slate-400 italic">Aucun médecin traitant trouvé.</div>';
                }} else {{
                    dropdown.innerHTML = matches.map((d, index) => `
                        <button type="button" id="passageMedecinOption-${{d.id}}" role="option" aria-selected="${{index === 0}}" onclick="selectPassageMedecinTraitant(${{d.id}})" onmouseenter="setPassageMedecinSearchActive(${{index}})" class="w-full text-left p-2.5 ${{index === 0 ? 'bg-brand-neon/10' : ''}} hover:bg-brand-neon/10 cursor-pointer transition">
                            <div class="text-xs font-bold text-slate-800 dark:text-slate-100">${{d.grade || 'Dr'}} ${{d.nom || ''}} ${{d.prenom || ''}}</div>
                            <div class="text-[10px] text-slate-400">${{d.wilaya || 'Algérie'}}${{d.telephone ? ' • ' + d.telephone : ''}}</div>
                        </button>
                    `).join('');
                }}
                dropdown.classList.remove('hidden');
                input?.setAttribute('aria-expanded', 'true');
                window.setPassageMedecinSearchActive(passageMedecinSearchActiveIndex);
            }};

            window.setPassageMedecinSearchActive = function(index) {{
                const dropdown = document.getElementById('passageMedecinTraitantSearchResults');
                const input = document.getElementById('passageMedecinTraitantSearchInput');
                if (!dropdown || index < 0 || index >= passageMedecinSearchMatches.length) return;
                passageMedecinSearchActiveIndex = index;
                dropdown.querySelectorAll('[role="option"]').forEach((option, optionIndex) => {{
                    const active = optionIndex === index;
                    option.setAttribute('aria-selected', String(active));
                    option.classList.toggle('bg-brand-neon/10', active);
                    if (active) {{
                        input?.setAttribute('aria-activedescendant', option.id);
                        option.scrollIntoView({{ block: 'nearest' }});
                    }}
                }});
            }};

            window.handlePassageMedecinTraitantSearchKeydown = function(event) {{
                const dropdown = document.getElementById('passageMedecinTraitantSearchResults');
                if (!dropdown || dropdown.classList.contains('hidden')) return;
                if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {{
                    if (!passageMedecinSearchMatches.length) return;
                    event.preventDefault();
                    const direction = event.key === 'ArrowDown' ? 1 : -1;
                    const count = passageMedecinSearchMatches.length;
                    const next = (passageMedecinSearchActiveIndex + direction + count) % count;
                    window.setPassageMedecinSearchActive(next);
                }} else if (event.key === 'Enter' && passageMedecinSearchMatches.length) {{
                    event.preventDefault();
                    const index = passageMedecinSearchActiveIndex >= 0 ? passageMedecinSearchActiveIndex : 0;
                    window.selectPassageMedecinTraitant(passageMedecinSearchMatches[index].id);
                }} else if (event.key === 'Escape') {{
                    dropdown.classList.add('hidden');
                    event.currentTarget.setAttribute('aria-expanded', 'false');
                    event.currentTarget.removeAttribute('aria-activedescendant');
                }}
            }};

            window.selectPassageMedecinTraitant = function(doctorId) {{
                const doc = (receptionInitData.medecins_traitants || []).find(d => d.id === doctorId);
                if (!doc) return;

                document.getElementById('passageMedecinTraitantId').value = doc.id;
                document.getElementById('passageMedecinTraitantSearchInput').value = `${{doc.grade || 'Dr'}} ${{doc.nom || ''}} ${{doc.prenom || ''}}`;
                document.getElementById('passageMedecinTraitantSearchResults').classList.add('hidden');
                document.getElementById('passageMedecinTraitantSearchInput')?.setAttribute('aria-expanded', 'false');
                document.getElementById('passageMedecinTraitantSearchInput')?.removeAttribute('aria-activedescendant');
                passageMedecinSearchMatches = [];
                passageMedecinSearchActiveIndex = -1;

                const card = document.getElementById('medecinTraitantSelectedCard');
                document.getElementById('selectedMedecinTraitantName').innerText = `${{doc.grade || 'Dr'}} ${{doc.nom || ''}} ${{doc.prenom || ''}}`;
                document.getElementById('selectedMedecinTraitantDetails').innerText = `${{doc.wilaya || 'Médecin Référent'}}${{doc.telephone ? ' • ' + doc.telephone : ''}}`;
                card.classList.remove('hidden');

                checkPassageFormValidity();
            }};

            window.clearSelectedMedecinTraitant = function() {{
                document.getElementById('passageMedecinTraitantId').value = '';
                document.getElementById('passageMedecinTraitantSearchInput').value = '';
                document.getElementById('medecinTraitantSelectedCard').classList.add('hidden');
                passageMedecinSearchMatches = [];
                passageMedecinSearchActiveIndex = -1;
                checkPassageFormValidity();
            }};

            // =================================================================
            // 6. PRESTATIONS & PANIER
            // =================================================================
            window.searchPrestationsLive = function() {{
                const q = (document.getElementById('prestationSearchInput')?.value || '').toLowerCase().trim();
                const dropdown = document.getElementById('prestationSearchResultsTable');
                if (!dropdown) return;

                const selectedType = (document.getElementById('passageTypeSelect')?.value || '').toLowerCase().trim();
                const normalizedSelectedType = normalizePassageSpecialite(selectedType);

                currentMatchingPrestations = (receptionInitData.consultations || []).filter(c => {{
                    const matchesQuery = !q || (c.designation && c.designation.toLowerCase().includes(q)) || (c.categorie && c.categorie.toLowerCase().includes(q));
                    const normalizedCategory = String(c.categorie || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
                    const matchesType = IS_RADIOLOGY_MODE
                        ? normalizedCategory.includes('radiolog') && normalizedCategory.includes('medical')
                        : (!normalizedSelectedType || normalizePassageSpecialite(c.specialite) === normalizedSelectedType);
                    return matchesQuery && matchesType;
                }}).map(c => ({{
                    nom: c.designation,
                    categorie: c.categorie,
                    specialite: c.specialite,
                    modalite: c.modalite || '',
                    transmettre_hl7: c.transmettre_hl7 === true,
                    prix_jour: parseFloat(c.tarif || 0),
                    prix_nuit: parseFloat(c.tarif_nuit || c.tarif || 0)
                }}));

                if (currentMatchingPrestations.length === 0) {{
                    dropdown.innerHTML = '<div class="p-3 text-xs text-slate-400 italic text-center">Aucune prestation trouvée pour cette discipline.</div>';
                }} else {{
                    dropdown.innerHTML = currentMatchingPrestations.map((r, i) => `
                        <div onclick="onSelectPrestationByIndex(${{i}})" class="p-2 hover:bg-brand-neon/10 flex items-center justify-between cursor-pointer text-xs">
                            <span class="font-bold text-slate-800 dark:text-slate-100">${{r.nom}}</span>
                            <div class="space-x-2 font-mono">
                                <span class="text-emerald-600 dark:text-brand-neon font-bold">☀️ ${{r.prix_jour}} DA</span>
                                <span class="text-purple-600 dark:text-purple-400 font-bold">🌙 ${{r.prix_nuit}} DA</span>
                            </div>
                        </div>
                    `).join('');
                }}
                dropdown.classList.remove('hidden');
            }};

            window.onSelectPrestationByIndex = function(index) {{
                selectedPrestationTemp = currentMatchingPrestations[index];
                if (!selectedPrestationTemp) return;

                document.getElementById('prestationSearchInput').value = selectedPrestationTemp.nom;
                document.getElementById('prestationSearchResultsTable').classList.add('hidden');
            }};

            window.autoDetectTarifPeriod = function() {{
                const timeInput = document.getElementById('passageHeure');
                const timeVal = timeInput ? timeInput.value : "12:00";
                const [h, m] = (timeVal || "12:00").split(':').map(Number);
                const isNight = (h >= 20 || h < 8);

                const selectPeriode = document.getElementById('selectAddPeriode');
                if (selectPeriode) selectPeriode.value = isNight ? 'Garde' : 'Jour';

                const badge = document.getElementById('autoTarifBadge');
                if (badge) {{
                    badge.innerText = isNight ? "🌙 Période Garde/Nuit active" : "☀️ Période Jour active";
                    badge.className = isNight 
                        ? "text-[10px] font-bold px-2.5 py-0.5 rounded-full bg-purple-100 dark:bg-purple-950/40 text-purple-800 dark:text-purple-300" 
                        : "text-[10px] font-bold px-2.5 py-0.5 rounded-full bg-amber-100 dark:bg-amber-950/40 text-amber-800 dark:text-amber-300";
                }}
            }};

            window.addSelectedPrestationToTable = function() {{
                if (!selectedPrestationTemp) {{
                    alert("Veuillez d'abord rechercher et sélectionner une prestation.");
                    return;
                }}

                const periode = document.getElementById('selectAddPeriode').value;
                const quantite = parseInt(document.getElementById('inputAddQuantite').value) || 1;
                const tarif = (periode === 'Jour') ? selectedPrestationTemp.prix_jour : selectedPrestationTemp.prix_nuit;

                currentPassageActs.push({{
                    nom: selectedPrestationTemp.nom,
                    categorie: selectedPrestationTemp.categorie,
                    specialite: selectedPrestationTemp.specialite,
                    modalite: selectedPrestationTemp.modalite || '',
                    transmettre_hl7: selectedPrestationTemp.transmettre_hl7 === true,
                    periode: periode,
                    quantite: quantite,
                    tarif: tarif,
                    sous_total: tarif * quantite
                }});

                renderPassageActsTable();
                document.getElementById('prestationSearchInput').value = '';
                selectedPrestationTemp = null;
                checkPassageFormValidity();
            }};

            window.removeActFromPassageTable = function(index) {{
                currentPassageActs.splice(index, 1);
                renderPassageActsTable();
                checkPassageFormValidity();
            }};

            window.renderPassageActsTable = function() {{
                const tbody = document.getElementById('passageActsTableBody');
                if (!tbody) return;
                tbody.innerHTML = '';

                let grandTotal = 0;
                currentPassageActs.forEach((act, idx) => {{
                    grandTotal += act.sous_total;
                    const tr = document.createElement('tr');
                    tr.className = "hover:bg-slate-50 dark:hover:bg-white/[0.02]";
                    tr.innerHTML = `
                        <td class="p-2.5 font-bold text-slate-800 dark:text-slate-100">${{act.nom}}</td>
                        <td class="p-2.5"><span class="px-2 py-0.5 rounded text-[10px] font-bold ${{act.periode === 'Jour' ? 'bg-amber-100 text-amber-800' : 'bg-purple-100 text-purple-800'}}">${{act.periode}}</span></td>
                        <td class="p-2.5 text-center font-bold">${{act.quantite}}</td>
                        <td class="p-2.5 font-mono">${{Number(act.tarif).toLocaleString()}} DA</td>
                        <td class="p-2.5 font-black text-emerald-600 dark:text-brand-neon font-mono">${{Number(act.sous_total).toLocaleString()}} DA</td>
                        <td class="p-2.5 text-right"><button type="button" onclick="removeActFromPassageTable(${{idx}})" class="text-rose-500 hover:text-rose-700 font-bold p-1">✕</button></td>
                    `;
                    tbody.appendChild(tr);
                }});

                if (currentPassageActs.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="6" class="p-3 text-center text-slate-400 italic">Aucune prestation ajoutée.</td></tr>';
                }}

                document.getElementById('passageMontantBrut').value = Number(grandTotal).toFixed(2);
                calculatePassageFinancials();
            }};

            // =================================================================
            // 7. ENCAISSEMENT & REMISES
            // =================================================================
            window.setRemiseType = function(type) {{
                document.getElementById('passageRemiseType').value = type;
                const btnDA = document.getElementById('btnRemiseDA');
                const btnPct = document.getElementById('btnRemisePct');
                const label = document.getElementById('remiseUnitLabel');

                if (type === 'DA') {{
                    btnDA.className = "px-2 py-0.5 rounded-md text-[10px] font-bold bg-brand-neon text-black";
                    btnPct.className = "px-2 py-0.5 rounded-md text-[10px] font-bold text-slate-500 hover:text-white";
                    label.innerText = "DA";
                }} else {{
                    btnPct.className = "px-2 py-0.5 rounded-md text-[10px] font-bold bg-brand-neon text-black";
                    btnDA.className = "px-2 py-0.5 rounded-md text-[10px] font-bold text-slate-500 hover:text-white";
                    label.innerText = "%";
                }}
                calculatePassageFinancials();
            }};

            window.calculatePassageFinancials = function() {{
                const brut = parseFloat(document.getElementById('passageMontantBrut').value || 0);
                const remiseType = document.getElementById('passageRemiseType').value || 'DA';
                const remiseValeur = parseFloat(document.getElementById('passageRemiseValeur').value || 0);

                let remiseMontantDA = (remiseType === '%') ? (brut * remiseValeur) / 100 : remiseValeur;
                remiseMontantDA = Math.min(brut, Math.max(0, remiseMontantDA));
                document.getElementById('passageRemiseMontantDA').value = remiseMontantDA.toFixed(2);

                const net = Math.max(0, brut - remiseMontantDA);
                document.getElementById('passageNetAPayerDisplay').innerText = Number(net).toLocaleString() + " DA";

                const versement = parseFloat(document.getElementById('passageVersement').value || 0);
                const reste = Math.max(0, net - versement);
                document.getElementById('passageReste').value = Number(reste).toFixed(2) + " DA";
            }};

            // =================================================================
            // 8. NUMÉRISATION WIA, GESTION DES SCANS & LIGHTBOX
            // =================================================================
            window.openPassageHardwareScannerModal = async function() {{
                const modal = document.getElementById('scannerDeviceModal');
                const select = document.getElementById('scannerDeviceSelect');
                const noScanner = document.getElementById('noScannerAlert');
                const statusMsg = document.getElementById('scannerStatusMsg');

                if (statusMsg) statusMsg.classList.add('hidden');
                if (noScanner) noScanner.classList.add('hidden');
                if (select) select.innerHTML = '<option value="">Recherche du matériel en cours...</option>';
                if (modal) {{
                    modal.style.zIndex = '10000';
                    modal.classList.remove('hidden');
                    modal.focus?.();
                }}

                try {{
                    const bridge = window.pywebview?.api;
                    if (!bridge?.client_scanner_detect) throw new Error('Ouvrez Cliniva avec le client Cliniva de ce poste pour utiliser son scanner.');
                    const res = await bridge.client_scanner_detect();
                    const scanners = res.scanners || [];
                    if (scanners.length > 0) {{
                        select.innerHTML = scanners.map(s => `<option value="${{s.id}}">🖨️ ${{s.name}}</option>`).join('');
                    }} else {{
                        if (noScanner) noScanner.classList.remove('hidden');
                        select.innerHTML = '<option value="">Aucun périphérique disponible</option>';
                    }}
                }} catch (e) {{
                    if (noScanner) {{ noScanner.classList.remove('hidden'); noScanner.querySelector('p').textContent = e.message || 'Scanner local indisponible. Vérifiez son branchement et son pilote WIA.'; }}
                    if (noScanner) noScanner.classList.remove('hidden');
                }}
            }};

            window.closeScannerDeviceModal = function() {{
                const modal = document.getElementById('scannerDeviceModal');
                if (modal) modal.classList.add('hidden');
            }};

            window.executePassageHardwareScan = async function() {{
                const statusMsg = document.getElementById('scannerStatusMsg');
                const btnLaunch = document.getElementById('btnLaunchScan');
                const deviceSelect = document.getElementById('scannerDeviceSelect');
                const typeDoc = document.getElementById('passageDocTypeSelect')?.value || 'Ordonnance';

                if (statusMsg) statusMsg.classList.remove('hidden');
                if (btnLaunch) btnLaunch.disabled = true;

                try {{
                    const bridge = window.pywebview?.api;
                    if (!bridge?.client_scanner_acquire) throw new Error('Le scanner client nécessite le lanceur Cliniva sur ce poste.');
                    const res = await bridge.client_scanner_acquire(deviceSelect?.value || '');

                    if (res.success && res.image_data) {{
                        closeScannerDeviceModal();
                        showPassageDocPreview(typeDoc, res.image_data);
                    }} else {{
                        alert(res.message || "Erreur lors de l'acquisition du scan.");
                    }}
                }} catch (err) {{
                    alert(err.message || "Erreur de communication avec le scanneur.");
                }} finally {{
                    if (statusMsg) statusMsg.classList.add('hidden');
                    if (btnLaunch) btnLaunch.disabled = false;
                }}
            }};

            window.previewPassageDoc = function(event) {{
                const file = event.target.files[0];
                const typeDoc = document.getElementById('passageDocTypeSelect').value;
                if (!file) return;

                const reader = new FileReader();
                reader.onload = function(e) {{
                    showPassageDocPreview(typeDoc, e.target.result);
                }};
                reader.readAsDataURL(file);
            }};

            function showPassageDocPreview(typeDoc, base64Data) {{
                if (!base64Data) return;
                pendingPassageDocPreview = {{ type: typeDoc || 'Document', data: base64Data }};

                const modal = document.getElementById('passageDocPreviewModal');
                const title = document.getElementById('passageDocPreviewTitle');
                const img = document.getElementById('passageDocPreviewImg');
                const pdf = document.getElementById('passageDocPreviewPdf');
                const isPdf = String(base64Data).toLowerCase().includes('application/pdf');

                if (title) title.innerText = (typeDoc || 'Document') + ' — vérifiez avant validation';
                const sizeLabel = document.getElementById('passageDocPreviewSize');
                if (sizeLabel) sizeLabel.textContent = `Taille : ${{formatScannedDocumentSize(base64Data)}}`;
                if (img) {{ img.classList.toggle('hidden', isPdf); img.src = isPdf ? '' : base64Data; }}
                if (pdf) {{ pdf.classList.toggle('hidden', !isPdf); pdf.src = isPdf ? base64Data : 'about:blank'; }}
                if (modal) {{
                    modal.style.zIndex = '10000';
                    modal.classList.remove('hidden');
                    modal.focus?.();
                }}
            }}

            window.validatePassageDocPreview = function() {{
                if (!pendingPassageDocPreview) return;
                currentPassageScannedDocs.push({{
                    type: pendingPassageDocPreview.type,
                    heure: new Date().toLocaleTimeString('fr-FR', {{ hour: '2-digit', minute: '2-digit' }}),
                    data: pendingPassageDocPreview.data
                }});
                pendingPassageDocPreview = null;
                closePassageDocPreview();
                renderPassageScannedDocsGallery();
            }};

            window.cancelPassageDocPreview = function() {{
                pendingPassageDocPreview = null;
                closePassageDocPreview();
                const input = document.getElementById('passageDocUploadInput');
                if (input) input.value = '';
            }};

            function closePassageDocPreview() {{
                const modal = document.getElementById('passageDocPreviewModal');
                const img = document.getElementById('passageDocPreviewImg');
                const pdf = document.getElementById('passageDocPreviewPdf');
                if (modal) modal.classList.add('hidden');
                if (img) img.src = '';
                if (pdf) pdf.src = 'about:blank';
            }}

            window.renderPassageScannedDocsGallery = function() {{
                const container = document.getElementById('passageScannedDocsGallery');
                if (!container) return;
                container.innerHTML = '';

                if (currentPassageScannedDocs.length === 0) {{
                    container.innerHTML = '<span class="text-[11px] text-slate-400 italic col-span-4 py-1 text-center">Aucun document scanné pour ce passage.</span>';
                    return;
                }}

                currentPassageScannedDocs.forEach((doc, idx) => {{
                    const div = document.createElement('div');
                    div.className = "bg-white dark:bg-[#121c15] p-2 rounded-2xl border border-slate-200 dark:border-white/10 shadow-xs flex items-center justify-between";
                    const isPdf = String(doc.data || '').toLowerCase().includes('application/pdf');

                    div.innerHTML = `
                        <button type="button" class="flex items-center space-x-2 truncate cursor-pointer flex-1 text-left" onclick="openPassageDocFromEditor(${{idx}})">
                            ${{isPdf 
                                ? '<div class="w-8 h-8 rounded-lg bg-rose-500/20 text-rose-500 flex items-center justify-center font-bold text-xs"><i class="ph-bold ph-file-pdf"></i></div>' 
                                : `<img src="${{doc.data}}" class="w-8 h-8 rounded-lg object-cover border border-slate-200">`}}
                            <div class="truncate">
                                <span class="text-[11px] font-bold text-slate-800 dark:text-slate-100 block truncate">${{escapePassageDisplay(doc.type || 'Document scanné')}}</span>
                                <span class="text-[9px] text-brand-neon font-medium">${{escapePassageDisplay(doc.heure || '')}} (🔍 Voir)</span>
                            </div>
                        </button>
                        <button type="button" onclick="removePassageScannedDoc(${{idx}})" class="text-rose-500 hover:text-rose-700 font-bold p-1 ml-1">✕</button>
                    `;
                    container.appendChild(div);
                }});
            }};

            window.removePassageScannedDoc = function(idx) {{
                currentPassageScannedDocs.splice(idx, 1);
                renderPassageScannedDocsGallery();
            }};

            window.openPassageDocLightbox = function(imgSrc, title) {{
                const modal = document.getElementById('passageDocLightboxModal');
                const imgEl = document.getElementById('lightboxDocImg');
                const titleEl = document.getElementById('lightboxDocTitle');

                currentLightboxRotation = 0;
                if (imgEl) {{
                    imgEl.src = imgSrc;
                    imgEl.style.transform = 'rotate(0deg)';
                }}
                if (titleEl) titleEl.innerText = title || "Document Scanné";
                if (modal) modal.classList.remove('hidden');
            }};

            window.closePassageDocLightbox = function() {{
                const modal = document.getElementById('passageDocLightboxModal');
                if (modal) modal.classList.add('hidden');
            }};

            window.rotateLightboxDoc = function() {{
                currentLightboxRotation = (currentLightboxRotation + 90) % 360;
                const imgEl = document.getElementById('lightboxDocImg');
                if (imgEl) imgEl.style.transform = `rotate(${{currentLightboxRotation}}deg)`;
            }};

            window.printLightboxDoc = function() {{
                const imgEl = document.getElementById('lightboxDocImg');
                if (!imgEl || !imgEl.src) return;
                const win = window.open('', '_blank');
                win.document.write(`<html><head><title>Impression Document</title></head><body style="margin:0;display:flex;justify-content:center;align-items:center;height:100vh;"><img src="${{imgEl.src}}" style="max-width:100%;max-height:100%;transform:rotate(${{currentLightboxRotation}}deg);" onload="window.print();window.close();"></body></html>`);
                win.document.close();
            }};

            window.viewFirstPassageDocFromTable = function(passageId) {{
                const p = passagesData.find(x => x.id_passage === passageId);
                if (!p || !p.documents_scannes || p.documents_scannes.length === 0) return;
                openPassageDocLightbox(p.documents_scannes[0].data, p.documents_scannes[0].type);
            }};

            // Visionneuse multi-documents : remplace l'ancien aperçu limité au premier scan.
            window.openPassageDocCollection = function(docs, startIndex = 0){{
                passageViewerDocs = (docs || []).filter(doc => doc && doc.data);
                if (!passageViewerDocs.length) return;
                passageViewerDocIndex = Math.max(0, Math.min(Number(startIndex) || 0, passageViewerDocs.length - 1));
                passageViewerZoom = 1; currentLightboxRotation = 0; renderPassageDocViewer();
                document.getElementById('passageDocLightboxModal')?.classList.remove('hidden');
            }};
            window.renderPassageDocViewer = function(){{
                const doc = passageViewerDocs[passageViewerDocIndex]; if (!doc) return;
                const img = document.getElementById('lightboxDocImg'); const pdf = document.getElementById('lightboxDocPdf');
                const isPdf = String(doc.data).toLowerCase().startsWith('data:application/pdf') || /pdf/i.test(doc.type || '');
                document.getElementById('lightboxDocTitle').textContent = doc.type || `Document scanné ${{passageViewerDocIndex + 1}}`;
                document.getElementById('lightboxDocSize').textContent = `Taille : ${{formatScannedDocumentSize(doc.data)}}`;
                document.getElementById('lightboxDocCounter').textContent = `${{passageViewerDocIndex + 1}} / ${{passageViewerDocs.length}}`;
                document.getElementById('lightboxPrevBtn').disabled = passageViewerDocIndex <= 0;
                document.getElementById('lightboxNextBtn').disabled = passageViewerDocIndex >= passageViewerDocs.length - 1;
                document.getElementById('lightboxZoomLabel').textContent = `${{Math.round(passageViewerZoom * 100)}}%`;
                currentLightboxRotation = 0;
                if (isPdf) {{ img.classList.add('hidden'); img.removeAttribute('src'); pdf.classList.remove('hidden'); pdf.src = doc.data + `#zoom=${{Math.round(passageViewerZoom * 100)}}`; }}
                else {{ pdf.classList.add('hidden'); pdf.src = 'about:blank'; img.classList.remove('hidden'); img.src = doc.data; img.style.width = `${{Math.round(passageViewerZoom * 100)}}%`; img.style.maxWidth = passageViewerZoom <= 1 ? '100%' : 'none'; img.style.transform = 'rotate(0deg)'; }}
            }};
            window.navigatePassageDoc = function(delta){{ const next = passageViewerDocIndex + Number(delta); if (next < 0 || next >= passageViewerDocs.length) return; passageViewerDocIndex = next; passageViewerZoom = 1; renderPassageDocViewer(); }};
            window.changePassageDocZoom = function(delta){{ passageViewerZoom = Math.max(0.5, Math.min(3, passageViewerZoom + Number(delta))); renderPassageDocViewer(); }};
            window.resetPassageDocZoom = function(){{ passageViewerZoom = 1; renderPassageDocViewer(); }};
            window.openPassageDocFromEditor = function(index){{ openPassageDocCollection(currentPassageScannedDocs, index); }};
            window.viewPassageDocsFromTable = function(passageId){{ const p = passagesData.find(x => String(x.id_passage) === String(passageId)); if (p) openPassageDocCollection(p.documents_scannes || [], 0); }};
            window.viewFirstPassageDocFromTable = window.viewPassageDocsFromTable;
            window.openPassageDocLightbox = function(data, title){{ openPassageDocCollection([{{data, type:title}}], 0); }};
            window.closePassageDocLightbox = function(){{ document.getElementById('passageDocLightboxModal')?.classList.add('hidden'); document.getElementById('lightboxDocImg').removeAttribute('src'); document.getElementById('lightboxDocPdf').src = 'about:blank'; passageViewerDocs = []; }};
            function formatScannedDocumentSize(data) {{
                const value = String(data || '');
                const encoded = value.includes(',') ? value.slice(value.indexOf(',') + 1) : value;
                if (!encoded) return 'indisponible';
                const padding = encoded.endsWith('==') ? 2 : (encoded.endsWith('=') ? 1 : 0);
                const bytes = Math.max(0, Math.floor(encoded.length * 3 / 4) - padding);
                return `${{(bytes / (1024 * 1024)).toLocaleString('fr-FR', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }})}} Mo`;
            }}
            window.rotateLightboxDoc = function(){{ currentLightboxRotation = (currentLightboxRotation + 90) % 360; const img = document.getElementById('lightboxDocImg'); if (img && !img.classList.contains('hidden')) img.style.transform = `rotate(${{currentLightboxRotation}}deg)`; }};
            window.printLightboxDoc = function(){{ const pdf = document.getElementById('lightboxDocPdf'); const img = document.getElementById('lightboxDocImg'); if (pdf && !pdf.classList.contains('hidden')) {{ pdf.contentWindow?.focus(); pdf.contentWindow?.print(); return; }} if (!img || !img.src) return; const win = window.open('', '_blank'); if (!win) return; win.document.write(`<html><head><title>Impression Document</title></head><body style="margin:0;display:flex;justify-content:center;align-items:center;height:100vh;"><img src="${{img.src}}" style="max-width:100%;max-height:100%;transform:rotate(${{currentLightboxRotation}}deg);" onload="window.print();window.close();"></body></html>`); win.document.close(); }};

            // =================================================================
            // 9. QUESTIONNAIRE BILINGUE EXACT (LECTURE & ÉCRITURE)
            // =================================================================
            function getRadioVal(name) {{
                const checked = document.querySelector(`input[name="${{name}}"]:checked`);
                return checked ? checked.value : 'NON';
            }}

            function setRadioVal(name, val) {{
                const target = val || 'NON';
                const el = document.querySelector(`input[name="${{name}}"][value="${{target}}"]`);
                if (el) el.checked = true;
            }}

            // =================================================================
            // 10. SOUMISSION DU PASSAGE AVEC CHARGEMENT SUR LE BOUTON
            // =================================================================
            window.openPassageModal = function(isEditing = false) {{
                passageEditMode = Boolean(isEditing);
                if (!passageEditMode) {{ passageEditPatientData = null; passageEditPatientDraft = null; }}
                const form = document.getElementById('passageForm');
                if (form) form.reset();

                document.getElementById('passageId').value = '';
                currentPassageActs = [];
                currentPassageScannedDocs = [];
                selectedPrestationTemp = null;

                clearSelectedPatient();
                clearSelectedMedecinTraitant();
                setRemiseType('DA');

                const now = new Date();
                document.getElementById('passageDate').value = now.toISOString().split('T')[0];
                document.getElementById('passageHeure').value = String(now.getHours()).padStart(2, '0') + ':' + String(now.getMinutes()).padStart(2, '0');

                populatePassageTypeSelect();
                autoDetectTarifPeriod();
                renderPassageActsTable();

                const qKeys = [
                    'q_hospitalise', 'q_dossier_interne', 'q_dossier_externe',
                    'q_urgence_vitale', 'q_grossesse', 'q_allergie', 'q_diabete',
                    'q_hta', 'q_asthme', 'q_insuffisance_renale', 'q_pace_maker',
                    'q_valve_cardiaque', 'q_prothese_metallique'
                ];
                qKeys.forEach(k => setRadioVal(k, 'NON'));

                document.getElementById('passageModalTitle').innerText = passageEditMode ? "Corriger les informations du passage" : "Enregistrer un Passage Patient";
                const saveButton = document.getElementById('btnSubmitPassage');
                if (saveButton) saveButton.textContent = passageEditMode ? 'Enregistrer les corrections' : 'Enregistrer & Émettre Demande';
                const financialSection = document.getElementById('passageFinancialSection');
                financialSection?.querySelectorAll('input,select,button').forEach(control => control.disabled = passageEditMode);
                const personalEditFields = document.getElementById('passagePersonalEditFields');
                personalEditFields?.classList.toggle('hidden', !passageEditMode);
                personalEditFields?.querySelectorAll('input,select').forEach(control => control.disabled = !passageEditMode);
                const statusSelect = document.getElementById('passageStatutSelect');
                if (statusSelect) statusSelect.disabled = passageEditMode;
                const newPatientButton = document.querySelector('#passageForm button[onclick="openPassagePatientForm()"] span');
                if (newPatientButton) newPatientButton.textContent = passageEditMode ? "Modifier la fiche patient" : "+ Nouveau Patient";
                const changePatientButton = document.querySelector('#patientSelectedSummaryCard button[onclick="clearSelectedPatient()"]');
                if (changePatientButton) changePatientButton.classList.toggle('hidden', passageEditMode);
                document.getElementById('passageModal').classList.remove('hidden');
                checkPassageFormValidity();
            }};

            window.closePassageModal = function() {{
                document.getElementById('passageModal').classList.add('hidden');
            }};

            window.handlePassageSubmit = async function(e) {{
                e.preventDefault();

                const submitBtn = document.getElementById('btnSubmitPassage');
                const originalHtml = submitBtn ? submitBtn.innerHTML : '';
                if (submitBtn) {{
                    submitBtn.disabled = true;
                    submitBtn.innerHTML = '<i class="ph-bold ph-spinner animate-spin text-base mr-2"></i> Enregistrement en cours...';
                }}

                try {{
                    const patientId = document.getElementById('passagePatientId').value;
                    const doctorId = document.getElementById('passageMedecinTraitantId').value;

                    const brut = parseFloat(document.getElementById('passageMontantBrut').value || 0);
                    const remiseDA = parseFloat(document.getElementById('passageRemiseMontantDA').value || 0);
                    const versement = parseFloat(document.getElementById('passageVersement').value || 0);
                    const reste = Math.max(0, (brut - remiseDA) - versement);

                    const payload = {{
                        id_passage: document.getElementById('passageId').value ? parseInt(document.getElementById('passageId').value) : null,
                        id_patient: parseInt(patientId),
                        medecin_traitant_id: parseInt(doctorId),
                        date_passage: document.getElementById('passageDate').value,
                        heure_passage: document.getElementById('passageHeure').value,
                        type_passage: document.getElementById('passageTypeSelect').value,
                        statut: document.getElementById('passageStatutSelect').value,
                        utilisateur: 'admin',
                        actes: currentPassageActs,
                        montant_total: brut,
                        remise: remiseDA,
                        remise_type: document.getElementById('passageRemiseType').value,
                        remise_valeur: parseFloat(document.getElementById('passageRemiseValeur').value || 0),
                        accordeur_remise: document.getElementById('passageAccordeurRemise').value.trim(),
                        versement: versement,
                        reste: reste,
                        mode_paiement: document.getElementById('passageModePaiement').value,
                        documents_scannes: currentPassageScannedDocs,
                        questionnaire: {{
                            observation_medicale: document.getElementById('passageObservation')?.value?.trim() || '',
                            hospitalise: getRadioVal('q_hospitalise'),
                            dossier_interne: getRadioVal('q_dossier_interne'),
                            dossier_externe: getRadioVal('q_dossier_externe'),
                            urgence_vitale: getRadioVal('q_urgence_vitale'),
                            grossesse: getRadioVal('q_grossesse'),
                            allergie: getRadioVal('q_allergie'),
                            diabete: getRadioVal('q_diabete'),
                            hta: getRadioVal('q_hta'),
                            asthme: getRadioVal('q_asthme'),
                            insuffisance_renale: getRadioVal('q_insuffisance_renale'),
                            pace_maker: getRadioVal('q_pace_maker'),
                            valve_cardiaque: getRadioVal('q_valve_cardiaque'),
                            prothese_metallique: getRadioVal('q_prothese_metallique')
                        }}
                    }};

                    if (passageEditMode) {{
                        const patientInfo = passageEditPatientDraft || {{}};
                        const correctionPayload = {{
                            id_passage: Number(document.getElementById('passageId').value),
                            id_patient: Number(patientId),
                            nom: document.getElementById('passageEditPatientNom').value.trim(),
                            prenom: document.getElementById('passageEditPatientPrenom').value.trim(),
                            date_naissance: document.getElementById('passageEditPatientDob').value,
                            genre: document.getElementById('passageEditPatientGenre').value,
                            groupe_sanguin: document.getElementById('passageEditPatientBlood').value,
                            telephone_1: document.getElementById('passageEditPatientPhone1').value.trim(),
                            telephone_2: document.getElementById('passageEditPatientPhone2').value.trim(),
                            email: document.getElementById('passageEditPatientEmail').value.trim(),
                            wilaya: document.getElementById('passageEditPatientWilaya').value,
                            commune: document.getElementById('passageEditPatientCommune').value.trim(),
                            type_piece_identite: patientInfo.type_piece_identite || 'Carte Chifa',
                            doc_identite: patientInfo.doc_identite || '', avatar: patientInfo.avatar || '',
                            photo_face: patientInfo.photo_face || '', photo_profil_droit: patientInfo.photo_profil_droit || '',
                            photo_profil_gauche: patientInfo.photo_profil_gauche || '', empreinte_index: patientInfo.empreinte_index || '',
                            pieces_scannees: patientInfo.pieces_scannees || [],
                            date_passage: document.getElementById('passageDate').value,
                            heure_passage: document.getElementById('passageHeure').value,
                            type_passage: document.getElementById('passageTypeSelect').value,
                            medecin_traitant_id: Number(doctorId),
                            actes: currentPassageActs,
                            documents_scannes: currentPassageScannedDocs,
                            questionnaire: {{
                                observation_medicale: document.getElementById('passageObservation')?.value?.trim() || '',
                                hospitalise: getRadioVal('q_hospitalise'), dossier_interne: getRadioVal('q_dossier_interne'),
                                dossier_externe: getRadioVal('q_dossier_externe'), urgence_vitale: getRadioVal('q_urgence_vitale'),
                                grossesse: getRadioVal('q_grossesse'), allergie: getRadioVal('q_allergie'),
                                diabete: getRadioVal('q_diabete'), hta: getRadioVal('q_hta'), asthme: getRadioVal('q_asthme'),
                                insuffisance_renale: getRadioVal('q_insuffisance_renale'), pace_maker: getRadioVal('q_pace_maker'),
                                valve_cardiaque: getRadioVal('q_valve_cardiaque'), prothese_metallique: getRadioVal('q_prothese_metallique')
                            }}
                        }};
                        const response = await fetch('/api/reception/patient/update-from-passage', {{
                            method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify(correctionPayload)
                        }});
                        const result = await response.json();
                        if (!response.ok || result.success === false) throw new Error(result.detail || result.message || 'La correction du passage a échoué.');
                        passageEditMode = false;
                        closePassageModal();
                        await loadReceptionPassagesModule();
                        if (typeof window.showToast === 'function') window.showToast('Passage corrigé. La demande d’examen a été régénérée.', 'success');
                        openDemandeExamenModal(Number(document.getElementById('passageId')?.value || correctionPayload.id_passage));
                        return;
                    }}

                    const res = await fetch('/api/reception/save', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify(payload)
                    }}).then(r => r.json());

                    if (res.success) {{
                        closePassageModal();
                        await loadReceptionPassagesModule();
                        if (res.id_passage) {{
                            openDemandeExamenModal(res.id_passage);
                        }}
                    }} else {{
                        alert(res.message);
                    }}
                }} catch(err) {{
                    alert("Erreur lors de l'enregistrement.");
                }} finally {{
                    if (submitBtn) {{
                        submitBtn.innerHTML = originalHtml;
                        checkPassageFormValidity();
                    }}
                }}
            }};

            window.deletePassage = async function(id) {{
                if (!confirm("Confirmez-vous la suppression définitive de ce passage ? Cette action est irréversible.")) return;
                const codeSuppression = prompt("Saisissez le code de suppression pour confirmer :");
                if (codeSuppression === null) return;
                try {{
                    const response = await fetch('/api/reception/delete', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ id, code_suppression: codeSuppression }})
                    }});
                    const result = await response.json();
                    if (!response.ok || result.success === false) {{
                        alert(result.detail || result.message || "La suppression a échoué.");
                        return;
                    }}
                    await loadReceptionPassagesModule();
                }} catch (error) {{
                    alert("Erreur lors de la suppression du passage.");
                }}
            }};

            window.magicDeletePassage = async function(id){{
                const passageRef = '#PASS-' + String(id).padStart(4, '0');
                if (!confirm('SUPPRESSION MAGIQUE — ' + passageRef + '\\n\\nCette action efface définitivement le passage et tous ses mouvements de caisse. Elle ne peut pas être annulée.')) return;
                const password = prompt('Confirmez avec le mot de passe du compte administrateur SYSTEM :');
                if (password === null || !password) return;
                const confirmation = prompt('Tapez exactement SUPPRIMER ' + passageRef + ' pour confirmer :');
                if (confirmation === null) return;
                try {{
                    const response = await fetch('/api/reception/magic-delete', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ id, password, confirmation }})
                    }});
                    const result = await response.json();
                    if (!response.ok || result.success === false) {{
                        alert(result.detail || result.message || 'La suppression magique a échoué.');
                        return;
                    }}
                    alert(result.message || 'Passage et mouvements supprimés.');
                    await loadReceptionPassagesModule();
                }} catch (error) {{
                    alert('Erreur lors de la suppression magique.');
                }}
            }};

            // =================================================================
            // 11. VISUALISEUR DEMANDE D'EXAMEN A4
            // =================================================================
            window.openDemandeExamenModal = function(passageId) {{
                const modal = document.getElementById('demandeExamenModal');
                const iframe = document.getElementById('demandeExamenIframe');
                if (iframe) iframe.src = '/api/reception/demande-examen/' + passageId + '?t=' + Date.now();
                if (modal) modal.classList.remove('hidden');
            }};

            window.closeDemandeExamenModal = function() {{
                const modal = document.getElementById('demandeExamenModal');
                const iframe = document.getElementById('demandeExamenIframe');
                if (iframe) iframe.src = 'about:blank';
                if (modal) modal.classList.add('hidden');
            }};

            window.printDemandeExamenIframe = function() {{
                const iframe = document.getElementById('demandeExamenIframe');
                if (iframe && iframe.contentWindow) {{
                    iframe.contentWindow.focus();
                    iframe.contentWindow.print();
                }}
            }};

            // Les changements de statut et l'utilisateur exécutant doivent apparaître
            // sur les autres postes sans demander un rechargement manuel de la page.
            if (!window.receptionPassageRefreshTimer) {{
                window.receptionPassageRefreshTimer = window.setInterval(() => {{
                    const passageTable = document.getElementById('passages-table-body');
                    if (!document.hidden && passageTable?.isConnected && typeof window.loadReceptionPassagesModule === 'function') {{
                        window.loadReceptionPassagesModule();
                    }}
                }}, 15000);
            }}

            loadReceptionPassagesModule();
        }})();
    </script>
    """
