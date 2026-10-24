
import json
import logging
import re
import threading
from pathlib import Path

from pydicom.dataset import Dataset
from pynetdicom import AE, evt, AllStoragePresentationContexts


# ============================================================
# CONFIGURATION
# ============================================================

IP_SERVEUR = "0.0.0.0"
PORT_SERVEUR = 11112
AET_SERVEUR = "PACS_RECEPTION"

DOSSIER_DICOM = Path(r"C:\DICOM_RECU")

# ============================================================

DOSSIER_DICOM.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

verrou = threading.Lock()


def nom_sur(valeur):
    """Nettoie une valeur pour son utilisation comme nom de fichier."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(valeur))[:100]


def recevoir_image(event):
    """Reception des images DICOM par C-STORE."""

    try:
        ds = event.dataset
        ds.file_meta = event.file_meta

        # Identifiants DICOM indispensables
        patient_id = str(getattr(ds, "PatientID", "") or "").strip()
        issuer = str(getattr(ds, "IssuerOfPatientID", "") or "").strip()
        study_uid = str(getattr(ds, "StudyInstanceUID", "") or "").strip()
        series_uid = str(getattr(ds, "SeriesInstanceUID", "") or "").strip()
        sop_uid = str(getattr(ds, "SOPInstanceUID", "") or "").strip()

        # Refuser les images sans identifiants suffisants
        if not all([patient_id, study_uid, series_uid, sop_uid]):
            logging.error(
                "IMAGE REFUSEE : PatientID, StudyUID, SeriesUID "
                "ou SOPInstanceUID manquant."
            )
            return 0xC210

        with verrou:
            dossier_etude = DOSSIER_DICOM / nom_sur(study_uid)
            fichier_identite = dossier_etude / "_patient.json"

            if fichier_identite.exists():
                with open(
                    fichier_identite, "r", encoding="utf-8"
                ) as f:
                    identite = json.load(f)

                # Ne pas mélanger les PatientID dans une même étude
                if (
                    identite.get("PatientID") != patient_id
                    or identite.get("IssuerOfPatientID") != issuer
                ):
                    logging.error(
                        "CONFLIT PATIENT : étude %s. Image refusée.",
                        study_uid
                    )
                    return 0xC210

            else:
                dossier_etude.mkdir(parents=True, exist_ok=True)

                identite = {
                    "PatientID": patient_id,
                    "IssuerOfPatientID": issuer
                }

                temporaire_json = dossier_etude / "_patient.tmp"

                with open(
                    temporaire_json, "w", encoding="utf-8"
                ) as f:
                    json.dump(
                        identite, f, ensure_ascii=False, indent=2
                    )

                temporaire_json.replace(fichier_identite)

            # Organisation par étude puis par série
            dossier_serie = dossier_etude / nom_sur(series_uid)
            dossier_serie.mkdir(parents=True, exist_ok=True)

            fichier = dossier_serie / (nom_sur(sop_uid) + ".dcm")

            # Ne pas écraser une image déjà reçue
            if fichier.exists():
                logging.warning(
                    "IMAGE DEJA PRESENTE : %s", sop_uid
                )
                return 0x0000

            # Écriture temporaire puis renommage
            temporaire = fichier.with_name(fichier.name + ".tmp")

            ds.save_as(
                str(temporaire),
                enforce_file_format=True
            )

            temporaire.replace(fichier)

        logging.info(
            "IMAGE RECUE | PatientID=%s | Study=%s | Series=%s | SOP=%s",
            patient_id,
            study_uid,
            series_uid,
            sop_uid
        )

        return 0x0000

    except Exception:
        logging.exception("ERREUR pendant la réception DICOM")
        return 0xC211


def connexion(event):
    """Affiche les connexions entrantes."""
    adresse = getattr(event.assoc.requestor, "address", "inconnue")
    aet_source = getattr(event.assoc.requestor, "ae_title", "inconnu")

    logging.info(
        "CONNEXION DICOM | Source=%s | AET=%s",
        adresse,
        aet_source
    )


def main():
    ae = AE(ae_title=AET_SERVEUR)

    # Prise en charge des contextes de stockage DICOM
    ae.supported_contexts = AllStoragePresentationContexts

    handlers = [
        (evt.EVT_C_STORE, recevoir_image),
        (evt.EVT_ACCEPTED, connexion),
    ]

    logging.info("=" * 60)
    logging.info("SERVEUR DICOM EN DEMARRAGE")
    logging.info("IP d'écoute : %s", IP_SERVEUR)
    logging.info("Port        : %s", PORT_SERVEUR)
    logging.info("AET         : %s", AET_SERVEUR)
    logging.info("Dossier     : %s", DOSSIER_DICOM)
    logging.info("=" * 60)

    try:
        ae.start_server(
            (IP_SERVEUR, PORT_SERVEUR),
            block=True,
            evt_handlers=handlers
        )

    except OSError:
        logging.exception(
            "Impossible de démarrer le serveur. "
            "Vérifie le port, les permissions et le pare-feu."
        )

    except KeyboardInterrupt:
        logging.info("Arrêt demandé par l'utilisateur.")

    finally:
        logging.info("Serveur DICOM arrêté.")


if __name__ == "__main__":
    main()
