"""Gestion multicatégorie des stocks, lots et bons internes Cliniva."""

from decimal import Decimal, InvalidOperation
from html import escape
import asyncio
import base64
from io import BytesIO
import json
import mimetypes
import time
from pathlib import Path
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request as UrlRequest, build_opener, urlopen
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.datastructures import UploadFile as StarletteUploadFile
from app.auth import get_current_user, track_action
from database import DOCS_DIR, get_db_cursor

stock_router = APIRouter(prefix="/api/gestion-stock", tags=["Gestion Stock"])

STOCK_CATALOG_TYPES = {"categorie": ("stock_categories", "categorie"), "unite": ("stock_units", "unite")}
STOCK_DOCUMENT_TYPES = {"Entrée", "Sortie", "Commande interne"}

_STOCK_GOOGLE_PHOTO_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_STOCK_GOOGLE_PHOTO_CACHE_TTL = 180


def _can(user: dict[str, Any], permission: str) -> bool:
    return user.get("role") == "Administrateur" or bool((user.get("permissions") or {}).get(permission))


def _require(user: dict[str, Any], permission: str) -> None:
    if not _can(user, permission):
        raise HTTPException(status_code=403, detail="Vous n’avez pas l’autorisation nécessaire pour la Gestion Stock.")


def _decimal(value: Any, label: str, *, allow_zero: bool = False) -> Decimal:
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=f"La quantité « {label} » est invalide.") from exc
    if not amount.is_finite() or amount < 0 or (not allow_zero and amount <= 0):
        raise HTTPException(status_code=400, detail=f"La quantité « {label} » doit être positive.")
    return amount


def _date(value: Any, label: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    from datetime import date
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"La date « {label} » est invalide.") from exc


def _get_serpapi_key() -> str:
    import os
    key = os.getenv("SERPAPI_API_KEY", "").strip()
    if key:
        return key
    try:
        from main import load_app_configuration
        ciphertext = str(load_app_configuration().get("stock_serpapi_key_ciphertext") or "")
        if not ciphertext:
            return ""
        import win32crypt
        encrypted = base64.b64decode(ciphertext, validate=True)
        return win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1].decode("utf-8")
    except Exception as exc:
        raise HTTPException(status_code=500, detail="La clé Google Images enregistrée ne peut pas être déchiffrée sur ce compte Windows. Reconfigure-la depuis Cliniva.") from exc


def _serpapi_search(query: str) -> list[dict[str, Any]]:
    key = _get_serpapi_key()
    if not key:
        raise HTTPException(status_code=424, detail="Google Images n’est pas encore configuré. Ouvre « Configurer Google Images » et enregistre ta clé SerpApi gratuite.")
    params = urlencode({"engine": "google_images", "q": query, "api_key": key,
                        "google_domain": "google.com", "gl": "dz", "hl": "fr",
                        "safe": "active"})
    request = UrlRequest(f"https://serpapi.com/search.json?{params}", headers={"Accept": "application/json", "User-Agent": "ClinivaStock/1.0"})
    try:
        with urlopen(request, timeout=25) as response:
            payload = json.loads(response.read(3 * 1024 * 1024))
    except HTTPError as exc:
        # SerpApi often returns a useful JSON explanation with HTTP errors
        # (invalid key, exhausted searches, or account limits). Surface that
        # explanation without ever exposing the request URL, which contains
        # the API key.
        try:
            error_payload = json.loads(exc.read(64 * 1024))
            provider_error = str(error_payload.get("error") or "").strip()
        except Exception:
            provider_error = ""
        if provider_error:
            if "invalid" in provider_error.lower() and "key" in provider_error.lower():
                raise HTTPException(status_code=424, detail="SerpApi refuse la clé enregistrée. Vérifie la clé dans la configuration Google Images.") from exc
            if "searches" in provider_error.lower() or "limit" in provider_error.lower() or "credit" in provider_error.lower():
                raise HTTPException(status_code=429, detail=f"Limite SerpApi atteinte : {provider_error[:180]}") from exc
            raise HTTPException(status_code=502, detail=f"SerpApi : {provider_error[:220]}") from exc
        raise HTTPException(status_code=502, detail=f"SerpApi est temporairement indisponible (HTTP {exc.code}).") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="La recherche Google Images est indisponible. Vérifie la connexion du serveur.") from exc
    if payload.get("error"):
        message = str(payload["error"])
        if "Invalid API key" in message or "invalid" in message.lower() and "key" in message.lower():
            raise HTTPException(status_code=424, detail="La clé SerpApi est invalide. Vérifie-la dans « Configurer Google Images ».")
        raise HTTPException(status_code=502, detail=f"SerpApi : {message[:220]}")
    raw_results = payload.get("images_results", [])
    results = []
    for image in raw_results[:12]:
        thumbnail = str(image.get("thumbnail") or "")
        parsed = urlsplit(thumbnail)
        # Depending on the Google result, SerpApi may provide its cached
        # thumbnail or a Google-hosted encrypted thumbnail. Both are safe to
        # render; arbitrary or non-HTTPS image URLs are discarded.
        host = (parsed.hostname or "").lower()
        serpapi_thumb = host == "serpapi.com" and parsed.path.startswith("/searches/")
        google_thumb = bool(re.fullmatch(r"encrypted-tbn[0-9]+\.gstatic\.com", host)) and parsed.path.startswith("/images")
        if parsed.scheme != "https" or parsed.username or parsed.password or not (serpapi_thumb or google_thumb):
            continue
        results.append({"id": str(image.get("position") or len(results) + 1),
                        "title": str(image.get("title") or "Image sans titre")[:250],
                        "creator": str(image.get("source") or "Source non indiquée")[:160],
                        "license": "Licence à vérifier auprès de la source",
                        "license_version": "",
                        "license_url": "https://creativecommons.org/licenses/",
                        "thumbnail": thumbnail,
                        "source_url": str(image.get("link") or "")[:1000]})
    if raw_results and not results:
        raise HTTPException(status_code=502, detail=f"Google a renvoyé {len(raw_results)} résultat(s), mais leurs miniatures ne sont pas compatibles. Essaie de nouveau ou vérifie la clé SerpApi.")
    return results


def _cached_serpapi_search(query: str) -> list[dict[str, Any]]:
    cache_key = query.strip().casefold()
    now = time.monotonic()
    cached = _STOCK_GOOGLE_PHOTO_CACHE.get(cache_key)
    if cached and now - cached[0] < _STOCK_GOOGLE_PHOTO_CACHE_TTL:
        return [dict(item) for item in cached[1]]
    results = _serpapi_search(query)
    if len(_STOCK_GOOGLE_PHOTO_CACHE) >= 64:
        oldest_key = min(_STOCK_GOOGLE_PHOTO_CACHE, key=lambda key: _STOCK_GOOGLE_PHOTO_CACHE[key][0])
        _STOCK_GOOGLE_PHOTO_CACHE.pop(oldest_key, None)
    _STOCK_GOOGLE_PHOTO_CACHE[cache_key] = (now, results)
    return [dict(item) for item in results]


def _download_serpapi_thumbnail(thumbnail_url: str) -> tuple[bytes, str]:
    parsed = urlsplit(thumbnail_url)
    host = (parsed.hostname or "").lower()
    serpapi_thumb = host == "serpapi.com" and parsed.path.startswith("/searches/") and not parsed.query
    google_thumb = bool(re.fullmatch(r"encrypted-tbn[0-9]+\.gstatic\.com", host)) and parsed.path.startswith("/images")
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment or not (serpapi_thumb or google_thumb):
        raise HTTPException(status_code=400, detail="Adresse de miniature Google invalide.")
    class SerpApiRedirectGuard(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            target = urlsplit(newurl)
            target_host = (target.hostname or "").lower()
            target_serpapi = target_host == "serpapi.com" and target.path.startswith("/searches/") and not target.query
            target_google = bool(re.fullmatch(r"encrypted-tbn[0-9]+\.gstatic\.com", target_host)) and target.path.startswith("/images")
            if target.scheme != "https" or target.username or target.password or not (target_serpapi or target_google):
                return None
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    request = UrlRequest(thumbnail_url, headers={"Accept": "image/*", "User-Agent": "ClinivaStock/1.0"})
    try:
        with build_opener(SerpApiRedirectGuard).open(request, timeout=20) as response:
            content_type = response.headers.get_content_type()
            content = response.read(8 * 1024 * 1024 + 1)
    except HTTPError as exc:
        raise HTTPException(status_code=502, detail="Impossible de récupérer cette miniature Google.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise HTTPException(status_code=502, detail="Téléchargement de la miniature Google indisponible.") from exc
    signatures = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n", "image/webp": b"RIFF"}
    if not content or len(content) > 8 * 1024 * 1024 or content_type not in signatures or not content.startswith(signatures[content_type]):
        raise HTTPException(status_code=415, detail="La miniature reçue n’est pas une image JPG, PNG ou WebP valide.")
    if content_type == "image/webp" and content[8:12] != b"WEBP":
        raise HTTPException(status_code=415, detail="La miniature WebP reçue est invalide.")
    return content, content_type


def _stock_document_data(cursor, document_ids: list[int]) -> list[dict[str, Any]]:
    if not document_ids:
        return []
    cursor.execute("""SELECT d.id, d.numero, d.type_document, d.statut, d.service_destination,
            d.fournisseur, d.agent_reception, d.demandeur, d.motif, d.notes, d.created_at, d.validated_at,
            COALESCE(u.prenom || ' ' || u.nom, d.demandeur, '') AS operateur
        FROM stock_documents d LEFT JOIN users u ON u.id=d.created_by
        WHERE d.id = ANY(%s) ORDER BY d.created_at DESC, d.id DESC""", (document_ids,))
    documents = [dict(row) for row in cursor.fetchall()]
    cursor.execute("""SELECT l.id, l.document_id, l.item_id, l.item_reference, l.item_designation,
            l.unite, l.quantite, l.quantite_unite_stock, l.unite_stock, l.quantite_par_unite,
            l.prix_achat, l.prix_vente, l.marge_vente, l.numero_lot, l.date_peremption, l.prix_unitaire,
            COALESCE(string_agg(DISTINCT CASE WHEN m.sens='Sortie' THEN lot.numero_lot END, ', '), '') AS lots_sortie
        FROM stock_document_lines l
        LEFT JOIN stock_movements m ON m.document_line_id=l.id AND m.sens='Sortie'
        LEFT JOIN stock_lots lot ON lot.id=m.lot_id
        WHERE l.document_id = ANY(%s)
        GROUP BY l.id ORDER BY l.document_id, l.id""", (document_ids,))
    lines_by_doc: dict[int, list[dict[str, Any]]] = {}
    for row in cursor.fetchall():
        line = dict(row)
        line["lots_utilises"] = line.pop("lots_sortie", "") or line.get("numero_lot", "")
        lines_by_doc.setdefault(line["document_id"], []).append(line)
    cursor.execute("""SELECT id, document_id, file_name, mime_type, page_number, file_size, created_at
        FROM stock_document_attachments WHERE document_id = ANY(%s)
        ORDER BY document_id, page_number, id""", (document_ids,))
    attachments_by_doc: dict[int, list[dict[str, Any]]] = {}
    for attachment in cursor.fetchall():
        row = dict(attachment)
        row["url"] = f"/api/gestion-stock/attachments/{row['id']}/file"
        attachments_by_doc.setdefault(row["document_id"], []).append(row)
    for document in documents:
        document["lignes"] = lines_by_doc.get(document["id"], [])
        document["pieces_jointes"] = attachments_by_doc.get(document["id"], [])
    return documents


@stock_router.get("/data")
def stock_data(request: Request):
    user = get_current_user(request)
    if not (_can(user, "stock_view") or _can(user, "stock_manage")):
        raise HTTPException(status_code=403, detail="Vous n’avez pas l’autorisation de consulter la Gestion Stock.")
    with get_db_cursor() as cursor:
        cursor.execute("SELECT nom FROM stock_categories ORDER BY nom")
        categories = [row["nom"] for row in cursor.fetchall()]
        cursor.execute("SELECT nom FROM stock_units ORDER BY nom")
        units = [row["nom"] for row in cursor.fetchall()]
        cursor.execute("""SELECT id, designation, adresse, email, telephone, contact,
                secteur_activite, observations, actif, created_at
            FROM stock_suppliers ORDER BY designation""")
        suppliers = [dict(row) for row in cursor.fetchall()]
        cursor.execute("""SELECT i.id, i.reference, i.designation, i.categorie, i.unite, i.photo_path,
                i.photo_credit, i.photo_license, i.photo_source_url,
                i.seuil_alerte, i.suivi_peremption, i.actif, i.notes, i.created_at,
                COALESCE(SUM(l.quantite_actuelle),0) AS stock_total,
                COALESCE(SUM(l.quantite_actuelle) FILTER (WHERE l.date_peremption IS NULL OR l.date_peremption >= CURRENT_DATE),0) AS stock_disponible,
                COALESCE(SUM(l.quantite_actuelle) FILTER (WHERE l.date_peremption < CURRENT_DATE),0) AS stock_perime,
                COUNT(l.id) FILTER (WHERE l.quantite_actuelle > 0) AS nombre_lots
            FROM stock_items i LEFT JOIN stock_lots l ON l.item_id=i.id
            WHERE i.actif=TRUE GROUP BY i.id ORDER BY i.categorie, i.designation""")
        items = [dict(row) for row in cursor.fetchall()]
        cursor.execute("""SELECT l.id, l.item_id, i.designation, i.reference, i.unite,
                l.numero_lot, l.date_peremption, l.quantite_actuelle, l.prix_unitaire,
                l.prix_achat, l.prix_vente, l.marge_vente, l.unite_stock, l.quantite_par_unite,
                (l.date_peremption < CURRENT_DATE) AS perime,
                (l.date_peremption BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '90 days') AS expire_bientot
            FROM stock_lots l JOIN stock_items i ON i.id=l.item_id
            WHERE i.actif=TRUE AND l.quantite_actuelle > 0
            ORDER BY l.date_peremption NULLS LAST, i.designation, l.id""")
        lots = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT id FROM stock_documents ORDER BY created_at DESC, id DESC LIMIT 100")
        doc_ids = [row["id"] for row in cursor.fetchall()]
        documents = _stock_document_data(cursor, doc_ids)
        cursor.execute("""SELECT m.id, m.document_id, m.document_line_id, m.sens, m.quantite,
                m.utilisateur, m.created_at, i.reference, i.designation, i.unite,
                l.numero_lot, l.date_peremption, d.numero AS bon_numero
            FROM stock_movements m JOIN stock_items i ON i.id=m.item_id
            JOIN stock_lots l ON l.id=m.lot_id JOIN stock_documents d ON d.id=m.document_id
            ORDER BY m.created_at DESC, m.id DESC LIMIT 100""")
        movements = [dict(row) for row in cursor.fetchall()]
        cursor.execute("""SELECT COALESCE(SUM(quantite_actuelle),0) AS quantite
            FROM stock_lots WHERE date_peremption < CURRENT_DATE""")
        expired_qty = cursor.fetchone()["quantite"]
        cursor.execute("""SELECT COUNT(*) AS total FROM stock_items i
            WHERE i.actif=TRUE AND i.seuil_alerte > 0 AND
            (SELECT COALESCE(SUM(l.quantite_actuelle),0) FROM stock_lots l
             WHERE l.item_id=i.id AND (l.date_peremption IS NULL OR l.date_peremption >= CURRENT_DATE)) <= i.seuil_alerte""")
        low_stock = cursor.fetchone()["total"]
        cursor.execute("""SELECT COUNT(*) AS total FROM stock_lots
            WHERE quantite_actuelle > 0 AND date_peremption BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '90 days'""")
        expiring_count = cursor.fetchone()["total"]
        cursor.execute("""SELECT COUNT(*) AS total FROM stock_items WHERE actif=TRUE""")
        item_count = cursor.fetchone()["total"]
    return {
        "items": items, "lots": lots, "documents": documents, "movements": movements,
        "categories": categories, "unites": units, "suppliers": suppliers,
        "summary": {"articles": item_count, "alertes_stock": low_stock,
                    "lots_perimes": len([lot for lot in lots if lot.get("perime")]),
                    "lots_proches": expiring_count, "quantite_perimee": expired_qty},
    }


@stock_router.get("/attachments/{attachment_id}/file")
def stock_document_attachment_file(attachment_id: int, request: Request):
    user = get_current_user(request)
    if not (_can(user, "stock_view") or _can(user, "stock_manage")):
        raise HTTPException(status_code=403, detail="Accès Gestion Stock non autorisé.")
    with get_db_cursor() as cursor:
        cursor.execute("SELECT file_path, file_name, mime_type FROM stock_document_attachments WHERE id=%s", (attachment_id,))
        row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Page numérisée introuvable.")
    root = Path(DOCS_DIR, "gestion_stock", "bons_entree").resolve()
    file_path = Path(row["file_path"]).resolve()
    try:
        file_path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Chemin de pièce jointe invalide.") from exc
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Fichier numérisé introuvable sur le serveur.")
    return FileResponse(file_path, media_type=row["mime_type"], filename=row["file_name"], content_disposition_type="inline")


@stock_router.post("/suppliers/save")
async def stock_supplier_save(request: Request):
    user = get_current_user(request)
    _require(user, "stock_manage")
    data = await request.json()
    designation = str(data.get("designation") or "").strip()
    fields = {
        "adresse": str(data.get("adresse") or "").strip(),
        "email": str(data.get("email") or "").strip(),
        "telephone": str(data.get("telephone") or "").strip(),
        "contact": str(data.get("contact") or "").strip(),
        "secteur_activite": str(data.get("secteur_activite") or "").strip(),
        "observations": str(data.get("observations") or "").strip(),
    }
    limits = {"adresse": 300, "email": 180, "telephone": 60, "contact": 180,
              "secteur_activite": 160, "observations": 2000}
    if not designation or len(designation) > 180:
        raise HTTPException(status_code=400, detail="La désignation du fournisseur est obligatoire (180 caractères maximum).")
    if any(len(fields[key]) > limit for key, limit in limits.items()):
        raise HTTPException(status_code=400, detail="Une information du fournisseur dépasse la longueur autorisée.")
    try:
        with get_db_cursor() as cursor:
            if data.get("id"):
                cursor.execute("""UPDATE stock_suppliers SET designation=%s, adresse=%s, email=%s,
                        telephone=%s, contact=%s, secteur_activite=%s, observations=%s,
                        updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING id""",
                    (designation, fields["adresse"], fields["email"], fields["telephone"],
                     fields["contact"], fields["secteur_activite"], fields["observations"], int(data["id"])))
                row = cursor.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Fournisseur introuvable.")
                supplier_id = row["id"]
            else:
                cursor.execute("""INSERT INTO stock_suppliers
                        (designation, adresse, email, telephone, contact, secteur_activite, observations)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (designation, fields["adresse"], fields["email"], fields["telephone"],
                     fields["contact"], fields["secteur_activite"], fields["observations"]))
                supplier_id = cursor.fetchone()["id"]
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "23505":
            raise HTTPException(status_code=409, detail="Un fournisseur porte déjà cette désignation.") from exc
        raise
    track_action(request, "STOCK_SUPPLIER_SAVE", f"Fournisseur enregistré : {designation}")
    return {"success": True, "id": supplier_id, "designation": designation}


@stock_router.post("/suppliers/{supplier_id}/toggle")
async def stock_supplier_toggle(supplier_id: int, request: Request):
    user = get_current_user(request)
    _require(user, "stock_manage")
    with get_db_cursor() as cursor:
        cursor.execute("""UPDATE stock_suppliers SET actif=NOT actif, updated_at=CURRENT_TIMESTAMP
            WHERE id=%s RETURNING designation, actif""", (supplier_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Fournisseur introuvable.")
    track_action(request, "STOCK_SUPPLIER_STATUS", f"Fournisseur {row['designation']} {'activé' if row['actif'] else 'archivé'}")
    return {"success": True, "actif": row["actif"]}


@stock_router.get("/items/{item_id}/photo")
def stock_item_photo(item_id: int, request: Request):
    user = get_current_user(request)
    if not (_can(user, "stock_view") or _can(user, "stock_manage")):
        raise HTTPException(status_code=403, detail="Accès non autorisé.")
    with get_db_cursor() as cursor:
        cursor.execute("SELECT photo_path FROM stock_items WHERE id=%s", (item_id,))
        row = cursor.fetchone()
    if not row or not row["photo_path"]:
        raise HTTPException(status_code=404, detail="Aucune photo pour cet article.")
    photo_path = (Path(DOCS_DIR) / row["photo_path"]).resolve()
    photo_root = (Path(DOCS_DIR) / "stock_items").resolve()
    if photo_root not in photo_path.parents or not photo_path.is_file():
        raise HTTPException(status_code=404, detail="Photo introuvable.")
    return FileResponse(photo_path, media_type=mimetypes.guess_type(photo_path.name)[0] or "application/octet-stream", headers={"Cache-Control": "private, max-age=300"})


@stock_router.get("/photo-search/google")
async def stock_google_photo_search(request: Request):
    user = get_current_user(request)
    _require(user, "stock_manage")
    query = str(request.query_params.get("q") or "").strip()
    if len(query) < 2 or len(query) > 200:
        raise HTTPException(status_code=400, detail="Saisis au moins deux caractères (200 maximum) pour rechercher une photo.")
    results = await asyncio.to_thread(_cached_serpapi_search, query)
    return {"results": results, "query": query}


@stock_router.get("/photo-search/google-settings")
def stock_google_photo_settings(request: Request):
    user = get_current_user(request)
    _require(user, "stock_manage")
    import os
    configured = bool(os.getenv("SERPAPI_API_KEY", "").strip())
    if not configured:
        try:
            from main import load_app_configuration
            configured = bool(load_app_configuration().get("stock_serpapi_key_ciphertext"))
        except Exception:
            configured = False
    return {"configured": configured}


@stock_router.post("/photo-search/google-settings")
async def stock_save_google_photo_settings(request: Request):
    user = get_current_user(request)
    _require(user, "stock_manage")
    data = await request.json()
    if data.get("clear"):
        from main import _persist_config_updates
        _persist_config_updates({"stock_serpapi_key_ciphertext": ""})
        return {"success": True, "configured": False}
    key = str(data.get("api_key") or "").strip()
    if len(key) < 12 or len(key) > 256:
        raise HTTPException(status_code=400, detail="Saisis une clé SerpApi valide.")
    try:
        import win32crypt
        ciphertext = base64.b64encode(win32crypt.CryptProtectData(key.encode("utf-8"), "Cliniva Google Images", None, None, None, 0)).decode("ascii")
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Cliniva n’a pas pu protéger la clé sur ce serveur Windows.") from exc
    from main import _persist_config_updates
    _persist_config_updates({"stock_serpapi_key_ciphertext": ciphertext})
    return {"success": True, "configured": True}


@stock_router.get("/photo-google-thumbnail")
async def stock_google_thumbnail(request: Request):
    user = get_current_user(request)
    _require(user, "stock_manage")
    thumbnail_url = str(request.query_params.get("url") or "")
    content, content_type = await asyncio.to_thread(_download_serpapi_thumbnail, thumbnail_url)
    return Response(content=content, media_type=content_type, headers={"Cache-Control": "private, max-age=3600"})


@stock_router.post("/catalog/save")
async def stock_catalog_save(request: Request):
    user = get_current_user(request)
    _require(user, "stock_manage")
    data = await request.json()
    kind = str(data.get("type") or "").strip()
    if kind not in STOCK_CATALOG_TYPES:
        raise HTTPException(status_code=400, detail="Type de liste invalide.")
    table, item_column = STOCK_CATALOG_TYPES[kind]
    name = str(data.get("nom") or "").strip()
    old_name = str(data.get("ancien_nom") or "").strip()
    if not name or len(name) > 80:
        raise HTTPException(status_code=400, detail="Le nom est obligatoire et limité à 80 caractères.")
    try:
        with get_db_cursor() as cursor:
            if old_name:
                cursor.execute(f"UPDATE {table} SET nom=%s WHERE nom=%s RETURNING nom", (name, old_name))
                if not cursor.fetchone():
                    raise HTTPException(status_code=404, detail="Élément de liste introuvable.")
                cursor.execute(f"UPDATE stock_items SET {item_column}=%s, updated_at=CURRENT_TIMESTAMP WHERE {item_column}=%s", (name, old_name))
            else:
                cursor.execute(f"INSERT INTO {table} (nom) VALUES (%s) RETURNING nom", (name,))
                cursor.fetchone()
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "23505":
            raise HTTPException(status_code=409, detail="Ce nom existe déjà dans la liste.") from exc
        raise
    track_action(request, "STOCK_CATALOG_SAVE", f"Liste de stock modifiée : {kind} · {old_name or name}")
    return {"success": True, "nom": name}


@stock_router.post("/catalog/delete")
async def stock_catalog_delete(request: Request):
    user = get_current_user(request)
    _require(user, "stock_manage")
    data = await request.json()
    kind = str(data.get("type") or "").strip()
    name = str(data.get("nom") or "").strip()
    if kind not in STOCK_CATALOG_TYPES or not name:
        raise HTTPException(status_code=400, detail="Élément de liste invalide.")
    table, item_column = STOCK_CATALOG_TYPES[kind]
    with get_db_cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) AS total FROM stock_items WHERE {item_column}=%s", (name,))
        if cursor.fetchone()["total"]:
            raise HTTPException(status_code=409, detail="Cette valeur est utilisée par un article et ne peut pas être supprimée.")
        cursor.execute(f"DELETE FROM {table} WHERE nom=%s RETURNING nom", (name,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Élément de liste introuvable.")
    track_action(request, "STOCK_CATALOG_DELETE", f"Élément de liste supprimé : {kind} · {name}")
    return {"success": True}


@stock_router.post("/items/save")
async def stock_item_save(request: Request):
    user = get_current_user(request)
    _require(user, "stock_manage")
    data = await request.form()
    photo = data.get("photo")
    remove_photo = str(data.get("supprimer_photo") or "").lower() in {"true", "1", "on"}
    photo_credit = str(data.get("photo_credit") or "").strip()[:240]
    photo_license = str(data.get("photo_license") or "").strip()[:100]
    photo_source_url = str(data.get("photo_source_url") or "").strip()[:1000]
    if photo_source_url and urlsplit(photo_source_url).scheme.lower() not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Le lien de provenance de la photo est invalide.")
    uploaded_photo_path = ""
    uploaded_photo_file: Path | None = None
    photo_content: bytes | None = None
    photo_suffix = ""
    # Request.form() creates Starlette UploadFile objects; FastAPI's
    # UploadFile subclass check does not match those instances on all versions.
    if isinstance(photo, StarletteUploadFile) and photo.filename:
        original_name = Path(photo.filename).name
        suffix = Path(original_name).suffix.lower()
        allowed = {".jpg": ("image/jpeg", b"\xff\xd8\xff"), ".jpeg": ("image/jpeg", b"\xff\xd8\xff"), ".png": ("image/png", b"\x89PNG\r\n\x1a\n"), ".webp": ("image/webp", b"RIFF")}
        if suffix not in allowed:
            raise HTTPException(status_code=400, detail="La photo doit être au format JPG, PNG ou WebP.")
        content = await photo.read(8 * 1024 * 1024 + 1)
        _, signature = allowed[suffix]
        if not content or len(content) > 8 * 1024 * 1024 or not content.startswith(signature):
            raise HTTPException(status_code=400, detail="Photo vide, invalide ou supérieure à 8 Mo.")
        if suffix == ".webp" and content[8:12] != b"WEBP":
            raise HTTPException(status_code=400, detail="Le fichier WebP est invalide.")
        suffix = ".jpg" if suffix == ".jpeg" else suffix
        photo_content = content
        photo_suffix = suffix
    try:
        item_id = int(data.get("id")) if data.get("id") else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Article invalide.") from exc
    designation = str(data.get("designation") or "").strip()
    category = str(data.get("categorie") or "").strip()
    unit = str(data.get("unite") or "unité").strip()
    reference = str(data.get("reference") or "").strip().upper()
    notes = str(data.get("notes") or "").strip()
    if not designation or len(designation) > 180:
        raise HTTPException(status_code=400, detail="La désignation est obligatoire et limitée à 180 caractères.")
    if not category or len(category) > 80:
        raise HTTPException(status_code=400, detail="La catégorie d’article est obligatoire.")
    if not unit or len(unit) > 30 or len(reference) > 60 or len(notes) > 2000:
        raise HTTPException(status_code=400, detail="Un champ de l’article dépasse la longueur autorisée.")
    threshold = _decimal(data.get("seuil_alerte", 0), "seuil d’alerte", allow_zero=True)
    track_expiry = str(data.get("suivi_peremption") or "").lower() in {"true", "1", "on"} or category == "Pharmaceutique"
    if photo_content is not None:
        upload_dir = Path(DOCS_DIR) / "stock_items"
        upload_dir.mkdir(parents=True, exist_ok=True)
        stored_name = f"{uuid4().hex}{photo_suffix}"
        uploaded_photo_file = upload_dir / stored_name
        uploaded_photo_file.write_bytes(photo_content)
        uploaded_photo_path = str(Path("stock_items") / stored_name)
    old_photo_path = ""
    try:
      with get_db_cursor() as cursor:
        try:
            cursor.execute("SELECT 1 FROM stock_categories WHERE nom=%s", (category,))
            if not cursor.fetchone():
                raise HTTPException(status_code=400, detail="Choisis une catégorie existante dans la liste.")
            cursor.execute("SELECT 1 FROM stock_units WHERE nom=%s", (unit,))
            if not cursor.fetchone():
                raise HTTPException(status_code=400, detail="Choisis une unité existante dans la liste.")
            if item_id:
                cursor.execute("SELECT photo_path, photo_credit, photo_license, photo_source_url FROM stock_items WHERE id=%s AND actif=TRUE FOR UPDATE", (item_id,))
                existing = cursor.fetchone()
                if not existing:
                    raise HTTPException(status_code=404, detail="Article introuvable ou archivé.")
                old_photo_path = existing["photo_path"] or ""
                photo_path = uploaded_photo_path or ("" if remove_photo else old_photo_path)
                if not uploaded_photo_path and not remove_photo:
                    photo_credit = existing["photo_credit"] or ""
                    photo_license = existing["photo_license"] or ""
                    photo_source_url = existing["photo_source_url"] or ""
                elif remove_photo and not uploaded_photo_path:
                    photo_credit = photo_license = photo_source_url = ""
                cursor.execute("""UPDATE stock_items SET reference=%s, designation=%s, categorie=%s,
                    unite=%s, seuil_alerte=%s, suivi_peremption=%s, notes=%s, photo_path=%s,
                    photo_credit=%s, photo_license=%s, photo_source_url=%s,
                    updated_at=CURRENT_TIMESTAMP WHERE id=%s AND actif=TRUE RETURNING id""",
                    (reference, designation, category, unit, threshold, track_expiry, notes, photo_path,
                     photo_credit, photo_license, photo_source_url, item_id))
                row = cursor.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Article introuvable ou archivé.")
                saved_id = item_id
            else:
                cursor.execute("""INSERT INTO stock_items
                    (reference, designation, categorie, unite, seuil_alerte, suivi_peremption, notes, photo_path,
                     photo_credit, photo_license, photo_source_url, created_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (reference, designation, category, unit, threshold, track_expiry, notes, uploaded_photo_path,
                     photo_credit if uploaded_photo_path else "", photo_license if uploaded_photo_path else "",
                     photo_source_url if uploaded_photo_path else "", user["id"]))
                saved_id = cursor.fetchone()["id"]
                if not reference:
                    reference = f"STK-{saved_id:06d}"
                    cursor.execute("UPDATE stock_items SET reference=%s WHERE id=%s", (reference, saved_id))
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise HTTPException(status_code=409, detail="Cette référence existe déjà.") from exc
            raise
    except Exception:
        if uploaded_photo_file and uploaded_photo_file.exists():
            uploaded_photo_file.unlink(missing_ok=True)
        raise
    if (remove_photo or uploaded_photo_path) and old_photo_path:
        previous_file = (Path(DOCS_DIR) / old_photo_path).resolve()
        previous_root = (Path(DOCS_DIR) / "stock_items").resolve()
        if previous_root in previous_file.parents and previous_file.is_file():
            previous_file.unlink(missing_ok=True)
    track_action(request, "STOCK_ITEM_SAVE", f"Article de stock enregistré : {reference} · {designation}")
    return {"success": True, "id": saved_id, "reference": reference}


@stock_router.post("/items/archive")
async def stock_item_archive(request: Request):
    user = get_current_user(request)
    _require(user, "stock_manage")
    data = await request.json()
    try:
        item_id = int(data.get("id"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Article invalide.") from exc
    with get_db_cursor() as cursor:
        cursor.execute("UPDATE stock_items SET actif=FALSE, updated_at=CURRENT_TIMESTAMP WHERE id=%s AND actif=TRUE RETURNING designation", (item_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Article introuvable.")
    track_action(request, "STOCK_ITEM_ARCHIVE", f"Article archivé : {row['designation']}")
    return {"success": True}


@stock_router.post("/documents/create")
async def stock_document_create(request: Request):
    user = get_current_user(request)
    _require(user, "stock_manage")
    data = await request.json()
    document_type = str(data.get("type_document") or "").strip()
    if document_type not in STOCK_DOCUMENT_TYPES:
        raise HTTPException(status_code=400, detail="Type de bon invalide.")
    lines = data.get("lignes")
    if not isinstance(lines, list) or not lines or len(lines) > 100:
        raise HTTPException(status_code=400, detail="Ajoute entre 1 et 100 lignes au bon.")
    pages_input = data.get("pages") or []
    if not isinstance(pages_input, list) or len(pages_input) > 30:
        raise HTTPException(status_code=400, detail="Le bon d’entrée peut contenir jusqu’à 30 pages numérisées.")
    if pages_input and document_type != "Entrée":
        raise HTTPException(status_code=400, detail="Les pages numérisées sont réservées aux bons d’entrée.")
    scan_pages: list[tuple[bytes, str, str]] = []
    total_scan_bytes = 0
    for page_number, page in enumerate(pages_input, start=1):
        data_url = str(page.get("data_url") or "") if isinstance(page, dict) else ""
        try:
            header, encoded = data_url.split(",", 1)
            mime_type = header.split(";", 1)[0].removeprefix("data:").lower()
            if mime_type not in {"image/jpeg", "image/png"}:
                raise ValueError("format")
            content = base64.b64decode(encoded, validate=True)
            if not content or len(content) > 12 * 1024 * 1024:
                raise ValueError("taille")
            from PIL import Image
            with Image.open(BytesIO(content)) as image:
                image_format = str(image.format or "").upper()
                if image.width * image.height > 40_000_000 or image_format not in {"JPEG", "PNG"} or (mime_type == "image/jpeg" and image_format != "JPEG") or (mime_type == "image/png" and image_format != "PNG"):
                    raise ValueError("image")
                image.verify()
            total_scan_bytes += len(content)
            if total_scan_bytes > 40 * 1024 * 1024:
                raise ValueError("total")
        except Exception as exc:
            message = "Les pages numérisées sont invalides, trop volumineuses ou dans un format non pris en charge."
            if "total" in str(exc):
                message = "Le poids total des pages numérisées ne doit pas dépasser 40 Mo."
            raise HTTPException(status_code=400, detail=f"Page {page_number} : {message}") from exc
        extension = "png" if mime_type == "image/png" else "jpg"
        scan_pages.append((content, mime_type, extension))
    service = str(data.get("service_destination") or "").strip()
    agent_reception = str(data.get("agent_reception") or "").strip() if document_type == "Entrée" else ""
    if document_type == "Entrée" and not agent_reception:
        raise HTTPException(status_code=400, detail="Indique l’agent de réception du bon d’entrée.")
    supplier_id = None
    supplier = ""
    if document_type == "Entrée" and data.get("fournisseur_id"):
        try:
            supplier_id = int(data.get("fournisseur_id"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Fournisseur invalide.") from exc
        with get_db_cursor() as cursor:
            cursor.execute("SELECT designation FROM stock_suppliers WHERE id=%s AND actif=TRUE", (supplier_id,))
            supplier_row = cursor.fetchone()
        if not supplier_row:
            raise HTTPException(status_code=400, detail="Le fournisseur choisi est introuvable ou archivé.")
        supplier = supplier_row["designation"]
    requester = f"{user.get('prenom', '')} {user.get('nom', '')}".strip()
    reason = str(data.get("motif") or "").strip()
    notes = str(data.get("notes") or "").strip()
    if document_type in {"Sortie", "Commande interne"} and not service:
        raise HTTPException(status_code=400, detail="Le service destinataire est obligatoire pour ce bon.")
    if len(service) > 180 or len(supplier) > 180 or len(agent_reception) > 180 or len(reason) > 250 or len(notes) > 2000:
        raise HTTPException(status_code=400, detail="Les informations du bon dépassent la longueur autorisée.")
    normalized = []
    seen: set[tuple[int, Any]] = set()
    for index, line in enumerate(lines, start=1):
        try:
            item_id = int(line.get("item_id"))
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(status_code=400, detail=f"Article invalide à la ligne {index}.") from exc
        quantity_stock = _decimal(line.get("quantite"), f"ligne {index}")
        quantity_per_unit = Decimal("1")
        stock_unit = ""
        purchase_price = sale_price = margin = None
        quantity = quantity_stock
        if document_type == "Entrée":
            quantity_per_unit = _decimal(line.get("quantite_par_unite") or "1", f"contenu par unité à la ligne {index}")
            stock_unit = str(line.get("unite_stock") or "").strip()
            if not stock_unit or len(stock_unit) > 80:
                raise HTTPException(status_code=400, detail=f"Choisis l’unité de stock à la ligne {index}.")
            purchase_raw = line.get("prix_achat")
            sale_raw = line.get("prix_vente")
            if purchase_raw in (None, ""):
                raise HTTPException(status_code=400, detail=f"Saisis le prix d’achat à la ligne {index}.")
            purchase_price = _decimal(purchase_raw, f"prix d’achat ligne {index}", allow_zero=True)
            margin = _decimal(line.get("marge_vente", 15), f"marge ligne {index}", allow_zero=True)
            calculated_sale = (purchase_price * (Decimal("1") + margin / Decimal("100"))).quantize(Decimal("0.01"))
            sale_price = _decimal(sale_raw if sale_raw not in (None, "") else calculated_sale, f"prix de vente ligne {index}", allow_zero=True)
            quantity = (quantity_stock * quantity_per_unit).quantize(Decimal("0.001"))
            if quantity <= 0:
                raise HTTPException(status_code=400, detail=f"La quantité convertie à la ligne {index} est trop petite.")
        lot_id = None
        if document_type == "Sortie" and line.get("lot_id"):
            try:
                lot_id = int(line["lot_id"])
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=f"Lot invalide à la ligne {index}.") from exc
        if document_type != "Sortie":
            lot_id = None
        duplicate_key = (item_id, str(line.get("numero_lot") or f"ligne-{index}")) if document_type == "Entrée" else (item_id, lot_id)
        if duplicate_key in seen:
            raise HTTPException(status_code=400, detail=f"La ligne {index} répète le même article et le même lot.")
        seen.add(duplicate_key)
        normalized.append({
            "item_id": item_id, "quantite": quantity, "lot_id": lot_id,
            "quantite_unite_stock": quantity_stock if document_type == "Entrée" else Decimal("0"),
            "unite_stock": stock_unit,
            "quantite_par_unite": quantity_per_unit,
            "prix_achat": purchase_price,
            "prix_vente": sale_price,
            "marge_vente": margin,
            "numero_lot": str(line.get("numero_lot") or "").strip(),
            "date_peremption": _date(line.get("date_peremption"), "péremption"),
            "prix_unitaire": ((purchase_price / quantity_per_unit).quantize(Decimal("0.01"))
                if document_type == "Entrée" else
                (_decimal(line.get("prix_unitaire"), "prix unitaire", allow_zero=True)
                 if line.get("prix_unitaire") not in (None, "") else None)),
        })

    prefix = {"Entrée": "BE", "Sortie": "BS", "Commande interne": "BCI"}[document_type]
    status = "En attente" if document_type == "Commande interne" else "Validé"
    with get_db_cursor() as cursor:
        cursor.execute("""INSERT INTO stock_documents
            (type_document, statut, service_destination, fournisseur, fournisseur_id, agent_reception, demandeur, motif, notes,
             created_by, validated_by, validated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    CASE WHEN %s='Validé' THEN CURRENT_TIMESTAMP ELSE NULL END)
            RETURNING id""",
            (document_type, status, service, supplier, supplier_id, agent_reception, requester, reason, notes, user["id"],
             user["id"] if status == "Validé" else None, status))
        document_id = cursor.fetchone()["id"]
        document_number = f"{prefix}-{document_id:08d}"
        cursor.execute("UPDATE stock_documents SET numero=%s WHERE id=%s", (document_number, document_id))
        prepared_lines = []
        for index, line in enumerate(normalized, start=1):
            cursor.execute("SELECT id, reference, designation, categorie, unite, suivi_peremption, actif FROM stock_items WHERE id=%s FOR UPDATE", (line["item_id"],))
            item = cursor.fetchone()
            if not item or not item["actif"]:
                raise HTTPException(status_code=404, detail=f"L’article de la ligne {index} n’existe plus ou est archivé.")
            if document_type == "Entrée":
                lot_number = line["numero_lot"]
                if not lot_number:
                    if item["categorie"] == "Pharmaceutique":
                        raise HTTPException(status_code=400, detail=f"Le numéro de lot est obligatoire pour l’article pharmaceutique de la ligne {index}.")
                    lot_number = f"SANS-LOT-{document_number}-{index}"
                if item["suivi_peremption"] and not line["date_peremption"]:
                    raise HTTPException(status_code=400, detail=f"La date de péremption est obligatoire pour l’article de la ligne {index}.")
                cursor.execute("""INSERT INTO stock_lots
                    (item_id, numero_lot, date_peremption, quantite_initiale, quantite_actuelle,
                     prix_unitaire, prix_achat, prix_vente, marge_vente, unite_stock, quantite_par_unite)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (item["id"], lot_number, line["date_peremption"], line["quantite"], line["quantite"],
                     line["prix_unitaire"], line["prix_achat"], line["prix_vente"], line["marge_vente"],
                     line["unite_stock"], line["quantite_par_unite"]))
                lot_id = cursor.fetchone()["id"]
                line["lot_id"] = lot_id
                line["numero_lot"] = lot_number
                cursor.execute("""INSERT INTO stock_document_lines
                    (document_id, item_id, item_reference, item_designation, unite, quantite,
                     lot_id, numero_lot, date_peremption, prix_unitaire, quantite_unite_stock,
                     unite_stock, quantite_par_unite, prix_achat, prix_vente, marge_vente)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (document_id, item["id"], item["reference"], item["designation"], item["unite"],
                     line["quantite"], lot_id, lot_number, line["date_peremption"], line["prix_unitaire"],
                     line["quantite_unite_stock"], line["unite_stock"], line["quantite_par_unite"],
                     line["prix_achat"], line["prix_vente"], line["marge_vente"]))
                line_id = cursor.fetchone()["id"]
                cursor.execute("""INSERT INTO stock_movements
                    (item_id, lot_id, document_id, document_line_id, sens, quantite, utilisateur, user_id)
                    VALUES (%s,%s,%s,%s,'Entrée',%s,%s,%s)""",
                    (item["id"], lot_id, document_id, line_id, line["quantite"], requester, user["id"]))
            else:
                cursor.execute("""INSERT INTO stock_document_lines
                    (document_id, item_id, item_reference, item_designation, unite, quantite,
                     lot_id, numero_lot, date_peremption, prix_unitaire)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'',NULL,NULL) RETURNING id""",
                    (document_id, item["id"], item["reference"], item["designation"], item["unite"],
                     line["quantite"], line["lot_id"]))
                line_id = cursor.fetchone()["id"]
                if document_type == "Sortie":
                    prepared_lines.append({**line, "line_id": line_id, "item": dict(item)})

        if document_type == "Sortie":
            for line in prepared_lines:
                query = """SELECT id, numero_lot, date_peremption, quantite_actuelle
                    FROM stock_lots WHERE item_id=%s AND quantite_actuelle > 0
                    AND (date_peremption IS NULL OR date_peremption >= CURRENT_DATE)"""
                params: list[Any] = [line["item_id"]]
                if line["lot_id"]:
                    query += " AND id=%s"
                    params.append(line["lot_id"])
                query += " ORDER BY date_peremption NULLS LAST, created_at, id FOR UPDATE"
                cursor.execute(query, tuple(params))
                available_lots = cursor.fetchall()
                remaining = line["quantite"]
                total_available = sum((Decimal(str(lot["quantite_actuelle"])) for lot in available_lots), Decimal("0"))
                if total_available < remaining:
                    raise HTTPException(status_code=409, detail=f"Stock insuffisant pour « {line['item']['designation']} ». Disponible : {total_available} {line['item']['unite']}.")
                for lot in available_lots:
                    if remaining <= 0:
                        break
                    lot_available = Decimal(str(lot["quantite_actuelle"]))
                    consumed = min(remaining, lot_available)
                    cursor.execute("UPDATE stock_lots SET quantite_actuelle=quantite_actuelle-%s WHERE id=%s", (consumed, lot["id"]))
                    cursor.execute("""INSERT INTO stock_movements
                        (item_id, lot_id, document_id, document_line_id, sens, quantite, utilisateur, user_id)
                        VALUES (%s,%s,%s,%s,'Sortie',%s,%s,%s)""",
                        (line["item_id"], lot["id"], document_id, line["line_id"], consumed, requester, user["id"]))
                    remaining -= consumed
        if scan_pages:
            storage_dir = Path(DOCS_DIR) / "gestion_stock" / "bons_entree" / document_number
            created_paths: list[Path] = []
            try:
                storage_dir.mkdir(parents=True, exist_ok=True)
                for page_number, (content, mime_type, extension) in enumerate(scan_pages, start=1):
                    filename = f"page_{page_number:03d}.{extension}"
                    file_path = storage_dir / filename
                    file_path.write_bytes(content)
                    created_paths.append(file_path)
                    cursor.execute("""INSERT INTO stock_document_attachments
                            (document_id, file_path, file_name, mime_type, page_number, file_size, created_by)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (document_id, str(file_path), filename, mime_type, page_number, len(content), user["id"]))
            except Exception:
                for file_path in created_paths:
                    try:
                        file_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
    track_action(request, "STOCK_DOCUMENT_CREATE", f"{document_type} {document_number} créé · {service or 'stock central'}" + (f" · réception : {agent_reception}" if agent_reception else "") + (f" · {len(scan_pages)} page(s) scannée(s)" if scan_pages else ""))
    return {"success": True, "id": document_id, "numero": document_number, "statut": status}


@stock_router.post("/documents/{document_id}/close")
def stock_document_close(document_id: int, request: Request):
    user = get_current_user(request)
    _require(user, "stock_manage")
    with get_db_cursor() as cursor:
        cursor.execute("""UPDATE stock_documents SET statut='Clôturé', validated_by=%s,
            validated_at=CURRENT_TIMESTAMP WHERE id=%s AND type_document='Commande interne'
            AND statut='En attente' RETURNING numero""", (user["id"], document_id))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Commande interne introuvable ou déjà clôturée.")
    track_action(request, "STOCK_ORDER_CLOSE", f"Commande interne {row['numero']} clôturée")
    return {"success": True}


def render_html(user: dict[str, Any] | None = None) -> str:
    if not user or not (_can(user, "stock_view") or _can(user, "stock_manage")):
        return '<div class="rounded-2xl border border-rose-200 bg-rose-50 p-5 font-semibold text-rose-700">Accès Gestion Stock non autorisé.</div>'
    can_manage = _can(user, "stock_manage")
    return (r'''
<style>
@media print { body * { visibility:hidden !important; } #stockPrintModal, #stockPrintModal * { visibility:visible !important; } #stockPrintModal { position:absolute!important; inset:0!important; display:block!important; padding:0!important; background:#fff!important; } #stockPrintModal > div { width:100%!important; max-width:none!important; max-height:none!important; border:0!important; box-shadow:none!important; overflow:visible!important; } #stockPrintSheet { padding:8mm!important; color:#111!important; } .stock-no-print { display:none!important; } }
#stockModule button .ph, #stockInternetPhotoModal button .ph, #stockGoogleConfigModal button .ph { display:inline-flex; flex:0 0 auto; align-items:center; justify-content:center; line-height:1; vertical-align:middle; }
#stockModule button, #stockInternetPhotoModal button, #stockGoogleConfigModal button { line-height:1.25; }
</style>
<section id="stockModule" class="space-y-5 text-slate-800" data-stock-manage="__MANAGE__">
 <header class="flex flex-wrap items-center justify-between gap-4 rounded-[2rem] bg-gradient-to-r from-[#101b13] to-[#183b20] px-7 py-6 text-white shadow-lg">
  <div class="flex items-center gap-4"><span class="grid h-12 w-12 place-items-center rounded-2xl bg-brand-neon text-emerald-950 shadow-lg"><i class="ph-bold ph-package text-2xl"></i></span><div><p class="text-xs font-bold uppercase tracking-[.18em] text-lime-300">Gestion opérationnelle · Inventaire</p><h1 class="mt-1 text-2xl font-extrabold">Gestion Stock</h1><p class="mt-1 text-sm text-slate-300">Pharmacie, informatique, technique, sanitaire et pièces.</p></div></div>
  <div class="flex flex-wrap gap-2">__ACTIONS__</div>
 </header>
 <section class="grid grid-cols-2 gap-3 xl:grid-cols-4">
  <article class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Articles actifs</span><i class="ph-bold ph-package text-xl text-brand-neon"></i></div><div id="stockKpiItems" class="mt-2 text-2xl font-extrabold dark:text-white">—</div></article>
  <article onclick="stockShowKpi('low')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();stockShowKpi('low')}" role="button" tabindex="0" title="Afficher les produits concernés" class="cursor-pointer transition hover:-translate-y-0.5 hover:border-sky-300 hover:shadow-md rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Sous le seuil</span><i class="ph-bold ph-warning-circle text-xl text-amber-500"></i></div><div id="stockKpiLow" class="mt-2 text-2xl font-extrabold dark:text-white">—</div></article>
  <article onclick="stockShowKpi('expiring')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();stockShowKpi('expiring')}" role="button" tabindex="0" title="Afficher les produits concernés" class="cursor-pointer transition hover:-translate-y-0.5 hover:border-sky-300 hover:shadow-md rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Péremption ≤ 90 j</span><i class="ph-bold ph-calendar-clock text-xl text-sky-500"></i></div><div id="stockKpiExpiring" class="mt-2 text-2xl font-extrabold dark:text-white">—</div></article>
  <article onclick="stockShowKpi('expired')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();stockShowKpi('expired')}" role="button" tabindex="0" title="Afficher les produits concernés" class="cursor-pointer transition hover:-translate-y-0.5 hover:border-sky-300 hover:shadow-md rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Lots périmés</span><i class="ph-bold ph-prohibit text-xl text-rose-500"></i></div><div id="stockKpiExpired" class="mt-2 text-2xl font-extrabold dark:text-white">—</div></article>
 </section>
 <nav class="flex w-fit max-w-full flex-wrap items-center gap-1 rounded-2xl border border-slate-200/80 bg-white p-1.5 shadow-sm dark:border-white/10 dark:bg-[#121c15]">
  <button type="button" data-stock-tab="inventory" onclick="stockSetTab('inventory')" class="rounded-xl bg-brand-neon px-4 py-2.5 text-xs font-bold text-black"><i class="ph-bold ph-books mr-1"></i>Inventaire & lots</button>
  <button type="button" data-stock-tab="documents" onclick="stockSetTab('documents')" class="rounded-xl px-4 py-2.5 text-xs font-bold text-slate-500 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-white/5"><i class="ph-bold ph-receipt mr-1"></i>Bons & mouvements</button>
  __SUPPLIER_TAB__
 </nav>
 <section id="stockPanelInventory" data-stock-panel="inventory" class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15] sm:p-5">
  <div class="mb-4 flex flex-wrap items-center justify-between gap-3"><div><h2 class="font-extrabold dark:text-white">Inventaire des articles</h2><p class="text-xs text-slate-500">Le stock disponible est recalculé après chaque bon validé. Actualisation automatique.</p></div><div class="flex flex-wrap gap-2"><select id="stockCategoryFilter" onchange="stockResetInventoryPage();stockRenderInventory()" class="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm dark:border-white/10 dark:bg-black/30 dark:text-white"><option value="">Toutes les catégories</option></select><input id="stockSearch" oninput="stockQueueInventorySearch()" class="w-44 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm dark:border-white/10 dark:bg-black/30 dark:text-white" placeholder="Rechercher"></div></div>
  <div class="overflow-x-auto"><table class="w-full min-w-[940px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600 dark:bg-black/20 dark:text-slate-400"><tr><th class="rounded-l-xl px-3 py-3 text-center">Image</th><th class="px-3 py-3">Référence / Article</th><th class="px-3 py-3">Catégorie</th><th class="px-3 py-3">Lots</th><th class="px-3 py-3">Stock disponible</th><th class="px-3 py-3">Seuil</th><th class="px-3 py-3">Péremption</th><th class="rounded-r-xl px-3 py-3 text-right">Actions</th></tr></thead><tbody id="stockItemRows"><tr><td colspan="8" class="p-8 text-center text-slate-400">Chargement…</td></tr></tbody></table></div><div id="stockItemPagination" class="mt-4"></div>
 </section>
 <section id="stockPanelDocuments" data-stock-panel="documents" class="hidden space-y-5">
  <div class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15] sm:p-5"><div class="mb-4 flex flex-wrap items-center justify-between gap-3"><div><h2 class="font-extrabold dark:text-white">Bons d’entrée, de sortie et commandes internes</h2><p class="text-xs text-slate-500">Les bons d’entrée et de sortie mettent le stock à jour dès leur validation.</p></div><input id="stockDocumentSearch" oninput="stockRenderDocuments()" class="w-52 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm dark:border-white/10 dark:bg-black/30 dark:text-white" placeholder="Rechercher un bon"></div><div class="overflow-x-auto"><table class="w-full min-w-[900px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600 dark:bg-black/20 dark:text-slate-400"><tr><th class="rounded-l-xl px-3 py-3">N° / Date</th><th class="px-3 py-3">Type de bon</th><th class="px-3 py-3">Service</th><th class="px-3 py-3">Demandeur</th><th class="px-3 py-3">Articles</th><th class="px-3 py-3">État</th><th class="rounded-r-xl px-3 py-3 text-right">Actions</th></tr></thead><tbody id="stockDocumentRows"><tr><td colspan="7" class="p-8 text-center text-slate-400">Chargement…</td></tr></tbody></table></div></div>
  <div class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15] sm:p-5"><div class="mb-4"><h2 class="font-extrabold dark:text-white">Mouvements récents</h2><p class="text-xs text-slate-500">Traçabilité par article, lot, bon, opérateur et date.</p></div><div class="overflow-x-auto"><table class="w-full min-w-[800px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600 dark:bg-black/20 dark:text-slate-400"><tr><th class="rounded-l-xl px-3 py-3">Date / Bon</th><th class="px-3 py-3">Article</th><th class="px-3 py-3">Lot</th><th class="px-3 py-3">Mouvement</th><th class="px-3 py-3">Quantité</th><th class="rounded-r-xl px-3 py-3">Opérateur</th></tr></thead><tbody id="stockMovementRows"><tr><td colspan="6" class="p-8 text-center text-slate-400">Aucun mouvement.</td></tr></tbody></table></div></div>
 </section>
  __SUPPLIER_PANEL__
 <div id="stockModal" class="fixed inset-0 z-[2147483600] hidden items-center justify-center bg-slate-950/65 p-3 backdrop-blur-sm"><div class="max-h-[94vh] w-full max-w-4xl overflow-y-auto rounded-3xl bg-white shadow-2xl dark:bg-[#121c15]"><div class="sticky top-0 z-10 flex items-center justify-between bg-gradient-to-r from-[#101b13] to-[#183b20] px-5 py-4 text-white"><div><div class="text-[10px] font-bold uppercase tracking-widest text-brand-neon">Gestion Stock</div><h3 id="stockModalTitle" class="text-lg font-black">Formulaire</h3></div><button type="button" onclick="stockCloseModal()" class="grid h-9 w-9 place-items-center rounded-xl bg-white/10 hover:bg-white/20" aria-label="Fermer"><i class="ph-bold ph-x"></i></button></div><form id="stockForm" onsubmit="stockSubmitForm(event)" class="space-y-4 p-5"><div id="stockFormFields"></div><div class="flex justify-end gap-2 border-t border-slate-100 pt-4 dark:border-white/10"><button type="button" onclick="stockCloseModal()" class="rounded-xl border border-slate-200 px-4 py-2.5 text-sm font-bold dark:border-white/10 dark:text-slate-200">Annuler</button><button id="stockSubmitButton" class="rounded-xl bg-brand-neon px-5 py-2.5 text-sm font-extrabold text-black shadow"><i class="ph-bold ph-check mr-1"></i>Valider</button></div></form></div></div>
 <div id="stockCatalogModal" class="fixed inset-0 z-[2147483604] hidden items-center justify-center bg-slate-950/60 p-3 backdrop-blur-sm"><div class="max-h-[86vh] w-full max-w-xl overflow-y-auto rounded-3xl bg-white shadow-2xl dark:bg-[#121c15]"><div class="sticky top-0 z-10 flex items-center justify-between bg-gradient-to-r from-[#101b13] to-[#183b20] px-5 py-4 text-white"><div><div class="text-[10px] font-bold uppercase tracking-widest text-brand-neon">Paramètres des articles</div><h3 id="stockCatalogTitle" class="text-lg font-black">Gérer la liste</h3></div><button type="button" onclick="stockCloseCatalog()" class="grid h-9 w-9 place-items-center rounded-xl bg-white/10 hover:bg-white/20" aria-label="Fermer"><i class="ph-bold ph-x"></i></button></div><form id="stockCatalogForm" onsubmit="stockCatalogSave(event)" class="space-y-4 p-5"><div><label id="stockCatalogLabel" for="stockCatalogName" class="block text-xs font-bold text-slate-600 dark:text-slate-300">Nom</label><div class="mt-2 flex gap-2"><input id="stockCatalogName" maxlength="80" required class="min-w-0 flex-1 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm dark:border-white/10 dark:bg-black/30 dark:text-white"><button id="stockCatalogSaveButton" class="rounded-xl bg-brand-neon px-4 py-2 text-xs font-extrabold text-black"><i class="ph-bold ph-plus mr-1"></i>Ajouter</button></div></div><div id="stockCatalogRows" class="max-h-[48vh] space-y-2 overflow-y-auto"></div><div class="flex justify-end border-t border-slate-100 pt-4 dark:border-white/10"><button type="button" onclick="stockCloseCatalog()" class="rounded-xl border border-slate-200 px-4 py-2.5 text-sm font-bold dark:border-white/10 dark:text-slate-200">Terminé</button></div></form></div></div>
 <div id="stockPrintModal" class="fixed inset-0 z-[2147483601] hidden items-center justify-center bg-slate-950/70 p-3 backdrop-blur-sm"><div class="max-h-[95vh] w-full max-w-3xl overflow-y-auto rounded-3xl bg-white shadow-2xl"><div class="stock-no-print sticky top-0 z-10 flex items-center justify-between bg-gradient-to-r from-[#101b13] to-[#183b20] px-5 py-3 text-white"><b id="stockPrintTitle">Bon de stock</b><div class="flex gap-2"><button type="button" onclick="window.print()" class="rounded-xl bg-brand-neon px-4 py-2 text-xs font-bold text-black"><i class="ph-bold ph-printer mr-1"></i>Imprimer / PDF</button><button type="button" onclick="stockClosePrint()" class="grid h-9 w-9 place-items-center rounded-xl bg-white/10" aria-label="Fermer"><i class="ph-bold ph-x"></i></button></div></div><div id="stockPrintSheet" class="p-8"></div></div></div>
 <div id="stockKpiModal" class="fixed inset-0 z-[2147483610] hidden items-center justify-center bg-slate-950/70 p-3 backdrop-blur-sm" role="dialog" aria-modal="true" aria-labelledby="stockKpiModalTitle" onclick="if(event.target===this)stockCloseKpi()"><section class="flex max-h-[92vh] w-full max-w-5xl flex-col overflow-hidden rounded-3xl bg-white shadow-2xl dark:bg-[#121c15]"><header class="flex items-center justify-between bg-gradient-to-r from-[#101b13] to-[#183b20] px-5 py-4 text-white"><div><p class="text-[10px] font-bold uppercase tracking-widest text-lime-300">Gestion Stock · Détail de l’indicateur</p><h2 id="stockKpiModalTitle" class="mt-1 text-lg font-black">Produits concernés</h2><p id="stockKpiModalSubtitle" class="mt-1 text-xs text-slate-300"></p></div><button type="button" onclick="stockCloseKpi()" class="grid h-9 w-9 place-items-center rounded-xl bg-white/10 hover:bg-white/20" aria-label="Fermer"><i class="ph-bold ph-x"></i></button></header><div class="min-h-0 overflow-auto p-4"><div class="overflow-x-auto rounded-2xl border border-slate-100 dark:border-white/10"><table class="w-full min-w-[760px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600 dark:bg-black/20 dark:text-slate-400"><tr><th class="rounded-l-xl px-3 py-3">Produit</th><th class="px-3 py-3">Référence</th><th class="px-3 py-3">Lot</th><th class="px-3 py-3">Quantité</th><th class="rounded-r-xl px-3 py-3">Péremption / seuil</th></tr></thead><tbody id="stockKpiModalRows"></tbody></table></div></div><footer class="flex justify-end border-t border-slate-100 p-4 dark:border-white/10"><button type="button" onclick="stockCloseKpi()" class="rounded-xl border border-slate-200 px-4 py-2 text-xs font-bold dark:border-white/10 dark:text-white">Fermer</button></footer></section></div>
 <div id="stockToast" class="fixed bottom-5 right-5 z-[2147483602] hidden max-w-sm rounded-xl bg-slate-900 px-4 py-3 text-sm font-semibold text-white shadow-xl"></div>
 <script>
 (()=>{
 const canManage=__MANAGE__, $=id=>document.getElementById(id), esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 let categories=[],units=[];let data={items:[],lots:[],documents:[],movements:[],suppliers:[],summary:{}};let activeForm=null,catalogEdit=null,stockInventoryPage=1,stockInventorySearchTimer=null,stockExpiringLotCounts=new Map();
 const toast=(msg,bad=false)=>{const t=$('stockToast');t.textContent=msg;t.className='fixed bottom-5 right-5 z-[2147483602] max-w-sm rounded-xl px-4 py-3 text-sm font-semibold text-white shadow-xl '+(bad?'bg-rose-700':'bg-slate-900');t.classList.remove('hidden');setTimeout(()=>t.classList.add('hidden'),4500)};
 const api=async(path,opts={})=>{const headers={...(opts.headers||{})};if(!(opts.body instanceof FormData))headers['Content-Type']='application/json';const r=await fetch('/api/gestion-stock'+path,{credentials:'same-origin',headers,...opts});const b=await r.json().catch(()=>({}));if(!r.ok)throw new Error(b.detail||'Une erreur est survenue.');return b;};
 window.stockRenderSuppliers=()=>{const body=$('stockSupplierRows');if(!body)return;const list=data.suppliers||[];body.innerHTML=list.length?list.map(s=>`<tr class="border-b border-slate-100 transition hover:bg-sky-50/60 dark:border-white/5 dark:hover:bg-white/[.03]"><td class="px-3 py-3"><b class="block dark:text-white">${esc(s.designation)}</b><small class="text-xs text-slate-400">${esc(s.secteur_activite||'Secteur non précisé')}</small></td><td class="px-3 py-3">${esc(s.contact||'—')}</td><td class="px-3 py-3">${esc(s.telephone||'—')}<small class="block text-slate-400">${esc(s.email||'')}</small></td><td class="px-3 py-3">${esc(s.adresse||'—')}${s.observations?`<small class="mt-1 block max-w-xs whitespace-pre-wrap text-slate-400">${esc(s.observations)}</small>`:''}</td><td class="px-3 py-3"><span class="rounded-full px-2.5 py-1 text-[10px] font-bold ${s.actif?'bg-emerald-50 text-emerald-700':'bg-slate-100 text-slate-500'}">${s.actif?'Actif':'Archivé'}</span></td><td class="px-3 py-3 text-right">${canManage?`<button type="button" onclick="stockEditSupplier(${s.id})" class="rounded-lg bg-slate-100 px-3 py-2 text-xs font-bold hover:bg-slate-200 dark:bg-white/10 dark:text-white"><i class="ph-bold ph-pencil-simple"></i> Modifier</button><button type="button" onclick="stockToggleSupplier(${s.id})" class="ml-1 rounded-lg ${s.actif?'bg-amber-50 text-amber-700':'bg-emerald-50 text-emerald-700'} px-3 py-2 text-xs font-bold">${s.actif?'Archiver':'Réactiver'}</button>`:'—'}</td></tr>`).join(''):'<tr><td colspan="6" class="p-10 text-center text-slate-400">Aucun fournisseur enregistré.</td></tr>';};
 window.stockEditSupplier=id=>{if(!canManage)return;const s=(data.suppliers||[]).find(x=>Number(x.id)===Number(id))||{};const textarea=(label,key)=>`<label class="block text-xs font-bold text-slate-600 dark:text-slate-300">${label}<textarea id="sf_${key}" rows="${key==='observations'?3:key==='adresse'?4:2}" maxlength="${key==='observations'?2000:300}" class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm dark:border-white/10 dark:bg-black/30 dark:text-white">${esc(s[key]||'')}</textarea></label>`;stockOpenModal(s.id?'Modifier le fournisseur':'Nouveau fournisseur',`<input id="sf_id" type="hidden" value="${s.id||''}"><div class="grid gap-4 sm:grid-cols-2">${field('Désignation *','sf_designation','text',s.designation||'','required maxlength="180"')}${field('Contact','sf_contact','text',s.contact||'','maxlength="180"')}${field('Téléphone','sf_telephone','tel',s.telephone||'','maxlength="60"')}${field('E-mail','sf_email','email',s.email||'','maxlength="180"')}${field('Secteur d’activité','sf_secteur_activite','text',s.secteur_activite||'','maxlength="160"')}<div class="sm:col-span-2">${textarea('Adresse','adresse')}</div><div class="sm:col-span-2">${textarea('Observations','observations')}</div></div>`,async()=>{const payload={id:$('sf_id').value||null,designation:$('sf_designation').value.trim(),contact:$('sf_contact').value.trim(),telephone:$('sf_telephone').value.trim(),email:$('sf_email').value.trim(),secteur_activite:$('sf_secteur_activite').value.trim(),adresse:$('sf_adresse').value.trim(),observations:$('sf_observations').value.trim()};await api('/suppliers/save',{method:'POST',body:JSON.stringify(payload)});stockCloseModal();toast('Fournisseur enregistré.');await refresh();});};
 window.stockToggleSupplier=async id=>{try{const s=(data.suppliers||[]).find(x=>Number(x.id)===Number(id));const action=s?.actif?'archiver':'réactiver';if(!confirm(`Voulez-vous ${action} ce fournisseur ?`))return;await api(`/suppliers/${id}/toggle`,{method:'POST',body:'{}'});await refresh();toast('Statut du fournisseur mis à jour.');}catch(e){toast(e.message,true);}};
 window.stockReceiptPages=[];
 window.stockRenderReceiptScans=()=>{const box=$('sdScanPreview');if(!box)return;box.innerHTML=window.stockReceiptPages.map((p,index)=>`<article class="relative overflow-hidden rounded-xl border border-slate-200 bg-white p-1.5 dark:border-white/10 dark:bg-black/20"><img src="${p.data_url}" alt="Page ${index+1}" class="h-28 w-full rounded-lg object-contain"><div class="flex items-center justify-between px-1 pt-1"><b class="text-[10px] text-slate-600 dark:text-slate-300">Page ${index+1}</b><button type="button" onclick="stockRemoveReceiptPage(${index})" class="grid h-6 w-6 place-items-center rounded-lg text-rose-600 hover:bg-rose-50" aria-label="Retirer la page ${index+1}"><i class="ph-bold ph-trash"></i></button></div></article>`).join('');const status=$('sdScanStatus');if(status)status.textContent=window.stockReceiptPages.length?`${window.stockReceiptPages.length} page(s) prête(s) à être ajoutée(s) au bon.`:'Aucune page numérisée pour le moment.';};
 window.stockRemoveReceiptPage=index=>{window.stockReceiptPages.splice(index,1);window.stockRenderReceiptScans();};
 window.stockDetectReceiptScanner=async()=>{const bridge=window.pywebview?.api;if(!bridge?.client_scanner_detect){toast('Ouvre Cliniva avec le client/serveur Cliniva sur le poste qui porte le scanner.',true);return;}const status=$('sdScanStatus'),select=$('sdScanDevice');if(status)status.textContent='Recherche des scanners WIA…';try{const result=await bridge.client_scanner_detect();if(!result?.success||!(result.scanners||[]).length)throw new Error(result?.message||'Aucun scanner WIA détecté.');select.innerHTML=result.scanners.map((scanner,index)=>`<option value="${esc(scanner.id)}">${esc(scanner.name||`Scanner ${index+1}`)}</option>`).join('');select.classList.remove('hidden');if(status)status.textContent=`${result.scanners.length} scanner(s) détecté(s). Tu peux scanner les pages une par une.`;}catch(error){if(status)status.textContent=error.message||'Détection du scanner impossible.';toast(error.message||'Détection du scanner impossible.',true);}};
 window.stockScanReceiptPage=async()=>{const bridge=window.pywebview?.api;if(!bridge?.client_scanner_acquire){toast('Le scanner nécessite le lanceur Cliniva sur le poste qui porte le scanner.',true);return;}if(!$('sdScanDevice')?.options.length)await window.stockDetectReceiptScanner();const select=$('sdScanDevice');if(!select?.options.length)return;if(window.stockReceiptPages.length>=30){toast('Un bon peut contenir au maximum 30 pages numérisées.',true);return;}const buttons=document.querySelectorAll('#stockModal button[onclick="stockScanReceiptPage()"]');buttons.forEach(button=>{button.disabled=true;button.classList.add('opacity-60');});const status=$('sdScanStatus');if(status)status.textContent=`Numérisation de la page ${window.stockReceiptPages.length+1}…`;try{const result=await bridge.client_scanner_acquire(select.value);if(!result?.success||!result.image_data)throw new Error(result?.message||'Le scanner n’a retourné aucune image.');window.stockReceiptPages.push({name:result.name||`page_${window.stockReceiptPages.length+1}.jpg`,data_url:result.image_data});window.stockRenderReceiptScans();}catch(error){if(status)status.textContent=error.message||'Échec de la numérisation.';toast(error.message||'Échec de la numérisation.',true);}finally{buttons.forEach(button=>{button.disabled=false;button.classList.remove('opacity-60');});}};
 window.stockShowReceiptPages=id=>{const doc=data.documents.find(item=>Number(item.id)===Number(id));if(!doc)return;let modal=$('stockReceiptPagesModal');if(!modal){modal=document.createElement('div');modal.id='stockReceiptPagesModal';modal.className='fixed inset-0 z-[2147483608] hidden items-center justify-center bg-slate-950/75 p-3 backdrop-blur-sm';document.body.appendChild(modal);}const pages=doc.pieces_jointes||[];modal.innerHTML=`<section class="flex max-h-[92vh] w-full max-w-5xl flex-col overflow-hidden rounded-3xl bg-white shadow-2xl dark:bg-[#121c15]"><header class="flex items-center justify-between bg-gradient-to-r from-[#101b13] to-[#183b20] px-5 py-4 text-white"><div><p class="text-[10px] font-bold uppercase tracking-widest text-lime-300">Bon d’entrée · ${esc(doc.numero)}</p><h2 class="text-lg font-black">Pages numérisées · ${pages.length}</h2></div><button type="button" onclick="stockCloseReceiptPages()" class="grid h-9 w-9 place-items-center rounded-xl bg-white/10" aria-label="Fermer"><i class="ph-bold ph-x"></i></button></header><div class="grid min-h-0 flex-1 gap-4 overflow-y-auto p-4 sm:grid-cols-2">${pages.map(page=>`<article class="overflow-hidden rounded-2xl border border-slate-200 bg-slate-50 p-2 dark:border-white/10 dark:bg-black/20"><a href="${page.url}" target="_blank" rel="noopener"><img src="${page.url}" alt="Page ${page.page_number}" class="max-h-[65vh] w-full rounded-xl object-contain"></a><div class="flex justify-between px-1 pt-2 text-xs"><b>Page ${page.page_number}</b><a href="${page.url}" target="_blank" rel="noopener" class="font-bold text-sky-700 underline">Ouvrir / télécharger</a></div></article>`).join('')||'<p class="col-span-2 py-12 text-center text-slate-400">Aucune page numérisée jointe à ce bon.</p>'}</div></section>`;modal.classList.remove('hidden');modal.classList.add('flex');};
 window.stockCloseReceiptPages=()=>{const modal=$('stockReceiptPagesModal');if(modal){modal.classList.add('hidden');modal.classList.remove('flex');}};
 window.stockShowKpi=kind=>{let title='',subtitle='',rows=[];if(kind==='low'){title='Produits sous le seuil d’alerte';rows=data.items.filter(item=>Number(item.seuil_alerte||0)>0&&Number(item.stock_disponible||0)<=Number(item.seuil_alerte)).map(item=>({designation:item.designation,categorie:item.categorie,reference:item.reference,lot:'—',quantity:`${fmt(item.stock_disponible)} ${item.unite||''}`,detail:`Seuil : ${fmt(item.seuil_alerte)} ${item.unite||''}`}));}else if(kind==='expiring'){title='Lots arrivant à péremption sous 90 jours';rows=(data.lots||[]).filter(lot=>lot.expire_bientot).map(lot=>({designation:lot.designation,reference:lot.reference,lot:lot.numero_lot,quantity:`${fmt(lot.quantite_actuelle)} ${lot.unite||''}`,detail:lot.date_peremption||'Date non définie'}));}else if(kind==='expired'){title='Lots périmés';rows=(data.lots||[]).filter(lot=>lot.perime).map(lot=>({designation:lot.designation,reference:lot.reference,lot:lot.numero_lot,quantity:`${fmt(lot.quantite_actuelle)} ${lot.unite||''}`,detail:lot.date_peremption||'Date non définie'}));}else return;subtitle=`${rows.length} produit(s) ou lot(s) concerné(s).`;const body=$('stockKpiModalRows');body.innerHTML=rows.length?rows.map(row=>`<tr class="border-t border-slate-100 hover:bg-sky-50/60 dark:border-white/5 dark:hover:bg-white/[.03]"><td class="px-3 py-3"><b class="dark:text-white">${esc(row.designation)}</b>${row.categorie?`<small class="block text-slate-400">${esc(row.categorie)}</small>`:''}</td><td class="px-3 py-3 font-mono text-xs">${esc(row.reference||'—')}</td><td class="px-3 py-3">${esc(row.lot||'—')}</td><td class="px-3 py-3 font-bold">${esc(row.quantity)}</td><td class="px-3 py-3 text-xs">${esc(row.detail)}</td></tr>`).join(''):'<tr><td colspan="5" class="p-10 text-center text-slate-400">Aucun produit ou lot dans cette catégorie.</td></tr>';$('stockKpiModalTitle').textContent=title;$('stockKpiModalSubtitle').textContent=subtitle;$('stockKpiModal').classList.remove('hidden');$('stockKpiModal').classList.add('flex');};
 window.stockCloseKpi=()=>{$('stockKpiModal')?.classList.add('hidden');$('stockKpiModal')?.classList.remove('flex');};
 window.stockSetTab=name=>{document.querySelectorAll('[data-stock-tab]').forEach(b=>{const on=b.dataset.stockTab===name;b.className='rounded-xl px-4 py-2.5 text-xs font-bold transition '+(on?'bg-brand-neon text-black shadow-md shadow-brand-neon/20':'text-slate-500 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-white/5')});document.querySelectorAll('[data-stock-panel]').forEach(p=>p.classList.toggle('hidden',p.dataset.stockPanel!==name));};
 function fillCategoryFilter(){const s=$('stockCategoryFilter'),selected=s.value;if(s.options.length===1)s.insertAdjacentHTML('beforeend',categories.map(c=>`<option>${esc(c)}</option>`).join(''));else{const options='<option value="">Toutes les catégories</option>'+categories.map(c=>`<option value="${esc(c)}">${esc(c)}</option>`).join('');s.innerHTML=options;s.value=selected;}}
 window.stockShowProduct=id=>{const i=data.items.find(x=>Number(x.id)===Number(id));if(!i)return;let modal=$('stockProductDetailModal');if(!modal){modal=document.createElement('div');modal.id='stockProductDetailModal';modal.className='fixed inset-0 z-[2147483607] hidden items-center justify-center bg-slate-950/75 p-3 backdrop-blur-sm';document.body.appendChild(modal);}const lots=data.lots.filter(l=>Number(l.item_id)===Number(i.id));const photo=i.photo_path?`<img src="/api/gestion-stock/items/${i.id}/photo?v=${encodeURIComponent(i.photo_path)}" alt="${esc(i.designation)}" class="max-h-72 w-full rounded-2xl bg-white object-contain p-3 dark:bg-black/20">`:'<div class="grid h-48 place-items-center rounded-2xl bg-slate-100 text-slate-400 dark:bg-white/5"><i class="ph ph-image text-5xl"></i><span class="mt-2 text-xs">Aucune photo</span></div>';modal.innerHTML=`<section class="flex max-h-[92vh] w-full max-w-4xl flex-col overflow-hidden rounded-3xl bg-white shadow-2xl dark:bg-[#121c15]"><header class="flex items-center justify-between bg-gradient-to-r from-[#101b13] to-[#183b20] px-5 py-4 text-white"><div><p class="text-[10px] font-bold uppercase tracking-widest text-lime-300">Fiche complète du produit</p><h2 class="text-xl font-black">${esc(i.designation)}</h2></div><button type="button" onclick="stockCloseProduct()" class="grid h-9 w-9 place-items-center rounded-xl bg-white/10 hover:bg-white/20" aria-label="Fermer"><i class="ph-bold ph-x"></i></button></header><div class="grid min-h-0 flex-1 gap-5 overflow-y-auto p-5 md:grid-cols-[minmax(220px,.8fr)_1.2fr]"> <div>${photo}${i.photo_credit?`<p class="mt-2 text-[11px] text-slate-500">Photo : ${esc(i.photo_credit)} · ${esc(i.photo_license||'Licence à vérifier')}</p>`:''}${i.photo_source_url?`<a class="mt-1 inline-block text-xs text-sky-700 underline" href="${esc(i.photo_source_url)}" target="_blank" rel="noopener noreferrer">Voir la source de la photo</a>`:''}</div><div class="space-y-4"><div class="grid grid-cols-2 gap-3">${[['Référence',i.reference||'—'],['Catégorie',i.categorie||'—'],['Unité',i.unite||'—'],['État',i.actif?'Actif':'Archivé'],['Stock disponible',`${fmt(i.stock_disponible)} ${i.unite||''}`],['Stock total',`${fmt(i.stock_total)} ${i.unite||''}`],['Stock périmé',`${fmt(i.stock_perime)} ${i.unite||''}`],['Seuil d’alerte',`${fmt(i.seuil_alerte)} ${i.unite||''}`],['Nombre de lots',String(i.nombre_lots||0)],['Suivi péremption',i.suivi_peremption?'Activé':'Désactivé']].map(([label,value])=>`<div class="rounded-xl border border-slate-200 bg-slate-50 p-3 dark:border-white/10 dark:bg-white/[.03]"><span class="block text-[10px] font-bold uppercase tracking-wide text-slate-500">${esc(label)}</span><b class="mt-1 block text-sm text-slate-800 dark:text-white">${esc(value)}</b></div>`).join('')}</div>${i.notes?`<div class="rounded-xl border border-slate-200 p-3 dark:border-white/10"><b class="text-xs text-slate-500">Notes</b><p class="mt-1 whitespace-pre-wrap text-sm dark:text-white">${esc(i.notes)}</p></div>`:''}<section><h3 class="mb-2 text-sm font-extrabold dark:text-white">Lots disponibles</h3><div class="overflow-x-auto rounded-xl border border-slate-200 dark:border-white/10"><table class="w-full min-w-[440px] text-left text-xs"><thead class="bg-slate-100 dark:bg-white/5"><tr><th class="p-2.5">Lot</th><th class="p-2.5">Péremption</th><th class="p-2.5 text-right">Quantité</th><th class="p-2.5">État</th></tr></thead><tbody>${lots.length?lots.map(l=>`<tr class="border-t border-slate-100 dark:border-white/10"><td class="p-2.5 font-semibold">${esc(l.numero_lot||'—')}</td><td class="p-2.5">${esc(l.date_peremption||'Sans date')}</td><td class="p-2.5 text-right font-bold">${fmt(l.quantite_actuelle)} ${esc(l.unite||i.unite||'')}</td><td class="p-2.5 ${l.perime?'text-rose-600':'text-emerald-700'}">${l.perime?'Périmé':l.expire_bientot?'Expire bientôt':'Disponible'}</td></tr>`).join(''):'<tr><td colspan="4" class="p-5 text-center text-slate-400">Aucun lot en stock.</td></tr>'}</tbody></table></div></section></div></div><footer class="flex flex-wrap justify-end gap-2 border-t border-slate-100 p-4 dark:border-white/10"><button type="button" onclick="stockCloseProduct()" class="rounded-xl border border-slate-200 px-4 py-2 text-xs font-bold dark:border-white/10 dark:text-white">Fermer</button>${canManage?`<button type="button" onclick="stockCloseProduct();stockEditItem(${i.id})" class="inline-flex items-center justify-center gap-2 rounded-xl bg-emerald-700 px-4 py-2 text-xs font-extrabold text-white"><i class="ph-bold ph-pencil-simple"></i>Modifier le produit</button>`:''}</footer></section>`;modal.classList.remove('hidden');modal.classList.add('flex');};
 window.stockCloseProduct=()=>{const modal=$('stockProductDetailModal');if(modal){modal.classList.add('hidden');modal.classList.remove('flex');}};
 function stockRenderInventory(){fillCategoryFilter();const q=($('stockSearch').value||'').trim().toLocaleLowerCase('fr'),cat=$('stockCategoryFilter').value;const filtered=data.items.filter(i=>(!cat||i.categorie===cat)&&(!q||(i.__searchText||`${i.reference} ${i.designation} ${i.categorie}`.toLocaleLowerCase('fr')).includes(q)));const pageSize=15,totalPages=Math.max(1,Math.ceil(filtered.length/pageSize));stockInventoryPage=Math.min(Math.max(1,stockInventoryPage),totalPages);const startIndex=(stockInventoryPage-1)*pageSize,rows=filtered.slice(startIndex,startIndex+pageSize);$('stockItemRows').innerHTML=rows.length?rows.map(i=>{const qty=Number(i.stock_disponible||0),low=Number(i.seuil_alerte||0)>0&&qty<=Number(i.seuil_alerte);const expiring=stockExpiringLotCounts.get(Number(i.id))||0;const expired=Number(i.stock_perime||0)>0;return `<tr class="border-b border-slate-100 transition hover:bg-sky-50/60 dark:border-white/5 dark:hover:bg-white/[.03]"><td class="px-3 py-2"><button type="button" onclick="stockShowProduct(${i.id})" title="Voir la fiche produit complète" class="mx-auto grid h-14 w-14 place-items-center overflow-hidden rounded-xl border border-slate-200 bg-white transition hover:scale-105 hover:border-emerald-400 hover:shadow-md dark:border-white/10 dark:bg-white/5">${i.photo_path?`<img src="/api/gestion-stock/items/${i.id}/photo?v=${encodeURIComponent(i.photo_path)}" alt="Photo ${esc(i.designation)}" loading="lazy" class="h-full w-full object-contain p-1" onerror="this.replaceWith(Object.assign(document.createElement('i'),{className:'ph ph-image text-xl text-slate-300'}))">`:'<i class="ph ph-image text-xl text-slate-300"></i>'}</button></td><td class="px-3 py-3"><b class="block text-slate-800 dark:text-white">${esc(i.designation)}</b><small class="text-xs text-slate-400">${esc(i.reference)}</small></td><td class="px-3 py-3"><span class="rounded-full bg-slate-100 px-2.5 py-1 text-[10px] font-bold text-slate-600 dark:bg-white/10 dark:text-slate-300">${esc(i.categorie)}</span></td><td class="px-3 py-3 font-semibold">${Number(i.nombre_lots||0)}</td><td class="px-3 py-3"><span class="font-extrabold ${low?'text-rose-600':'text-emerald-700 dark:text-emerald-300'}">${fmt(i.stock_disponible)} ${esc(i.unite)}</span>${expired?`<small class="block text-[10px] text-rose-500">${fmt(i.stock_perime)} périmé(s) bloqué(s)</small>`:''}</td><td class="px-3 py-3 text-slate-500">${fmt(i.seuil_alerte)} ${esc(i.unite)}</td><td class="px-3 py-3">${expired?'<span class="text-xs font-bold text-rose-600">Périmé</span>':expiring?`<span class="text-xs font-bold text-amber-600">${expiring} lot(s) bientôt</span>`:i.suivi_peremption?'<span class="text-xs text-emerald-600">Suivi actif</span>':'<span class="text-xs text-slate-400">—</span>'}</td><td class="px-3 py-3 text-right"><div class="flex justify-end gap-1"><button type="button" title="Lots" onclick="stockShowLots(${i.id})" class="grid h-8 w-8 place-items-center rounded-lg bg-sky-50 text-sky-700 hover:bg-sky-100"><i class="ph-bold ph-list-bullets"></i></button>${canManage?`<button type="button" title="Entrée" onclick="stockOpenDocument('Entrée',${i.id})" class="grid h-8 w-8 place-items-center rounded-lg bg-emerald-50 text-emerald-700 hover:bg-emerald-100"><i class="ph-bold ph-arrow-down-left"></i></button><button type="button" title="Sortie" onclick="stockOpenDocument('Sortie',${i.id})" class="grid h-8 w-8 place-items-center rounded-lg bg-amber-50 text-amber-700 hover:bg-amber-100"><i class="ph-bold ph-arrow-up-right"></i></button><button type="button" title="Modifier" onclick="stockEditItem(${i.id})" class="grid h-8 w-8 place-items-center rounded-lg bg-slate-100 text-slate-600 hover:bg-slate-200"><i class="ph-bold ph-pencil-simple"></i></button>`:''}</div></td></tr>`}).join(''):'<tr><td colspan="8" class="p-10 text-center text-slate-400">Aucun article ne correspond.</td></tr>';const pager=$('stockItemPagination');if(pager){const first=filtered.length?startIndex+1:0,last=Math.min(startIndex+pageSize,filtered.length);pager.innerHTML=filtered.length?`<div class="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-100 bg-slate-50 px-3 py-2 text-xs text-slate-600 dark:border-white/10 dark:bg-white/[.03] dark:text-slate-300"><span>Affichage de ${first} à ${last} sur ${filtered.length} produits</span><div class="flex items-center gap-2"><button type="button" onclick="stockGoInventoryPage(${stockInventoryPage-1})" ${stockInventoryPage<=1?'disabled':''} class="rounded-lg border border-slate-200 px-3 py-1.5 font-bold disabled:opacity-40 dark:border-white/10">Précédent</button><span class="min-w-20 text-center font-bold">Page ${stockInventoryPage} / ${totalPages}</span><button type="button" onclick="stockGoInventoryPage(${stockInventoryPage+1})" ${stockInventoryPage>=totalPages?'disabled':''} class="rounded-lg border border-slate-200 px-3 py-1.5 font-bold disabled:opacity-40 dark:border-white/10">Suivant</button></div></div>`:'<div class="mt-2 text-center text-xs text-slate-400">Aucun produit à afficher.</div>';}};
window.stockResetInventoryPage=()=>{stockInventoryPage=1;};window.stockQueueInventorySearch=()=>{stockInventoryPage=1;clearTimeout(stockInventorySearchTimer);stockInventorySearchTimer=setTimeout(stockRenderInventory,100);};window.stockGoInventoryPage=page=>{stockInventoryPage=Math.max(1,Number(page)||1);stockRenderInventory();};
function fmt(v){return Number(v||0).toLocaleString('fr-FR',{maximumFractionDigits:3});}
 function stockRenderDocuments(){const q=($('stockDocumentSearch').value||'').toLocaleLowerCase('fr');const docs=data.documents.filter(d=>`${d.numero} ${d.type_document} ${d.service_destination} ${d.demandeur} ${d.fournisseur||''} ${d.agent_reception||''}`.toLocaleLowerCase('fr').includes(q));$('stockDocumentRows').innerHTML=docs.length?docs.map(d=>`<tr class="border-b border-slate-100 hover:bg-sky-50/60 dark:border-white/5 dark:hover:bg-white/[.03]"><td class="px-3 py-3"><b class="block dark:text-white">${esc(d.numero)}</b><small class="text-xs text-slate-400">${dateTime(d.created_at)}</small>${d.agent_reception?`<small class="block text-sky-700">Réception : ${esc(d.agent_reception)}</small>`:''}</td><td class="px-3 py-3"><span class="rounded-full px-2.5 py-1 text-[10px] font-bold ${d.type_document==='Entrée'?'bg-emerald-50 text-emerald-700':d.type_document==='Sortie'?'bg-amber-50 text-amber-700':'bg-sky-50 text-sky-700'}">${esc(d.type_document)}</span></td><td class="px-3 py-3">${esc(d.service_destination||'Stock central')}</td><td class="px-3 py-3">${esc(d.demandeur)}</td><td class="px-3 py-3">${d.lignes.length} ligne(s)</td><td class="px-3 py-3"><span class="text-xs font-bold ${d.statut==='En attente'?'text-amber-600':d.statut==='Clôturé'?'text-slate-500':'text-emerald-700'}">${esc(d.statut)}</span></td><td class="px-3 py-3 text-right"><button onclick="stockPrintDocument(${d.id})" class="rounded-lg bg-slate-100 px-3 py-2 text-xs font-bold hover:bg-slate-200"><i class="ph-bold ph-eye mr-1"></i>Voir / Imprimer</button>${d.pieces_jointes?.length?`<button onclick="stockShowReceiptPages(${d.id})" class="ml-1 inline-flex items-center gap-1 rounded-lg bg-sky-50 px-3 py-2 text-xs font-bold text-sky-800 hover:bg-sky-100"><i class="ph-bold ph-files"></i>${d.pieces_jointes.length} page(s)</button>`:''}${canManage&&d.type_document==='Commande interne'&&d.statut==='En attente'?`<button onclick="stockCloseOrder(${d.id})" class="ml-1 rounded-lg bg-emerald-100 px-3 py-2 text-xs font-bold text-emerald-800 hover:bg-emerald-200">Clôturer</button>`:''}</td></tr>`).join(''):'<tr><td colspan="7" class="p-10 text-center text-slate-400">Aucun bon trouvé.</td></tr>';}
 function dateTime(v){return v?new Date(v).toLocaleString('fr-FR'):'—';}
 function renderMovements(){const body=$('stockMovementRows');body.innerHTML=data.movements.length?data.movements.map(m=>`<tr class="border-b border-slate-100 hover:bg-sky-50/60 dark:border-white/5"><td class="px-3 py-3"><b>${esc(m.bon_numero)}</b><small class="block text-xs text-slate-400">${dateTime(m.created_at)}</small></td><td class="px-3 py-3 font-semibold dark:text-white">${esc(m.designation)} <small class="block text-xs text-slate-400">${esc(m.reference)}</small></td><td class="px-3 py-3">${esc(m.numero_lot)}${m.date_peremption?`<small class="block text-xs text-slate-400">${esc(m.date_peremption)}</small>`:''}</td><td class="px-3 py-3"><span class="rounded-full px-2.5 py-1 text-[10px] font-bold ${m.sens==='Entrée'?'bg-emerald-50 text-emerald-700':'bg-rose-50 text-rose-700'}">${esc(m.sens)}</span></td><td class="px-3 py-3 font-extrabold ${m.sens==='Entrée'?'text-emerald-700':'text-rose-600'}">${m.sens==='Entrée'?'+':'−'}${fmt(m.quantite)} ${esc(m.unite)}</td><td class="px-3 py-3 text-slate-500">${esc(m.utilisateur)}</td></tr>`).join(''):'<tr><td colspan="6" class="p-8 text-center text-slate-400">Aucun mouvement enregistré.</td></tr>';}
 async function refresh(){if(!$('stockModule'))return;try{data=await api('/data');if(!$('stockModule'))return;data.items=(data.items||[]).map(item=>({...item,__searchText:`${item.reference||''} ${item.designation||''} ${item.categorie||''}`.toLocaleLowerCase('fr')}));stockExpiringLotCounts=new Map();for(const lot of data.lots||[]){if(lot.expire_bientot){const itemId=Number(lot.item_id);stockExpiringLotCounts.set(itemId,(stockExpiringLotCounts.get(itemId)||0)+1);}}categories=data.categories||[];units=data.unites||[];data.suppliers=data.suppliers||[];stockRenderSuppliers();$('stockKpiItems').textContent=data.summary.articles||0;$('stockKpiLow').textContent=data.summary.alertes_stock||0;$('stockKpiExpiring').textContent=data.summary.lots_proches||0;$('stockKpiExpired').textContent=fmt(data.summary.quantite_perimee||0);stockRenderInventory();stockRenderDocuments();renderMovements();}catch(e){if($('stockModule'))toast(e.message,true);}}
 window.stockOpenModal=(title,html,handler)=>{activeForm=handler;window.stockSelectedInternetPhoto=null;$('stockSubmitButton').classList.remove('hidden');$('stockSubmitButton').textContent='Valider';$('stockModalTitle').textContent=title;if(title.toLocaleLowerCase('fr').includes('article')){const toolbar=`<div class="mb-3 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-emerald-200 bg-emerald-50/70 p-3 dark:border-emerald-400/20 dark:bg-emerald-400/[.06]"><div><b class="block text-xs text-emerald-900 dark:text-emerald-200">Photo du produit</b><span class="text-[11px] text-emerald-800/75 dark:text-emerald-100/70">Recherche par nom et référence, ou conserve le téléversement manuel.</span></div><button type="button" onclick="stockSearchInternetPhoto()" class="inline-flex items-center gap-2 rounded-xl bg-emerald-700 px-4 py-2.5 text-xs font-extrabold text-white shadow-sm transition hover:bg-emerald-800"><i class="ph-bold ph-globe-hemisphere-west text-base"></i>Rechercher sur Internet</button><input id="siPhotoCredit" type="hidden" value=""><input id="siPhotoLicense" type="hidden" value=""><input id="siPhotoSource" type="hidden" value=""></div>`;const marker='<section class="sm:col-span-2 overflow-hidden rounded-2xl border border-slate-200 bg-gradient-to-br';const index=html.indexOf(marker);html=index>=0?html.slice(0,index)+toolbar+html.slice(index):toolbar+html;}$('stockFormFields').innerHTML=html;const photoName=$('siPhotoFileName'),photoFooter=photoName?.closest('section')?.lastElementChild;if(photoFooter&&!$('stockCropButton')){const cropButton=document.createElement('button');cropButton.id='stockCropButton';cropButton.type='button';cropButton.disabled=!($('siPhoto')?.files?.length || ($('siPhotoPreview') && !$('siPhotoPreview').classList.contains('hidden') && $('siPhotoPreview').getAttribute('src')));cropButton.className='inline-flex items-center justify-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs font-bold text-emerald-800 disabled:cursor-not-allowed disabled:opacity-40 dark:border-emerald-400/20 dark:bg-emerald-400/10 dark:text-emerald-200';cropButton.innerHTML='<i class="ph-bold ph-crop"></i>Recadrer';cropButton.onclick=window.stockOpenCropper;photoFooter.insertBefore(cropButton,photoFooter.firstChild);}$('stockModal').classList.remove('hidden');$('stockModal').classList.add('flex');document.body.classList.add('overflow-hidden');};
 window.stockCloseModal=()=>{$('stockModal').classList.add('hidden');$('stockModal').classList.remove('flex');window.stockCloseCatalog();if(window.stockPhotoPreviewUrl){URL.revokeObjectURL(window.stockPhotoPreviewUrl);window.stockPhotoPreviewUrl=null;}document.body.classList.remove('overflow-hidden');};
 window.stockCloseCatalog=()=>{$('stockCatalogModal').classList.add('hidden');$('stockCatalogModal').classList.remove('flex');};
 window.stockSearchInternetPhoto=()=>{const modalId='stockInternetPhotoModal';let modal=$(modalId);if(!modal){modal=document.createElement('div');modal.id=modalId;modal.className='fixed inset-0 z-[2147483605] hidden items-center justify-center bg-slate-950/75 p-3 backdrop-blur-sm';modal.innerHTML=`<section class="flex max-h-[92vh] w-full max-w-6xl flex-col overflow-hidden rounded-3xl bg-white shadow-2xl dark:bg-[#121c15]"><header class="flex items-center justify-between bg-gradient-to-r from-[#101b13] to-[#183b20] px-5 py-4 text-white"><div><div class="text-[10px] font-bold uppercase tracking-widest text-emerald-300">Recherche d’images en ligne</div><h3 class="text-lg font-black">Photos du produit</h3></div><button type="button" onclick="stockCloseInternetPhoto()" class="grid h-9 w-9 place-items-center rounded-xl bg-white/10 hover:bg-white/20" aria-label="Fermer"><i class="ph-bold ph-x"></i></button></header><div class="flex flex-wrap gap-2 border-b border-slate-100 p-4 dark:border-white/10"><input id="siPhotoQuery" class="min-w-0 flex-1 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm dark:border-white/10 dark:bg-black/30 dark:text-white" placeholder="Nom et référence du produit"><button type="button" onclick="stockRunInternetPhotoSearch()" class="inline-flex items-center justify-center gap-2 rounded-xl bg-emerald-700 px-4 py-2.5 text-xs font-extrabold text-white"><i class="ph-bold ph-magnifying-glass"></i>Rechercher</button></div><p class="px-4 pt-3 text-[11px] text-slate-500">Recherche Google Images via SerpApi. Vérifie les droits d’utilisation auprès de la source.</p><div id="siPhotoResults" class="min-h-32 flex-1 overflow-y-auto p-4"><div class="grid min-h-28 place-items-center text-sm text-slate-400">Saisis un nom ou une référence pour rechercher.</div></div><footer class="flex justify-end border-t border-slate-100 p-3 dark:border-white/10"><button type="button" onclick="stockCloseInternetPhoto()" class="rounded-xl border border-slate-200 px-4 py-2 text-xs font-bold dark:border-white/10 dark:text-white">Fermer</button></footer></section>`;document.body.appendChild(modal);}const query=[$('siReference')?.value,$('siName')?.value].map(v=>(v||'').trim()).filter(Boolean).join(' ');$('siPhotoQuery').value=query;modal.classList.remove('hidden');modal.classList.add('flex');if(query.length>=2){window.stockRunInternetPhotoSearch();}};
 window.stockCloseInternetPhoto=()=>{const modal=$('stockInternetPhotoModal');if(modal){modal.classList.add('hidden');modal.classList.remove('flex');}};
 window.stockPhotoSearchSource='google';
 window.stockEnsurePhotoSources=()=>{const query=$('siPhotoQuery');if(!query)return;const bar=query.parentElement;if(bar.dataset.sourcesReady)return;bar.dataset.sourcesReady='1';const controls=document.createElement('div');controls.className='flex w-full items-center justify-end';controls.innerHTML='<button type="button" onclick="stockConfigureGoogleImages()" class="rounded-lg border border-slate-200 px-3 py-2 text-[11px] font-bold text-slate-600"><i class="ph-bold ph-gear-six mr-1"></i>Configurer Google</button>';bar.parentElement.insertBefore(controls,bar);};
 const photoSourcesObserver=new MutationObserver(()=>window.stockEnsurePhotoSources());photoSourcesObserver.observe(document.body,{childList:true,subtree:true});
 window.stockSetPhotoProvider=()=>{window.stockPhotoSearchSource='google';window.stockRunInternetPhotoSearch();};
 window.stockConfigureGoogleImages=async()=>{let modal=$('stockGoogleConfigModal');if(!modal){modal=document.createElement('div');modal.id='stockGoogleConfigModal';modal.className='fixed inset-0 z-[2147483607] hidden items-center justify-center bg-slate-950/70 p-3 backdrop-blur-sm';modal.innerHTML='<section class="w-full max-w-lg overflow-hidden rounded-3xl bg-white shadow-2xl dark:bg-[#121c15]"><header class="bg-gradient-to-r from-[#101b13] to-[#183b20] px-5 py-4 text-white"><div class="text-[10px] font-bold uppercase tracking-widest text-emerald-300">Connexion de recherche</div><h3 class="text-lg font-black">Configurer Google Images</h3></header><div class="space-y-4 p-5"><p class="text-sm leading-6 text-slate-600 dark:text-slate-300">La recherche automatique utilise SerpApi pour interroger Google Images. Crée un compte gratuit puis colle ici ta clé API; elle sera protégée sur le serveur Cliniva et ne sera jamais affichée.</p><a href="https://serpapi.com/users/sign_up" target="_blank" rel="noopener noreferrer" class="inline-flex items-center gap-2 rounded-lg bg-sky-50 px-3 py-2 text-xs font-bold text-sky-800"><i class="ph-bold ph-arrow-square-out"></i>Créer un compte gratuit</a><label class="block text-xs font-bold text-slate-600 dark:text-slate-300">Clé API SerpApi<input id="stockSerpApiKey" type="password" autocomplete="new-password" class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-3 text-sm dark:border-white/10 dark:bg-black/30 dark:text-white" placeholder="Colle la clé ici"></label><p id="stockSerpApiStatus" class="text-xs text-slate-500"></p><div class="flex justify-between gap-2"><button type="button" onclick="stockClearGoogleKey()" class="rounded-xl px-3 py-2 text-xs font-bold text-rose-700 hover:bg-rose-50">Effacer la clé</button><div class="flex gap-2"><button type="button" onclick="stockCloseGoogleConfig()" class="rounded-xl border border-slate-200 px-4 py-2 text-xs font-bold dark:border-white/10 dark:text-white">Annuler</button><button type="button" onclick="stockSaveGoogleKey()" class="rounded-xl bg-emerald-700 px-4 py-2 text-xs font-extrabold text-white">Enregistrer</button></div></div></div></section>';document.body.appendChild(modal);}try{const status=await api('/photo-search/google-settings');$('stockSerpApiStatus').textContent=status.configured?'Une clé est enregistrée. Laisse le champ vide pour la conserver.':'Aucune clé n’est configurée.';}catch(error){$('stockSerpApiStatus').textContent=error.message||'Configuration indisponible.';}modal.classList.remove('hidden');modal.classList.add('flex');};
 window.stockCloseGoogleConfig=()=>{const modal=$('stockGoogleConfigModal');if(modal){modal.classList.add('hidden');modal.classList.remove('flex');}};
 window.stockSaveGoogleKey=async()=>{const key=$('stockSerpApiKey').value.trim();if(!key){toast('Colle d’abord la clé API SerpApi.',true);return;}try{await api('/photo-search/google-settings',{method:'POST',body:JSON.stringify({api_key:key})});$('stockSerpApiKey').value='';window.stockCloseGoogleConfig();toast('Google Images est configuré.');if(window.stockPhotoSearchSource==='google')window.stockRunInternetPhotoSearch();}catch(error){toast(error.message,true);}};
 window.stockClearGoogleKey=async()=>{if(!confirm('Effacer la clé SerpApi enregistrée sur ce serveur ?'))return;try{await api('/photo-search/google-settings',{method:'POST',body:JSON.stringify({clear:true})});$('stockSerpApiKey').value='';$('stockSerpApiStatus').textContent='Clé effacée.';toast('Clé Google effacée.');}catch(error){toast(error.message,true);}};
 const field=(label,id,type='text',value='',extra='')=>`<label class="block text-xs font-bold text-slate-600 dark:text-slate-300">${label}<input id="${id}" type="${type}" value="${esc(value)}" ${extra} class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm font-medium outline-none focus:border-brand-neon dark:border-white/10 dark:bg-black/30 dark:text-white"></label>`;
 const catalogField=(label,id,values,selected,kind)=>`<label class="block text-xs font-bold text-slate-600 dark:text-slate-300">${label}<div class="mt-2 flex gap-2"><select id="${id}" class="min-w-0 flex-1 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm font-medium dark:border-white/10 dark:bg-black/30 dark:text-white">${values.map(v=>`<option value="${esc(v)}" ${v===selected?'selected':''}>${esc(v)}</option>`).join('')}</select>${canManage?`<button type="button" onclick="stockManageCatalog('${kind}')" class="shrink-0 rounded-xl border border-emerald-200 bg-emerald-50 px-3 text-xs font-bold text-emerald-800 hover:bg-emerald-100 dark:border-emerald-400/20 dark:bg-emerald-400/10 dark:text-emerald-200" title="Gérer ${label.toLowerCase()}"><i class="ph-bold ph-sliders-horizontal mr-1"></i>Gérer</button>`:''}</div></label>`;
 const photoField=(path='')=>`<section class="sm:col-span-2 overflow-hidden rounded-2xl border border-slate-200 bg-gradient-to-br from-slate-50 to-white shadow-sm dark:border-white/10 dark:from-white/[.04] dark:to-[#121c15]"><div class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200/80 px-4 py-3.5 dark:border-white/10"><div class="flex items-center gap-3"><span class="grid h-10 w-10 place-items-center rounded-xl bg-emerald-100 text-emerald-800 dark:bg-emerald-400/10 dark:text-emerald-300"><i class="ph-bold ph-image-square text-xl"></i></span><div><h4 class="text-sm font-extrabold text-slate-800 dark:text-white">Photo de l’article</h4><p class="mt-0.5 text-[11px] text-slate-500">Une image claire aide à reconnaître rapidement le produit.</p></div></div><span class="rounded-full border border-slate-200 bg-white px-2.5 py-1 text-[10px] font-bold uppercase tracking-wide text-slate-500 dark:border-white/10 dark:bg-white/5 dark:text-slate-300">Facultatif</span></div><div id="stockPhotoDropzone" class="group relative m-4 grid min-h-48 place-items-center overflow-hidden rounded-2xl border-2 border-dashed border-slate-300 bg-white transition hover:border-emerald-400 hover:bg-emerald-50/40 dark:border-white/15 dark:bg-black/20 dark:hover:border-emerald-400/60 dark:hover:bg-emerald-400/[.04]"><div id="siPhotoPlaceholder" class="${path?'hidden':''} pointer-events-none flex flex-col items-center px-5 py-8 text-center"><span class="grid h-14 w-14 place-items-center rounded-2xl bg-emerald-50 text-emerald-700 shadow-sm transition group-hover:scale-105 dark:bg-emerald-400/10 dark:text-emerald-300"><i class="ph-bold ph-upload-simple text-2xl"></i></span><b class="mt-3 text-sm font-bold text-slate-700 dark:text-slate-200">Dépose la photo ici</b><span class="mt-1 text-xs text-slate-400">ou choisis une image depuis ton ordinateur</span></div><img id="siPhotoPreview" src="${path?`/api/gestion-stock/items/${path}/photo`:''}" class="${path?'':'hidden'} pointer-events-none max-h-64 w-full object-contain p-3" alt="Aperçu de la photo de l’article"><input id="siPhoto" type="file" accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp" onchange="stockPreviewPhoto(event)" class="absolute inset-0 z-10 h-full w-full cursor-pointer opacity-0" aria-label="Choisir ou déposer une photo de l’article"><span class="pointer-events-none absolute bottom-3 right-3 z-0 rounded-lg bg-slate-900/75 px-2.5 py-1.5 text-[10px] font-bold text-white opacity-0 transition group-hover:opacity-100"><i class="ph-bold ph-arrows-out-simple mr-1"></i>Cliquer pour remplacer</span></div><div class="flex flex-wrap items-center justify-between gap-3 px-4 pb-4"><div class="flex min-w-0 items-center gap-2 text-[11px] text-slate-500"><i class="ph-bold ph-info text-emerald-600"></i><span>JPG, PNG ou WebP · 8 Mo maximum</span><span id="siPhotoFileName" class="hidden max-w-48 truncate rounded-full bg-emerald-50 px-2.5 py-1 font-semibold text-emerald-800 dark:bg-emerald-400/10 dark:text-emerald-200"></span></div>${path?'<label class="inline-flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-xs font-semibold text-rose-600 transition hover:bg-rose-50 dark:hover:bg-rose-400/10"><input id="siRemovePhoto" type="checkbox" class="h-4 w-4 rounded accent-rose-600">Retirer cette photo</label>':''}</div></section>`;
 function stockItemFormData(id=null){const d=new FormData();if(id)d.append('id',id);['designation','reference','categorie','unite','seuil_alerte','notes'].forEach(k=>{const fieldId={designation:'siName',reference:'siReference',categorie:'siCategory',unite:'siUnit',seuil_alerte:'siThreshold',notes:'siNotes'}[k];d.append(k,$(fieldId).value);});d.append('suivi_peremption',$('siExpiry').checked?'true':'false');d.append('supprimer_photo',$('siRemovePhoto')?.checked?'true':'false');d.append('photo_credit',$('siPhotoCredit')?.value||'');d.append('photo_license',$('siPhotoLicense')?.value||'');d.append('photo_source_url',$('siPhotoSource')?.value||'');const file=$('siPhoto').files?.[0];if(file)d.append('photo',file,file.name);return d;}
 window.stockPreviewPhoto=(e,fromInternet=false)=>{const file=e.target.files?.[0];if(!file)return;if(!fromInternet){$('siPhotoCredit').value='';$('siPhotoLicense').value='';$('siPhotoSource').value='';window.stockSelectedInternetPhoto=null;}if(window.stockPhotoPreviewUrl)URL.revokeObjectURL(window.stockPhotoPreviewUrl);window.stockPhotoPreviewUrl=URL.createObjectURL(file);$('siPhotoPreview').src=window.stockPhotoPreviewUrl;$('siPhotoPreview').classList.remove('hidden');$('siPhotoPlaceholder').classList.add('hidden');const name=$('siPhotoFileName');name.textContent=file.name;name.classList.remove('hidden');const cropButton=$('stockCropButton');if(cropButton)cropButton.disabled=false;};
 window.stockOpenCropper=async()=>{const input=$('siPhoto'),preview=$('siPhotoPreview');let file=input?.files?.[0];if(!file&&preview?.src&&!preview.classList.contains('hidden')){try{const response=await fetch(preview.src,{credentials:'same-origin'});if(!response.ok)throw new Error();const blob=await response.blob();file=new File([blob],`produit.${blob.type==='image/png'?'png':blob.type==='image/webp'?'webp':'jpg'}`,{type:blob.type});}catch{toast('Impossible de charger la photo à recadrer.',true);return;}}if(!file){toast('Choisis une image avant de la recadrer.',true);return;}const sourceUrl=URL.createObjectURL(file),image=new Image();image.onload=()=>{let modal=$('stockCropModal');if(!modal){modal=document.createElement('div');modal.id='stockCropModal';modal.className='fixed inset-0 z-[2147483608] hidden items-center justify-center bg-slate-950/80 p-3 backdrop-blur-sm';document.body.appendChild(modal);}modal.innerHTML=`<section class="flex max-h-[94vh] w-full max-w-5xl flex-col overflow-hidden rounded-3xl bg-white shadow-2xl dark:bg-[#121c15]"><header class="flex items-center justify-between bg-gradient-to-r from-[#101b13] to-[#183b20] px-5 py-4 text-white"><div><p class="text-[10px] font-bold uppercase tracking-widest text-lime-300">Photo du produit</p><h3 class="text-lg font-black">Recadrer l’image</h3></div><button type="button" onclick="stockCloseCropper()" class="grid h-9 w-9 place-items-center rounded-xl bg-white/10" aria-label="Fermer"><i class="ph-bold ph-x"></i></button></header><div class="overflow-auto p-4"><p class="mb-3 text-xs text-slate-500">Fais glisser la zone claire pour la déplacer. Fais glisser son coin inférieur droit pour la redimensionner.</p><div class="grid place-items-center rounded-2xl bg-slate-950 p-2"><canvas id="stockCropCanvas" class="max-h-[68vh] max-w-full touch-none rounded-lg"></canvas></div></div><footer class="flex justify-end gap-2 border-t border-slate-100 p-4 dark:border-white/10"><button type="button" onclick="stockCloseCropper()" class="rounded-xl border border-slate-200 px-4 py-2.5 text-xs font-bold dark:border-white/10 dark:text-white">Annuler</button><button type="button" onclick="stockApplyCrop()" class="inline-flex items-center justify-center gap-2 rounded-xl bg-emerald-700 px-5 py-2.5 text-xs font-extrabold text-white"><i class="ph-bold ph-crop"></i>Utiliser ce recadrage</button></footer></section>`;modal.classList.remove('hidden');modal.classList.add('flex');const canvas=$('stockCropCanvas'),maxScale=Math.min(1,1200/image.naturalWidth,780/image.naturalHeight),width=Math.max(1,Math.round(image.naturalWidth*maxScale)),height=Math.max(1,Math.round(image.naturalHeight*maxScale)),ctx=canvas.getContext('2d');canvas.width=width;canvas.height=height;window.stockCropState={image,sourceUrl,scale:maxScale,crop:{x:width*.1,y:height*.1,w:width*.8,h:height*.8},mode:'',start:null,initial:null};const draw=()=>{const s=window.stockCropState;if(!s)return;ctx.clearRect(0,0,width,height);ctx.drawImage(image,0,0,width,height);ctx.fillStyle='rgba(0,0,0,.62)';ctx.fillRect(0,0,width,height);ctx.clearRect(s.crop.x,s.crop.y,s.crop.w,s.crop.h);ctx.drawImage(image,s.crop.x/maxScale,s.crop.y/maxScale,s.crop.w/maxScale,s.crop.h/maxScale,s.crop.x,s.crop.y,s.crop.w,s.crop.h);ctx.strokeStyle='#fff';ctx.lineWidth=Math.max(2,width/600);ctx.strokeRect(s.crop.x,s.crop.y,s.crop.w,s.crop.h);const hsize=Math.max(12,width/45);ctx.fillStyle='#34d399';ctx.fillRect(s.crop.x+s.crop.w-hsize/2,s.crop.y+s.crop.h-hsize/2,hsize,hsize);};const pos=e=>{const r=canvas.getBoundingClientRect();return{x:(e.clientX-r.left)*canvas.width/r.width,y:(e.clientY-r.top)*canvas.height/r.height};};canvas.onpointerdown=e=>{e.preventDefault();const s=window.stockCropState,p=pos(e),c=s.crop,handle=Math.max(18,width/35);canvas.setPointerCapture(e.pointerId);s.start=p;s.initial={...c};if(Math.abs(p.x-(c.x+c.w))<handle&&Math.abs(p.y-(c.y+c.h))<handle)s.mode='resize';else if(p.x>=c.x&&p.x<=c.x+c.w&&p.y>=c.y&&p.y<=c.y+c.h)s.mode='move';else{s.crop={x:p.x,y:p.y,w:1,h:1};s.initial={...s.crop};s.mode='resize';}draw();};canvas.onpointermove=e=>{const s=window.stockCropState;if(!s?.mode)return;const p=pos(e),dx=p.x-s.start.x,dy=p.y-s.start.y;if(s.mode==='move'){s.crop.x=Math.max(0,Math.min(width-s.crop.w,s.initial.x+dx));s.crop.y=Math.max(0,Math.min(height-s.crop.h,s.initial.y+dy));}else{s.crop.w=Math.max(35,Math.min(width-s.crop.x,s.initial.w+dx));s.crop.h=Math.max(35,Math.min(height-s.crop.y,s.initial.h+dy));}draw();};canvas.onpointerup=canvas.onpointercancel=()=>{if(window.stockCropState)window.stockCropState.mode='';};draw();};image.onerror=()=>{URL.revokeObjectURL(sourceUrl);toast('Cette image ne peut pas être recadrée.',true);};image.src=sourceUrl;};
 window.stockCloseCropper=()=>{const modal=$('stockCropModal'),state=window.stockCropState;if(modal){modal.classList.add('hidden');modal.classList.remove('flex');}if(state?.sourceUrl)URL.revokeObjectURL(state.sourceUrl);window.stockCropState=null;};
 window.stockApplyCrop=()=>{const s=window.stockCropState,input=$('siPhoto');if(!s||!input)return;const c=s.crop,scale=Math.min(1,2400/Math.max(c.w/s.scale,c.h/s.scale)),out=document.createElement('canvas');out.width=Math.max(1,Math.round(c.w/s.scale*scale));out.height=Math.max(1,Math.round(c.h/s.scale*scale));const ctx=out.getContext('2d');ctx.drawImage(s.image,c.x/s.scale,c.y/s.scale,c.w/s.scale,c.h/s.scale,0,0,out.width,out.height);out.toBlob(blob=>{if(!blob){toast('Impossible de créer l’image recadrée.',true);return;}const cropped=new File([blob],`photo_produit_${Date.now()}.jpg`,{type:'image/jpeg'}),transfer=new DataTransfer();transfer.items.add(cropped);input.files=transfer.files;window.stockCloseCropper();window.stockPreviewPhoto({target:input},true);toast('Recadrage appliqué. Il sera enregistré avec le produit.');},'image/jpeg',.9);};
 window.stockNewItem=()=>{if(!canManage)return;stockOpenModal('Nouvel article',`<div class="grid gap-3 sm:grid-cols-2">${field('Désignation *','siName')} ${field('Référence (facultative)','siReference')}${catalogField('Catégorie *','siCategory',categories,categories[0],'categorie')}${catalogField('Unité de stock *','siUnit',units,units[0],'unite')}${field('Seuil d’alerte','siThreshold','number','0','min="0" step="0.001"')}<label class="flex items-center gap-2 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm font-semibold text-amber-800"><input id="siExpiry" type="checkbox" class="h-4 w-4 accent-amber-600">Suivre la date de péremption</label>${photoField()}<label class="block text-xs font-bold text-slate-600 dark:text-slate-300 sm:col-span-2">Notes<textarea id="siNotes" rows="2" class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm dark:border-white/10 dark:bg-black/30 dark:text-white"></textarea></label></div>`,async()=>{const r=await api('/items/save',{method:'POST',body:stockItemFormData()});stockCloseModal();toast(`Article ${r.reference} enregistré.`);await refresh();});};
 window.stockEditItem=id=>{const i=data.items.find(x=>Number(x.id)===Number(id));if(!i)return;stockOpenModal('Modifier un article',`<div class="grid gap-3 sm:grid-cols-2">${field('Désignation *','siName','text',i.designation)}${field('Référence','siReference','text',i.reference)}${catalogField('Catégorie *','siCategory',categories,i.categorie,'categorie')}${catalogField('Unité *','siUnit',units,i.unite,'unite')}${field('Seuil d’alerte','siThreshold','number',i.seuil_alerte,'min="0" step="0.001"')}<label class="flex items-center gap-2 rounded-xl border p-3 text-sm font-semibold"><input id="siExpiry" type="checkbox" ${i.suivi_peremption?'checked':''}>Suivre la date de péremption</label>${photoField(i.photo_path?i.id:'')}<label class="block text-xs font-bold sm:col-span-2">Notes<textarea id="siNotes" rows="2" class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm">${esc(i.notes||'')}</textarea></label></div>`,async()=>{await api('/items/save',{method:'POST',body:stockItemFormData(i.id)});stockCloseModal();toast('Article modifié.');await refresh();});};
 window.stockManageCatalog=kind=>{if(!canManage||!['categorie','unite'].includes(kind))return;catalogEdit=null;$('stockCatalogTitle').textContent=kind==='categorie'?'Gérer les catégories':'Gérer les unités';$('stockCatalogLabel').textContent=kind==='categorie'?'Nom de la catégorie':'Nom de l’unité';$('stockCatalogName').value='';$('stockCatalogSaveButton').innerHTML='<i class="ph-bold ph-plus mr-1"></i>Ajouter';$('stockCatalogModal').dataset.kind=kind;stockRenderCatalog();$('stockCatalogModal').classList.remove('hidden');$('stockCatalogModal').classList.add('flex');};
 function stockRenderCatalog(){const modal=$('stockCatalogModal'),kind=modal.dataset.kind,values=kind==='categorie'?categories:units;$('stockCatalogRows').innerHTML=values.length?values.map((name,index)=>`<div class="flex items-center justify-between gap-2 rounded-xl border border-slate-100 bg-slate-50 px-3 py-2 dark:border-white/10 dark:bg-white/[.03]"><span class="min-w-0 truncate text-sm font-semibold dark:text-white">${esc(name)}</span><div class="flex shrink-0 gap-1"><button type="button" data-stock-catalog-edit="${index}" class="grid h-8 w-8 place-items-center rounded-lg bg-sky-50 text-sky-700 hover:bg-sky-100" title="Modifier"><i class="ph-bold ph-pencil-simple"></i></button><button type="button" data-stock-catalog-delete="${index}" class="grid h-8 w-8 place-items-center rounded-lg bg-rose-50 text-rose-700 hover:bg-rose-100" title="Supprimer"><i class="ph-bold ph-trash"></i></button></div></div>`).join(''):'<p class="rounded-xl bg-slate-50 p-4 text-center text-sm text-slate-500">Aucune valeur. Ajoute-en une ci-dessus.</p>'; $('stockCatalogRows').querySelectorAll('[data-stock-catalog-edit]').forEach(btn=>btn.addEventListener('click',()=>{const name=values[Number(btn.dataset.stockCatalogEdit)];catalogEdit=name;$('stockCatalogName').value=name;$('stockCatalogSaveButton').innerHTML='<i class="ph-bold ph-check mr-1"></i>Enregistrer';}));$('stockCatalogRows').querySelectorAll('[data-stock-catalog-delete]').forEach(btn=>btn.addEventListener('click',()=>stockCatalogDelete(kind,values[Number(btn.dataset.stockCatalogDelete)])));}
 window.stockCatalogSave=async e=>{e.preventDefault();const modal=$('stockCatalogModal'),kind=modal.dataset.kind,old=catalogEdit,name=$('stockCatalogName').value.trim(),button=$('stockCatalogSaveButton');if(!name)return;button.disabled=true;try{await api('/catalog/save',{method:'POST',body:JSON.stringify({type:kind,nom:name,ancien_nom:old||''})});catalogEdit=null;$('stockCatalogName').value='';await refresh();stockRenderCatalog();const select=$(kind==='categorie'?'siCategory':'siUnit');if(select){select.innerHTML=(kind==='categorie'?categories:units).map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');select.value=name;}button.innerHTML='<i class="ph-bold ph-plus mr-1"></i>Ajouter';toast(old?'Valeur mise à jour.':'Valeur ajoutée.');}catch(err){toast(err.message,true);}finally{button.disabled=false;}};
 async function stockCatalogDelete(kind,name){if(!confirm(`Supprimer « ${name} » de la liste ?`))return;try{await api('/catalog/delete',{method:'POST',body:JSON.stringify({type:kind,nom:name})});catalogEdit=null;await refresh();stockRenderCatalog();toast('Valeur supprimée.');}catch(err){toast(err.message,true);}}
 window.stockShowLots=id=>{const i=data.items.find(x=>Number(x.id)===Number(id));if(!i)return;const lots=data.lots.filter(l=>Number(l.item_id)===Number(id));stockOpenModal(`Lots · ${i.designation}`,lots.length?`<div class="overflow-x-auto"><table class="w-full min-w-[560px] text-sm"><thead class="bg-slate-100 text-left text-xs"><tr><th class="p-3">Lot</th><th class="p-3">Péremption</th><th class="p-3">Quantité</th><th class="p-3">Achat / unité stock</th><th class="p-3">Vente / unité stock</th><th class="p-3">État</th></tr></thead><tbody>${lots.map(l=>`<tr class="border-b"><td class="p-3 font-bold">${esc(l.numero_lot)}</td><td class="p-3">${esc(l.date_peremption||'—')}</td><td class="p-3">${fmt(l.quantite_actuelle)} ${esc(l.unite)}</td><td class="p-3">${l.prix_achat!=null?fmt(l.prix_achat)+' DA / '+esc(l.unite_stock||l.unite):'—'}</td><td class="p-3">${l.prix_vente!=null?fmt(l.prix_vente)+' DA / '+esc(l.unite_stock||l.unite):'—'}</td><td class="p-3">${l.perime?'<b class="text-rose-600">Périmé · sortie bloquée</b>':l.expire_bientot?'<b class="text-amber-600">Expire dans 90 jours</b>':'<span class="text-emerald-700">Disponible</span>'}</td></tr>`).join('')}</tbody></table></div>`:'<p class="rounded-2xl bg-slate-50 p-8 text-center text-sm text-slate-500">Aucun lot en stock pour cet article.</p>',null);$('stockSubmitButton').classList.add('hidden');};
 window.stockOpenDocument=(type,prefillId=null)=>{if(!canManage)return;const entry=type==='Entrée';window.stockReceiptPages=[];stockOpenModal(`Nouveau bon · ${type}`,`<input id="sdType" type="hidden" value="${esc(type)}"><div class="grid gap-3 sm:grid-cols-2">${entry?`<label class="block text-xs font-bold text-slate-600 dark:text-slate-300">Fournisseur<select id="sdSupplier" class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm dark:border-white/10 dark:bg-black/30 dark:text-white"><option value="">Choisir un fournisseur</option>${(data.suppliers||[]).filter(s=>s.actif).map(s=>`<option value="${s.id}">${esc(s.designation)}</option>`).join('')}</select></label>${field('Agent de réception *','sdReceiver','text','__RECEIVER__','required maxlength="180"')}`:''}${type!=='Entrée'?field('Service destinataire *','sdService'):''}${field('Motif','sdReason')}${field('Notes','sdNotes','text','','maxlength="1000"')}</div>${entry?`<section class="mt-4 rounded-2xl border border-sky-200 bg-sky-50/70 p-4 dark:border-sky-400/20 dark:bg-sky-400/[.05]"><div class="flex flex-wrap items-center justify-between gap-3"><div><b class="block text-sm text-slate-800 dark:text-white"><i class="ph-bold ph-scan text-sky-600 mr-1"></i>Numériser le bon d’entrée</b><p class="mt-1 text-[11px] text-slate-500">Scanne les pages une par une depuis le serveur ou le poste client, puis répète pour les pages suivantes.</p></div><div class="flex flex-wrap items-center gap-2"><select id="sdScanDevice" class="hidden max-w-xs rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs dark:border-white/10 dark:bg-black/30 dark:text-white"></select><button type="button" onclick="stockDetectReceiptScanner()" class="rounded-xl border border-sky-200 bg-white px-3 py-2 text-xs font-bold text-sky-800 hover:bg-sky-100 dark:border-white/10 dark:bg-white/5 dark:text-sky-200"><i class="ph-bold ph-printer mr-1"></i>Détecter le scanner</button><button type="button" onclick="stockScanReceiptPage()" class="rounded-xl bg-sky-700 px-3 py-2 text-xs font-extrabold text-white hover:bg-sky-800"><i class="ph-bold ph-scan mr-1"></i>Scanner une page</button></div></div><div id="sdScanStatus" class="mt-2 text-[11px] text-slate-500"></div><div id="sdScanPreview" class="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4"></div></section>`:''}<div class="mt-4 flex items-center justify-between"><b class="text-sm dark:text-white">Articles du bon</b><button type="button" onclick="stockAddLine()" class="rounded-xl bg-slate-100 px-3 py-2 text-xs font-bold dark:bg-white/10 dark:text-white"><i class="ph-bold ph-plus mr-1"></i>Ajouter une ligne</button></div><div id="stockDocLines" class="mt-3 space-y-2"></div>`,async()=>{const type=$('sdType').value;const rows=[...$('stockDocLines').querySelectorAll('.stock-doc-line')];const lines=rows.map(r=>({item_id:r.querySelector('.sd-item').value,quantite:r.querySelector('.sd-qty').value,lot_id:r.querySelector('.sd-lot')?.value||null,numero_lot:r.querySelector('.sd-lot-code')?.value||'',date_peremption:r.querySelector('.sd-expiry')?.value||'',prix_unitaire:r.querySelector('.sd-price')?.value||'',unite_stock:r.querySelector('.sd-stock-unit')?.value||'',quantite_par_unite:r.querySelector('.sd-quantity-per-unit')?.value||'1',prix_achat:r.querySelector('.sd-purchase-price')?.value||'',prix_vente:r.querySelector('.sd-sale-price')?.value||'',marge_vente:r.querySelector('.sd-margin')?.value||'15'}));const res=await api('/documents/create',{method:'POST',body:JSON.stringify({type_document:type,fournisseur_id:$('sdSupplier')?.value||'',agent_reception:$('sdReceiver')?.value||'',pages:window.stockReceiptPages||[],service_destination:$('sdService')?.value||'',motif:$('sdReason').value,notes:$('sdNotes').value,lignes:lines})});stockCloseModal();toast(`${res.numero} créé · ${res.statut}.`);await refresh();stockSetTab('documents');});stockAddLine(prefillId);};

window.stockAddLine=(prefillId=null)=>{const type=$('sdType').value,entry=type==='Entrée';const row=document.createElement('div');row.className='stock-doc-line grid gap-2 rounded-2xl border border-slate-200 p-3 '+(entry?'sm:grid-cols-2 2xl:grid-cols-[1.35fr_.65fr_.8fr_.8fr_.9fr_.9fr_.8fr_.65fr_.9fr]':'sm:grid-cols-[1.5fr_.7fr_1fr_1fr_.7fr_auto]')+' dark:border-white/10';const selectedItem=data.items.find(i=>Number(i.id)===Number(prefillId));const itemOptions=data.items.map(i=>`<option value="${i.id}" ${Number(i.id)===Number(prefillId)?'selected':''}>${esc(i.designation)} · ${esc(i.reference)}</option>`).join('');const unitOptions=units.map(unit=>`<option value="${esc(unit)}" ${unit===(selectedItem?.unite||units[0])?'selected':''}>${esc(unit)}</option>`).join('');row.innerHTML=`<label class="text-[10px] font-bold text-slate-500">Article<select class="sd-item mt-1 w-full rounded-lg border bg-slate-50 p-2 text-xs dark:border-white/10 dark:bg-black/30 dark:text-white"><option value="">Choisir…</option>${itemOptions}</select></label><label class="text-[10px] font-bold text-slate-500">${entry?'Quantité reçue':'Quantité'}<input class="sd-qty mt-1 w-full rounded-lg border bg-slate-50 p-2 text-xs dark:border-white/10 dark:bg-black/30 dark:text-white" type="number" min="0.001" step="0.001"></label>${entry?`<label class="text-[10px] font-bold text-slate-500">Unité de stock<select class="sd-stock-unit mt-1 w-full rounded-lg border bg-slate-50 p-2 text-xs dark:border-white/10 dark:bg-black/30 dark:text-white">${unitOptions}</select></label><label class="text-[10px] font-bold text-slate-500">Quantité par unité ( <span class="sd-base-unit">${esc(selectedItem?.unite||'unité')}</span> )<input class="sd-quantity-per-unit mt-1 w-full rounded-lg border bg-slate-50 p-2 text-xs dark:border-white/10 dark:bg-black/30 dark:text-white" type="number" min="0.001" step="0.001" value="1"><small class="sd-conversion mt-1 block font-medium text-slate-400">Stock final : —</small></label><label class="text-[10px] font-bold text-slate-500">N° lot<input class="sd-lot-code mt-1 w-full rounded-lg border bg-slate-50 p-2 text-xs dark:border-white/10 dark:bg-black/30 dark:text-white" placeholder="Lot"></label><label class="text-[10px] font-bold text-slate-500">Péremption<input class="sd-expiry mt-1 w-full rounded-lg border bg-slate-50 p-2 text-xs dark:border-white/10 dark:bg-black/30 dark:text-white" type="date"></label><label class="text-[10px] font-bold text-slate-500">Prix d’achat / unité stock<input class="sd-purchase-price mt-1 w-full rounded-lg border bg-slate-50 p-2 text-xs dark:border-white/10 dark:bg-black/30 dark:text-white" type="number" min="0" step="0.01" required></label><label class="text-[10px] font-bold text-slate-500">Marge vente (%)<input class="sd-margin mt-1 w-full rounded-lg border bg-slate-50 p-2 text-xs dark:border-white/10 dark:bg-black/30 dark:text-white" type="number" min="0" step="0.1" value="15"></label><label class="text-[10px] font-bold text-slate-500">Prix de vente / unité stock<input class="sd-sale-price mt-1 w-full rounded-lg border bg-slate-50 p-2 text-xs dark:border-white/10 dark:bg-black/30 dark:text-white" type="number" min="0" step="0.01" required></label>`:type==='Sortie'?`<label class="text-[10px] font-bold text-slate-500">Lot (facultatif)<select class="sd-lot mt-1 w-full rounded-lg border bg-slate-50 p-2 text-xs dark:border-white/10 dark:bg-black/30 dark:text-white"><option value="">Auto · FEFO</option></select></label><span class="self-end pb-2 text-[10px] text-slate-400">Lots expirés bloqués</span>`:'<span class="sm:col-span-3 self-center text-[10px] text-slate-400">La commande interne ne modifie pas le stock.</span>'}${entry?'':`<button type="button" onclick="this.closest('.stock-doc-line').remove()" class="h-8 w-8 self-end rounded-lg bg-rose-50 text-rose-600" title="Retirer la ligne"><i class="ph-bold ph-trash"></i></button>`}`;const updateConversion=()=>{const qty=Number(row.querySelector('.sd-qty').value||0),factor=Number(row.querySelector('.sd-quantity-per-unit')?.value||1),baseUnit=row.querySelector('.sd-base-unit')?.textContent||'';const label=row.querySelector('.sd-conversion');if(label)label.textContent=qty>0?`Stock final : ${(qty*factor).toLocaleString('fr-FR',{maximumFractionDigits:3})} ${baseUnit}`:'Stock final : —';};const updateSale=()=>{const purchase=Number(row.querySelector('.sd-purchase-price')?.value),margin=Number(row.querySelector('.sd-margin')?.value);const sale=row.querySelector('.sd-sale-price');if(sale&&Number.isFinite(purchase)&&purchase>=0&&Number.isFinite(margin)&&margin>=0)sale.value=(purchase*(1+margin/100)).toFixed(2);};row.querySelector('.sd-item').addEventListener('change',()=>{if(!entry){stockFillLots(row);return;}const selected=data.items.find(i=>Number(i.id)===Number(row.querySelector('.sd-item').value));row.querySelector('.sd-base-unit').textContent=selected?.unite||'unité';if(!row.dataset.unitTouched&&selected?.unite)row.querySelector('.sd-stock-unit').value=selected.unite;updateConversion();});row.querySelector('.sd-qty').addEventListener('input',updateConversion);if(entry){row.querySelector('.sd-stock-unit').addEventListener('change',()=>row.dataset.unitTouched='1');row.querySelector('.sd-quantity-per-unit').addEventListener('input',updateConversion);row.querySelector('.sd-purchase-price').addEventListener('input',updateSale);row.querySelector('.sd-margin').addEventListener('input',updateSale);} $('stockDocLines').appendChild(row);if(prefillId&&!entry)stockFillLots(row);updateConversion();};

function stockFillLots(row){const select=row.querySelector('.sd-lot');if(!select)return;const id=Number(row.querySelector('.sd-item').value);const lots=data.lots.filter(l=>Number(l.item_id)===id&&!l.perime);select.innerHTML='<option value="">Auto · FEFO</option>'+lots.map(l=>`<option value="${l.id}">${esc(l.numero_lot)} · ${fmt(l.quantite_actuelle)} ${esc(l.unite)}${l.date_peremption?' · '+esc(l.date_peremption):''}</option>`).join('');}
 window.stockSubmitForm=async e=>{e.preventDefault();const btn=$('stockSubmitButton');if(btn)btn.disabled=true;try{if(activeForm)await activeForm();}catch(err){toast(err.message,true);}finally{if(btn)btn.disabled=false;}};
 window.stockCloseOrder=async id=>{try{await api(`/documents/${id}/close`,{method:'POST',body:'{}'});toast('Commande interne clôturée.');await refresh();}catch(e){toast(e.message,true);}};
 window.stockPrintDocument=id=>{const d=data.documents.find(x=>Number(x.id)===Number(id));if(!d)return;const input=d.type_document==='Entrée'?'Bon d’entrée':d.type_document==='Sortie'?'Bon de sortie':'Bon de commande interne';$('stockPrintTitle').textContent=`${input} · ${d.numero}`;const total=d.lignes.reduce((a,l)=>a+Number(l.quantite||0)*Number(l.prix_unitaire||0),0);$('stockPrintSheet').innerHTML=`<div style="font-family:Arial,sans-serif;color:#17231b"><div style="display:flex;justify-content:space-between;align-items:flex-start;border-bottom:3px solid #1c482a;padding-bottom:16px"><div><div style="font-size:24px;font-weight:800;color:#183b20">CLINIVA</div><div style="font-size:11px;color:#64746a;margin-top:4px">Gestion des stocks · Document interne</div></div><div style="text-align:right"><div style="font-size:20px;font-weight:800">${esc(input)}</div><div style="font-size:13px;font-weight:700;color:#238342;margin-top:5px">${esc(d.numero)}</div></div></div><div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:18px 0;font-size:12px"><div><b>Date :</b> ${esc(dateTime(d.created_at))}</div>${d.type_document==='Entrée'&&d.fournisseur?`<div><b>Fournisseur :</b> ${esc(d.fournisseur)}</div>`:''}<div><b>Statut :</b> ${esc(d.statut)}</div><div><b>Service destinataire :</b> ${esc(d.service_destination||'Stock central')}</div><div><b>Demandeur :</b> ${esc(d.demandeur)}</div>${d.agent_reception?`<div><b>Agent de réception :</b> ${esc(d.agent_reception)}</div>`:''}${d.motif?`<div style="grid-column:1/-1"><b>Motif :</b> ${esc(d.motif)}</div>`:''}</div><table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr style="background:#eaf1eb"><th style="padding:9px;border:1px solid #cbd5ce;text-align:left">Réf.</th><th style="padding:9px;border:1px solid #cbd5ce;text-align:left">Désignation</th><th style="padding:9px;border:1px solid #cbd5ce;text-align:left">Lot / péremption</th>${d.type_document==='Entrée'?'<th style="padding:9px;border:1px solid #cbd5ce;text-align:right">Qté reçue / stock</th><th style="padding:9px;border:1px solid #cbd5ce;text-align:right">Prix achat / unité stock</th><th style="padding:9px;border:1px solid #cbd5ce;text-align:right">Marge</th><th style="padding:9px;border:1px solid #cbd5ce;text-align:right">Prix vente / unité stock</th>':'<th style="padding:9px;border:1px solid #cbd5ce;text-align:right">Quantité</th>'}</tr></thead><tbody>${d.lignes.map(l=>`<tr><td style="padding:9px;border:1px solid #cbd5ce">${esc(l.item_reference)}</td><td style="padding:9px;border:1px solid #cbd5ce">${esc(l.item_designation)}</td><td style="padding:9px;border:1px solid #cbd5ce">${esc(l.lots_utilises||'—')}${l.date_peremption?' · '+esc(l.date_peremption):''}</td><td style="padding:9px;border:1px solid #cbd5ce;text-align:right">${d.type_document==='Entrée'?`${fmt(l.quantite_unite_stock||l.quantite)} ${esc(l.unite_stock||l.unite)}<br><small>→ ${fmt(l.quantite)} ${esc(l.unite)}</small>`:`${fmt(l.quantite)} ${esc(l.unite)}`}</td>${d.type_document==='Entrée'?`<td style="padding:9px;border:1px solid #cbd5ce;text-align:right">${fmt(l.prix_achat??l.prix_unitaire)} DA</td><td style="padding:9px;border:1px solid #cbd5ce;text-align:right">${fmt(l.marge_vente||0)} %</td><td style="padding:9px;border:1px solid #cbd5ce;text-align:right">${fmt(l.prix_vente||0)} DA</td>`:''}</tr>`).join('')}</tbody>${d.type_document==='Entrée'&&total?`<tfoot><tr><td colspan="6" style="padding:9px;text-align:right;font-weight:bold">Total valorisé</td><td style="padding:9px;text-align:right;font-weight:bold">${fmt(total)} DA</td></tr></tfoot>`:''}</table>${d.notes?`<div style="margin-top:14px;padding:10px;border:1px solid #d5ded7"><b>Notes :</b> ${esc(d.notes)}</div>`:''}<div style="display:grid;grid-template-columns:1fr 1fr;gap:40px;margin-top:55px;font-size:12px;text-align:center"><div>Remis par :<br><br><br>Signature</div><div>Reçu par :<br><br><br>Signature</div></div><div style="margin-top:35px;border-top:1px solid #cbd5ce;padding-top:8px;text-align:center;font-size:9px;color:#77837a">Document généré par Cliniva · ${esc(d.numero)}</div></div>`;$('stockPrintModal').classList.remove('hidden');$('stockPrintModal').classList.add('flex');};
 window.stockClosePrint=()=>{$('stockPrintModal').classList.add('hidden');$('stockPrintModal').classList.remove('flex');};
 window.addEventListener('keydown',e=>{if(e.key==='Escape'){stockCloseModal();stockCloseCatalog();stockClosePrint();stockCloseKpi();stockCloseCropper();stockCloseProduct();}});
 if(canManage){const actions=document.querySelector('#stockModule header .flex.flex-wrap.gap-2');actions.innerHTML='<button onclick="stockNewItem()" class="inline-flex items-center gap-2 rounded-xl border border-white/20 bg-white/10 px-4 py-2.5 text-xs font-bold text-white hover:bg-white/20"><i class="ph-bold ph-plus-circle"></i>Nouvel article</button><button onclick="stockOpenDocument(\'Entrée\')" class="inline-flex items-center gap-2 rounded-xl bg-brand-neon px-4 py-2.5 text-xs font-extrabold text-black shadow"><i class="ph-bold ph-arrow-down-left"></i>Bon d’entrée</button><button onclick="stockOpenDocument(\'Sortie\')" class="inline-flex items-center gap-2 rounded-xl bg-amber-400 px-4 py-2.5 text-xs font-extrabold text-black shadow"><i class="ph-bold ph-arrow-up-right"></i>Bon de sortie</button><button onclick="stockOpenDocument(\'Commande interne\')" class="inline-flex items-center gap-2 rounded-xl border border-white/20 bg-white/10 px-4 py-2.5 text-xs font-bold text-white hover:bg-white/20"><i class="ph-bold ph-clipboard-text"></i>Commande interne</button>';}
 // Recherche Google Images seule : préchargement dès la saisie, debounce et cache client.
 window.stockPhotoSearchSource='google';
 window.stockGoogleBrowserCache=window.stockGoogleBrowserCache||new Map();
 window.stockGooglePendingRequests=window.stockGooglePendingRequests||new Map();
 window.stockGoogleFetch=async query=>{const key=query.trim().toLocaleLowerCase();const cached=window.stockGoogleBrowserCache.get(key);if(cached&&Date.now()-cached.at<180000)return cached.data;if(window.stockGooglePendingRequests.has(key))return window.stockGooglePendingRequests.get(key);const pending=api('/photo-search/google?q='+encodeURIComponent(query)).then(data=>{window.stockGoogleBrowserCache.set(key,{at:Date.now(),data});return data;}).finally(()=>window.stockGooglePendingRequests.delete(key));window.stockGooglePendingRequests.set(key,pending);return pending;};
 window.stockRenderGoogleResults=(query,photos)=>{const results=$('siPhotoResults');if(!photos.length){results.innerHTML=`<div class="rounded-2xl border border-amber-200 bg-amber-50 px-5 py-8 text-center text-sm text-amber-900"><i class="ph-bold ph-image-broken mb-2 block text-2xl"></i><b>Aucune image trouvée pour « ${esc(query)} ».</b><p class="mt-2 text-xs">Essaie un nom de produit plus précis.</p></div>`;return;}window.stockInternetPhotoResults=photos;results.innerHTML=`<div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">${photos.map((photo,index)=>`<article class="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-white/10 dark:bg-black/20"><div class="grid h-40 place-items-center bg-slate-100 dark:bg-white/5"><img src="/api/gestion-stock/photo-google-thumbnail?url=${encodeURIComponent(photo.thumbnail)}" alt="${esc(photo.title)}" loading="lazy" class="h-full w-full object-contain" onerror="this.closest('div').innerHTML='<span class=&quot;text-xs text-slate-400&quot;>Aperçu indisponible</span>'"></div><div class="space-y-2 p-3"><b class="block truncate text-xs text-slate-800 dark:text-white" title="${esc(photo.title)}">${esc(photo.title)}</b><div class="text-[10px] text-slate-500">${esc(photo.creator||'Source non indiquée')} · Licence à vérifier</div><div class="flex items-center justify-between gap-2"><a href="${esc(photo.source_url)}" target="_blank" rel="noopener noreferrer" class="text-[10px] font-semibold text-sky-700 underline">Voir la source</a><button type="button" onclick="stockUseInternetPhoto(${index})" class="inline-flex items-center justify-center gap-2 rounded-lg bg-emerald-700 px-3 py-2 text-[10px] font-extrabold text-white hover:bg-emerald-800"><i class="ph-bold ph-check"></i>Choisir</button></div></div></article>`).join('')}</div>`;};
 window.stockRunInternetPhotoSearch=async()=>{const query=$('siPhotoQuery')?.value.trim();const results=$('siPhotoResults');if(!query||query.length<2){results.innerHTML='<div class="py-10 text-center text-sm text-amber-700">Saisis au moins deux caractères.</div>';return;}const runId=(window.stockGoogleSearchRunId||0)+1;window.stockGoogleSearchRunId=runId;results.innerHTML='<div class="grid min-h-28 place-items-center text-sm text-slate-500"><span><i class="ph-bold ph-spinner animate-spin mr-2"></i>Résultats Google Images…</span></div>';try{const data=await window.stockGoogleFetch(query);if(runId!==window.stockGoogleSearchRunId||$('siPhotoQuery')?.value.trim()!==query)return;window.stockRenderGoogleResults(query,data.results||[]);}catch(error){if(runId===window.stockGoogleSearchRunId)results.innerHTML=`<div class="rounded-xl bg-rose-50 p-4 text-sm text-rose-700">${esc(error.message||'Recherche Google Images impossible.')}</div>`;}};
 window.stockScheduleGooglePrefetch=(query,display=false)=>{clearTimeout(window.stockGooglePrefetchTimer);query=(query||'').trim();if(query.length<2)return;window.stockGooglePrefetchTimer=setTimeout(async()=>{if(display)window.stockRunInternetPhotoSearch();else{try{await window.stockGoogleFetch(query);}catch{}}},450);};
 window.stockEnsurePhotoSources=()=>{const query=$('siPhotoQuery');if(!query)return;const bar=query.parentElement;if(bar.dataset.sourcesReady)return;bar.dataset.sourcesReady='1';const controls=document.createElement('div');controls.className='flex w-full items-center justify-end';controls.innerHTML='<button type="button" onclick="stockConfigureGoogleImages()" class="rounded-lg border border-slate-200 px-3 py-2 text-[11px] font-bold text-slate-600"><i class="ph-bold ph-gear-six mr-1"></i>Configurer Google</button>';bar.parentElement.insertBefore(controls,bar);};
 window.stockSetPhotoProvider=()=>{window.stockPhotoSearchSource='google';window.stockRunInternetPhotoSearch();};
 const stockOpenGoogleSearch=window.stockSearchInternetPhoto;window.stockSearchInternetPhoto=()=>{window.stockPhotoSearchSource='google';stockOpenGoogleSearch();window.stockEnsurePhotoSources();const modal=$('stockInternetPhotoModal');const description=modal?.querySelector('p');if(description)description.textContent='Recherche Google Images via SerpApi. Vérifie les droits d’utilisation auprès de la source.';};
 window.stockUseInternetPhoto=async index=>{const photo=window.stockInternetPhotoResults?.[index];if(!photo)return;try{const url='/api/gestion-stock/photo-google-thumbnail?url='+encodeURIComponent(photo.thumbnail);const response=await fetch(url,{credentials:'same-origin'});if(!response.ok){let message='Impossible de récupérer cette photo.';try{const body=await response.json();message=body.detail||message;}catch{}throw new Error(message);}const blob=await response.blob();const extension=blob.type==='image/png'?'png':blob.type==='image/webp'?'webp':'jpg';const file=new File([blob],`google_${photo.id||index+1}.${extension}`,{type:blob.type});const transfer=new DataTransfer();transfer.items.add(file);$('siPhoto').files=transfer.files;window.stockPreviewPhoto({target:$('siPhoto')},true);$('siPhotoCredit').value=photo.creator||'';$('siPhotoLicense').value='Licence à vérifier auprès de la source';$('siPhotoSource').value=photo.source_url||'';window.stockSelectedInternetPhoto=photo;window.stockCloseInternetPhoto();toast('Photo ajoutée à l’aperçu. Elle sera enregistrée avec l’article.');}catch(error){toast(error.message||'Téléchargement impossible.',true);}};
 document.addEventListener('input',event=>{if(event.target?.id==='siName'||event.target?.id==='siReference'){const query=[$('siReference')?.value,$('siName')?.value].map(value=>(value||'').trim()).filter(Boolean).join(' ');window.stockScheduleGooglePrefetch(query,false);}else if(event.target?.id==='siPhotoQuery'){window.stockGoogleSearchRunId=(window.stockGoogleSearchRunId||0)+1;window.stockScheduleGooglePrefetch(event.target.value,true);}});
 refresh();window.setInterval(refresh,7000);
 })();
 </script>
</section>
'''.replace("__MANAGE__", "true" if can_manage else "false")
        .replace("__RECEIVER__", escape(f"{user.get('prenom', '')} {user.get('nom', '')}".strip()))
        .replace("__SUPPLIER_TAB__", '<button type="button" data-stock-tab="suppliers" onclick="stockSetTab(\'suppliers\')" class="rounded-xl px-4 py-2.5 text-xs font-bold text-slate-500 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-white/5"><i class="ph-bold ph-truck mr-1"></i>Gestion fournisseurs</button>' if can_manage else '')
        .replace("__SUPPLIER_PANEL__", '<section id="stockPanelSuppliers" data-stock-panel="suppliers" class="hidden rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15] sm:p-5"><div class="mb-5 flex flex-wrap items-center justify-between gap-3"><div><h2 class="font-extrabold dark:text-white">Gestion fournisseurs</h2><p class="text-xs text-slate-500">Coordonnées et informations des fournisseurs des bons d’entrée.</p></div><button type="button" onclick="stockEditSupplier()" class="inline-flex items-center gap-2 rounded-xl bg-brand-neon px-4 py-2.5 text-xs font-extrabold text-black"><i class="ph-bold ph-plus-circle"></i>Nouveau fournisseur</button></div><div class="overflow-x-auto rounded-2xl border border-slate-100 dark:border-white/10"><table class="w-full min-w-[850px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600 dark:bg-black/20 dark:text-slate-400"><tr><th class="rounded-l-xl px-3 py-3">Fournisseur / secteur</th><th class="px-3 py-3">Contact</th><th class="px-3 py-3">Téléphone / e-mail</th><th class="px-3 py-3">Adresse</th><th class="px-3 py-3">État</th><th class="rounded-r-xl px-3 py-3 text-right">Actions</th></tr></thead><tbody id="stockSupplierRows"><tr><td colspan="6" class="p-8 text-center text-slate-400">Chargement…</td></tr></tbody></table></div></section>'))
