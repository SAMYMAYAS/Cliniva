"""Publie une archive de sources Cliniva consommable par les lanceurs clients."""

from __future__ import annotations

import hashlib
from importlib import metadata
import json
import logging
import os
from pathlib import Path
import tempfile
import zipfile
from datetime import datetime, timezone

logger = logging.getLogger("ClinivaClientUpdates")

PACKAGE_ROOTS = ("app", "modules", "templates", "static", "resources", "worklist", "Sip_app")
PACKAGE_FILES = ("main.py", "client.py", "database.py", "pip.txt", "RCP.py")
CLIENT_DISTRIBUTIONS = (
    "fastapi", "uvicorn", "jinja2", "pydantic", "psycopg2-binary", "pynetdicom",
    "pydicom", "pylibjpeg", "pylibjpeg-libjpeg", "pylibjpeg-openjpeg", "pylibjpeg-rle",
    "pywin32", "numpy", "Pillow", "opencv-python", "pytesseract", "python-docx",
)
EXCLUDED_PARTS = {
    ".git", ".idea", ".pnpm-store", "node_modules", "__pycache__", ".venv",
    "venv", "env", "uploads", "tmp", "output", "tests", "dist", "build",
}


def _files_to_publish(project_root: Path):
    for name in PACKAGE_FILES:
        candidate = project_root / name
        if candidate.is_file():
            yield candidate
    for relative_root in PACKAGE_ROOTS:
        root = project_root / relative_root
        if not root.is_dir():
            continue
        for candidate in root.rglob("*"):
            if not candidate.is_file():
                continue
            relative_parts = candidate.relative_to(project_root).parts
            if any(part in EXCLUDED_PARTS for part in relative_parts):
                continue
            if candidate.suffix.lower() in {".pyc", ".pyo", ".log", ".tmp"}:
                continue
            if relative_parts[:2] == ("static", "client_updates"):
                continue
            yield candidate


def publish_client_release(project_root: str | Path, static_root: str | Path) -> dict:
    """Construit seulement si le contenu a changé; aucune config ni donnée clinique n'est incluse."""
    project_root = Path(project_root).resolve()
    release_dir = Path(static_root).resolve() / "client_updates"
    release_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(set(_files_to_publish(project_root)))
    locked_versions = []
    for distribution in CLIENT_DISTRIBUTIONS:
        try:
            locked_versions.append(f"{distribution}=={metadata.version(distribution)}")
        except metadata.PackageNotFoundError:
            logger.warning("Dépendance absente du serveur, non ajoutée au verrou client: %s", distribution)
    requirements_lock = "\n".join(locked_versions) + ("\n" if locked_versions else "")
    source_hash = hashlib.sha256()
    for file_path in files:
        relative = file_path.relative_to(project_root).as_posix()
        source_hash.update(relative.encode("utf-8"))
        source_hash.update(b"\0")
        with file_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                source_hash.update(chunk)
    source_hash.update(b"requirements-lock.txt\0")
    source_hash.update(requirements_lock.encode("utf-8"))
    version = source_hash.hexdigest()[:16]
    manifest_path = release_dir / "latest.json"
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("version") == version and (release_dir / previous.get("filename", "")).is_file():
            return previous
    except (OSError, ValueError, TypeError):
        pass

    filename = f"cliniva-client-{version}.zip"
    archive_path = release_dir / filename
    fd, temp_name = tempfile.mkstemp(prefix=".cliniva-client-", suffix=".zip", dir=release_dir)
    os.close(fd)
    try:
        with zipfile.ZipFile(temp_name, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for file_path in files:
                archive.write(file_path, file_path.relative_to(project_root).as_posix())
            archive.writestr("requirements-lock.txt", requirements_lock)
        os.replace(temp_name, archive_path)
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)

    digest_builder = hashlib.sha256()
    with archive_path.open("rb") as archive_file:
        for chunk in iter(lambda: archive_file.read(1024 * 1024), b""):
            digest_builder.update(chunk)
    digest = digest_builder.hexdigest()
    manifest = {
        "version": version,
        "filename": filename,
        "sha256": digest,
        "size": archive_path.stat().st_size,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "package_url": f"/static/client_updates/{filename}",
        "source_files": len(files),
        "dependencies_sha256": hashlib.sha256(requirements_lock.encode("utf-8")).hexdigest(),
    }
    fd, temp_manifest = tempfile.mkstemp(prefix=".latest-", suffix=".json", dir=release_dir)
    os.close(fd)
    try:
        Path(temp_manifest).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_manifest, manifest_path)
    finally:
        if os.path.exists(temp_manifest):
            os.remove(temp_manifest)

    # Garde la version précédente pour permettre à un client déjà en téléchargement de finir.
    archives = sorted(release_dir.glob("cliniva-client-*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
    for old_archive in archives[2:]:
        try:
            old_archive.unlink()
        except OSError:
            logger.warning("Ancienne archive client non supprimée: %s", old_archive)
    logger.info("Mise à jour client publiée: version=%s fichiers=%s taille=%s octets", version, len(files), manifest["size"])
    return manifest
