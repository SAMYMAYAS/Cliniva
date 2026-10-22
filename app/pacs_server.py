"""Récepteur PACS C-STORE local au serveur ou au poste client Cliniva."""

from __future__ import annotations

import logging
import base64
import io
import math
import os
import re
import shutil
import socket
import threading
import unicodedata
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from database import DB_CONFIG, get_db_cursor

logger = logging.getLogger("ClinivaPacsReceiver")
_UID_RE = re.compile(r"^[0-9.]+$")
_CONTRAST_NEGATIVE_RE = re.compile(
    r"\b(?:sans|non|no|without)\s+(?:injection|injecte(?:e|s|es)?|produit(?:\s+de\s+contraste)?|contraste|pc|c)\b"
)
_CONTRAST_NEGATIVE_CODE_RE = re.compile(r"-\s*c\b")
_CONTRAST_POSITIVE_RE = re.compile(
    r"\b(?:injection|injecte(?:e|s|es)?|produit(?:\s+de\s+contraste)?|avec\s+contraste)\b"
    r"|\+\s*(?:c|pc|gado)\b"
)


def _contrast_series_state(description: Any) -> bool | None:
    """Interprète les marqueurs usuels de contraste fournis dans le nom de série."""
    text = str(description or "").casefold()
    text = "".join(char for char in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(char))
    has_negative = bool(_CONTRAST_NEGATIVE_RE.search(text) or _CONTRAST_NEGATIVE_CODE_RE.search(text))
    # Retire les expressions négatives avant de chercher « injection », sinon
    # « sans injection » serait détecté à tort comme positif.
    positive_text = _CONTRAST_NEGATIVE_RE.sub(" ", text)
    if _CONTRAST_POSITIVE_RE.search(positive_text):
        return True
    if has_negative:
        return False
    # Un signe + isolé ou en suffixe est également utilisé par les modalités.
    if re.search(r"\+(?:\s|$)", text):
        return True
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def _required_uid(dataset, keyword: str) -> str:
    value = _text(getattr(dataset, keyword, None))
    if not value or not _UID_RE.fullmatch(value):
        raise ValueError(f"UID DICOM absent ou invalide : {keyword}.")
    return value


def _save_dicom_dataset(dataset, target: str | Path) -> None:
    """Save a standards-compliant DICOM without pydicom's deprecated flag."""
    try:
        dataset.save_as(target, enforce_file_format=True)
    except TypeError as exc:
        # Compatibility with older pydicom releases that predate this option.
        if "enforce_file_format" not in str(exc):
            raise
        dataset.save_as(target, write_like_original=False)


def _patient_weight(dataset) -> float | None:
    try:
        raw = getattr(dataset, "PatientWeight", None)
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _patient_height_cm(dataset) -> float | None:
    """Convertit la taille DICOM PatientSize (mètres) en centimètres."""
    try:
        raw = getattr(dataset, "PatientSize", None)
        height_m = float(raw) if raw not in (None, "") else None
        if height_m is None or not math.isfinite(height_m) or height_m <= 0:
            return None
        return round(height_m * 100, 1)
    except (TypeError, ValueError):
        return None


def _atomic_save(dataset, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{threading.get_ident()}.{datetime.utcnow():%f}.part")
    try:
        # Pixel Data is never decoded; compressed MRI objects keep their syntax.
        _save_dicom_dataset(dataset, temporary)
        temporary.replace(target)
        return target.stat().st_size
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_copy(target: Path, source_factory) -> int:
    """Copie un DICOM vers son emplacement final sans exposer de fichier partiel."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{threading.get_ident()}.{uuid.uuid4().hex}.part")
    try:
        with source_factory() as source, temporary.open("wb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
        size = temporary.stat().st_size
        temporary.replace(target)
        return size
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


class PacsServer:
    """SCP C-STORE permissif; fichiers locaux et métadonnées PostgreSQL partagées."""

    def __init__(self, saved_config: dict[str, Any] | None = None) -> None:
        self._lock = threading.RLock()
        self._server = None
        self._last_error = ""
        self._schema_ready = False
        self._active_study_uids: dict[str, int] = {}
        self._active_study_uids_lock = threading.Lock()
        self._node = socket.gethostname()
        local_appdata = os.getenv("LOCALAPPDATA")
        default_data = (Path(local_appdata) if local_appdata else Path.home()) / "Cliniva" / "PACS"
        self._config: dict[str, Any] = {
            # Keep separate from the local MWL SCP, which defaults to port 11112.
            "ae_title": "PACS_V1", "host": "0.0.0.0", "port": 11113,
            "data_dir": str(default_data),
        }
        if saved_config:
            self._config.update(saved_config)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._server is not None,
                "config": dict(self._config),
                "last_error": self._last_error,
                "node": self._node,
                "database": {"host": DB_CONFIG.get("host", ""), "name": DB_CONFIG.get("database", "")},
            }

    @staticmethod
    def _ensure_schema() -> None:
        """Crée les tables PACS dans la base PostgreSQL configurée pour Cliniva."""
        schema = """
        CREATE TABLE IF NOT EXISTS pacs_patients (
            id BIGSERIAL PRIMARY KEY,
            patient_id TEXT NOT NULL UNIQUE,
            patient_name TEXT,
            birth_date TEXT,
            sex TEXT,
            weight DOUBLE PRECISION,
            height_cm DOUBLE PRECISION
        );
        ALTER TABLE pacs_patients ADD COLUMN IF NOT EXISTS height_cm DOUBLE PRECISION;
        CREATE TABLE IF NOT EXISTS pacs_studies (
            id BIGSERIAL PRIMARY KEY,
            receiver_node TEXT NOT NULL,
            study_instance_uid TEXT NOT NULL,
            patient_id BIGINT NOT NULL REFERENCES pacs_patients(id),
            study_id TEXT,
            accession_number TEXT,
            study_date TEXT,
            study_time TEXT,
            study_description TEXT,
            modality TEXT,
            status TEXT NOT NULL DEFAULT 'RECEIVING',
            received_images BIGINT NOT NULL DEFAULT 0,
            total_size BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMPTZ,
            UNIQUE(receiver_node, study_instance_uid)
        );
        CREATE TABLE IF NOT EXISTS pacs_series (
            id BIGSERIAL PRIMARY KEY,
            study_id BIGINT NOT NULL REFERENCES pacs_studies(id) ON DELETE CASCADE,
            receiver_node TEXT NOT NULL,
            series_instance_uid TEXT NOT NULL,
            series_number TEXT,
            series_description TEXT,
            modality TEXT,
            body_part TEXT,
            series_date TEXT,
            series_time TEXT,
            received_images BIGINT NOT NULL DEFAULT 0,
            expected_images BIGINT,
            UNIQUE(receiver_node, series_instance_uid)
        );
        ALTER TABLE pacs_series ADD COLUMN IF NOT EXISTS series_date TEXT;
        ALTER TABLE pacs_series ADD COLUMN IF NOT EXISTS series_time TEXT;
        CREATE TABLE IF NOT EXISTS pacs_instances (
            id BIGSERIAL PRIMARY KEY,
            series_id BIGINT NOT NULL REFERENCES pacs_series(id) ON DELETE CASCADE,
            sop_instance_uid TEXT NOT NULL,
            sop_class_uid TEXT,
            instance_number TEXT,
            acquisition_date TEXT,
            acquisition_time TEXT,
            file_path TEXT NOT NULL,
            file_size BIGINT NOT NULL DEFAULT 0,
            received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(series_id, sop_instance_uid)
        );
        ALTER TABLE pacs_instances ADD COLUMN IF NOT EXISTS acquisition_date TEXT;
        ALTER TABLE pacs_instances ADD COLUMN IF NOT EXISTS acquisition_time TEXT;
        CREATE TABLE IF NOT EXISTS pacs_quarantine (
            id BIGSERIAL PRIMARY KEY,
            receiver_node TEXT NOT NULL,
            reason TEXT NOT NULL,
            sop_instance_uid TEXT,
            study_instance_uid TEXT,
            series_instance_uid TEXT,
            file_path TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS pacs_nodes (
            id BIGSERIAL PRIMARY KEY,
            receiver_node TEXT NOT NULL,
            name TEXT NOT NULL,
            ae_title TEXT NOT NULL,
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(receiver_node, ae_title, host, port)
        );
        CREATE TABLE IF NOT EXISTS pacs_transfer_jobs (
            id BIGSERIAL PRIMARY KEY,
            receiver_node TEXT NOT NULL,
            node_id BIGINT,
            destination_name TEXT NOT NULL,
            destination_ae TEXT NOT NULL,
            patient_identifier TEXT,
            study_id BIGINT,
            series_id BIGINT,
            transfer_kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'EN_COURS',
            total_instances INTEGER NOT NULL DEFAULT 0,
            sent_instances INTEGER NOT NULL DEFAULT 0,
            failed_instances INTEGER NOT NULL DEFAULT 0,
            details TEXT,
            started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS ix_pacs_studies_node_updated
            ON pacs_studies(receiver_node, updated_at DESC);
        CREATE INDEX IF NOT EXISTS ix_pacs_studies_patient_status
            ON pacs_studies(patient_id, status);
        CREATE INDEX IF NOT EXISTS ix_pacs_series_study
            ON pacs_series(study_id);
        CREATE INDEX IF NOT EXISTS ix_pacs_instances_series
            ON pacs_instances(series_id);
        CREATE INDEX IF NOT EXISTS ix_pacs_quarantine_node_created
            ON pacs_quarantine(receiver_node, created_at DESC);
        """
        with get_db_cursor() as cursor:
            cursor.execute(schema)

    def _ensure_ready(self) -> None:
        """Initialise les tables une seule fois, même si le récepteur est arrêté."""
        with self._lock:
            if not self._schema_ready:
                self._ensure_schema()
                self._schema_ready = True

    def start(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if self._server is not None:
                return self.status()
            config = {**self._config, **(settings or {})}
            ae_title = str(config.get("ae_title", "PACS_V1")).strip().upper()
            host = str(config.get("host", "0.0.0.0")).strip() or "0.0.0.0"
            try:
                port = int(config.get("port", 11112))
            except (TypeError, ValueError) as exc:
                raise ValueError("Le port PACS doit être un nombre entre 1 et 65535.") from exc
            if not re.fullmatch(r"[A-Z0-9_ -]{1,16}", ae_title):
                raise ValueError("L’AE Title PACS doit contenir 1 à 16 caractères valides.")
            if not 1 <= port <= 65535:
                raise ValueError("Le port PACS doit être compris entre 1 et 65535.")
            data_dir = Path(str(config.get("data_dir") or "")).expanduser()
            if not data_dir.is_absolute():
                raise ValueError("Le dossier des fichiers DICOM doit être un chemin absolu local.")
            storage_dir = data_dir / "storage" / "studies"
            quarantine_dir = data_dir / "quarantine"
            storage_dir.mkdir(parents=True, exist_ok=True)
            quarantine_dir.mkdir(parents=True, exist_ok=True)

            try:
                from pynetdicom import AE, evt, AllStoragePresentationContexts, _config
                from pynetdicom.sop_class import Verification
            except ImportError as exc:
                raise RuntimeError("pynetdicom et pydicom doivent être installés sur ce poste client.") from exc

            self._ensure_ready()
            _config.UNRESTRICTED_STORAGE_SERVICE = True
            ae = AE(ae_title=ae_title.encode("ascii"))
            # In unrestricted mode pynetdicom treats unknown public and private
            # abstract syntaxes as Storage; registering a fixed list here would
            # unnecessarily constrain incoming modality objects.
            standard_storage_class_count = len({
                str(context.abstract_syntax) for context in AllStoragePresentationContexts
            })
            ae.add_supported_context(Verification, scu_role=False, scp_role=True)
            ae.maximum_pdu_size = 0
            association_studies: dict[int, set[str]] = {}
            association_studies_lock = threading.Lock()

            def track_study(association, study_uid: str) -> None:
                with association_studies_lock:
                    study_uids = association_studies.setdefault(id(association), set())
                    if study_uid in study_uids:
                        return
                    study_uids.add(study_uid)
                with self._active_study_uids_lock:
                    self._active_study_uids[study_uid] = self._active_study_uids.get(study_uid, 0) + 1

            def untrack_studies(study_uids: set[str]) -> None:
                with self._active_study_uids_lock:
                    for study_uid in study_uids:
                        remaining = self._active_study_uids.get(study_uid, 0) - 1
                        if remaining > 0:
                            self._active_study_uids[study_uid] = remaining
                        else:
                            self._active_study_uids.pop(study_uid, None)

            def quarantine(dataset, reason: str) -> None:
                study_uid = _text(getattr(dataset, "StudyInstanceUID", None))
                series_uid = _text(getattr(dataset, "SeriesInstanceUID", None))
                sop_uid = _text(getattr(dataset, "SOPInstanceUID", None))
                filename = f"{datetime.utcnow():%Y%m%d%H%M%S%f}_{_safe_component(sop_uid or 'unknown')}.dcm"
                path = quarantine_dir / filename
                try:
                    _atomic_save(dataset, path)
                except Exception:
                    logger.exception("Échec de conservation du fichier PACS en quarantaine")
                    path = None
                with get_db_cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO pacs_quarantine
                           (receiver_node, reason, sop_instance_uid, study_instance_uid,
                            series_instance_uid, file_path)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (self._node, reason, sop_uid, study_uid, series_uid, str(path) if path else None),
                    )
                logger.error("PACS QUARANTINE: %s | study=%s series=%s sop=%s", reason, study_uid, series_uid, sop_uid)

            def association_accepted(event) -> None:
                requestor = getattr(getattr(event.assoc, "requestor", None), "ae_title", "?")
                logger.info("Association PACS acceptée depuis %s", requestor)
                rejected_contexts = getattr(event.assoc, "rejected_contexts", []) or []
                if rejected_contexts:
                    details = [
                        f"{context.abstract_syntax} (résultat {context.result})"
                        for context in rejected_contexts[:30]
                    ]
                    logger.warning("Contextes DICOM refusés pour %s : %s",
                                   requestor, "; ".join(details))

            def association_released(event) -> None:
                # A modality releases the association after its C-STORE batch is sent.
                # Mark only studies actually received through this association as done.
                with association_studies_lock:
                    study_uids = association_studies.pop(id(event.assoc), set())
                if not study_uids:
                    return
                untrack_studies(study_uids)
                try:
                    with get_db_cursor() as cursor:
                        cursor.execute("""UPDATE pacs_studies SET status='COMPLETED',
                            updated_at=CURRENT_TIMESTAMP
                            WHERE receiver_node=%s AND study_instance_uid=ANY(%s)
                              AND status='RECEIVING'""", (self._node, list(study_uids)))
                    logger.info("Réception PACS terminée pour %s examen(s)", len(study_uids))
                except Exception:
                    logger.exception("Impossible de finaliser l’état des examens PACS reçus")

            def association_aborted(event) -> None:
                with association_studies_lock:
                    study_uids = association_studies.pop(id(event.assoc), set())
                untrack_studies(study_uids)
                if study_uids:
                    logger.warning("Association DICOM interrompue; examen(s) encore incomplet(s) : %s",
                                   len(study_uids))

            def store(event) -> int:
                dataset = event.dataset
                dataset.file_meta = event.file_meta
                target = None
                try:
                    patient_id = _text(getattr(dataset, "PatientID", None))
                    if not patient_id:
                        raise ValueError("PatientID DICOM absent.")
                    study_uid = _required_uid(dataset, "StudyInstanceUID")
                    series_uid = _required_uid(dataset, "SeriesInstanceUID")
                    sop_uid = _required_uid(dataset, "SOPInstanceUID")
                    sop_class_uid = _text(getattr(dataset, "SOPClassUID", None)) or str(event.context.abstract_syntax)
                    target = storage_dir / _safe_component(study_uid) / _safe_component(series_uid) / f"{_safe_component(sop_uid)}.dcm"
                    conflict_reason = None
                    duplicate = False
                    with get_db_cursor() as cursor:
                        cursor.execute(
                            "SELECT i.id FROM pacs_instances i JOIN pacs_series s ON s.id=i.series_id "
                            "JOIN pacs_studies st ON st.id=s.study_id "
                            "WHERE st.receiver_node=%s AND i.sop_instance_uid=%s LIMIT 1",
                            (self._node, sop_uid),
                        )
                        if cursor.fetchone():
                            duplicate = True
                        else:
                            cursor.execute("""INSERT INTO pacs_patients
                                (patient_id, patient_name, birth_date, sex, weight, height_cm)
                                VALUES (%s,%s,%s,%s,%s,%s)
                                ON CONFLICT (patient_id) DO UPDATE SET
                                  patient_name=COALESCE(EXCLUDED.patient_name, pacs_patients.patient_name),
                                  birth_date=COALESCE(EXCLUDED.birth_date, pacs_patients.birth_date),
                                  sex=COALESCE(EXCLUDED.sex, pacs_patients.sex),
                                  weight=COALESCE(EXCLUDED.weight, pacs_patients.weight),
                                  height_cm=COALESCE(EXCLUDED.height_cm, pacs_patients.height_cm)
                                RETURNING id""",
                                (patient_id, _text(getattr(dataset, "PatientName", None)),
                                 _text(getattr(dataset, "PatientBirthDate", None)),
                                 _text(getattr(dataset, "PatientSex", None)), _patient_weight(dataset),
                                 _patient_height_cm(dataset)))
                            patient_db_id = cursor.fetchone()["id"]
                            cursor.execute("""SELECT id, patient_id FROM pacs_studies
                                WHERE receiver_node=%s AND study_instance_uid=%s""", (self._node, study_uid))
                            study = cursor.fetchone()
                            if study and study["patient_id"] != patient_db_id:
                                conflict_reason = f"Study/Patient conflict: {study_uid}"
                            else:
                                if study:
                                    study_db_id = study["id"]
                                else:
                                    cursor.execute("""INSERT INTO pacs_studies
                                        (receiver_node, study_instance_uid, patient_id, study_id,
                                         accession_number, study_date, study_time, study_description, modality)
                                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                                        (self._node, study_uid, patient_db_id,
                                         _text(getattr(dataset, "StudyID", None)),
                                         _text(getattr(dataset, "AccessionNumber", None)),
                                         _text(getattr(dataset, "StudyDate", None)),
                                         _text(getattr(dataset, "StudyTime", None)),
                                         _text(getattr(dataset, "StudyDescription", None)),
                                         _text(getattr(dataset, "Modality", None))))
                                    study_db_id = cursor.fetchone()["id"]
                                cursor.execute("SELECT id, study_id FROM pacs_series WHERE receiver_node=%s AND series_instance_uid=%s",
                                               (self._node, series_uid))
                                series = cursor.fetchone()
                                if series:
                                    if series["study_id"] != study_db_id:
                                        conflict_reason = f"Series/Study conflict: {series_uid}"
                                        series_db_id = None
                                    else:
                                        series_db_id = series["id"]
                                else:
                                    cursor.execute("""INSERT INTO pacs_series
                                        (study_id, receiver_node, series_instance_uid, series_number, series_description, modality, body_part, series_date, series_time)
                                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                                        (study_db_id, self._node, series_uid,
                                         _text(getattr(dataset, "SeriesNumber", None)),
                                         _text(getattr(dataset, "SeriesDescription", None)),
                                         _text(getattr(dataset, "Modality", None)),
                                         _text(getattr(dataset, "BodyPartExamined", None)),
                                         _text(getattr(dataset, "SeriesDate", None)),
                                         _text(getattr(dataset, "SeriesTime", None))))
                                    series_db_id = cursor.fetchone()["id"]

                                if series_db_id is not None:
                                    cursor.execute("SELECT id FROM pacs_instances WHERE series_id=%s AND sop_instance_uid=%s",
                                                   (series_db_id, sop_uid))
                                    if cursor.fetchone():
                                        duplicate = True
                                    else:
                                        size = _atomic_save(dataset, target)
                                        cursor.execute("""INSERT INTO pacs_instances
                                            (series_id, sop_instance_uid, sop_class_uid, instance_number, acquisition_date, acquisition_time, file_path, file_size)
                                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                                            (series_db_id, sop_uid, sop_class_uid,
                                             _text(getattr(dataset, "InstanceNumber", None)),
                                             _text(getattr(dataset, "AcquisitionDate", None) or getattr(dataset, "ContentDate", None)),
                                             _text(getattr(dataset, "AcquisitionTime", None) or getattr(dataset, "ContentTime", None)),
                                             str(target), size))
                                        cursor.execute("""UPDATE pacs_series SET received_images=received_images+1 WHERE id=%s""",
                                                       (series_db_id,))
                                        cursor.execute("""UPDATE pacs_studies SET received_images=received_images+1,
                                            total_size=total_size+%s, status='RECEIVING', updated_at=CURRENT_TIMESTAMP
                                            WHERE id=%s""", (size, study_db_id))
                    if conflict_reason:
                        quarantine(dataset, conflict_reason)
                        return 0xA900
                    if duplicate:
                        track_study(event.assoc, study_uid)
                        logger.info("C-STORE doublon accepté : %s", sop_uid)
                        return 0x0000
                    track_study(event.assoc, study_uid)
                    logger.info("C-STORE stocké : patient=%s study=%s series=%s sop=%s", patient_id, study_uid, series_uid, sop_uid)
                    return 0x0000
                except Exception as exc:
                    logger.exception("Échec C-STORE PACS : %s", exc)
                    if target and target.exists():
                        indexed = False
                        database_checked = False
                        try:
                            with get_db_cursor() as cursor:
                                cursor.execute("""SELECT i.id FROM pacs_instances i
                                    JOIN pacs_series s ON s.id=i.series_id
                                    JOIN pacs_studies st ON st.id=s.study_id
                                    WHERE st.receiver_node=%s AND i.sop_instance_uid=%s LIMIT 1""",
                                    (self._node, _text(getattr(dataset, "SOPInstanceUID", None))))
                                indexed = bool(cursor.fetchone())
                                database_checked = True
                        except Exception:
                            logger.exception("Impossible de vérifier l’index lors du contrôle d’un doublon concurrent")
                        if indexed:
                            logger.info("C-STORE concurrent idempotent retry accepté")
                            return 0x0000
                        if database_checked:
                            try:
                                target.unlink()
                            except OSError:
                                logger.exception("Impossible de nettoyer le fichier DICOM non indexé")
                    try:
                        quarantine(dataset, f"Unhandled receiver error: {exc}")
                    except Exception:
                        logger.exception("Échec de mise en quarantaine PACS")
                    return 0xC211

            event_handlers = [(evt.EVT_ACCEPTED, association_accepted),
                              (evt.EVT_C_STORE, store), (evt.EVT_RELEASED, association_released)]
            aborted_event = getattr(evt, "EVT_ABORTED", None)
            if aborted_event is not None:
                event_handlers.append((aborted_event, association_aborted))
            try:
                self._server = ae.start_server(
                    (host, port), block=False,
                    evt_handlers=event_handlers,
                )
            except Exception as exc:
                self._last_error = str(exc)
                raise RuntimeError(f"Impossible de démarrer le PACS sur {host}:{port} : {exc}") from exc
            self._config = {"ae_title": ae_title, "host": host, "port": port, "data_dir": str(data_dir)}
            self._last_error = ""
            logger.info("Récepteur PACS local démarré AE=%s port=%s; PostgreSQL=%s/%s; "
                        "Storage non restreint (classes standard connues=%s)",
                        ae_title, port, DB_CONFIG.get("host"), DB_CONFIG.get("database"),
                        standard_storage_class_count)
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            server = self._server
            if server is not None:
                server.shutdown()
                self._server = None
            with self._active_study_uids_lock:
                self._active_study_uids.clear()
            return self.status()

    def stats(self) -> dict[str, int]:
        self._ensure_ready()
        with get_db_cursor() as cursor:
            cursor.execute("""SELECT COUNT(DISTINCT st.patient_id) AS patients,
                COUNT(DISTINCT st.id) AS studies,
                COUNT(DISTINCT st.id) FILTER (WHERE st.status='RECEIVING') AS receiving,
                COUNT(i.id) AS instances,
                (SELECT COUNT(*) FROM pacs_quarantine q WHERE q.receiver_node=%s) AS quarantine
                FROM pacs_studies st LEFT JOIN pacs_series s ON s.study_id=st.id
                LEFT JOIN pacs_instances i ON i.series_id=s.id WHERE st.receiver_node=%s""",
                (self._node, self._node))
            row = cursor.fetchone()
            result = {key: int(row[key] or 0) for key in
                      ("patients", "studies", "receiving", "instances", "quarantine")}
            cursor.execute("SELECT COALESCE(SUM(total_size),0) AS bytes FROM pacs_studies WHERE receiver_node=%s",
                           (self._node,))
            result["storage_used_bytes"] = int(cursor.fetchone()["bytes"] or 0)
        try:
            disk_path = Path(self._config["data_dir"])
            while not disk_path.exists() and disk_path.parent != disk_path:
                disk_path = disk_path.parent
            disk = shutil.disk_usage(disk_path)
            result["disk_free_bytes"] = int(disk.free)
            result["disk_total_bytes"] = int(disk.total)
        except (OSError, KeyError, TypeError):
            result["disk_free_bytes"] = 0
            result["disk_total_bytes"] = 0
        return result

    @staticmethod
    def _series_rows(cursor, study_id: int) -> list[dict[str, Any]]:
        cursor.execute("""SELECT s.id, s.series_instance_uid, s.series_number, s.series_description,
            s.modality, s.body_part, s.series_date, s.series_time, s.received_images,
            (ARRAY_AGG(i.acquisition_date ORDER BY i.id) FILTER (WHERE i.acquisition_date IS NOT NULL))[1] AS acquisition_date,
            (ARRAY_AGG(i.acquisition_time ORDER BY i.id) FILTER (WHERE i.acquisition_time IS NOT NULL))[1] AS acquisition_time,
            COALESCE(SUM(i.file_size),0) AS total_size
            FROM pacs_series s LEFT JOIN pacs_instances i ON i.series_id=s.id
            WHERE s.study_id=%s GROUP BY s.id ORDER BY s.series_number NULLS LAST, s.id""", (study_id,))
        return [{"id": row["id"], "uid": row["series_instance_uid"], "number": row["series_number"],
                 "description": row["series_description"], "modality": row["modality"],
                 "body_part": row["body_part"],
                 "date": row["acquisition_date"] or row["series_date"],
                 "time": row["acquisition_time"] or row["series_time"],
                 "images": row["received_images"],
                 "total_size": int(row["total_size"] or 0)}
                for row in cursor.fetchall()]

    @staticmethod
    def _serialize_study(row: dict[str, Any], series: list[dict[str, Any]]) -> dict[str, Any]:
        patient = {"id": row["patient_identifier"], "name": row["patient_name"],
                   "birth_date": row["birth_date"], "sex": row["sex"], "weight": row["weight"],
                   "height_cm": row["height_cm"]}
        series_states = [(_contrast_series_state(item.get("description")), item.get("description"))
                         for item in series]
        study_state = _contrast_series_state(row["study_description"])
        detected_series = [description for state, description in series_states if state is True and description]
        if study_state is True or detected_series:
            contrast_status = "Avec injection"
        elif study_state is False or any(state is False for state, _ in series_states):
            contrast_status = "Sans injection"
        else:
            contrast_status = None
        study = {"id": row["id"], "uid": row["study_instance_uid"], "study_id": row["study_code"],
                 "accession_number": row["accession_number"], "date": row["study_date"],
                 "time": row["study_time"], "description": row["study_description"],
                 "modality": row["modality"], "status": row["status"],
                 "contrast_status": contrast_status, "contrast_series": detected_series,
                 "received_images": row["received_images"], "total_size": row["total_size"],
                 "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None}
        return {"patient": patient, "study": study, "series": series}

    def studies(self, query: str = "", limit: int = 15,
                filters: dict[str, Any] | None = None, offset: int = 0) -> dict[str, Any]:
        self._ensure_ready()
        limit = max(1, min(int(limit), 250))
        offset = max(0, int(offset))
        query_text = str(query or "").strip()[:200]
        pattern = f"%{query_text}%"
        filters = filters if isinstance(filters, dict) else {}
        last_name = str(filters.get("last_name") or "").strip()[:100]
        first_name = str(filters.get("first_name") or "").strip()[:100]
        modality = str(filters.get("modality") or "").strip()[:24]
        conditions = ["st.receiver_node=%s", "(%s='' OR p.patient_id ILIKE %s OR "
                     "COALESCE(p.patient_name,'') ILIKE %s OR COALESCE(st.study_id,'') ILIKE %s OR "
                     "COALESCE(st.accession_number,'') ILIKE %s OR "
                     "COALESCE(st.study_description,'') ILIKE %s OR COALESCE(st.modality,'') ILIKE %s)"]
        params: list[Any] = [self._node, query_text, pattern, pattern, pattern, pattern, pattern, pattern]
        if last_name:
            conditions.append("(split_part(COALESCE(p.patient_name,''), '^', 1) ILIKE %s OR p.patient_name ILIKE %s)")
            params.extend((f"%{last_name}%", f"%{last_name}%"))
        if first_name:
            conditions.append("(split_part(COALESCE(p.patient_name,''), '^', 2) ILIKE %s OR p.patient_name ILIKE %s)")
            params.extend((f"%{first_name}%", f"%{first_name}%"))
        if modality:
            conditions.append("UPPER(COALESCE(st.modality,''))=UPPER(%s)")
            params.append(modality)
        for key, operator in (("height_min_cm", ">="), ("height_max_cm", "<=")):
            value = filters.get(key)
            if value not in (None, ""):
                try:
                    numeric_value = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError("La taille doit être une valeur numérique en centimètres.") from exc
                if not math.isfinite(numeric_value) or numeric_value <= 0:
                    raise ValueError("La taille doit être une valeur positive en centimètres.")
                conditions.append(f"p.height_cm {operator} %s")
                params.append(numeric_value)
        for key, operator in (("date_from", ">="), ("date_to", "<=")):
            value = str(filters.get(key) or "").strip()
            if value:
                try:
                    normalized_date = datetime.strptime(value, "%Y-%m-%d").strftime("%Y%m%d")
                except ValueError as exc:
                    raise ValueError("La période de recherche contient une date invalide.") from exc
                conditions.append(f"st.study_date {operator} %s")
                params.append(normalized_date)
        where_clause = " AND ".join(conditions)
        with get_db_cursor() as cursor:
            cursor.execute("""SELECT COUNT(*) AS total
                FROM pacs_studies st JOIN pacs_patients p ON p.id=st.patient_id
                WHERE """ + where_clause, params)
            total = int(cursor.fetchone()["total"] or 0)
            page_params = [*params, limit, offset]
            cursor.execute("""SELECT st.id, st.study_instance_uid, st.study_id AS study_code,
                st.accession_number, st.study_date, st.study_time, st.study_description,
                st.modality, st.status, st.received_images, st.total_size, st.updated_at,
                p.patient_id AS patient_identifier, p.patient_name, p.birth_date, p.sex, p.weight, p.height_cm
                FROM pacs_studies st JOIN pacs_patients p ON p.id=st.patient_id
                WHERE """ + where_clause + " ORDER BY st.updated_at DESC LIMIT %s OFFSET %s",
                page_params)
            rows = cursor.fetchall()
            items = [self._serialize_study(row, self._series_rows(cursor, row["id"])) for row in rows]
            return {"items": items, "total": total}

    def study_detail(self, study_id: int) -> dict[str, Any] | None:
        self._ensure_ready()
        with get_db_cursor() as cursor:
            cursor.execute("""SELECT st.id, st.study_instance_uid, st.study_id AS study_code,
                st.accession_number, st.study_date, st.study_time, st.study_description,
                st.modality, st.status, st.received_images, st.total_size, st.updated_at,
                p.patient_id AS patient_identifier, p.patient_name, p.birth_date, p.sex, p.weight, p.height_cm
                FROM pacs_studies st JOIN pacs_patients p ON p.id=st.patient_id
                WHERE st.receiver_node=%s AND st.id=%s""", (self._node, int(study_id)))
            row = cursor.fetchone()
            return self._serialize_study(row, self._series_rows(cursor, row["id"])) if row else None

    def study_folder(self, study_id: int) -> Path:
        """Return the root folder containing this study's DICOM series and instances."""
        self._ensure_ready()
        with get_db_cursor() as cursor:
            cursor.execute("""SELECT st.study_instance_uid FROM pacs_studies st
                WHERE st.receiver_node=%s AND st.id=%s""", (self._node, int(study_id)))
            row = cursor.fetchone()
        if not row:
            raise ValueError("Examen PACS introuvable.")
        storage_root = (Path(self._config["data_dir"]) / "storage" / "studies").resolve()
        folder = (storage_root / _safe_component(str(row["study_instance_uid"]))).resolve()
        try:
            folder.relative_to(storage_root)
        except ValueError as exc:
            raise ValueError("Le dossier de l’examen sort du stockage DICOM autorisé.") from exc
        if not folder.is_dir():
            raise FileNotFoundError("Le dossier DICOM de cet examen n’existe pas sur ce poste.")
        return folder

    def series_instances(self, study_id: int, series_id: int) -> list[dict[str, Any]]:
        """Retourne les instances d’une série appartenant à cette machine et cet examen."""
        self._ensure_ready()
        with get_db_cursor() as cursor:
            cursor.execute("""SELECT i.id, i.instance_number, i.sop_instance_uid
                FROM pacs_instances i
                JOIN pacs_series s ON s.id=i.series_id
                JOIN pacs_studies st ON st.id=s.study_id
                WHERE st.receiver_node=%s AND st.id=%s AND s.id=%s
                ORDER BY CASE WHEN i.instance_number ~ '^-?[0-9]+(\\.[0-9]+)?$'
                    THEN i.instance_number::numeric END NULLS LAST, i.id""",
                (self._node, int(study_id), int(series_id)))
            return [{"id": row["id"], "number": row["instance_number"], "uid": row["sop_instance_uid"]}
                    for row in cursor.fetchall()]

    def nodes(self) -> list[dict[str, Any]]:
        self._ensure_ready()
        with get_db_cursor() as cursor:
            cursor.execute("""SELECT id,name,ae_title,host,port,enabled
                FROM pacs_nodes WHERE receiver_node=%s ORDER BY name,id""", (self._node,))
            return [dict(row) for row in cursor.fetchall()]

    def save_node(self, node: dict[str, Any]) -> dict[str, Any]:
        self._ensure_ready()
        name = str(node.get("name") or "").strip()[:100]
        # AETs are case-sensitive identifiers in the configured node entry;
        # preserve the spelling supplied by the administrator.
        ae_title = str(node.get("ae_title") or "").strip()
        host = str(node.get("host") or "").strip()[:255]
        try:
            port = int(node.get("port"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Le port du nœud doit être numérique.") from exc
        if not name or not host or not re.fullmatch(r"[A-Z0-9_ -]{1,16}", ae_title, re.IGNORECASE) or not 1 <= port <= 65535:
            raise ValueError("Renseignez le nom, l’adresse, l’AE Title valide et un port entre 1 et 65535.")
        node_id = node.get("id")
        with get_db_cursor() as cursor:
            if node_id:
                cursor.execute("""UPDATE pacs_nodes SET name=%s,ae_title=%s,host=%s,port=%s,
                    enabled=%s,updated_at=CURRENT_TIMESTAMP WHERE receiver_node=%s AND id=%s RETURNING id""",
                    (name, ae_title, host, port, bool(node.get("enabled", True)), self._node, int(node_id)))
                row = cursor.fetchone()
                if not row:
                    raise ValueError("Nœud DICOM introuvable.")
            else:
                cursor.execute("""INSERT INTO pacs_nodes(receiver_node,name,ae_title,host,port,enabled)
                    VALUES(%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (self._node, name, ae_title, host, port, bool(node.get("enabled", True))))
                node_id = cursor.fetchone()["id"]
        return {"ok": True, "id": int(node_id)}

    def delete_node(self, node_id: int) -> dict[str, Any]:
        self._ensure_ready()
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM pacs_nodes WHERE receiver_node=%s AND id=%s RETURNING id",
                           (self._node, int(node_id)))
            if not cursor.fetchone():
                raise ValueError("Nœud DICOM introuvable.")
        return {"ok": True}

    def echo_node(self, node_id: int) -> dict[str, Any]:
        self._ensure_ready()
        with get_db_cursor() as cursor:
            cursor.execute("SELECT * FROM pacs_nodes WHERE receiver_node=%s AND id=%s AND enabled=TRUE",
                           (self._node, int(node_id)))
            node = cursor.fetchone()
        if not node:
            raise ValueError("Nœud DICOM introuvable ou désactivé.")
        try:
            from pynetdicom import AE
            from pynetdicom.sop_class import Verification
        except ImportError as exc:
            raise RuntimeError("Le test DICOM nécessite pynetdicom.") from exc
        ae = AE(ae_title="CLINIVA")
        ae.acse_timeout = 5
        ae.dimse_timeout = 5
        ae.network_timeout = 5
        ae.add_requested_context(Verification)
        assoc = ae.associate(node["host"], int(node["port"]), ae_title=node["ae_title"].encode("ascii"))
        if not assoc.is_established:
            raise ConnectionError("Association impossible : vérifiez l’adresse, le port, l’AE Title et le pare-feu.")
        try:
            status = assoc.send_c_echo()
            code = getattr(status, "Status", None)
            if code != 0x0000:
                raise RuntimeError(f"Le nœud a répondu avec le statut DICOM 0x{int(code or 0):04X}.")
        finally:
            assoc.release()
        return {"ok": True, "message": f"C-ECHO réussi vers {node['ae_title']} ({node['host']}:{node['port']})."}

    def _transfer_paths(self, kind: str, record_id: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        self._ensure_ready()
        with get_db_cursor() as cursor:
            if kind == "patient":
                cursor.execute("""SELECT i.id,i.file_path,i.sop_class_uid,p.patient_id AS patient_identifier
                    FROM pacs_instances i JOIN pacs_series se ON se.id=i.series_id
                    JOIN pacs_studies st ON st.id=se.study_id JOIN pacs_patients p ON p.id=st.patient_id
                    WHERE st.receiver_node=%s AND p.id=(SELECT patient_id FROM pacs_studies WHERE receiver_node=%s AND id=%s) ORDER BY st.id,se.id,i.id""",
                    (self._node, self._node, int(record_id)))
            elif kind == "study":
                cursor.execute("""SELECT i.id,i.file_path,i.sop_class_uid,p.patient_id AS patient_identifier
                    FROM pacs_instances i JOIN pacs_series se ON se.id=i.series_id
                    JOIN pacs_studies st ON st.id=se.study_id JOIN pacs_patients p ON p.id=st.patient_id
                    WHERE st.receiver_node=%s AND st.id=%s ORDER BY se.id,i.id""", (self._node, int(record_id)))
            elif kind == "series":
                cursor.execute("""SELECT i.id,i.file_path,i.sop_class_uid,p.patient_id AS patient_identifier
                    FROM pacs_instances i JOIN pacs_series se ON se.id=i.series_id
                    JOIN pacs_studies st ON st.id=se.study_id JOIN pacs_patients p ON p.id=st.patient_id
                    WHERE st.receiver_node=%s AND se.id=%s ORDER BY i.id""", (self._node, int(record_id)))
            else:
                raise ValueError("Type de transfert inconnu.")
            rows = [dict(row) for row in cursor.fetchall()]
        if not rows:
            raise ValueError("Aucun fichier DICOM local à envoyer.")
        return rows, {"patient_identifier": rows[0]["patient_identifier"]}

    def send_to_node(self, node_id: int, kind: str, record_id: int,
                     progress_callback=None) -> dict[str, Any]:
        """Envoie par C-STORE les images d’un patient, examen ou série et journalise le résultat."""
        self._ensure_ready()
        with get_db_cursor() as cursor:
            cursor.execute("SELECT * FROM pacs_nodes WHERE receiver_node=%s AND id=%s AND enabled=TRUE",
                           (self._node, int(node_id)))
            node = cursor.fetchone()
        if not node:
            raise ValueError("Choisissez un nœud DICOM actif.")
        rows, context = self._transfer_paths(kind, int(record_id))
        try:
            from pynetdicom import AE, ALL_TRANSFER_SYNTAXES
            import pydicom
            from pydicom.uid import UID
        except ImportError as exc:
            raise RuntimeError("L’envoi DICOM nécessite pynetdicom et pydicom.") from exc
        if kind == "patient":
            study_id = series_id = None
        elif kind == "study":
            study_id, series_id = int(record_id), None
        else:
            with get_db_cursor() as cursor:
                cursor.execute("SELECT study_id FROM pacs_series WHERE receiver_node=%s AND id=%s",
                               (self._node, int(record_id)))
                sr = cursor.fetchone()
                if not sr:
                    raise ValueError("Série DICOM introuvable.")
                study_id, series_id = int(sr["study_id"]), int(record_id)
        with get_db_cursor() as cursor:
            cursor.execute("""INSERT INTO pacs_transfer_jobs(receiver_node,node_id,destination_name,
                destination_ae,patient_identifier,study_id,series_id,transfer_kind,total_instances)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (self._node, int(node_id), node["name"], node["ae_title"], context["patient_identifier"],
                 study_id, series_id, kind, len(rows)))
            job_id = int(cursor.fetchone()["id"])
        sent = failed = 0
        failures: list[str] = []
        # Use small context groups to stay under DICOM's 128 presentation-context limit.
        classes = list(dict.fromkeys(str(r["sop_class_uid"] or "") for r in rows if r["sop_class_uid"]))
        send_rows = [row for row in rows if row["sop_class_uid"]]
        failed = len(rows) - len(send_rows)
        processed = failed
        report_progress = progress_callback or (lambda *_args: None)
        report_progress({"processed": processed, "total": len(rows), "sent": sent,
                         "failed": failed, "message": "Préparation de l’association DICOM"})
        if failed:
            failures.append(f"{failed} instance(s) sans SOP Class UID n’ont pas pu être envoyées.")
        batches = [classes[i:i + 120] for i in range(0, len(classes), 120)] or [[]]
        try:
            for batch in batches:
                ae = AE(ae_title="CLINIVA")
                ae.acse_timeout = 10
                ae.dimse_timeout = 30
                ae.network_timeout = 20
                for sop_class in batch:
                    # Offer compressed and uncompressed syntaxes so the peer can
                    # choose one it supports for each storage SOP class.
                    ae.add_requested_context(sop_class, ALL_TRANSFER_SYNTAXES)
                assoc = ae.associate(node["host"], int(node["port"]), ae_title=node["ae_title"].encode("ascii"))
                if not assoc.is_established:
                    failed += len(send_rows) - sent - failed
                    failures.append("Association DICOM impossible.")
                    processed = len(rows)
                    report_progress({"processed": processed, "total": len(rows), "sent": sent,
                                     "failed": failed, "message": "Association refusée par le nœud"})
                    break
                try:
                    allowed = set(batch)
                    for row in send_rows:
                        if str(row["sop_class_uid"] or "") not in allowed:
                            continue
                        try:
                            ds = pydicom.dcmread(row["file_path"])
                            source_syntax = str(getattr(ds.file_meta, "TransferSyntaxUID", ""))
                            accepted_syntaxes = [
                                str(syntax)
                                for presentation_context in assoc.accepted_contexts
                                if str(presentation_context.abstract_syntax) == str(row["sop_class_uid"])
                                for syntax in presentation_context.transfer_syntax
                            ]
                            if not accepted_syntaxes:
                                raise RuntimeError(
                                    f"Le nœud {node['name']} n’a accepté aucun Transfer Syntax "
                                    f"pour la classe DICOM {row['sop_class_uid']}."
                                )
                            # C-STORE must use the syntax selected during association
                            # negotiation, not just any syntax offered by the sender.
                            target_syntax = accepted_syntaxes[0]
                            if source_syntax and source_syntax != target_syntax:
                                # Keep the archived source file unchanged; transcode
                                # only this in-memory copy to the peer-selected syntax.
                                original_sop_uid = str(getattr(ds, "SOPInstanceUID", ""))
                                try:
                                    source_uid = UID(source_syntax)
                                    target_uid = UID(target_syntax)
                                    if source_uid.is_compressed:
                                        try:
                                            ds.decompress(generate_instance_uid=False)
                                        except TypeError:
                                            # Compatibility with pydicom releases predating
                                            # the generate_instance_uid argument.
                                            ds.decompress()
                                    if target_uid.is_compressed:
                                        ds.compress(target_uid, generate_instance_uid=False)
                                    else:
                                        # The pixel payload is now uncompressed; update
                                        # the encoding declared for the negotiated context.
                                        ds.file_meta.TransferSyntaxUID = target_uid
                                except Exception as exc:
                                    raise RuntimeError(
                                        f"Impossible de convertir l’image du Transfer Syntax "
                                        f"{source_syntax} vers celui demandé par le nœud "
                                        f"({target_syntax}) : {exc}"
                                    ) from exc
                                if original_sop_uid:
                                    ds.SOPInstanceUID = original_sop_uid
                                    ds.file_meta.MediaStorageSOPInstanceUID = original_sop_uid
                            status = assoc.send_c_store(ds)
                            if status and int(getattr(status, "Status", 0xFFFF)) in (0x0000, 0xB000, 0xB006, 0xB007):
                                sent += 1
                            else:
                                failed += 1
                                failures.append(f"Instance {row['id']}: statut C-STORE en échec.")
                        except Exception as exc:
                            failed += 1
                            failures.append(f"Instance {row['id']}: {exc}")
                        processed += 1
                        report_progress({"processed": processed, "total": len(rows), "sent": sent,
                                         "failed": failed,
                                         "message": f"Transfert des images : {processed}/{len(rows)}"})
                finally:
                    assoc.release()
            state = "SUCCÈS" if failed == 0 else "PARTIEL" if sent else "ÉCHEC"
            detail = "\n".join(failures[:100])
            with get_db_cursor() as cursor:
                cursor.execute("""UPDATE pacs_transfer_jobs SET status=%s,sent_instances=%s,
                    failed_instances=%s,details=%s,completed_at=CURRENT_TIMESTAMP WHERE id=%s""",
                    (state, sent, failed, detail, job_id))
            return {"ok": failed == 0, "job_id": job_id, "status": state, "sent": sent,
                    "failed": failed, "total": len(rows), "details": failures[:10]}
        except Exception as exc:
            with get_db_cursor() as cursor:
                cursor.execute("""UPDATE pacs_transfer_jobs SET status='ÉCHEC',sent_instances=%s,
                    failed_instances=%s,details=%s,completed_at=CURRENT_TIMESTAMP WHERE id=%s""",
                    (sent, max(failed, len(rows)-sent), str(exc)[:4000], job_id))
            raise

    def transfer_history(self, limit: int = 200) -> list[dict[str, Any]]:
        self._ensure_ready()
        with get_db_cursor() as cursor:
            cursor.execute("""SELECT id,destination_name,destination_ae,patient_identifier,transfer_kind,
                status,total_instances,sent_instances,failed_instances,details,started_at,completed_at
                FROM pacs_transfer_jobs WHERE receiver_node=%s ORDER BY started_at DESC LIMIT %s""",
                (self._node, max(1, min(int(limit), 500))))
            rows = [dict(row) for row in cursor.fetchall()]
            for row in rows:
                for key in ("started_at", "completed_at"):
                    if row.get(key) is not None:
                        row[key] = row[key].isoformat()
            return rows

    def update_patient(self, study_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """Met à jour l’identité patient en base et dans les fichiers DICOM locaux de l’examen."""
        self._ensure_ready()
        patient_id = str(values.get("patient_id") or "").strip()[:100]
        patient_name = str(values.get("patient_name") or "").strip()[:200]
        birth_raw = str(values.get("birth_date") or "").strip()
        sex = str(values.get("sex") or "").strip().upper()[:1]
        try:
            weight = float(values["weight"]) if values.get("weight") not in (None, "") else None
            height_cm = float(values["height_cm"]) if values.get("height_cm") not in (None, "") else None
        except (TypeError, ValueError) as exc:
            raise ValueError("Le poids et la taille doivent être numériques.") from exc
        if weight is not None and (not math.isfinite(weight) or weight <= 0 or weight > 500):
            raise ValueError("Le poids doit être compris entre 0 et 500 kg.")
        if height_cm is not None and (not math.isfinite(height_cm) or height_cm <= 0 or height_cm > 300):
            raise ValueError("La taille doit être comprise entre 0 et 300 cm.")
        if not patient_id or not patient_name or (sex and sex not in {"M", "F", "O"}):
            raise ValueError("L’identifiant et le nom du patient sont obligatoires; sexe M/F/O.")
        birth_date = ""
        if birth_raw:
            try:
                birth_date = datetime.strptime(birth_raw, "%Y-%m-%d").strftime("%Y%m%d") if "-" in birth_raw else datetime.strptime(birth_raw, "%Y%m%d").strftime("%Y%m%d")
            except ValueError as exc:
                raise ValueError("Date de naissance invalide.") from exc
        with get_db_cursor() as cursor:
            cursor.execute("""SELECT p.id AS patient_pk FROM pacs_studies st JOIN pacs_patients p ON p.id=st.patient_id
                WHERE st.receiver_node=%s AND st.id=%s""", (self._node, int(study_id)))
            row = cursor.fetchone()
            if not row:
                raise ValueError("Examen introuvable sur ce récepteur.")
            patient_pk = int(row["patient_pk"])
            cursor.execute("SELECT id FROM pacs_patients WHERE patient_id=%s AND id<>%s", (patient_id, patient_pk))
            if cursor.fetchone():
                raise ValueError("Cet identifiant patient est déjà utilisé dans le PACS.")
            cursor.execute("""SELECT DISTINCT i.file_path FROM pacs_instances i
                JOIN pacs_series se ON se.id=i.series_id JOIN pacs_studies st ON st.id=se.study_id
                WHERE st.receiver_node=%s AND st.patient_id=%s""", (self._node, patient_pk))
            paths = [Path(r["file_path"]) for r in cursor.fetchall()]
        try:
            import pydicom
        except ImportError as exc:
            raise RuntimeError("La modification patient nécessite pydicom.") from exc
        staged: list[tuple[Path, Path, Path]] = []
        try:
            for path in paths:
                if not path.is_file():
                    continue
                dataset = pydicom.dcmread(str(path))
                dataset.PatientID = patient_id
                dataset.PatientName = patient_name
                dataset.PatientBirthDate = birth_date
                dataset.PatientSex = sex
                if weight is not None:
                    dataset.PatientWeight = weight
                elif "PatientWeight" in dataset:
                    del dataset.PatientWeight
                if height_cm is not None:
                    dataset.PatientSize = height_cm / 100
                elif "PatientSize" in dataset:
                    del dataset.PatientSize
                temp = path.with_name(path.name + ".cliniva.tmp")
                backup = path.with_name(path.name + ".cliniva.bak")
                _save_dicom_dataset(dataset, temp)
                staged.append((path, temp, backup))
            for original, temp, backup in staged:
                original.replace(backup)
                temp.replace(original)
            with get_db_cursor() as cursor:
                cursor.execute("""UPDATE pacs_patients SET patient_id=%s,patient_name=%s,birth_date=%s,sex=%s,
                    weight=%s,height_cm=%s WHERE id=%s""",
                    (patient_id, patient_name, birth_date, sex, weight, height_cm, patient_pk))
            for _, _, backup in staged:
                backup.unlink(missing_ok=True)
            return {"ok": True, "updated_files": len(staged)}
        except Exception:
            for original, temp, backup in reversed(staged):
                if backup.exists():
                    original.unlink(missing_ok=True)
                    backup.replace(original)
                temp.unlink(missing_ok=True)
            raise

    def update_study_datetime(self, study_id: int, values: dict[str, Any],
                               progress_callback=None) -> dict[str, Any]:
        """Aligne la date et l'heure DICOM de l'examen, de ses séries et de toutes ses images."""
        self._ensure_ready()
        date_raw = str(values.get("date") or "").strip()
        time_raw = str(values.get("time") or "").strip()
        try:
            study_date = datetime.strptime(date_raw, "%Y-%m-%d").strftime("%Y%m%d")
        except ValueError as exc:
            raise ValueError("Saisissez une date d’examen valide.") from exc
        try:
            time_format = "%H:%M:%S" if len(time_raw) == 8 else "%H:%M"
            study_time = datetime.strptime(time_raw, time_format).strftime("%H%M%S")
        except ValueError as exc:
            raise ValueError("Saisissez une heure d’examen valide.") from exc

        with get_db_cursor() as cursor:
            cursor.execute("""SELECT id,status,study_instance_uid FROM pacs_studies
                WHERE receiver_node=%s AND id=%s""", (self._node, int(study_id)))
            study = cursor.fetchone()
            if not study:
                raise ValueError("Examen introuvable sur ce récepteur PACS.")
            with self._active_study_uids_lock:
                is_receiving_now = self._active_study_uids.get(
                    str(study["study_instance_uid"] or ""), 0) > 0
            if is_receiving_now:
                raise ValueError("Attendez la fin de réception de l’examen avant de modifier sa date et son heure.")
            cursor.execute("""SELECT i.file_path, s.id AS series_id
                FROM pacs_instances i JOIN pacs_series s ON s.id=i.series_id
                WHERE s.receiver_node=%s AND s.study_id=%s ORDER BY s.id,i.id""",
                (self._node, int(study_id)))
            records = [dict(row) for row in cursor.fetchall()]
            cursor.execute("SELECT id FROM pacs_series WHERE receiver_node=%s AND study_id=%s",
                           (self._node, int(study_id)))
            series_ids = [int(row["id"]) for row in cursor.fetchall()]

        try:
            import pydicom
        except ImportError as exc:
            raise RuntimeError("La modification de date/heure nécessite pydicom.") from exc
        storage_root = (Path(self._config["data_dir"]) / "storage" / "studies").resolve()
        staged: list[tuple[Path, Path, Path]] = []
        total_files = len(records)
        report_progress = progress_callback or (lambda *_args: None)
        report_progress(2, "Préparation de la modification")
        try:
            for index, record in enumerate(records, start=1):
                path = Path(record["file_path"]).resolve()
                try:
                    path.relative_to(storage_root)
                except ValueError as exc:
                    raise ValueError("Un fichier de l’examen se trouve hors du stockage PACS autorisé.") from exc
                if not path.is_file():
                    raise FileNotFoundError(f"Fichier DICOM introuvable : {path.name}")
                dataset = pydicom.dcmread(str(path))
                for attribute, value in (
                    ("StudyDate", study_date), ("StudyTime", study_time),
                    ("SeriesDate", study_date), ("SeriesTime", study_time),
                    ("AcquisitionDate", study_date), ("AcquisitionTime", study_time),
                    ("ContentDate", study_date), ("ContentTime", study_time),
                    ("AcquisitionDateTime", f"{study_date}{study_time}"),
                ):
                    setattr(dataset, attribute, value)
                temp = path.with_name(path.name + ".cliniva.datetime.tmp")
                backup = path.with_name(path.name + ".cliniva.datetime.bak")
                _save_dicom_dataset(dataset, temp)
                staged.append((path, temp, backup))
                report_progress(5 + int(index / max(total_files, 1) * 75),
                                f"Mise à jour des images : {index}/{total_files}")
            report_progress(82, "Enregistrement des fichiers DICOM")
            for index, (original, temp, backup) in enumerate(staged, start=1):
                original.replace(backup)
                temp.replace(original)
                report_progress(82 + int(index / max(total_files, 1) * 12),
                                f"Application des fichiers : {index}/{total_files}")
            report_progress(96, "Mise à jour de l’index PACS")
            with get_db_cursor() as cursor:
                cursor.execute("""UPDATE pacs_studies SET study_date=%s,study_time=%s,
                    updated_at=CURRENT_TIMESTAMP WHERE receiver_node=%s AND id=%s""",
                    (study_date, study_time, self._node, int(study_id)))
                cursor.execute("""UPDATE pacs_series SET series_date=%s,series_time=%s
                    WHERE receiver_node=%s AND study_id=%s""",
                    (study_date, study_time, self._node, int(study_id)))
                cursor.execute("""UPDATE pacs_instances i SET acquisition_date=%s,acquisition_time=%s
                    FROM pacs_series s WHERE i.series_id=s.id AND s.receiver_node=%s AND s.study_id=%s""",
                    (study_date, study_time, self._node, int(study_id)))
            for _, _, backup in staged:
                backup.unlink(missing_ok=True)
            report_progress(100, "Modification terminée")
            return {"ok": True, "updated_files": len(staged), "updated_series": len(series_ids),
                    "date": study_date, "time": study_time}
        except Exception:
            for original, temp, backup in reversed(staged):
                if backup.exists():
                    original.unlink(missing_ok=True)
                    backup.replace(original)
                temp.unlink(missing_ok=True)
            raise

    def delete_study(self, study_id: int) -> dict[str, Any]:
        """Supprime un examen de l’index local et ses fichiers DICOM associés."""
        self._ensure_ready()
        storage_root = (Path(self._config["data_dir"]) / "storage" / "studies").resolve()
        with get_db_cursor() as cursor:
            cursor.execute("""SELECT i.file_path FROM pacs_instances i JOIN pacs_series se ON se.id=i.series_id
                JOIN pacs_studies st ON st.id=se.study_id WHERE st.receiver_node=%s AND st.id=%s""",
                (self._node, int(study_id)))
            paths = [Path(r["file_path"]).resolve() for r in cursor.fetchall()]
            cursor.execute("DELETE FROM pacs_studies WHERE receiver_node=%s AND id=%s RETURNING id",
                           (self._node, int(study_id)))
            if not cursor.fetchone():
                raise ValueError("Examen introuvable sur ce récepteur.")
        deleted = 0
        for path in paths:
            try:
                if path.is_relative_to(storage_root) and path.is_file():
                    path.unlink()
                    deleted += 1
            except OSError:
                logger.exception("Échec de suppression du fichier DICOM %s", path)
        return {"ok": True, "deleted_files": deleted, "files": len(paths)}

    def image_preview(self, study_id: int, instance_id: int,
                      window_center: float | None = None,
                      window_width: float | None = None) -> dict[str, Any]:
        """Convertit une instance DICOM en aperçu PNG transmis directement au viewer."""
        self._ensure_ready()
        with get_db_cursor() as cursor:
            cursor.execute("""SELECT i.file_path FROM pacs_instances i
                JOIN pacs_series s ON s.id=i.series_id
                JOIN pacs_studies st ON st.id=s.study_id
                WHERE st.receiver_node=%s AND st.id=%s AND i.id=%s""",
                (self._node, int(study_id), int(instance_id)))
            row = cursor.fetchone()
        if not row:
            raise ValueError("Cette image n’appartient pas à l’examen demandé.")
        file_path = Path(row["file_path"])
        if not file_path.is_file():
            raise FileNotFoundError("Le fichier DICOM local est introuvable.")
        try:
            import numpy as np
            import pydicom
            from PIL import Image
            try:
                from pydicom.pixels import apply_modality_lut
            except ImportError:
                from pydicom.pixel_data_handlers.util import apply_modality_lut
        except ImportError as exc:
            raise RuntimeError("Le viewer nécessite pydicom, NumPy et Pillow sur cette machine.") from exc

        try:
            dataset = pydicom.dcmread(str(file_path))
            pixels = dataset.pixel_array
        except Exception as exc:
            logger.exception("Impossible de décoder l’image DICOM %s", file_path)
            if "Unable to decompress" in str(exc) or "all plugins are missing dependencies" in str(exc):
                raise RuntimeError(
                    "Codec DICOM compressé manquant. Installez pylibjpeg et ses codecs "
                    "(pylibjpeg-libjpeg, pylibjpeg-openjpeg, pylibjpeg-rle, pyjpegls), "
                    "puis redémarrez Cliniva."
                ) from exc
            raise RuntimeError(f"Décodage DICOM impossible : {exc}") from exc

        raw_pixels = None
        if pixels.ndim == 4 and pixels.shape[-1] in (3, 4):
            pixels = pixels[0]
        if pixels.ndim == 3 and pixels.shape[-1] in (3, 4):
            image = Image.fromarray(pixels.astype(np.uint8))
            center = width = None
        else:
            if pixels.ndim > 2:
                pixels = pixels[0]
            try:
                values = np.asarray(apply_modality_lut(pixels, dataset), dtype=np.float32)
            except Exception:
                slope = float(getattr(dataset, "RescaleSlope", 1) or 1)
                intercept = float(getattr(dataset, "RescaleIntercept", 0) or 0)
                values = np.asarray(pixels, dtype=np.float32) * slope + intercept
            default_center = getattr(dataset, "WindowCenter", None)
            default_width = getattr(dataset, "WindowWidth", None)
            if isinstance(default_center, (list, tuple)) or hasattr(default_center, "__iter__") and not isinstance(default_center, (str, bytes)):
                default_center = default_center[0]
            if isinstance(default_width, (list, tuple)) or hasattr(default_width, "__iter__") and not isinstance(default_width, (str, bytes)):
                default_width = default_width[0]
            center = float(window_center) if window_center is not None else (float(default_center) if default_center is not None else None)
            width = float(window_width) if window_width is not None else (float(default_width) if default_width is not None else None)
            finite_values = values[np.isfinite(values)]
            if finite_values.size == 0:
                raise RuntimeError("L’image DICOM ne contient aucune valeur de pixel exploitable.")
            if width is None or not math.isfinite(width) or width <= 0:
                low, high = np.percentile(finite_values, [1, 99]) if finite_values.size > 1 else (finite_values[0], finite_values[0] + 1)
                width = max(float(high - low), 1.0)
                center = float((high + low) / 2)
            if center is None or not math.isfinite(center):
                center = float(np.median(finite_values))
            raw_pixels = np.ascontiguousarray(values, dtype="<f4").tobytes()
            low, high = center - width / 2, center + width / 2
            grayscale = np.clip((values - low) * (255.0 / max(high - low, 1e-6)), 0, 255).astype(np.uint8)
            if str(getattr(dataset, "PhotometricInterpretation", "")) == "MONOCHROME1":
                grayscale = 255 - grayscale
            image = Image.fromarray(grayscale)

        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        def _dicom_values(tag: str) -> list[float] | None:
            raw = getattr(dataset, tag, None)
            if raw is None:
                return None
            try:
                values = [float(value) for value in raw]
            except (TypeError, ValueError):
                try:
                    values = [float(raw)]
                except (TypeError, ValueError):
                    return None
            return values or None

        return {"data": base64.b64encode(buffer.getvalue()).decode("ascii") if raw_pixels is None else "",
                "rows": int(getattr(dataset, "Rows", image.height)),
                "columns": int(getattr(dataset, "Columns", image.width)),
                "instance_number": _text(getattr(dataset, "InstanceNumber", None)),
                "modality": _text(getattr(dataset, "Modality", None)),
                "window_center": center, "window_width": width,
                "photometric": _text(getattr(dataset, "PhotometricInterpretation", None)),
                "raw_pixel_data": base64.b64encode(raw_pixels).decode("ascii") if raw_pixels is not None else None,
                "pixel_spacing": _dicom_values("PixelSpacing"),
                "slice_thickness": _text(getattr(dataset, "SliceThickness", None)),
                "slice_location": _text(getattr(dataset, "SliceLocation", None)),
                "image_position": _dicom_values("ImagePositionPatient"),
                "image_orientation": _dicom_values("ImageOrientationPatient"),
                "patient_position": _text(getattr(dataset, "PatientPosition", None)),
                "bits_stored": _text(getattr(dataset, "BitsStored", None)),
                "manufacturer": _text(getattr(dataset, "Manufacturer", None)),
                "model": _text(getattr(dataset, "ManufacturerModelName", None)),
                "series_description": _text(getattr(dataset, "SeriesDescription", None)),
                "study_date": _text(getattr(dataset, "StudyDate", None)),
                "study_time": _text(getattr(dataset, "StudyTime", None)),
                "frame_of_reference_uid": _text(getattr(dataset, "FrameOfReferenceUID", None))}

    def _index_imported_dataset(self, dataset, source_factory, quarantine_dir: Path) -> str:
        patient_id = _text(getattr(dataset, "PatientID", None))
        if not patient_id:
            return "skipped"
        study_uid = _required_uid(dataset, "StudyInstanceUID")
        series_uid = _required_uid(dataset, "SeriesInstanceUID")
        sop_uid = _required_uid(dataset, "SOPInstanceUID")
        file_meta = getattr(dataset, "file_meta", None)
        sop_class_uid = (_text(getattr(dataset, "SOPClassUID", None))
                         or _text(getattr(file_meta, "MediaStorageSOPClassUID", None)))
        target = (Path(self._config["data_dir"]) / "storage" / "studies"
                  / _safe_component(study_uid) / _safe_component(series_uid)
                  / f"{_safe_component(sop_uid)}.dcm")

        with get_db_cursor() as cursor:
            cursor.execute("""SELECT i.id FROM pacs_instances i
                JOIN pacs_series s ON s.id=i.series_id
                JOIN pacs_studies st ON st.id=s.study_id
                WHERE st.receiver_node=%s AND i.sop_instance_uid=%s LIMIT 1""",
                (self._node, sop_uid))
            if cursor.fetchone():
                return "duplicate"

            cursor.execute("""INSERT INTO pacs_patients
                (patient_id, patient_name, birth_date, sex, weight, height_cm)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (patient_id) DO UPDATE SET
                  patient_name=COALESCE(EXCLUDED.patient_name, pacs_patients.patient_name),
                  birth_date=COALESCE(EXCLUDED.birth_date, pacs_patients.birth_date),
                  sex=COALESCE(EXCLUDED.sex, pacs_patients.sex),
                  weight=COALESCE(EXCLUDED.weight, pacs_patients.weight),
                  height_cm=COALESCE(EXCLUDED.height_cm, pacs_patients.height_cm)
                RETURNING id""",
                (patient_id, _text(getattr(dataset, "PatientName", None)),
                 _text(getattr(dataset, "PatientBirthDate", None)),
                 _text(getattr(dataset, "PatientSex", None)), _patient_weight(dataset),
                 _patient_height_cm(dataset)))
            patient_db_id = cursor.fetchone()["id"]

            cursor.execute("""SELECT id, patient_id FROM pacs_studies
                WHERE receiver_node=%s AND study_instance_uid=%s""", (self._node, study_uid))
            study = cursor.fetchone()
            conflict_reason = None
            if study and study["patient_id"] != patient_db_id:
                conflict_reason = f"Study/Patient conflict: {study_uid}"
                study_db_id = None
            elif study:
                study_db_id = study["id"]
            else:
                cursor.execute("""INSERT INTO pacs_studies
                    (receiver_node, study_instance_uid, patient_id, study_id, accession_number,
                     study_date, study_time, study_description, modality)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (self._node, study_uid, patient_db_id,
                     _text(getattr(dataset, "StudyID", None)),
                     _text(getattr(dataset, "AccessionNumber", None)),
                     _text(getattr(dataset, "StudyDate", None)),
                     _text(getattr(dataset, "StudyTime", None)),
                     _text(getattr(dataset, "StudyDescription", None)),
                     _text(getattr(dataset, "Modality", None))))
                study_db_id = cursor.fetchone()["id"]

            series_db_id = None
            if not conflict_reason:
                cursor.execute("""SELECT id, study_id FROM pacs_series
                    WHERE receiver_node=%s AND series_instance_uid=%s""", (self._node, series_uid))
                series = cursor.fetchone()
                if series and series["study_id"] != study_db_id:
                    conflict_reason = f"Series/Study conflict: {series_uid}"
                elif series:
                    series_db_id = series["id"]
                else:
                    cursor.execute("""INSERT INTO pacs_series
                        (study_id, receiver_node, series_instance_uid, series_number,
                         series_description, modality, body_part, expected_images, series_date, series_time)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (study_db_id, self._node, series_uid,
                         _text(getattr(dataset, "SeriesNumber", None)),
                         _text(getattr(dataset, "SeriesDescription", None)),
                         _text(getattr(dataset, "Modality", None)),
                         _text(getattr(dataset, "BodyPartExamined", None)),
                         _text(getattr(dataset, "NumberOfSeriesRelatedInstances", None)),
                         _text(getattr(dataset, "SeriesDate", None)),
                         _text(getattr(dataset, "SeriesTime", None))))
                    series_db_id = cursor.fetchone()["id"]

            if conflict_reason:
                quarantine_name = f"{datetime.utcnow():%Y%m%d%H%M%S%f}_{uuid.uuid4().hex}.dcm"
                quarantine_path = quarantine_dir / quarantine_name
                _atomic_copy(quarantine_path, source_factory)
                cursor.execute("""INSERT INTO pacs_quarantine
                    (receiver_node, reason, sop_instance_uid, study_instance_uid,
                     series_instance_uid, file_path)
                    VALUES (%s,%s,%s,%s,%s,%s)""",
                    (self._node, conflict_reason, sop_uid, study_uid, series_uid, str(quarantine_path)))
                return "quarantined"

            if target.exists():
                quarantine_name = f"{datetime.utcnow():%Y%m%d%H%M%S%f}_{uuid.uuid4().hex}.dcm"
                quarantine_path = quarantine_dir / quarantine_name
                _atomic_copy(quarantine_path, source_factory)
                cursor.execute("""INSERT INTO pacs_quarantine
                    (receiver_node, reason, sop_instance_uid, study_instance_uid,
                     series_instance_uid, file_path)
                    VALUES (%s,%s,%s,%s,%s,%s)""",
                    (self._node, "Unindexed DICOM target path already exists", sop_uid,
                     study_uid, series_uid, str(quarantine_path)))
                return "quarantined"

            size = _atomic_copy(target, source_factory)
            cursor.execute("""INSERT INTO pacs_instances
                (series_id, sop_instance_uid, sop_class_uid, instance_number, acquisition_date, acquisition_time, file_path, file_size)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (series_db_id, sop_uid, sop_class_uid,
                 _text(getattr(dataset, "InstanceNumber", None)),
                 _text(getattr(dataset, "AcquisitionDate", None) or getattr(dataset, "ContentDate", None)),
                 _text(getattr(dataset, "AcquisitionTime", None) or getattr(dataset, "ContentTime", None)),
                 str(target), size))
            cursor.execute("UPDATE pacs_series SET received_images=received_images+1 WHERE id=%s",
                           (series_db_id,))
            cursor.execute("""UPDATE pacs_studies SET received_images=received_images+1,
                total_size=total_size+%s, status='RECEIVING', updated_at=CURRENT_TIMESTAMP
                WHERE id=%s""", (size, study_db_id))
        return "imported"

    @staticmethod
    def _dicom_import_tags() -> list[str]:
        return ["PatientID", "PatientName", "PatientBirthDate", "PatientSex", "PatientWeight",
                "PatientSize", "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID",
                "SOPClassUID", "StudyID", "AccessionNumber", "StudyDate", "StudyTime",
                "StudyDescription", "Modality", "SeriesNumber", "SeriesDescription",
                "BodyPartExamined", "InstanceNumber", "NumberOfSeriesRelatedInstances"]

    def import_source(self, source_path: str, source_type: str) -> dict[str, Any]:
        """Importe récursivement les objets DICOM d’un dossier ou d’un ZIP sécurisé."""
        self._ensure_ready()
        try:
            import pydicom
        except ImportError as exc:
            raise RuntimeError("L’importation nécessite pydicom sur cette machine.") from exc
        source = Path(source_path).expanduser().resolve()
        source_type = str(source_type or "").strip().lower()
        if source_type not in {"folder", "zip"}:
            raise ValueError("Type de source d’importation inconnu.")
        if source_type == "folder" and not source.is_dir():
            raise ValueError("Le dossier DICOM sélectionné est introuvable.")
        if source_type == "zip" and (not source.is_file() or not zipfile.is_zipfile(source)):
            raise ValueError("L’archive ZIP sélectionnée est invalide ou introuvable.")

        storage_dir = Path(self._config["data_dir"]) / "storage" / "studies"
        quarantine_dir = Path(self._config["data_dir"]) / "quarantine"
        storage_dir.mkdir(parents=True, exist_ok=True)
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        report: dict[str, Any] = {"ok": True, "source_type": source_type, "scanned": 0,
                                  "imported": 0, "duplicates": 0, "quarantined": 0,
                                  "skipped": 0, "failed": 0, "errors": []}
        completed_studies: set[str] = set()
        allowed_extensions = {".dcm", ".dicom", ".ima", ""}

        def process(source_factory) -> None:
            report["scanned"] += 1
            try:
                with source_factory() as dicom_file:
                    dataset = pydicom.dcmread(dicom_file, stop_before_pixels=True,
                                              force=True, specific_tags=self._dicom_import_tags())
                sop_class = _text(getattr(dataset, "SOPClassUID", None))
                if not sop_class and getattr(dataset, "file_meta", None):
                    sop_class = _text(getattr(dataset.file_meta, "MediaStorageSOPClassUID", None))
                if sop_class:
                    dataset.SOPClassUID = sop_class
                try:
                    _required_uid(dataset, "StudyInstanceUID")
                    _required_uid(dataset, "SeriesInstanceUID")
                    _required_uid(dataset, "SOPInstanceUID")
                except ValueError:
                    report["skipped"] += 1
                    return
                if not _text(getattr(dataset, "PatientID", None)):
                    report["skipped"] += 1
                    return
                outcome = self._index_imported_dataset(dataset, source_factory, quarantine_dir)
                report[outcome] = report.get(outcome, 0) + 1
                if outcome in {"imported", "duplicates"}:
                    study_uid = _text(getattr(dataset, "StudyInstanceUID", None))
                    if study_uid:
                        completed_studies.add(study_uid)
            except Exception as exc:
                report["failed"] += 1
                if len(report["errors"]) < 10:
                    report["errors"].append(str(exc)[:240])
                logger.warning("Échec de l’importation d’un objet DICOM PACS : %s", exc)

        if source_type == "folder":
            for root, directories, filenames in os.walk(source, followlinks=False):
                directories[:] = [name for name in directories if not (Path(root) / name).is_symlink()]
                for filename in filenames:
                    file_path = Path(root) / filename
                    if file_path.suffix.lower() not in allowed_extensions or file_path.is_symlink():
                        continue
                    process(lambda file_path=file_path: file_path.open("rb"))
        else:
            with zipfile.ZipFile(source, "r") as archive:
                entries = [entry for entry in archive.infolist() if not entry.is_dir()
                           and Path(entry.filename).suffix.lower() in allowed_extensions]
                total_size = sum(entry.file_size for entry in entries)
                if len(entries) > 250_000 or total_size > 100 * 1024 ** 3:
                    raise ValueError("Archive trop volumineuse : maximum 250 000 fichiers et 100 Go décompressés.")
                for entry in entries:
                    process(lambda entry=entry: archive.open(entry, "r"))
        if completed_studies:
            with get_db_cursor() as cursor:
                cursor.execute("""UPDATE pacs_studies SET status='COMPLETED', updated_at=CURRENT_TIMESTAMP
                    WHERE receiver_node=%s AND study_instance_uid=ANY(%s) AND status='RECEIVING'""",
                    (self._node, list(completed_studies)))
        return report

