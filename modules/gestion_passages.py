
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
from pathlib import Path
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from database import DOCS_DIR, get_db_cursor
from app.auth import hash_password, track_action, get_current_user_optional
from modules.gestion_patients import ALGERIA_WILAYAS, GROUPES_SANGUINS, TYPES_PIECES_IDENTITE

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
                                  p.medecin_traitant_id,
                                  COALESCE(pat.nom, 'Patient')          as nom,
                                  COALESCE(pat.prenom, '')              as prenom,
                                  COALESCE(pat.telephone_1, '-')        as telephone_1,
                                  COALESCE(pat.telephone_2, '')          as telephone_2,
                                  COALESCE(pat.email, '')                as email,
                                  pat.avatar,
                                  pat.date_naissance,
                                  pat.genre,
                                  pat.wilaya,
                                  pat.commune,
                                  td.nom                                as medecin_traitant_nom,
                                  td.prenom                             as medecin_traitant_prenom,
                                  td.grade                              as medecin_traitant_grade,
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
                "SELECT id, designation, categorie, tarif, tarif_nuit, specialite FROM consultation_types ORDER BY designation ASC;")
            consultations = [dict(r) for r in cursor.fetchall()]

            cursor.execute(
                "SELECT id, grade, nom, prenom, telephone, email, adresse, wilaya, commune FROM treating_doctors ORDER BY nom ASC;")
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
async def api_get_passages(request: Request):
    try:
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
        total = manager.count()
        init_data = manager.get_initial_data()
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
    telephone_1 = str(payload.get("telephone_1") or "").strip()
    telephone_2 = str(payload.get("telephone_2") or "").strip()
    email = str(payload.get("email") or "").strip()
    wilaya = str(payload.get("wilaya") or "").strip()
    commune = str(payload.get("commune") or "").strip()

    if not all((nom, prenom, date_naissance, genre, telephone_1, wilaya, commune)):
        raise HTTPException(status_code=400, detail="Nom, prénom, date de naissance, genre, téléphone, wilaya et commune sont obligatoires.")
    if len(nom) > 100 or len(prenom) > 100 or len(telephone_1) > 30 or len(telephone_2) > 30 or len(email) > 254 or len(wilaya) > 100 or len(commune) > 100:
        raise HTTPException(status_code=400, detail="Une ou plusieurs informations dépassent la longueur autorisée.")
    if genre not in {"Homme", "Femme", "Autre"}:
        raise HTTPException(status_code=400, detail="Genre invalide.")
    try:
        date.fromisoformat(date_naissance)
    except ValueError:
        raise HTTPException(status_code=400, detail="Date de naissance invalide.")

    with get_db_cursor() as cursor:
        cursor.execute("SELECT 1 FROM patient_passages WHERE id = %s AND id_patient = %s;", (passage_id, patient_id))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Le patient n'est pas associé à ce passage.")
        cursor.execute("""
            UPDATE patients
            SET nom = %s, prenom = %s, date_naissance = %s, genre = %s,
                telephone_1 = %s, telephone_2 = %s, email = %s, wilaya = %s, commune = %s
            WHERE id = %s;
        """, (nom, prenom, date_naissance, genre, telephone_1, telephone_2, email, wilaya, commune, patient_id))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Patient introuvable.")

    track_action(request, "MODIFICATION_PATIENT_DEPUIS_PASSAGE", f"Fiche patient #{patient_id} modifiée depuis le passage #{passage_id}")
    return {"success": True, "message": "Fiche patient mise à jour. La demande d'examen peut être régénérée.", "patient_id": patient_id}


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
def render_html() -> str:
    """Génère l'HTML complet avec Dark Mode, Modales d'ajout rapide au premier plan et validation."""
    doc_types_options = "".join([f'<option value="{dt}">{dt}</option>' for dt in DOCS_PASSAGE_TYPES])
    wilayas_options = "".join([f'<option value="{w}">{w}</option>' for w in sorted(ALGERIA_WILAYAS.keys())])
    groupes_options = "".join([f'<option value="{g}">{g}</option>' for g in GROUPES_SANGUINS])
    pieces_options = "".join([f'<option value="{tp}">{tp}</option>' for tp in TYPES_PIECES_IDENTITE])
    wilayas_json = json.dumps(ALGERIA_WILAYAS)

    return f"""
    <div id="view-reception" class="space-y-6">

        <!-- BANDEAU D'EN-TÊTE -->
        <div class="banner-glow rounded-3xl p-6 lg:p-8 text-white shadow-xl flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
            <div>
                <h1 class="text-2xl font-extrabold tracking-tight mb-1">Accueil & Passages des Patients</h1>
                <p class="text-xs text-slate-300">Enregistrement des admissions, numérisation des ordonnances, questionnaire médical bilingue et accès direct au DME.</p>
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
                <div class="inline-flex items-center gap-1 p-1 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20" aria-label="Style du tableau des passages">
                    <button type="button" id="passageStyleClassicBtn" onclick="setPassageDisplayStyle('classic')" class="px-2.5 py-1.5 rounded-lg text-[10px] font-bold transition">Tableau</button>
                    <button type="button" id="passageStyleCardsBtn" onclick="setPassageDisplayStyle('cards')" class="px-2.5 py-1.5 rounded-lg text-[10px] font-bold transition">Lignes arrondies</button>
                </div>
                <button type="button" onclick="openPassageFiltersModal()" class="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-slate-900 dark:bg-brand-neon text-white dark:text-black text-xs font-bold shadow-sm hover:opacity-90 transition">
                    <i class="ph-bold ph-sliders-horizontal text-sm"></i><span>Rechercher et filtrer</span><span id="passageFilterCount" class="hidden min-w-5 h-5 px-1 rounded-full bg-white/20 dark:bg-black/10 items-center justify-center text-[10px]">0</span>
                </button>
            </div>
        </div>

        <!-- MODALE RECHERCHE ET FILTRES -->
        <div id="passageFiltersModal" class="fixed inset-0 bg-slate-950/60 backdrop-blur-sm hidden items-center justify-center z-[70] p-4" onclick="if(event.target===this) closePassageFiltersModal()">
            <div class="w-full max-w-3xl max-h-[90vh] overflow-hidden rounded-3xl bg-white dark:bg-[#121c15] border border-slate-200 dark:border-white/10 shadow-2xl flex flex-col">
                <div class="px-6 py-5 border-b border-slate-100 dark:border-white/5 flex items-center justify-between">
                    <div class="flex items-center gap-3"><div class="w-10 h-10 rounded-2xl bg-brand-neon/20 text-emerald-700 dark:text-brand-neon flex items-center justify-center"><i class="ph-bold ph-magnifying-glass text-lg"></i></div><div><h2 class="font-bold text-slate-800 dark:text-white">Recherche et filtres</h2><p class="text-[11px] text-slate-500 dark:text-slate-400">Combinez plusieurs critères pour retrouver un passage.</p></div></div>
                    <button type="button" onclick="closePassageFiltersModal()" class="w-9 h-9 rounded-xl text-slate-400 hover:bg-slate-100 dark:hover:bg-white/5"><i class="ph-bold ph-x"></i></button>
                </div>
                <div class="p-6 overflow-y-auto">
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                        <label class="sm:col-span-2"><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Recherche libre</span><div class="flex items-center gap-2 px-3 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20"><i class="ph ph-magnifying-glass text-slate-400"></i><input id="filterPassageSearch" type="search" placeholder="Nom, prénom, téléphone, numéro, médecin ou acte..." class="w-full py-2.5 text-xs outline-none bg-transparent text-slate-800 dark:text-white"></div></label>
                        <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Numéro de passage</span><input id="filterPassageNumero" type="text" placeholder="Ex. 125" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                        <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Téléphone patient</span><input id="filterPassageTelephone" type="search" placeholder="Numéro de téléphone" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                        <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Date de naissance</span><input id="filterPassageDateNaissance" type="date" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                        <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Médecin traitant</span><input id="filterPassageMedecin" type="search" placeholder="Nom du médecin" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                        <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Acte ou prestation</span><input id="filterPassageActe" type="search" placeholder="Nom de l'acte" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                        <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Type de passage</span><select id="filterPassageType" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"><option value="">Tous les types</option></select></label>
                        <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Statut</span><select id="filterPassageStatut" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"><option value="">Tous les statuts</option><option>En attente</option><option>En cours</option><option>Terminé</option></select></label>
                        <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Date à partir du</span><input id="filterPassageDateDebut" type="date" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                        <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Date jusqu'au</span><input id="filterPassageDateFin" type="date" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                    </div>
                </div>
                <div class="px-6 py-4 border-t border-slate-100 dark:border-white/5 flex items-center justify-between gap-3 bg-slate-50/70 dark:bg-black/10">
                    <button type="button" onclick="clearPassageFilters()" class="px-3.5 py-2 rounded-xl text-xs font-semibold text-slate-500 hover:bg-slate-200/70 dark:hover:bg-white/5">Effacer les critères</button>
                    <div class="flex gap-2"><button type="button" onclick="closePassageFiltersModal()" class="px-4 py-2 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-semibold text-slate-600 dark:text-slate-300">Annuler</button><button type="button" onclick="applyPassageFilters()" class="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-brand-neon text-black text-xs font-bold hover:opacity-90"><i class="ph-bold ph-check"></i>Afficher les résultats</button></div>
                </div>
            </div>
        </div>

        <!-- MODALE MODIFICATION DES INFORMATIONS PATIENT -->
        <div id="passagePatientEditModal" class="fixed inset-0 bg-slate-950/65 backdrop-blur-sm hidden items-center justify-center z-[85] p-4" onclick="if(event.target===this) closePassagePatientEditModal()">
            <form id="passagePatientEditForm" onsubmit="savePatientInfoFromPassage(event)" class="w-full max-w-2xl max-h-[92vh] overflow-y-auto rounded-3xl bg-white dark:bg-[#121c15] border border-slate-200 dark:border-white/10 shadow-2xl">
                <div class="sticky top-0 z-10 px-6 py-4 bg-white dark:bg-[#121c15] border-b border-slate-100 dark:border-white/5 flex items-center justify-between rounded-t-3xl">
                    <div class="flex items-center gap-3"><span class="w-10 h-10 rounded-2xl bg-brand-neon/20 text-emerald-700 dark:text-brand-neon flex items-center justify-center"><i class="ph-bold ph-identification-card text-lg"></i></span><div><h2 class="text-sm font-bold text-slate-800 dark:text-white">Modifier la fiche patient</h2><p id="passagePatientEditSubtitle" class="text-[11px] text-slate-500 dark:text-slate-400">Les changements seront repris sur la demande d'examen.</p></div></div>
                    <button type="button" onclick="closePassagePatientEditModal()" class="w-9 h-9 rounded-xl text-slate-400 hover:bg-slate-100 dark:hover:bg-white/5"><i class="ph-bold ph-x"></i></button>
                </div>
                <input type="hidden" id="passagePatientEditPassageId"><input type="hidden" id="passagePatientEditPatientId">
                <div class="p-6 grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Nom *</span><input id="passagePatientEditNom" required maxlength="100" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                    <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Prénom *</span><input id="passagePatientEditPrenom" required maxlength="100" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                    <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Date de naissance *</span><input id="passagePatientEditDob" type="date" required class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                    <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Genre *</span><select id="passagePatientEditGenre" required class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"><option>Homme</option><option>Femme</option><option>Autre</option></select></label>
                    <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Téléphone principal *</span><input id="passagePatientEditPhone1" type="tel" required maxlength="30" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                    <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Téléphone secondaire</span><input id="passagePatientEditPhone2" type="tel" maxlength="30" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                    <label class="sm:col-span-2"><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">E-mail</span><input id="passagePatientEditEmail" type="email" maxlength="254" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                    <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Wilaya *</span><select id="passagePatientEditWilaya" required class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"><option value="">Choisir une wilaya</option>{wilayas_options}</select></label>
                    <label><span class="block mb-1.5 text-[11px] font-semibold text-slate-600 dark:text-slate-300">Commune *</span><input id="passagePatientEditCommune" required maxlength="100" class="w-full px-3 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-black/20 text-xs outline-none text-slate-800 dark:text-white"></label>
                </div>
                <div class="px-6 py-4 border-t border-slate-100 dark:border-white/5 bg-slate-50/70 dark:bg-black/10 flex justify-end gap-2 rounded-b-3xl">
                    <button type="button" onclick="closePassagePatientEditModal()" class="px-4 py-2.5 rounded-xl border border-slate-200 dark:border-white/10 text-xs font-semibold text-slate-600 dark:text-slate-300">Annuler</button>
                    <button id="passagePatientEditSubmit" type="submit" class="inline-flex items-center gap-2 px-5 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold hover:opacity-90"><i class="ph-bold ph-floppy-disk"></i>Enregistrer et régénérer la demande</button>
                </div>
            </form>
        </div>

        <!-- TABLEAU DES PASSAGES -->
        <div class="bg-white dark:bg-[#121c15] rounded-3xl p-5 shadow-sm border border-slate-100 dark:border-white/5 flex flex-col transition-colors">
            <div class="overflow-x-auto">
                <table id="passagesDataTable" data-style="classic" class="w-full min-w-[1080px] text-left text-xs table-fixed border-separate border-spacing-0">
                    <thead class="bg-slate-50 dark:bg-white/[0.035]">
                        <tr class="text-[10px] uppercase tracking-wider text-slate-500 dark:text-slate-400">
                            <th class="w-[14%] py-3 pl-3 pr-2 rounded-l-xl font-bold">Passage / horaire</th>
                            <th class="w-[23%] py-3 px-2 font-bold">Patient</th>
                            <th class="w-[17%] py-3 px-2 font-bold">Médecin / type</th>
                            <th class="w-[22%] py-3 px-2 font-bold">Prestations / actes</th>
                            <th class="w-[15%] py-3 px-2 font-bold">Règlement</th>
                            <th class="w-[9%] py-3 pl-2 pr-3 rounded-r-xl font-bold text-right">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="passages-table-body" class="text-slate-700 dark:text-slate-300 font-medium"></tbody>
                </table>
                <style>
                    #passagesDataTable[data-style="cards"] {{ border-spacing: 0 9px; }}
                    #passagesDataTable[data-style="cards"] thead {{ background: #334155; }}
                    #passagesDataTable[data-style="cards"] thead tr {{ color: #fff; }}
                    #passagesDataTable[data-style="cards"] tbody tr:not(.passage-expand-details) {{ background: #fff; box-shadow: 0 3px 12px rgba(15,23,42,.08); }}
                    #passagesDataTable[data-style="cards"] tbody tr:not(.passage-expand-details) td:first-child {{ border-radius: 14px 0 0 14px; }}
                    #passagesDataTable[data-style="cards"] tbody tr:not(.passage-expand-details) td:last-child {{ border-radius: 0 14px 14px 0; }}
                    #passagesDataTable[data-style="cards"] tbody tr:not(.passage-expand-details) td {{ border-top: 1px solid #e2e8f0; border-bottom: 1px solid #e2e8f0; color: #1e293b; }}
                    #passagesDataTable[data-style="cards"] tbody tr.passage-expand-details td {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; color: #334155; }}
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
                            <button type="button" onclick="openFullPatientModal()" class="bg-brand-neon text-black px-3 py-1.5 rounded-xl text-xs font-bold shadow-sm inline-flex items-center gap-1.5 hover:opacity-90 transition cursor-pointer">
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
                            <input type="text" id="passagePatientSearchInput" oninput="searchPassagePatientLive()" placeholder="Nom, prénom, téléphone, dossier ou date de naissance..." class="w-full text-xs outline-none bg-transparent text-slate-800 dark:text-white font-bold">
                        </div>
                        <div id="passagePatientSearchResults" class="hidden absolute top-full left-0 right-0 mt-1 bg-white dark:bg-[#121c15] rounded-2xl shadow-xl border border-slate-200 dark:border-white/10 max-h-48 overflow-y-auto z-30 divide-y divide-slate-100 dark:divide-white/5"></div>
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
                                <input type="text" id="passageMedecinTraitantSearchInput" oninput="searchPassageMedecinTraitantLive()" placeholder="Rechercher par nom du médecin traitant..." class="w-full text-xs outline-none bg-transparent text-slate-800 dark:text-white font-bold">
                            </div>
                            <div id="passageMedecinTraitantSearchResults" class="hidden absolute top-full left-0 right-0 mt-1 bg-white dark:bg-[#121c15] rounded-2xl shadow-xl border border-slate-200 dark:border-white/10 max-h-48 overflow-y-auto z-30 divide-y divide-slate-100 dark:divide-white/5"></div>
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
                <div class="bg-brand-neon/10 dark:bg-brand-neon/5 p-4 rounded-3xl border border-brand-neon/20 space-y-4">
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
                <div class="bg-white dark:bg-[#121c15] p-5 rounded-3xl border border-slate-200 dark:border-white/10 space-y-4">
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
                    <i class="ph-bold ph-user-plus text-brand-neon"></i> Fiche Patient Complète (Sélection Automatique dans le Passage)
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
                            <img id="recPreviewFace" src="https://images.unsplash.com/photo-1535713875002-d1d0cf377fde?w=120" class="w-10 h-10 rounded-xl object-cover border border-brand-neon">
                            <span class="text-[11px] font-bold text-slate-800 dark:text-white">1. Face</span>
                        </div>
                        <div class="bg-white dark:bg-black/30 p-2 rounded-2xl border border-slate-200 dark:border-white/10 flex items-center space-x-2.5">
                            <img id="recPreviewPDroit" src="https://images.unsplash.com/photo-1535713875002-d1d0cf377fde?w=120" class="w-10 h-10 rounded-xl object-cover border border-slate-200 opacity-40">
                            <span class="text-[11px] font-bold text-slate-400">2. Profil Droit</span>
                        </div>
                        <div class="bg-white dark:bg-black/30 p-2 rounded-2xl border border-slate-200 dark:border-white/10 flex items-center space-x-2.5">
                            <img id="recPreviewPGauche" src="https://images.unsplash.com/photo-1535713875002-d1d0cf377fde?w=120" class="w-10 h-10 rounded-xl object-cover border border-slate-200 opacity-40">
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
                    <button type="submit" class="px-6 py-2.5 rounded-xl bg-brand-neon text-black text-xs font-bold shadow-md">Enregistrer & Sélectionner</button>
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
    <div id="passageDocPreviewModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center hidden z-[110] p-4">
        <div class="bg-white dark:bg-[#121c15] rounded-3xl w-full max-w-4xl max-h-[92vh] flex flex-col shadow-2xl border border-slate-100 dark:border-white/10 overflow-hidden">
            <div class="px-6 py-3.5 border-b border-slate-100 dark:border-white/5 flex justify-between items-center bg-slate-50 dark:bg-black/20">
                <div>
                    <h3 class="text-sm font-bold text-slate-800 dark:text-white">Aperçu du Document Scanné</h3>
                    <p id="passageDocPreviewTitle" class="text-[11px] text-slate-400">Vérifiez la lisibilité avant validation définitive.</p>
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
                    <h3 id="lightboxDocTitle" class="text-sm font-bold text-slate-800 dark:text-white">Document Scanné</h3>
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

    <!-- SCRIPT DU MODULE ACCUEIL & PASSAGES -->
    <script>
        (function() {{
            const ALGERIA_WILAYAS_DATA = {wilayas_json};
            let passagesData = [];
            let canMagicDeletePassages = false;
            let canEditPatientInfo = false;
            let filteredPassagesList = [];
            let receptionInitData = {{ patients: [], consultations: [], medecins_traitants: [], horaires_tarification: {{}}, clinic_name: '' }};
            let currentPassageActs = [];
            let currentPassageScannedDocs = [];
            let currentMatchingPrestations = [];
            let selectedPrestationTemp = null;
            let pendingPassageDocPreview = null;
            let currentLightboxRotation = 0;
            let passageViewerDocs = [];
            let passageViewerDocIndex = 0;
            let passageViewerZoom = 1;
            let passageDisplayStyle = 'classic';
            try {{ passageDisplayStyle = localStorage.getItem('clinivaPassagesDisplayStyle') || 'classic'; }} catch (_) {{}}

            let currentPassagePage = 1;
            const PASSAGES_PER_PAGE = 15;

            // =================================================================
            // 1. CHARGEMENT ET FILTRAGE
            // =================================================================
            window.loadReceptionPassagesModule = async function() {{
                try {{
                    const res = await fetch('/api/reception/passages');
                    const data = await res.json();
                    passagesData = data.passages || [];
                    canMagicDeletePassages = data.can_magic_delete === true;
                    canEditPatientInfo = data.can_edit_patient_info === true;
                    receptionInitData = data.init_data || {{}};

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
                    const idMatch = !numero || String(p.id_passage || '').replace(/^0+/, '').includes(numero);
                    return (!q || searchable.includes(q)) && idMatch &&
                        (!telephone || String(p.telephone_1 || '').replace(/\\s+/g, '').includes(telephone)) &&
                        (!dateNaissance || dobISO === dateNaissance) &&
                        (!medecin || medecinNom.includes(medecin)) &&
                        (!acte || actes.includes(acte)) &&
                        (!type || String(p.type_passage || '').toLocaleLowerCase('fr') === type) &&
                        (!statut || String(p.statut || '').toLocaleLowerCase('fr') === statut) &&
                        (!dateDebut || passageDate >= dateDebut) && (!dateFin || passageDate <= dateFin);
                }});

                const activeCount = [q, numero, telephone, dateNaissance, medecin, acte, type, statut, dateDebut, dateFin].filter(Boolean).length;
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

            window.applyPassageFilters = function() {{
                const dateDebut = document.getElementById('filterPassageDateDebut')?.value;
                const dateFin = document.getElementById('filterPassageDateFin')?.value;
                if (dateDebut && dateFin && dateDebut > dateFin) {{
                    alert("La date de début doit être antérieure ou égale à la date de fin.");
                    return;
                }}
                filterPassages();
                closePassageFiltersModal();
            }};

            window.clearPassageFilters = function() {{
                ['filterPassageSearch', 'filterPassageNumero', 'filterPassageTelephone', 'filterPassageDateNaissance', 'filterPassageMedecin', 'filterPassageActe', 'filterPassageType', 'filterPassageStatut', 'filterPassageDateDebut', 'filterPassageDateFin'].forEach(id => {{
                    const field = document.getElementById(id);
                    if (field) field.value = '';
                }});
                filterPassages();
            }};

            function escapePassageDisplay(value) {{
                return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
            }}

            window.openPassagePatientEditModal = function(passageId) {{
                if (!canEditPatientInfo) return;
                const patient = passagesData.find(p => Number(p.id_passage) === Number(passageId));
                if (!patient) return;
                const setValue = (id, value) => {{ const field = document.getElementById(id); if (field) field.value = value ?? ''; }};
                setValue('passagePatientEditPassageId', patient.id_passage);
                setValue('passagePatientEditPatientId', patient.id_patient);
                setValue('passagePatientEditNom', patient.nom);
                setValue('passagePatientEditPrenom', patient.prenom);
                setValue('passagePatientEditDob', String(patient.date_naissance || '').slice(0, 10));
                setValue('passagePatientEditGenre', patient.genre || 'Homme');
                setValue('passagePatientEditPhone1', patient.telephone_1 === '-' ? '' : patient.telephone_1);
                setValue('passagePatientEditPhone2', patient.telephone_2);
                setValue('passagePatientEditEmail', patient.email === '-' ? '' : patient.email);
                const wilaya = document.getElementById('passagePatientEditWilaya');
                if (wilaya) {{
                    const existing = Array.from(wilaya.options).some(option => option.value === (patient.wilaya || ''));
                    if (!existing && patient.wilaya) wilaya.add(new Option(patient.wilaya, patient.wilaya));
                    wilaya.value = patient.wilaya || '';
                }}
                setValue('passagePatientEditCommune', patient.commune);
                const subtitle = document.getElementById('passagePatientEditSubtitle');
                if (subtitle) subtitle.textContent = `${{patient.nom || ''}} ${{patient.prenom || ''}} · #PASS-${{String(passageId).padStart(4, '0')}}`;
                const modal = document.getElementById('passagePatientEditModal');
                modal?.classList.remove('hidden');
                modal?.classList.add('flex');
            }};

            window.closePassagePatientEditModal = function() {{
                const modal = document.getElementById('passagePatientEditModal');
                modal?.classList.add('hidden');
                modal?.classList.remove('flex');
            }};

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
                            telephone_1: value('passagePatientEditPhone1'), telephone_2: value('passagePatientEditPhone2'),
                            email: value('passagePatientEditEmail'), wilaya: value('passagePatientEditWilaya'), commune: value('passagePatientEditCommune')
                        }})
                    }});
                    const result = await response.json();
                    if (!response.ok || result.success === false) throw new Error(result.detail || result.message || 'La fiche patient n’a pas pu être mise à jour.');
                    closePassagePatientEditModal();
                    if (typeof window.showToast === 'function') window.showToast('Fiche patient mise à jour. Demande d’examen régénérée avec les nouvelles informations.', 'success');
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
                passageDisplayStyle = style === 'cards' ? 'cards' : 'classic';
                try {{ localStorage.setItem('clinivaPassagesDisplayStyle', passageDisplayStyle); }} catch (_) {{}}
                renderPassagesTable();
            }};

            window.togglePassageDetails = function(passageId) {{
                const row = document.getElementById(`passageDetails-${{passageId}}`);
                const button = document.getElementById(`passageExpandBtn-${{passageId}}`);
                if (!row) return;
                const open = row.classList.toggle('hidden') === false;
                if (button) button.textContent = open ? '−' : '+';
            }};

            window.renderPassagesTable = function() {{
                const tbody = document.getElementById('passages-table-body');
                if (!tbody) return;
                tbody.innerHTML = '';
                const dataTable = document.getElementById('passagesDataTable');
                if (dataTable) dataTable.dataset.style = passageDisplayStyle;
                document.getElementById('passageStyleClassicBtn')?.classList.toggle('bg-white', passageDisplayStyle === 'classic');
                document.getElementById('passageStyleClassicBtn')?.classList.toggle('text-slate-900', passageDisplayStyle === 'classic');
                document.getElementById('passageStyleCardsBtn')?.classList.toggle('bg-white', passageDisplayStyle === 'cards');
                document.getElementById('passageStyleCardsBtn')?.classList.toggle('text-slate-900', passageDisplayStyle === 'cards');

                const totalItems = filteredPassagesList.length;
                const totalPages = Math.ceil(totalItems / PASSAGES_PER_PAGE) || 1;

                if (currentPassagePage > totalPages) currentPassagePage = totalPages;
                if (currentPassagePage < 1) currentPassagePage = 1;

                const startIndex = (currentPassagePage - 1) * PASSAGES_PER_PAGE;
                const endIndex = startIndex + PASSAGES_PER_PAGE;
                const pageItems = filteredPassagesList.slice(startIndex, endIndex);

                if (pageItems.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="6" class="text-center py-8 text-slate-400">Aucun passage enregistré.</td></tr>';
                }} else {{
                    const statutBadges = {{
                        'En attente': 'bg-amber-50 dark:bg-amber-950/40 text-amber-700 dark:text-amber-300 border-amber-200 dark:border-amber-800/40',
                        'En cours': 'bg-sky-50 dark:bg-sky-950/40 text-sky-700 dark:text-sky-300 border-sky-200 dark:border-sky-800/40',
                        'Terminé': 'bg-brand-neon/20 text-emerald-800 dark:text-brand-neon border-brand-neon/30'
                    }};

                    pageItems.forEach(p => {{
                        const tr = document.createElement('tr');
                        tr.className = "align-top odd:bg-white even:bg-slate-50/50 dark:odd:bg-transparent dark:even:bg-white/[0.018] border-b border-slate-100 dark:border-white/5 hover:bg-emerald-50/70 dark:hover:bg-brand-neon/[0.045] transition-colors";

                        const actesBadges = (p.actes && p.actes.length > 0) 
                            ? p.actes.map(a => `<span class="block w-fit max-w-full px-2 py-1 rounded-lg text-[10px] font-bold bg-slate-100 dark:bg-black/30 text-slate-700 dark:text-slate-300 border border-slate-200 dark:border-white/10 break-words">${{a.nom}}</span>`).join('')
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

                        tr.innerHTML = `
                            <td class="py-4 pl-3 pr-2">
                                <div class="text-slate-400 font-mono text-[10px]">#PASS-${{String(p.id_passage).padStart(4, '0')}}</div>
                                <div class="mt-2 font-semibold text-slate-700 dark:text-slate-200">${{p.date_passage}}</div>
                                <div class="text-[11px] text-slate-500 dark:text-slate-400">${{p.heure_passage}}</div>
                            </td>
                            <td class="py-4 px-2">
                                <div class="flex items-start gap-2.5 cursor-pointer group min-w-0" onclick="openDmeFromPassage(${{patientIdParam}}, ${{passageIdParam}})" title="Cliquer pour ouvrir le Dossier Médical Électronique (DME)">
                                <button type="button" onclick="event.stopPropagation(); togglePassageDetails(${{passageIdParam}})" id="passageExpandBtn-${{passageIdParam}}" class="${{passageDisplayStyle === 'cards' ? '' : 'hidden'}} w-5 h-5 shrink-0 mt-1 rounded-full text-emerald-600 font-black border border-emerald-200 hover:bg-emerald-50" title="Afficher les détails">+</button>
                                <img src="${{p.avatar || 'https://images.unsplash.com/photo-1535713875002-d1d0cf377fde?w=120&auto=format&fit=crop&q=80'}}" class="w-8 h-8 shrink-0 rounded-full object-cover border border-slate-200 dark:border-white/10 shadow-sm">
                                <div class="min-w-0">
                                    <div class="font-bold text-slate-800 dark:text-slate-100 text-xs group-hover:text-brand-neon transition flex items-center gap-1">
                                        <span class="break-words">${{p.nom}} ${{p.prenom}}</span>
                                    </div>
                                    <div class="mt-1 text-[10px] text-slate-400 font-mono break-all">${{p.telephone_1 || '-'}}</div>
                                    <div class="mt-1.5"><span class="inline-flex px-2 py-0.5 rounded-full text-[9px] font-bold border ${{statutBadges[p.statut] || 'bg-slate-100 text-slate-600 border-slate-200'}}">${{p.statut || 'Statut inconnu'}}</span></div>
                                </div>
                                </div>
                            </td>
                            <td class="py-4 px-2">
                                <div>${{medTraitantBadge}}</div>
                                <div class="mt-2"><span class="inline-block max-w-full px-2 py-1 rounded-xl text-[10px] font-bold bg-slate-100 dark:bg-black/30 border border-slate-200 dark:border-white/10 text-slate-700 dark:text-slate-300 break-words">${{p.type_passage}}</span></div>
                            </td>
                            <td class="py-4 px-2"><div class="space-y-1.5">${{actesBadges}}</div></td>
                            <td class="py-4 px-2">
                                <div class="font-black text-slate-900 dark:text-white font-mono text-xs">${{Number(netAPayer).toLocaleString()}} DA</div>
                                <div class="mt-2 border-l-2 border-slate-200 dark:border-white/10 pl-2">${{versementResteHtml}}</div>
                            </td>
                            <td class="py-4 pl-2 pr-3 text-right">
                                <details class="relative inline-block text-left" onclick="event.stopPropagation()">
                                    <summary class="list-none cursor-pointer select-none inline-flex items-center gap-1 px-2 py-1.5 rounded-lg border border-slate-200 dark:border-white/10 bg-white dark:bg-black/20 hover:bg-slate-50 dark:hover:bg-white/5 text-[10px] font-bold text-slate-700 dark:text-slate-200" title="Actions du patient">
                                        <i class="ph-bold ph-dots-three-outline-vertical"></i><span>Actions</span>
                                    </summary>
                                    <div class="absolute right-0 top-full mt-1 z-30 min-w-48 p-1 rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-[#121c15] shadow-xl">
                                        ${{canEditPatientInfo ? `<button type="button" onclick="this.closest('details').open=false; openPassagePatientEditModal(${{passageIdParam}})" class="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-left text-[11px] text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-white/5"><i class="ph-bold ph-identification-card text-sky-500"></i>Modifier fiche patient</button>` : ''}}
                                        ${{docsCount > 0 ? `<button type="button" onclick="this.closest('details').open=false; viewPassageDocsFromTable(${{passageIdParam}})" class="w-full flex items-center justify-between gap-2 px-2.5 py-2 rounded-lg text-left text-[11px] text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-white/5"><span><i class="ph-bold ph-folder-open mr-1.5 text-brand-neon"></i>Documents scannés</span><span class="text-[10px] font-bold">${{docsCount}}</span></button>` : `<div class="px-2.5 py-2 text-[10px] text-slate-400 italic">Aucun document scanné</div>`}}
                                        <button type="button" onclick="this.closest('details').open=false; openDmeFromPassage(${{patientIdParam}}, ${{passageIdParam}})" class="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-left text-[11px] text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-white/5"><i class="ph-bold ph-stethoscope text-brand-neon"></i>Ouvrir le DME</button>
                                        <button type="button" onclick="this.closest('details').open=false; openDemandeExamenModal(${{passageIdParam}})" class="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-left text-[11px] text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-white/5"><i class="ph-bold ph-file-text text-brand-neon"></i>Demande d'examen</button>
                                        <button type="button" onclick="this.closest('details').open=false; deletePassage(${{passageIdParam}})" class="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-left text-[11px] text-rose-600 dark:text-rose-400 hover:bg-rose-50 dark:hover:bg-rose-500/10"><i class="ph-bold ph-trash"></i>Supprimer le passage</button>
                                        ${{canMagicDeletePassages ? `<div class="my-1 border-t border-rose-200 dark:border-rose-900/50"></div><button type="button" onclick="this.closest('details').open=false; magicDeletePassage(${{passageIdParam}})" title="Supprime le passage et ses mouvements de caisse" class="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-left text-[11px] font-bold text-white bg-rose-700 hover:bg-rose-800"><i class="ph-bold ph-magic-wand"></i>Suppression magique</button>` : ''}}
                                    </div>
                                </details>
                            </td>
                        `;
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
            window.openFullPatientModal = function() {{
                const form = document.getElementById('receptionFullPatientForm');
                if (form) form.reset();

                document.getElementById('recPatAvatar').value = '';
                document.getElementById('recPatPhotoFace').value = '';
                document.getElementById('recPatPhotoPDroit').value = '';
                document.getElementById('recPatPhotoPGauche').value = '';
                document.getElementById('recPatEmpreinte').value = '';
                document.getElementById('recPatAgeDisplay').value = '';

                const def = 'https://images.unsplash.com/photo-1535713875002-d1d0cf377fde?w=120';
                document.getElementById('recPreviewFace').src = def;
                document.getElementById('recPreviewPDroit').src = def;
                document.getElementById('recPreviewPGauche').src = def;

                handleRecPatWilayaChange();
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
                    type_piece_identite: "Carte Chifa",
                    avatar: document.getElementById('recPatAvatar').value,
                    photo_face: document.getElementById('recPatPhotoFace').value,
                    photo_profil_droit: document.getElementById('recPatPhotoPDroit').value,
                    photo_profil_gauche: document.getElementById('recPatPhotoPGauche').value,
                    empreinte_index: document.getElementById('recPatEmpreinte').value,
                    pieces_scannees: []
                }};

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

                if (specs.length === 0) {{
                    select.innerHTML = '<option value="" disabled selected>⚠️ Aucune spécialité cochée dans votre profil</option>';
                    return;
                }}

                select.innerHTML = specs.map(s => {{
                    const isSel = (selectedVal && selectedVal.trim().toLowerCase() === s.trim().toLowerCase()) ? 'selected' : '';
                    return `<option value="${{s}}" ${{isSel}}>🩺 ${{s}}</option>`;
                }}).join('');

                if (selectedVal && specs.includes(selectedVal)) {{
                    select.value = selectedVal;
                }} else if (specs.length > 0) {{
                    select.value = specs[0];
                }}
            }};

            window.onPassageTypeChange = function() {{
                const input = document.getElementById('prestationSearchInput');
                if (input) input.value = '';
                searchPrestationsLive();
            }};

            window.searchPassagePatientLive = function() {{
                const q = (document.getElementById('passagePatientSearchInput')?.value || '').toLowerCase().trim();
                const dropdown = document.getElementById('passagePatientSearchResults');
                if (!dropdown) return;

                if (!q) {{
                    dropdown.classList.add('hidden');
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

                if (matches.length === 0) {{
                    dropdown.innerHTML = '<div class="p-3 text-xs text-slate-400 italic text-center">Aucun patient trouvé.</div>';
                }} else {{
                    dropdown.innerHTML = matches.map(p => `
                        <div onclick="selectPassagePatient(${{p.id}})" class="p-2.5 hover:bg-brand-neon/10 flex items-center justify-between cursor-pointer transition">
                            <div class="flex items-center space-x-2.5">
                                <img src="${{p.avatar || 'https://images.unsplash.com/photo-1535713875002-d1d0cf377fde?w=120&auto=format&fit=crop&q=80'}}" class="w-8 h-8 rounded-full object-cover border border-slate-200">
                                <div>
                                    <span class="text-xs font-bold text-slate-800 dark:text-slate-100">${{p.nom}} ${{p.prenom}}</span>
                                    <span class="text-[10px] text-slate-400 block font-mono">#DOS-${{String(p.id).padStart(4, '0')}} | Tél: ${{p.telephone_1}}</span>
                                    <span class="text-[10px] text-slate-400 block">Né(e) le : ${{p.date_naissance || 'Non renseignée'}}</span>
                                </div>
                            </div>
                            <span class="px-2 py-0.5 rounded text-[10px] font-black bg-rose-50 text-rose-600 border border-rose-200">${{p.groupe_sanguin || 'O+'}}</span>
                        </div>
                    `).join('');
                }}
                dropdown.classList.remove('hidden');
            }};

            window.selectPassagePatient = function(patientId) {{
                const patient = (receptionInitData.patients || []).find(p => p.id === patientId);
                if (!patient) return;

                document.getElementById('passagePatientId').value = patient.id;
                document.getElementById('passagePatientSearchInput').value = `${{patient.nom}} ${{patient.prenom}}`;
                document.getElementById('passagePatientSearchResults').classList.add('hidden');

                const card = document.getElementById('patientSelectedSummaryCard');
                document.getElementById('selectedPatientAvatar').src = patient.avatar || 'https://images.unsplash.com/photo-1535713875002-d1d0cf377fde?w=120&auto=format&fit=crop&q=80';
                document.getElementById('selectedPatientName').innerText = `${{patient.nom}} ${{patient.prenom}}`;
                document.getElementById('selectedPatientDetails').innerText = `#DOS-${{String(patient.id).padStart(4, '0')}} | Tél: ${{patient.telephone_1}} | Groupe: ${{patient.groupe_sanguin || 'O+'}}`;
                card.classList.remove('hidden');

                checkPassageFormValidity();
            }};

            window.clearSelectedPatient = function() {{
                document.getElementById('passagePatientId').value = '';
                document.getElementById('passagePatientSearchInput').value = '';
                document.getElementById('patientSelectedSummaryCard').classList.add('hidden');
                checkPassageFormValidity();
            }};

            window.searchPassageMedecinTraitantLive = function() {{
                const q = (document.getElementById('passageMedecinTraitantSearchInput')?.value || '').toLowerCase().trim();
                const dropdown = document.getElementById('passageMedecinTraitantSearchResults');
                if (!dropdown) return;

                if (!q) {{
                    dropdown.classList.add('hidden');
                    return;
                }}

                const matches = (receptionInitData.medecins_traitants || []).filter(d =>
                    (`${{d.grade || ''}} ${{d.nom || ''}} ${{d.prenom || ''}}`).toLowerCase().includes(q) ||
                    (d.telephone && d.telephone.includes(q))
                );

                if (!matches.length) {{
                    dropdown.innerHTML = '<div class="p-3 text-center text-xs text-slate-400 italic">Aucun médecin traitant trouvé.</div>';
                }} else {{
                    dropdown.innerHTML = matches.map(d => `
                        <div onclick="selectPassageMedecinTraitant(${{d.id}})" class="p-2.5 hover:bg-brand-neon/10 cursor-pointer">
                            <div class="text-xs font-bold text-slate-800 dark:text-slate-100">${{d.grade || 'Dr'}} ${{d.nom || ''}} ${{d.prenom || ''}}</div>
                            <div class="text-[10px] text-slate-400">${{d.wilaya || 'Algérie'}}${{d.telephone ? ' • ' + d.telephone : ''}}</div>
                        </div>
                    `).join('');
                }}
                dropdown.classList.remove('hidden');
            }};

            window.selectPassageMedecinTraitant = function(doctorId) {{
                const doc = (receptionInitData.medecins_traitants || []).find(d => d.id === doctorId);
                if (!doc) return;

                document.getElementById('passageMedecinTraitantId').value = doc.id;
                document.getElementById('passageMedecinTraitantSearchInput').value = `${{doc.grade || 'Dr'}} ${{doc.nom || ''}} ${{doc.prenom || ''}}`;
                document.getElementById('passageMedecinTraitantSearchResults').classList.add('hidden');

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

                currentMatchingPrestations = (receptionInitData.consultations || []).filter(c => {{
                    const matchesQuery = !q || (c.designation && c.designation.toLowerCase().includes(q)) || (c.categorie && c.categorie.toLowerCase().includes(q));
                    const matchesType = !selectedType || (c.specialite && c.specialite.toLowerCase().trim() === selectedType);
                    return matchesQuery && matchesType;
                }}).map(c => ({{
                    nom: c.designation,
                    categorie: c.categorie,
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
                if (modal) modal.classList.remove('hidden');

                try {{
                    const res = await fetch('/api/patients-module/scanner/detect').then(r => r.json());
                    const scanners = res.scanners || [];
                    if (scanners.length > 0) {{
                        select.innerHTML = scanners.map(s => `<option value="${{s.id}}">🖨️ ${{s.name}}</option>`).join('');
                    }} else {{
                        if (noScanner) noScanner.classList.remove('hidden');
                        select.innerHTML = '<option value="">Aucun périphérique disponible</option>';
                    }}
                }} catch (e) {{
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
                    const res = await fetch('/api/patients-module/scanner/acquire', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ device_id: deviceSelect?.value || '' }})
                    }}).then(r => r.json());

                    if (res.success && res.image_data) {{
                        closeScannerDeviceModal();
                        showPassageDocPreview(typeDoc, res.image_data);
                    }} else {{
                        alert(res.message || "Erreur lors de l'acquisition du scan.");
                    }}
                }} catch (err) {{
                    alert("Erreur de communication avec le scanneur.");
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
                if (img) {{ img.classList.toggle('hidden', isPdf); img.src = isPdf ? '' : base64Data; }}
                if (pdf) {{ pdf.classList.toggle('hidden', !isPdf); pdf.src = isPdf ? base64Data : 'about:blank'; }}
                if (modal) modal.classList.remove('hidden');
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
            window.openPassageModal = function() {{
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

                document.getElementById('passageModalTitle').innerText = "Enregistrer un Passage Patient";
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

            loadReceptionPassagesModule();
        }})();
    </script>
    """
