"""Publie uniquement le lanceur client, jamais les sources applicatives Cliniva."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import zipfile
from datetime import datetime, timezone

logger = logging.getLogger("ClinivaClientUpdates")


def publish_client_release(project_root: str | Path, static_root: str | Path) -> dict:
    """Publie une archive ne contenant que client.py et retire les anciennes archives source."""
    project_root = Path(project_root).resolve()
    release_dir = Path(static_root).resolve() / "client_updates"
    release_dir.mkdir(parents=True, exist_ok=True)
    client_path = project_root / "client.py"
    if not client_path.is_file():
        raise FileNotFoundError("Le lanceur client.py est introuvable.")

    digest_builder = hashlib.sha256()
    with client_path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest_builder.update(chunk)
    version = digest_builder.hexdigest()[:16]
    filename = f"cliniva-client-{version}.zip"
    archive_path = release_dir / filename
    manifest_path = release_dir / "latest.json"

    # Une ancienne version du service y publiait les modules serveur. Les archives
    # générées dans ce répertoire sont retirées pour ne plus les exposer aux clients.
    for old_archive in release_dir.glob("cliniva-client-*.zip"):
        if old_archive.name != filename:
            try:
                old_archive.unlink()
            except OSError:
                logger.exception("Impossible de retirer une ancienne archive client : %s", old_archive)

    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("version") == version and archive_path.is_file():
            return previous
    except (OSError, ValueError, TypeError):
        pass

    fd, temp_name = tempfile.mkstemp(prefix=".cliniva-client-", suffix=".zip", dir=release_dir)
    os.close(fd)
    try:
        with zipfile.ZipFile(temp_name, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.write(client_path, "client.py")
        os.replace(temp_name, archive_path)
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)

    archive_hash = hashlib.sha256()
    with archive_path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            archive_hash.update(chunk)
    manifest = {
        "version": version,
        "filename": filename,
        "sha256": archive_hash.hexdigest(),
        "size": archive_path.stat().st_size,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "package_url": f"/static/client_updates/{filename}",
        "source_files": 1,
        "contents": ["client.py"],
    }
    fd, temp_manifest = tempfile.mkstemp(prefix=".latest-", suffix=".json", dir=release_dir)
    os.close(fd)
    try:
        Path(temp_manifest).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_manifest, manifest_path)
    finally:
        if os.path.exists(temp_manifest):
            os.remove(temp_manifest)
    logger.info("Lanceur client publié sans sources serveur : version=%s taille=%s octets", version, manifest["size"])
    return manifest
