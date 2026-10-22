"""Serveur DICOM Modality Worklist intégré au backend Cliniva.

Les éléments MWL sont construits depuis les patients et passages PostgreSQL.
Un acte est publié quand sa case de transmission à la worklist est cochée.
"""

from __future__ import annotations

import fnmatch
import logging
import threading
import unicodedata
from datetime import date, datetime
from typing import Any, Dict, Iterator, List

from database import get_db_cursor

logger = logging.getLogger("ClinivaMWL")
_server = None
_server_lock = threading.Lock()
_station_ae_title = ""
_runtime_options = {
    "host": "0.0.0.0",
    "port": 11112,
    "ae_title": "CLINIVA_MWL",
    "station_ae_title": "",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _fold(value: Any) -> str:
    value = unicodedata.normalize("NFKD", _clean(value)).casefold()
    return "".join(char for char in value if not unicodedata.combining(char))


def _matches(query: Any, value: Any) -> bool:
    """Applique les jokers DICOM usuels (* et ?) sans tenir compte de la casse."""
    wanted = _clean(query)
    if not wanted:
        return True
    return fnmatch.fnmatchcase(_fold(value), _fold(wanted))


def _date_matches(query: Any, value: date) -> bool:
    wanted = _clean(query)
    if not wanted:
        return True
    text = value.strftime("%Y%m%d")
    if "-" in wanted:
        start, end = (wanted.split("-", 1) + [""])[:2]
        return (not start or text >= start) and (not end or text <= end)
    return text == wanted


def _requested_text(identifier: Any, name: str) -> str:
    try:
        return _clean(getattr(identifier, name, ""))
    except Exception:
        return ""


def _load_worklist_rows() -> List[Dict[str, Any]]:
    """Lit la Worklist gérée, avec les données patient actuelles."""
    from modules.gestion_worklist import sync_worklist_items

    sync_worklist_items()
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT w.id AS worklist_id, w.passage_id, w.accession_number,
                   w.requested_procedure_id, w.procedure_description,
                   w.modality, w.scheduled_date AS date_passage,
                   w.scheduled_time AS heure_passage, w.station_ae_title,
                   w.status AS worklist_status,
                   pat.id AS patient_id, pat.nom, pat.prenom,
                   pat.date_naissance, pat.genre,
                   COALESCE(td.grade, '') AS medecin_grade,
                   COALESCE(td.prenom, '') AS medecin_prenom,
                   COALESCE(td.nom, '') AS medecin_nom
            FROM mwl_worklist_items w
            JOIN patients pat ON pat.id = w.patient_id
            JOIN patient_passages p ON p.id = w.passage_id
            LEFT JOIN treating_doctors td ON td.id = p.medecin_traitant_id
            WHERE w.enabled = TRUE
              AND w.status NOT IN ('COMPLETED', 'DISCONTINUED')
              AND COALESCE(p.statut, '') NOT IN ('Annulé', 'Annule', 'Terminé', 'Termine')
            ORDER BY w.scheduled_date, w.scheduled_time, w.id
            """
        )
        return [dict(row) for row in cursor.fetchall()]


def _build_identifier(row: Dict[str, Any], station_ae_title: str):
    from pydicom.dataset import Dataset
    from pydicom.sequence import Sequence
    from pydicom.uid import generate_uid

    identifier = Dataset()
    identifier.SpecificCharacterSet = "ISO_IR 192"
    identifier.PatientName = f"{_clean(row.get('nom'))}^{_clean(row.get('prenom'))}"
    identifier.PatientID = str(row.get("patient_id") or "")
    birth = row.get("date_naissance")
    if birth:
        identifier.PatientBirthDate = birth.strftime("%Y%m%d")
    gender = _fold(row.get("genre"))
    identifier.PatientSex = "M" if gender in {"homme", "masculin", "male", "m"} else (
        "F" if gender in {"femme", "feminin", "female", "f"} else ""
    )
    passage_id = int(row.get("passage_id") or 0)
    index = int(row.get("act_index") or 1)
    identifier.AccessionNumber = _clean(row.get("accession_number")) or str(passage_id)
    identifier.StudyInstanceUID = generate_uid(entropy_srcs=[f"cliniva-mwl-{row.get('worklist_id')}"])
    identifier.RequestedProcedureID = _clean(row.get("requested_procedure_id")) or f"CL-{passage_id}-{index}"
    identifier.RequestedProcedureDescription = row["procedure_description"]
    doctor_name = (
        f"{_clean(row.get('medecin_nom'))}^{_clean(row.get('medecin_prenom'))}"
        f"^^{_clean(row.get('medecin_grade'))}"
    ).strip("^")
    identifier.ReferringPhysicianName = doctor_name

    step = Dataset()
    step.ScheduledStationAETitle = _clean(row.get("station_ae_title")) or station_ae_title
    step.ScheduledProcedureStepStartDate = row["date_passage"].strftime("%Y%m%d")
    raw_time = _clean(row.get("heure_passage"))
    try:
        parsed_time = datetime.strptime(raw_time[:5], "%H:%M")
        step.ScheduledProcedureStepStartTime = parsed_time.strftime("%H%M%S")
    except ValueError:
        step.ScheduledProcedureStepStartTime = ""
    step.Modality = row["modality"]
    step.ScheduledProcedureStepID = _clean(row.get("requested_procedure_id")) or f"CL-{passage_id}-{index}"
    step.ScheduledProcedureStepDescription = row["procedure_description"]
    step.ScheduledPerformingPhysicianName = doctor_name
    step.ScheduledProcedureStepStatus = row.get("worklist_status") or "SCHEDULED"
    identifier.ScheduledProcedureStepSequence = Sequence([step])
    return identifier


def _find_matches(identifier: Any) -> Iterator[Dict[str, Any]]:
    patient_name = _requested_text(identifier, "PatientName")
    patient_id = _requested_text(identifier, "PatientID")
    accession = _requested_text(identifier, "AccessionNumber")
    requested_description = _requested_text(identifier, "RequestedProcedureDescription")
    requested_procedure_id = _requested_text(identifier, "RequestedProcedureID")
    step_sequence = getattr(identifier, "ScheduledProcedureStepSequence", []) or []
    step_query = step_sequence[0] if step_sequence else None
    modality = _requested_text(step_query, "Modality") if step_query is not None else ""
    station_ae = _requested_text(step_query, "ScheduledStationAETitle") if step_query is not None else ""
    start_date = _requested_text(step_query, "ScheduledProcedureStepStartDate") if step_query is not None else ""

    with get_db_cursor() as cursor:
        cursor.execute("SELECT ae_title, active, modality FROM mwl_station_titles ORDER BY ae_title")
        title_rows = [dict(row) for row in cursor.fetchall()]
        registered_titles = {row["ae_title"]: bool(row["active"]) for row in title_rows}
    active_titles = [row for row in title_rows if row["active"] and
                     (not row["modality"] or not modality or _matches(row["modality"], modality))]

    if station_ae and registered_titles and not any(
        active and _matches(station_ae, title) for title, active in registered_titles.items()
    ):
        return

    for row in _load_worklist_rows():
        name = f"{_clean(row.get('nom'))}^{_clean(row.get('prenom'))}"
        if not _matches(patient_name, name) or not _matches(patient_id, row.get("patient_id")):
            continue
        if not _matches(accession, row.get("accession_number")):
            continue
        if not _matches(requested_procedure_id, row.get("requested_procedure_id")):
            continue
        if not _matches(requested_description, row.get("procedure_description")):
            continue
        if not _matches(modality, row.get("modality")):
            continue
        destination_ae = _clean(row.get("station_ae_title"))
        if destination_ae in registered_titles and not registered_titles[destination_ae]:
            continue
        if registered_titles and destination_ae and destination_ae not in registered_titles:
            continue
        if destination_ae:
            title_config = next((title for title in title_rows if title["ae_title"] == destination_ae), None)
            if title_config and title_config["modality"] and not _matches(title_config["modality"], row["modality"]):
                continue
            if station_ae and not _matches(station_ae, destination_ae):
                continue
        elif station_ae:
            matching_titles = [title for title in active_titles
                               if _matches(station_ae, title["ae_title"])
                               and (not title["modality"] or _matches(title["modality"], row["modality"]))]
            if registered_titles and not matching_titles:
                continue
            row["station_ae_title"] = matching_titles[0]["ae_title"] if matching_titles else _station_ae_title
        elif active_titles:
            matching_titles = [title for title in active_titles
                               if not title["modality"] or _matches(title["modality"], row["modality"])]
            if matching_titles:
                row["station_ae_title"] = matching_titles[0]["ae_title"]
            elif registered_titles:
                continue
        elif registered_titles:
            continue
        if not _date_matches(start_date, row["date_passage"]):
            continue
        yield row


def _on_c_find(event):
    """Répond aux requêtes C-FIND MWL des modalités DICOM."""
    try:
        identifier = event.identifier
        for row in _find_matches(identifier):
            yield 0xFF00, _build_identifier(row, _station_ae_title)
        yield 0x0000, None
    except Exception:
        logger.exception("Erreur pendant le traitement d'une requête DICOM MWL C-FIND.")
        yield 0xC000, None


def start_mwl_server(host: str = "0.0.0.0", port: int = 11112,
                     ae_title: str = "CLINIVA_MWL",
                     station_ae_title: str = "") -> None:
    """Démarre le SCP MWL en arrière-plan et lève les erreurs de configuration."""
    global _server, _station_ae_title, _runtime_options
    from pynetdicom import AE, evt
    from pynetdicom.sop_class import ModalityWorklistInformationFind

    with _server_lock:
        if _server is not None:
            return
        _station_ae_title = station_ae_title[:16].strip()
        _runtime_options = {
            "host": host,
            "port": int(port),
            "ae_title": ae_title[:16],
            "station_ae_title": _station_ae_title,
        }
        ae = AE(ae_title=ae_title[:16])
        ae.add_supported_context(ModalityWorklistInformationFind)
        _server = ae.start_server(
            (host, int(port)),
            block=False,
            evt_handlers=[(evt.EVT_C_FIND, _on_c_find)],
        )
    logger.info("Serveur DICOM MWL démarré sur %s:%s (AE Title %s).", host, port, ae_title[:16])


def configure_mwl_server(host: str = "0.0.0.0", port: int = 11112,
                         ae_title: str = "CLINIVA_MWL",
                         station_ae_title: str = "") -> None:
    """Enregistre les paramètres qui seront utilisés par les commandes Démarrer."""
    global _station_ae_title, _runtime_options
    with _server_lock:
        _station_ae_title = station_ae_title[:16].strip()
        _runtime_options = {
            "host": host,
            "port": int(port),
            "ae_title": ae_title[:16],
            "station_ae_title": _station_ae_title,
        }


def stop_mwl_server() -> None:
    """Arrête le listener et libère le port DICOM."""
    global _server, _station_ae_title
    with _server_lock:
        server = _server
        _server = None
        _station_ae_title = ""
    if server is not None:
        server.shutdown()
        logger.info("Serveur DICOM MWL arrêté.")


def get_mwl_server_status() -> Dict[str, Any]:
    """Retourne l'état et les paramètres d'écoute effectifs du listener."""
    with _server_lock:
        return {"running": _server is not None, **_runtime_options}


def restart_mwl_server() -> None:
    """Démarre le listener avec ses derniers paramètres effectifs."""
    options = get_mwl_server_status()
    start_mwl_server(**options_without_state(options))


def options_without_state(options: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "host": options["host"],
        "port": options["port"],
        "ae_title": options["ae_title"],
        "station_ae_title": options["station_ae_title"],
    }
