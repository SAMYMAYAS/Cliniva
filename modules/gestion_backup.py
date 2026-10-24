"""Sauvegarde complète de Cliniva : fichiers, PostgreSQL et données DICOM."""

from __future__ import annotations

import json
import logging
import os
import signal
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.auth import get_current_user_optional
from database import DB_CONFIG

logger = logging.getLogger("ClinivaBackup")
backup_router = APIRouter(prefix="/api/backup", tags=["Sauvegarde complète"])
PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".cliniva")) / "Cliniva"
SETTINGS_FILE = APP_DATA_DIR / "backup_settings.json"
_JOB_LOCK = threading.RLock()
_PAUSE_EVENT = threading.Event()
_PAUSE_EVENT.set()
_PG_PROCESS_LOCK = threading.RLock()
_PG_PROCESS: subprocess.Popen | None = None
_PG_PROCESS_SUSPENDED = False
_JOB: dict[str, Any] = {
    "id": None, "state": "idle", "percent": 0, "message": "",
    "stage": "", "speed_bps": 0, "stage_bytes": 0,
    "stage_total_bytes": 0, "current_file": "",
}
_SETTINGS_LOCK = threading.RLock()
_SCHEDULER_STOP = threading.Event()
_SCHEDULER_THREAD: threading.Thread | None = None
_EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def _require_admin(request: Request) -> dict[str, Any]:
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Session expirée. Veuillez vous reconnecter.")
    if user.get("role") != "Administrateur":
        raise HTTPException(status_code=403, detail="La sauvegarde complète est réservée à l’administrateur.")
    return user


def _default_destination() -> str:
    return str(Path.home() / "Cliniva_Backups")


def _read_settings() -> dict[str, Any]:
    settings: dict[str, Any] = {
        "destination": _default_destination(),
        "schedule_enabled": False,
        "schedules": [],
    }
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            settings["destination"] = str(data.get("destination") or settings["destination"])
            settings["schedule_enabled"] = bool(data.get("schedule_enabled", False))
            if isinstance(data.get("schedules"), list):
                settings["schedules"] = [
                    item for item in data["schedules"]
                    if isinstance(item, dict) and item.get("id") and item.get("time")
                ]
    except (OSError, ValueError):
        pass
    return settings


def _write_settings(settings: dict[str, Any]) -> None:
    with _SETTINGS_LOCK:
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        temporary = SETTINGS_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, SETTINGS_FILE)


def _set_schedule_run(schedule_id: str | None, *, run_at: str, status: str, message: str = "") -> None:
    if not schedule_id:
        return
    with _SETTINGS_LOCK:
        settings = _read_settings()
        for schedule in settings.get("schedules", []):
            if str(schedule.get("id")) == schedule_id:
                schedule["last_run_at"] = run_at
                schedule["last_run_status"] = status
                schedule["last_run_message"] = message
                break
        _write_settings(settings)


def _launch_backup(destination: Path, schedule_id: str | None = None) -> str:
    global _JOB
    with _JOB_LOCK:
        if _JOB.get("state") in {"running", "paused"}:
            raise RuntimeError("Une sauvegarde complète est déjà en cours.")
        job_id = uuid.uuid4().hex
        _PAUSE_EVENT.set()
        _JOB = {"id": job_id, "state": "running", "percent": 1,
                "message": "Démarrage de la sauvegarde automatique…" if schedule_id else "Démarrage de la sauvegarde…",
                "output": None, "schedule_id": schedule_id, "stage": "Préparation",
                "speed_bps": 0, "stage_bytes": 0, "stage_total_bytes": 0, "current_file": ""}
    try:
        if schedule_id:
            _set_schedule_run(schedule_id, run_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                              status="running", message="Sauvegarde en cours")
        threading.Thread(target=_run_backup, args=(job_id, destination, schedule_id),
                         name="ClinivaFullBackup", daemon=True).start()
    except Exception:
        with _JOB_LOCK:
            if _JOB.get("id") == job_id:
                _JOB.update(state="failed", message="Impossible de démarrer la sauvegarde.")
        raise
    return job_id


def _backup_scheduler_loop() -> None:
    logger.info("Planificateur de sauvegarde Cliniva démarré.")
    while not _SCHEDULER_STOP.wait(20):
        now = datetime.now()
        today = now.date().isoformat()
        current_minute = now.strftime("%H:%M")
        settings = _read_settings()
        if not settings.get("schedule_enabled"):
            continue
        due = [
            item for item in settings.get("schedules", [])
            if item.get("enabled", True)
            and str(item.get("time") or "") == current_minute
            and str(item.get("last_run_at") or "")[:10] != today
        ]
        for schedule in due:
            with _JOB_LOCK:
                backup_running = _JOB.get("state") in {"running", "paused"}
            if backup_running:
                _set_schedule_run(str(schedule["id"]), run_at=now.astimezone().isoformat(timespec="seconds"),
                                  status="skipped", message="Ignorée : une autre sauvegarde était en cours")
                continue
            try:
                destination = Path(str(settings["destination"])).expanduser().resolve()
                _launch_backup(destination, str(schedule["id"]))
            except Exception as exc:
                logger.exception("Impossible de lancer la sauvegarde planifiée %s", schedule.get("time"))
                _set_schedule_run(str(schedule["id"]), run_at=now.astimezone().isoformat(timespec="seconds"),
                                  status="failed", message=str(exc))


def start_backup_scheduler() -> None:
    """Démarre le planificateur horaire dans le processus serveur Cliniva."""
    global _SCHEDULER_THREAD
    if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
        return
    _SCHEDULER_STOP.clear()
    _SCHEDULER_THREAD = threading.Thread(target=_backup_scheduler_loop,
                                         name="ClinivaBackupScheduler", daemon=True)
    _SCHEDULER_THREAD.start()


def stop_backup_scheduler() -> None:
    _SCHEDULER_STOP.set()
    if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
        _SCHEDULER_THREAD.join(timeout=2)


def _pg_dump_executable() -> str | None:
    found = shutil.which("pg_dump")
    if found:
        return found
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    candidates = sorted(program_files.glob("PostgreSQL/*/bin/pg_dump.exe"), reverse=True)
    return str(candidates[0]) if candidates else None


def _configured_external_roots() -> list[tuple[str, Path]]:
    config_path = Path.cwd() / "config.json"
    if not config_path.is_file():
        config_path = PROJECT_ROOT / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    roots: list[tuple[str, Path]] = []
    for key, label in (("pacs_config", "pacs_data"), ("dicom_print_config", "dicom_print")):
        item = config.get(key) if isinstance(config, dict) else None
        path_key = "data_dir" if key == "pacs_config" else "storage_dir"
        if isinstance(item, dict):
            value = item.get(path_key)
        elif key == "pacs_config":
            local_appdata = Path(os.environ.get("LOCALAPPDATA") or Path.home())
            value = str(local_appdata / "Cliniva" / "PACS")
        else:
            value = str(Path.home() / "DicomPrints")
        if not value:
            local_appdata = Path(os.environ.get("LOCALAPPDATA") or Path.home())
            value = str(local_appdata / "Cliniva" / "PACS") if key == "pacs_config" else str(Path.home() / "DicomPrints")
        if value:
            try:
                path = Path(str(value)).expanduser().resolve()
                if path.is_dir() and path != PROJECT_ROOT and PROJECT_ROOT not in path.parents:
                    roots.append((label, path))
            except (OSError, RuntimeError):
                logger.warning("Chemin de données DICOM ignoré pendant la sauvegarde : %s", value)
    unique: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for label, path in roots:
        key = os.path.normcase(str(path))
        if key not in seen:
            unique.append((label, path))
            seen.add(key)
    return unique


def _iter_files(root: Path, project_root: bool = False):
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = [name for name in directories
                          if name not in _EXCLUDED_DIRS
                          and not (current_path / name).is_symlink()]
        for filename in filenames:
            path = current_path / filename
            if path.is_symlink():
                continue
            yield path


def _control_pg_dump_process(*, pause: bool) -> None:
    global _PG_PROCESS_SUSPENDED
    with _PG_PROCESS_LOCK:
        process = _PG_PROCESS
        if process is None or process.poll() is not None:
            _PG_PROCESS_SUSPENDED = False
            return
        if pause == _PG_PROCESS_SUSPENDED:
            return
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            ntdll = ctypes.WinDLL("ntdll")
            kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            native_call = ntdll.NtSuspendProcess if pause else ntdll.NtResumeProcess
            native_call.argtypes = [ctypes.c_void_p]
            native_call.restype = ctypes.c_long
            handle = kernel32.OpenProcess(0x0800, False, process.pid)  # PROCESS_SUSPEND_RESUME
            if not handle:
                raise OSError(ctypes.get_last_error(), "Impossible d’accéder au processus pg_dump.")
            try:
                status = native_call(handle)
                if status != 0:
                    raise OSError(f"Windows n’a pas pu {'suspendre' if pause else 'reprendre'} pg_dump (NTSTATUS {status:#x}).")
            finally:
                kernel32.CloseHandle(handle)
        else:
            os.kill(process.pid, signal.SIGSTOP if pause else signal.SIGCONT)
        _PG_PROCESS_SUSPENDED = pause


def _wait_if_paused() -> None:
    while not _PAUSE_EVENT.wait(0.25):
        with _JOB_LOCK:
            if _JOB.get("state") == "paused":
                _JOB["speed_bps"] = 0


def _copy_tree(root: Path, target: Path, project_root: bool, progress: dict[str, Any], total_bytes: int,
               stage: str, stage_start_bytes: int, stage_total_bytes: int) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for source in _iter_files(root, project_root=project_root):
        relative = source.relative_to(root)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        file_size = source.stat().st_size
        progress["current_file"] = str(relative)
        with source.open("rb") as src, destination.open("wb") as dst:
            while True:
                _wait_if_paused()
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
                progress["copied_bytes"] += len(chunk)
                now = time.monotonic()
                elapsed = now - progress["speed_sample_at"]
                if elapsed >= 0.5:
                    progress["speed_bps"] = int((progress["copied_bytes"] - progress["speed_sample_bytes"]) / elapsed)
                    progress["speed_sample_at"] = now
                    progress["speed_sample_bytes"] = progress["copied_bytes"]
                file_percent = 8 + int(62 * progress["copied_bytes"] / max(total_bytes, 1))
                with _JOB_LOCK:
                    _JOB.update(
                        percent=min(file_percent, 70), stage=stage,
                        message=f"Copie : {relative}", speed_bps=progress["speed_bps"],
                        stage_bytes=progress["copied_bytes"] - stage_start_bytes,
                        stage_total_bytes=stage_total_bytes, current_file=str(relative),
                    )
        shutil.copystat(source, destination)


def _run_backup(job_id: str, destination: Path, schedule_id: str | None = None) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_dir = destination / f"Cliniva_FullBackup_{stamp}"
    partial_dir = destination / f".Cliniva_FullBackup_{stamp}_{job_id[:8]}.partial"
    try:
        external_roots = _configured_external_roots()
        source_roots = [("application", PROJECT_ROOT, True)] + [
            (label, path, False) for label, path in external_roots
        ]
        for _, source, _ in source_roots:
            if destination == source or source in destination.parents:
                raise ValueError("Choisissez un répertoire de sauvegarde situé hors des données à sauvegarder.")
        destination.mkdir(parents=True, exist_ok=True)
        if not destination.is_dir():
            raise OSError("Le répertoire de destination n’est pas accessible.")
        planned_files = [(label, source, project) for label, root, project in source_roots
                         for source in _iter_files(root, project_root=project)]
        total_bytes = sum(path.stat().st_size for _, path, _ in planned_files)
        if partial_dir.exists() or final_dir.exists():
            raise FileExistsError("Un dossier de sauvegarde portant ce nom existe déjà.")
        partial_dir.mkdir(parents=True)
        with _JOB_LOCK:
            _JOB.update(state="running", percent=5, stage="Préparation", speed_bps=0,
                        stage_bytes=0, stage_total_bytes=total_bytes, current_file="",
                        message="Préparation de la sauvegarde complète…")

        copied = {"copied_bytes": 0, "speed_bps": 0,
                  "speed_sample_at": time.monotonic(), "speed_sample_bytes": 0}
        for label, source, project in source_roots:
            target = partial_dir / ("application" if project else f"dicom/{label}")
            stage = "Fichiers Cliniva" if project else ("Données PACS" if label == "pacs_data" else "Images DICOM Print")
            stage_total = sum(path.stat().st_size for path in _iter_files(source, project_root=project))
            _copy_tree(source, target, project, copied, total_bytes, stage, copied["copied_bytes"], stage_total)

        with _JOB_LOCK:
            _JOB.update(percent=72, stage="Base PostgreSQL", stage_bytes=0,
                        stage_total_bytes=0, current_file="", speed_bps=0,
                        message="Export de la base PostgreSQL…")
        pg_dump = _pg_dump_executable()
        if not pg_dump:
            raise RuntimeError("pg_dump est introuvable. Installez les outils en ligne de commande PostgreSQL sur le serveur.")
        database_dir = partial_dir / "database"
        database_dir.mkdir()
        dump_path = database_dir / f"{DB_CONFIG['database']}.dump"
        env = os.environ.copy()
        env["PGPASSWORD"] = str(DB_CONFIG.get("password", ""))
        command = [pg_dump, "--no-password", "--format=custom", "--no-owner", "--no-acl",
                   "--host", str(DB_CONFIG.get("host", "localhost")),
                   "--port", str(DB_CONFIG.get("port", 5432)),
                   "--username", str(DB_CONFIG.get("user", "postgres")),
                   "--dbname", str(DB_CONFIG["database"]), "--file", str(dump_path)]
        error_log = database_dir / "pg_dump_error.log"
        started = time.monotonic()
        sample_at = started
        sample_size = 0
        global _PG_PROCESS, _PG_PROCESS_SUSPENDED
        with error_log.open("w", encoding="utf-8") as stderr_file:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=stderr_file, env=env,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with _PG_PROCESS_LOCK:
                _PG_PROCESS = process
                _PG_PROCESS_SUSPENDED = False
                if not _PAUSE_EVENT.is_set():
                    _control_pg_dump_process(pause=True)
            while process.poll() is None:
                _wait_if_paused()
                time.sleep(0.5)
                current_size = dump_path.stat().st_size if dump_path.exists() else 0
                now = time.monotonic()
                if now - sample_at >= 0.5:
                    speed = int((current_size - sample_size) / (now - sample_at))
                    sample_at, sample_size = now, current_size
                else:
                    speed = 0
                with _JOB_LOCK:
                    _JOB.update(percent=min(94, 72 + int(min(current_size / (1024 * 1024 * 100), 1) * 22)),
                                stage="Base PostgreSQL", stage_bytes=current_size,
                                stage_total_bytes=0, speed_bps=speed,
                                message=f"Export PostgreSQL : {current_size / (1024 * 1024):.1f} Mo générés")
            return_code = process.wait()
            with _PG_PROCESS_LOCK:
                _PG_PROCESS = None
                _PG_PROCESS_SUSPENDED = False
        _wait_if_paused()
        if return_code:
            details = error_log.read_text(encoding="utf-8", errors="replace").strip()
            error_log.unlink(missing_ok=True)
            raise RuntimeError("Échec de pg_dump : " + (details or "erreur PostgreSQL inconnue"))
        error_log.unlink(missing_ok=True)

        _wait_if_paused()
        with _JOB_LOCK:
            _JOB.update(percent=97, stage="Finalisation", speed_bps=0,
                        stage_bytes=0, stage_total_bytes=0, current_file="",
                        message="Finalisation et vérification des fichiers…")
        manifest = {
            "application": "Cliniva",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "backup_size_bytes": sum(path.stat().st_size for _, path, _ in planned_files) + dump_path.stat().st_size,
            "database": {"engine": "PostgreSQL", "name": DB_CONFIG["database"],
                         "host": DB_CONFIG.get("host"), "port": DB_CONFIG.get("port"),
                         "dump": f"database/{dump_path.name}"},
            "included": ["Fichiers de l’application et uploads", "Base PostgreSQL", "Données PACS/DICOM Print configurées"],
            "external_data_roots": [{"label": label, "source": str(path)} for label, path in external_roots],
            "excluded_generated_directories": sorted(_EXCLUDED_DIRS),
            "database_credentials_included": False,
        }
        (partial_dir / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        partial_dir.rename(final_dir)
        with _JOB_LOCK:
            _JOB.update(state="completed", percent=100, stage="Terminée", speed_bps=0,
                        stage_bytes=0, stage_total_bytes=0, current_file="",
                        message="Sauvegarde complète terminée.",
                        output=str(final_dir), bytes=sum(p.stat().st_size for p in _iter_files(final_dir)))
        _set_schedule_run(schedule_id, run_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                          status="completed", message="Sauvegarde terminée")
        logger.info("Sauvegarde complète Cliniva terminée : %s", final_dir)
    except Exception as exc:
        logger.exception("Échec de la sauvegarde complète Cliniva")
        with _JOB_LOCK:
            _JOB.update(state="failed", stage="Échec", speed_bps=0, message=str(exc),
                        output=str(partial_dir) if partial_dir.exists() else None)
        _set_schedule_run(schedule_id, run_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                          status="failed", message=str(exc))


@backup_router.get("/settings")
async def get_backup_settings(request: Request):
    _require_admin(request)
    return _read_settings()


@backup_router.post("/settings")
async def save_backup_settings(request: Request):
    _require_admin(request)
    payload = await request.json()
    raw_destination = str(payload.get("destination", "")).strip()
    if not raw_destination:
        raise HTTPException(status_code=400, detail="Indiquez le répertoire de destination.")
    destination = Path(raw_destination).expanduser()
    if not destination.is_absolute():
        raise HTTPException(status_code=400, detail="Le répertoire doit être un chemin absolu accessible depuis le serveur.")
    destination = destination.resolve()
    try:
        for source in [PROJECT_ROOT, *[path for _, path in _configured_external_roots()]]:
            if destination == source or source in destination.parents:
                raise HTTPException(status_code=400, detail="Le dossier de sauvegarde doit être hors des répertoires source.")
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Chemin invalide : {exc}") from exc
    settings = _read_settings()
    settings["destination"] = str(destination)
    _write_settings(settings)
    return {"ok": True, "destination": str(destination)}


@backup_router.post("/schedule")
async def save_backup_schedule(request: Request):
    _require_admin(request)
    payload = await request.json()
    raw_schedules = payload.get("schedules", [])
    if not isinstance(raw_schedules, list) or len(raw_schedules) > 24:
        raise HTTPException(status_code=400, detail="Ajoutez au maximum 24 horaires de sauvegarde.")
    settings = _read_settings()
    previous = {str(item.get("id")): item for item in settings.get("schedules", []) if item.get("id")}
    schedules: list[dict[str, Any]] = []
    seen_times: set[str] = set()
    for item in raw_schedules:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="Un horaire de sauvegarde est invalide.")
        time_value = str(item.get("time") or "").strip()
        try:
            datetime.strptime(time_value, "%H:%M")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Heure invalide : {time_value or '(vide)'}.") from exc
        if time_value in seen_times:
            raise HTTPException(status_code=400, detail=f"L’heure {time_value} est déjà dans la liste.")
        seen_times.add(time_value)
        schedule_id = str(item.get("id") or uuid.uuid4().hex)
        old = previous.get(schedule_id, {})
        schedules.append({
            "id": schedule_id,
            "time": time_value,
            "enabled": bool(item.get("enabled", True)),
            "last_run_at": old.get("last_run_at"),
            "last_run_status": old.get("last_run_status", "never"),
            "last_run_message": old.get("last_run_message", "Jamais exécutée"),
        })
    settings["schedule_enabled"] = bool(payload.get("enabled", False))
    settings["schedules"] = sorted(schedules, key=lambda item: item["time"])
    _write_settings(settings)
    return {"ok": True, "schedule_enabled": settings["schedule_enabled"], "schedules": settings["schedules"]}


@backup_router.post("/start")
async def start_full_backup(request: Request):
    _require_admin(request)
    settings = _read_settings()
    destination = Path(settings["destination"]).expanduser().resolve()
    try:
        job_id = _launch_backup(destination)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "job_id": job_id}


@backup_router.get("/status/{job_id}")
async def full_backup_status(job_id: str, request: Request):
    _require_admin(request)
    with _JOB_LOCK:
        if _JOB.get("id") != job_id:
            raise HTTPException(status_code=404, detail="Sauvegarde introuvable.")
        return dict(_JOB)


@backup_router.get("/current")
async def current_full_backup(request: Request):
    _require_admin(request)
    with _JOB_LOCK:
        return dict(_JOB)


@backup_router.post("/pause/{job_id}")
async def pause_full_backup(job_id: str, request: Request):
    _require_admin(request)
    with _JOB_LOCK:
        if _JOB.get("id") != job_id or _JOB.get("state") != "running":
            raise HTTPException(status_code=409, detail="Cette sauvegarde ne peut pas être mise en pause.")
    _PAUSE_EVENT.clear()
    try:
        _control_pg_dump_process(pause=True)
    except Exception as exc:
        _PAUSE_EVENT.set()
        raise HTTPException(status_code=500, detail=f"Impossible de mettre la sauvegarde en pause : {exc}") from exc
    with _JOB_LOCK:
        if _JOB.get("id") != job_id or _JOB.get("state") != "running":
            _PAUSE_EVENT.set()
            try:
                _control_pg_dump_process(pause=False)
            except Exception:
                logger.exception("Impossible de reprendre pg_dump après une pause devenue obsolète")
            raise HTTPException(status_code=409, detail="La sauvegarde a déjà changé d’état.")
        _JOB["resume_message"] = _JOB.get("message", "Sauvegarde en cours")
        _JOB.update(state="paused", message="Sauvegarde en pause — progression conservée.", speed_bps=0)
        return dict(_JOB)


@backup_router.post("/resume/{job_id}")
async def resume_full_backup(job_id: str, request: Request):
    _require_admin(request)
    with _JOB_LOCK:
        if _JOB.get("id") != job_id or _JOB.get("state") != "paused":
            raise HTTPException(status_code=409, detail="Cette sauvegarde n’est pas en pause.")
    try:
        _control_pg_dump_process(pause=False)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Impossible de reprendre la sauvegarde : {exc}") from exc
    _PAUSE_EVENT.set()
    with _JOB_LOCK:
        if _JOB.get("id") == job_id and _JOB.get("state") == "paused":
            message = _JOB.pop("resume_message", "Sauvegarde reprise.")
            _JOB.update(state="running", message=message)
        return dict(_JOB)


@backup_router.get("/history")
async def full_backup_history(request: Request):
    _require_admin(request)
    destination = Path(_read_settings()["destination"]).expanduser()
    if not destination.is_dir():
        return {"items": [], "destination": str(destination)}
    items: list[dict[str, Any]] = []
    try:
        folders = sorted(
            (path for path in destination.iterdir()
             if path.is_dir() and path.name.startswith("Cliniva_FullBackup_")),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for folder in folders[:100]:
            manifest_path = folder / "MANIFEST.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                database = manifest.get("database") or {}
                dump_relative = str(database.get("dump") or "")
                dump_path = (folder / dump_relative).resolve() if dump_relative else None
                valid_dump = bool(dump_path and folder.resolve() in dump_path.parents and dump_path.is_file())
                created_at = manifest.get("created_at") or datetime.fromtimestamp(
                    folder.stat().st_mtime).astimezone().isoformat(timespec="seconds")
                size_bytes = manifest.get("backup_size_bytes")
                items.append({
                    "name": folder.name,
                    "created_at": created_at,
                    "status": "completed" if valid_dump else "incomplete",
                    "size_bytes": int(size_bytes) if size_bytes is not None else None,
                    "database": str(database.get("name") or "PostgreSQL"),
                    "path": str(folder),
                })
            except (OSError, ValueError, TypeError):
                items.append({
                    "name": folder.name,
                    "created_at": datetime.fromtimestamp(folder.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                    "status": "incomplete", "size_bytes": None,
                    "database": "—", "path": str(folder),
                })
    except OSError as exc:
        logger.warning("Lecture de l’historique des sauvegardes impossible : %s", exc)
        raise HTTPException(status_code=500, detail="Impossible de lire le répertoire des sauvegardes.") from exc
    return {"items": items, "destination": str(destination)}


def render_html(user: dict[str, Any] | None = None) -> str:
    if not user or user.get("role") != "Administrateur":
        raise HTTPException(status_code=403, detail="La sauvegarde complète est réservée à l’administrateur.")
    return r"""
<section class="space-y-6">
  <header class="flex flex-wrap items-center justify-between gap-4 rounded-[2rem] bg-gradient-to-r from-[#101b13] to-[#183b20] px-7 py-6 text-white shadow-lg">
    <div><p class="text-xs font-bold uppercase tracking-[.18em] text-lime-300">Administration · Protection des données</p><h1 class="mt-1 text-2xl font-extrabold">Sauvegarde complète</h1><p class="mt-1 text-sm text-slate-300">Application, base PostgreSQL, dossiers patients et stockage DICOM.</p></div>
    <span class="grid h-14 w-14 place-items-center rounded-2xl bg-white/10 text-lime-300"><i class="ph-bold ph-shield-check text-3xl"></i></span>
  </header>
  <article class="rounded-[1.75rem] border border-slate-200 bg-white p-6 shadow-sm">
    <div class="mb-5 flex items-center gap-3"><span class="grid h-11 w-11 place-items-center rounded-xl bg-emerald-50 text-emerald-700"><i class="ph-bold ph-hard-drive text-xl"></i></span><div><h2 class="text-lg font-extrabold text-slate-800">Répertoire de sauvegarde</h2><p class="text-xs text-slate-500">Le chemin doit être accessible depuis le serveur qui exécute Cliniva et PostgreSQL.</p></div></div>
    <label for="backupDestination" class="block text-xs font-bold text-slate-600">Chemin absolu de destination</label>
    <div class="mt-2 flex flex-col gap-3 sm:flex-row"><input id="backupDestination" type="text" spellcheck="false" placeholder="Ex. D:\Cliniva_Backups ou \\NAS\Cliniva_Backups" class="min-w-0 flex-1 rounded-xl border border-slate-200 px-4 py-3 text-sm outline-none focus:ring-2 focus:ring-lime-200"><button id="backupSavePath" type="button" class="rounded-xl border border-slate-200 px-4 py-3 text-sm font-bold text-slate-700 hover:bg-slate-50"><i class="ph-bold ph-floppy-disk mr-1"></i>Enregistrer le chemin</button></div>
    <div class="mt-5 grid gap-3 md:grid-cols-3"><div class="rounded-2xl bg-slate-50 p-4"><i class="ph-bold ph-code text-xl text-emerald-700"></i><p class="mt-2 text-sm font-extrabold text-slate-800">Fichiers Cliniva</p><p class="mt-1 text-xs text-slate-500">Code, modèles, configuration, documents et uploads.</p></div><div class="rounded-2xl bg-slate-50 p-4"><i class="ph-bold ph-database text-xl text-indigo-600"></i><p class="mt-2 text-sm font-extrabold text-slate-800">Base PostgreSQL</p><p class="mt-1 text-xs text-slate-500">Export complet restaurable avec pg_restore.</p></div><div class="rounded-2xl bg-slate-50 p-4"><i class="ph-bold ph-images text-xl text-amber-600"></i><p class="mt-2 text-sm font-extrabold text-slate-800">Images DICOM</p><p class="mt-1 text-xs text-slate-500">Stockage PACS et réceptions DICOM Print configurés.</p></div></div>
    <div class="mt-5 flex flex-wrap items-center gap-3"><button id="backupStart" type="button" class="rounded-xl bg-emerald-700 px-5 py-3 text-sm font-extrabold text-white shadow-sm hover:bg-emerald-800"><i class="ph-bold ph-shield-check mr-2"></i>Lancer la sauvegarde complète</button><span class="text-xs text-slate-500">L’opération s’effectue en arrière-plan; les fichiers source ne sont pas modifiés.</span></div>
    <div id="backupProgressWrap" class="mt-6 hidden rounded-2xl border border-slate-200 bg-slate-50 p-4"><div class="flex flex-wrap items-center justify-between gap-3"><p id="backupMessage" class="min-w-0 flex-1 truncate text-sm font-bold text-slate-700">Préparation…</p><div class="flex shrink-0 items-center gap-2"><button id="backupPause" type="button" class="hidden rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-xs font-bold text-amber-800 hover:bg-amber-100"><i class="ph-bold ph-pause mr-1"></i>Mettre en pause</button><button id="backupResume" type="button" class="hidden rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-1.5 text-xs font-bold text-emerald-800 hover:bg-emerald-100"><i class="ph-bold ph-play mr-1"></i>Continuer</button><span id="backupPercent" class="text-sm font-extrabold text-emerald-700">0 %</span></div></div><div class="mt-3 h-2.5 overflow-hidden rounded-full bg-slate-200"><div id="backupProgressBar" class="h-full w-0 rounded-full bg-gradient-to-r from-lime-400 to-emerald-600 transition-all duration-300"></div></div><div class="mt-4 grid gap-3 sm:grid-cols-3"><div class="rounded-xl border border-slate-200 bg-white px-3 py-2"><p class="text-[10px] font-bold uppercase tracking-wide text-slate-400">Étape</p><p id="backupStage" class="mt-1 truncate text-xs font-bold text-slate-700">En attente</p></div><div class="rounded-xl border border-slate-200 bg-white px-3 py-2"><p class="text-[10px] font-bold uppercase tracking-wide text-slate-400">Débit instantané</p><p id="backupSpeed" class="mt-1 text-xs font-bold text-emerald-700">0 Ko/s</p></div><div class="rounded-xl border border-slate-200 bg-white px-3 py-2"><p class="text-[10px] font-bold uppercase tracking-wide text-slate-400">Transféré dans l’étape</p><p id="backupTransferStats" class="mt-1 text-xs font-bold text-slate-700">0 octet</p></div></div><p id="backupCurrentFile" class="mt-3 truncate text-xs text-slate-500" title=""></p><p id="backupOutput" class="mt-2 break-all text-xs text-slate-500"></p></div>
    <div class="mt-5 rounded-xl border border-amber-200 bg-amber-50 p-4 text-xs leading-relaxed text-amber-900"><b>À protéger :</b> la sauvegarde contient des données médicales et des images DICOM. Choisissez un disque ou un partage sécurisé avec suffisamment d’espace. Pour exporter la base, l’outil PostgreSQL <code>pg_dump</code> doit être installé sur le serveur.</div>
  </article>
  <article class="rounded-[1.75rem] border border-slate-200 bg-white p-6 shadow-sm">
    <div class="mb-5 flex flex-wrap items-center justify-between gap-3"><div class="flex items-center gap-3"><span class="grid h-11 w-11 place-items-center rounded-xl bg-indigo-50 text-indigo-700"><i class="ph-bold ph-clock-counter-clockwise text-xl"></i></span><div><h2 class="text-lg font-extrabold text-slate-800">Historique des sauvegardes</h2><p class="text-xs text-slate-500">Les 100 dernières sauvegardes présentes dans le répertoire configuré.</p></div></div><button id="backupHistoryRefresh" type="button" class="rounded-xl border border-slate-200 px-4 py-2.5 text-sm font-bold text-slate-700 hover:bg-slate-50"><i class="ph-bold ph-arrows-clockwise mr-1"></i>Actualiser</button></div>
    <p id="backupHistoryLocation" class="mb-3 break-all text-[11px] text-slate-400"></p>
    <div class="overflow-x-auto rounded-2xl border border-slate-200"><table class="w-full min-w-[760px] border-collapse text-left text-sm"><thead class="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-500"><tr><th class="px-4 py-3">Date et heure</th><th class="px-4 py-3">État</th><th class="px-4 py-3">Taille</th><th class="px-4 py-3">Base</th><th class="px-4 py-3">Emplacement</th></tr></thead><tbody id="backupHistoryRows" class="divide-y divide-slate-100"><tr><td colspan="5" class="px-4 py-6 text-center text-xs text-slate-400">Chargement de l’historique…</td></tr></tbody></table></div>
  </article>
  <article class="rounded-[1.75rem] border border-slate-200 bg-white p-6 shadow-sm">
    <div class="flex flex-wrap items-center justify-between gap-4">
      <div><p class="text-xs font-bold uppercase tracking-[.12em] text-emerald-700">Sans intervention quotidienne</p><h2 class="mt-1 text-lg font-extrabold text-slate-800">Planification automatique</h2><p class="mt-1 text-xs text-slate-500">Ajoute une ligne par heure souhaitée. Chaque horaire actif lance une sauvegarde complète chaque jour.</p></div>
      <label class="inline-flex cursor-pointer items-center gap-3 rounded-xl border border-slate-200 px-4 py-3 text-sm font-bold text-slate-700"><input id="backupScheduleEnabled" type="checkbox" class="h-4 w-4 accent-emerald-700"><span>Planification activée</span></label>
    </div>
    <div class="mt-5 flex flex-wrap items-end gap-3 rounded-2xl bg-slate-50 p-4">
      <label class="flex flex-col text-xs font-bold text-slate-600">Heure de sauvegarde<span class="mt-2 flex h-11 items-center gap-2"><input id="backupScheduleTime" type="time" lang="fr-FR" class="box-border h-11 rounded-xl border border-slate-200 bg-white px-3 text-sm text-slate-800"><span id="backupSchedulePeriod" class="min-w-[88px] text-[10px] font-semibold text-emerald-700">Format 24 h</span></span></label>
      <button id="backupScheduleAdd" type="button" class="inline-flex h-11 items-center justify-center rounded-xl bg-emerald-700 px-4 text-sm font-bold text-white hover:bg-emerald-800"><i class="ph-bold ph-plus mr-1"></i>Ajouter l’heure</button>
      <span id="backupScheduleNotice" class="max-w-xl text-xs text-slate-500">Cliniva doit rester démarré sur le serveur. Une seule sauvegarde complète peut tourner à la fois : un horaire atteint pendant une sauvegarde sera indiqué comme ignoré.</span>
    </div>
    <div class="mt-5 overflow-x-auto rounded-2xl border border-slate-200">
      <table class="w-full min-w-[680px] border-collapse text-left text-sm">
        <thead class="bg-slate-50 text-[10px] uppercase tracking-wider text-slate-500"><tr><th class="px-4 py-3">Heure</th><th class="px-4 py-3">Fréquence</th><th class="px-4 py-3">État</th><th class="px-4 py-3">Dernière exécution</th><th class="px-4 py-3 text-right">Action</th></tr></thead>
        <tbody id="backupScheduleRows" class="divide-y divide-slate-100"><tr><td colspan="5" class="px-4 py-6 text-center text-xs text-slate-400">Aucun horaire configuré.</td></tr></tbody>
      </table>
    </div>
    <div class="mt-4 flex flex-wrap items-center justify-between gap-3"><p class="text-xs text-slate-500">Les sauvegardes manuelles restent disponibles à tout moment.</p><button id="backupScheduleSave" type="button" class="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-sm font-bold text-emerald-800 hover:bg-emerald-100"><i class="ph-bold ph-floppy-disk mr-1"></i>Enregistrer la planification</button></div>
  </article>
</section>
<script>
(()=>{
  const $=id=>document.getElementById(id);let currentJob=null,pollTimer=null,schedules=[];
  async function api(url,options={}){const response=await fetch(url,{headers:{'Content-Type':'application/json'},...options});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.detail||'Erreur de communication avec le serveur.');return data;}
  function formatBytes(value){const bytes=Math.max(0,Number(value)||0);if(bytes<1024)return bytes+' octet';if(bytes<1024*1024)return (bytes/1024).toFixed(1)+' Ko';if(bytes<1024*1024*1024)return (bytes/1024/1024).toFixed(1)+' Mo';return (bytes/1024/1024/1024).toFixed(2)+' Go';}
  function escapeHtml(value){return String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));}
  async function loadHistory(){const body=$('backupHistoryRows');body.innerHTML='<tr><td colspan="5" class="px-4 py-6 text-center text-xs text-slate-400">Chargement de l’historique…</td></tr>';try{const result=await api('/api/backup/history');$('backupHistoryLocation').textContent='Répertoire : '+(result.destination||'');const items=Array.isArray(result.items)?result.items:[];if(!items.length){body.innerHTML='<tr><td colspan="5" class="px-4 py-6 text-center text-xs text-slate-400">Aucune sauvegarde trouvée dans ce répertoire.</td></tr>';return;}body.innerHTML=items.map(item=>{const complete=item.status==='completed';const badge=complete?'<span class="rounded-full bg-emerald-100 px-2.5 py-1 text-[10px] font-bold text-emerald-800">Terminée</span>':'<span class="rounded-full bg-amber-100 px-2.5 py-1 text-[10px] font-bold text-amber-800">Incomplète</span>';let date=item.created_at||'';const parsed=new Date(date);if(!Number.isNaN(parsed.getTime()))date=parsed.toLocaleString('fr-FR');return '<tr class="hover:bg-slate-50"><td class="whitespace-nowrap px-4 py-3 text-xs font-semibold text-slate-700">'+escapeHtml(date)+'</td><td class="px-4 py-3">'+badge+'</td><td class="whitespace-nowrap px-4 py-3 text-xs font-bold text-slate-700">'+(item.size_bytes==null?'—':escapeHtml(formatBytes(item.size_bytes)))+'</td><td class="px-4 py-3 text-xs text-slate-600">'+escapeHtml(item.database||'—')+'</td><td class="max-w-[360px] px-4 py-3 text-[11px] text-slate-500"><span class="block truncate" title="'+escapeHtml(item.path||'')+'">'+escapeHtml(item.path||item.name||'')+'</span></td></tr>';}).join('');}catch(error){body.innerHTML='<tr><td colspan="5" class="px-4 py-6 text-center text-xs font-semibold text-rose-600">'+escapeHtml(error.message)+'</td></tr>';}}
  function renderProgress(state){const speed=Math.max(0,Number(state.speed_bps)||0);$('backupStage').textContent=state.stage||'Préparation';$('backupSpeed').textContent=formatBytes(speed)+'/s';const transferred=Number(state.stage_bytes)||0,total=Number(state.stage_total_bytes)||0;$('backupTransferStats').textContent=formatBytes(transferred)+(total?' / '+formatBytes(total):'');const file=state.current_file||'';$('backupCurrentFile').textContent=file?(state.stage==='Base PostgreSQL'?'Export de la base PostgreSQL en cours':'Fichier : '+file):'';$('backupCurrentFile').title=file;$('backupPause').classList.toggle('hidden',state.state!=='running');$('backupResume').classList.toggle('hidden',state.state!=='paused');}
  function showError(message){$('backupProgressWrap').classList.remove('hidden');$('backupMessage').textContent=message;$('backupMessage').className='min-w-0 truncate text-sm font-bold text-rose-700';}
  async function poll(){if(!currentJob||!document.getElementById('backupProgressWrap'))return;try{const state=await api('/api/backup/status/'+encodeURIComponent(currentJob));if(!document.getElementById('backupProgressWrap'))return;$('backupProgressWrap').classList.remove('hidden');$('backupMessage').textContent=state.message||state.state;$('backupMessage').className='min-w-0 truncate text-sm font-bold '+(state.state==='failed'?'text-rose-700':'text-slate-700');const percent=Number(state.percent)||0;$('backupPercent').textContent=percent+' %';$('backupProgressBar').style.width=percent+'%';$('backupOutput').textContent=state.output?'Destination : '+state.output:'';renderProgress(state);if(state.state==='running'||state.state==='paused'){pollTimer=setTimeout(poll,1000);}else{$('backupStart').disabled=false;$('backupStart').classList.remove('opacity-60','cursor-not-allowed');currentJob=null;loadHistory();}}catch(error){if(document.getElementById('backupProgressWrap'))showError(error.message);$('backupStart')?.removeAttribute('disabled');currentJob=null;}}
  async function controlBackup(action){if(!currentJob)return;const button=action==='pause'?$('backupPause'):$('backupResume');button.disabled=true;try{await api('/api/backup/'+action+'/'+encodeURIComponent(currentJob),{method:'POST',body:'{}'});await poll();}catch(error){showError(error.message);}finally{button.disabled=false;}}
  $('backupPause').addEventListener('click',()=>controlBackup('pause'));
  $('backupResume').addEventListener('click',()=>controlBackup('resume'));
  $('backupSavePath').addEventListener('click',async()=>{try{const result=await api('/api/backup/settings',{method:'POST',body:JSON.stringify({destination:$('backupDestination').value})});$('backupDestination').value=result.destination;$('backupMessage').textContent='Répertoire de sauvegarde enregistré.';$('backupMessage').className='min-w-0 truncate text-sm font-bold text-emerald-700';$('backupProgressWrap').classList.remove('hidden');loadHistory();}catch(error){showError(error.message);}});
  $('backupHistoryRefresh').addEventListener('click',loadHistory);
  function timePeriod(value){if(!/^\d{2}:\d{2}$/.test(value||''))return 'Format 24 h';const hour=Number(value.slice(0,2));if(hour>=5&&hour<12)return 'Matin';if(hour>=12&&hour<18)return 'Après-midi';if(hour>=18&&hour<22)return 'Soir';return 'Nuit';}
  function updateSchedulePeriod(){const period=$('backupSchedulePeriod');if(period)period.textContent=timePeriod($('backupScheduleTime').value);}
  function renderSchedules(){const body=$('backupScheduleRows');if(!schedules.length){body.innerHTML='<tr><td colspan="5" class="px-4 py-6 text-center text-xs text-slate-400">Aucun horaire configuré.</td></tr>';return;}body.innerHTML=schedules.map((item,index)=>{const state=item.last_run_status==='completed'?'Terminée':item.last_run_status==='running'?'En cours':item.last_run_status==='failed'?'Échec':item.last_run_status==='skipped'?'Ignorée':'Jamais exécutée';const tone=item.last_run_status==='completed'?'text-emerald-700':item.last_run_status==='failed'?'text-rose-700':'text-slate-500';const last=item.last_run_at?new Date(item.last_run_at).toLocaleString('fr-FR')+' · '+state:state;return '<tr class="hover:bg-slate-50"><td class="px-4 py-3"><span class="font-mono font-bold text-slate-800">'+item.time+'</span><span class="ml-2 inline-flex rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-600">'+timePeriod(item.time)+'</span></td><td class="px-4 py-3 text-slate-600">Tous les jours</td><td class="px-4 py-3"><button type="button" data-schedule-toggle="'+index+'" class="rounded-full px-3 py-1 text-[10px] font-bold '+(item.enabled?'bg-emerald-100 text-emerald-800':'bg-slate-100 text-slate-500')+'">'+(item.enabled?'Activé':'Désactivé')+'</button></td><td class="px-4 py-3 text-xs '+tone+'">'+last+'</td><td class="px-4 py-3 text-right"><button type="button" data-schedule-delete="'+index+'" class="rounded-lg px-3 py-2 text-xs font-bold text-rose-600 hover:bg-rose-50"><i class="ph-bold ph-trash mr-1"></i>Supprimer</button></td></tr>';}).join('');body.querySelectorAll('[data-schedule-toggle]').forEach(button=>button.addEventListener('click',()=>{const item=schedules[Number(button.dataset.scheduleToggle)];item.enabled=!item.enabled;renderSchedules();}));body.querySelectorAll('[data-schedule-delete]').forEach(button=>button.addEventListener('click',()=>{schedules.splice(Number(button.dataset.scheduleDelete),1);renderSchedules();}));}
  async function saveSchedule(){const result=await api('/api/backup/schedule',{method:'POST',body:JSON.stringify({enabled:$('backupScheduleEnabled').checked,schedules})});schedules=result.schedules||[];renderSchedules();$('backupScheduleNotice').textContent=result.schedule_enabled?'Planification enregistrée et active.':'Planification enregistrée et suspendue.';$('backupScheduleNotice').className='text-xs text-emerald-700';}
  $('backupScheduleAdd').addEventListener('click',()=>{const time=$('backupScheduleTime').value;if(!time){$('backupScheduleNotice').textContent='Choisis une heure avant de l’ajouter.';$('backupScheduleNotice').className='text-xs font-semibold text-rose-600';return;}if(schedules.some(item=>item.time===time)){$('backupScheduleNotice').textContent='Cette heure existe déjà dans le tableau.';$('backupScheduleNotice').className='text-xs font-semibold text-rose-600';return;}schedules.push({id:Date.now().toString(36)+Math.random().toString(36).slice(2,8),time,enabled:true});schedules.sort((a,b)=>a.time.localeCompare(b.time));$('backupScheduleTime').value='';renderSchedules();});
  $('backupScheduleTime').addEventListener('input',updateSchedulePeriod);
  $('backupScheduleSave').addEventListener('click',()=>saveSchedule().catch(error=>{$('backupScheduleNotice').textContent=error.message;$('backupScheduleNotice').className='text-xs font-semibold text-rose-600';}));
  $('backupScheduleEnabled').addEventListener('change',()=>saveSchedule().catch(error=>{$('backupScheduleNotice').textContent=error.message;$('backupScheduleNotice').className='text-xs font-semibold text-rose-600';}));
  $('backupStart').addEventListener('click',async()=>{try{await api('/api/backup/settings',{method:'POST',body:JSON.stringify({destination:$('backupDestination').value})});const result=await api('/api/backup/start',{method:'POST',body:'{}'});currentJob=result.job_id;$('backupStart').disabled=true;$('backupStart').classList.add('opacity-60','cursor-not-allowed');$('backupProgressBar').style.width='1%';$('backupPercent').textContent='1 %';$('backupOutput').textContent='';$('backupMessage').textContent='Démarrage de la sauvegarde…';$('backupMessage').className='min-w-0 truncate text-sm font-bold text-slate-700';$('backupProgressWrap').classList.remove('hidden');renderProgress({state:'running',stage:'Préparation',speed_bps:0,stage_bytes:0});poll();}catch(error){showError(error.message);}});
  api('/api/backup/settings').then(settings=>{$('backupDestination').value=settings.destination||'';$('backupScheduleEnabled').checked=settings.schedule_enabled===true;schedules=Array.isArray(settings.schedules)?settings.schedules:[];renderSchedules();loadHistory();}).catch(error=>showError(error.message));
  api('/api/backup/current').then(state=>{if(state.id&&state.state!=='idle'){$('backupProgressWrap').classList.remove('hidden');$('backupMessage').textContent=state.message||state.state;$('backupPercent').textContent=(Number(state.percent)||0)+' %';$('backupProgressBar').style.width=(Number(state.percent)||0)+'%';$('backupOutput').textContent=state.output?'Destination : '+state.output:'';renderProgress(state);if(state.state==='running'||state.state==='paused'){currentJob=state.id;$('backupStart').disabled=true;poll();}}}).catch(()=>{});
})();
</script>
"""
