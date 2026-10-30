"""Gestion multicatégorie des stocks, lots et bons internes Cliniva."""

from decimal import Decimal, InvalidOperation
from html import escape
import json
import mimetypes
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from app.auth import get_current_user, track_action
from database import DOCS_DIR, get_db_cursor

stock_router = APIRouter(prefix="/api/gestion-stock", tags=["Gestion Stock"])

STOCK_CATALOG_TYPES = {"categorie": ("stock_categories", "categorie"), "unite": ("stock_units", "unite")}
STOCK_DOCUMENT_TYPES = {"Entrée", "Sortie", "Commande interne"}


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


def _stock_document_data(cursor, document_ids: list[int]) -> list[dict[str, Any]]:
    if not document_ids:
        return []
    cursor.execute("""SELECT d.id, d.numero, d.type_document, d.statut, d.service_destination,
            d.demandeur, d.motif, d.notes, d.created_at, d.validated_at,
            COALESCE(u.prenom || ' ' || u.nom, d.demandeur, '') AS operateur
        FROM stock_documents d LEFT JOIN users u ON u.id=d.created_by
        WHERE d.id = ANY(%s) ORDER BY d.created_at DESC, d.id DESC""", (document_ids,))
    documents = [dict(row) for row in cursor.fetchall()]
    cursor.execute("""SELECT l.id, l.document_id, l.item_id, l.item_reference, l.item_designation,
            l.unite, l.quantite, l.numero_lot, l.date_peremption, l.prix_unitaire,
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
    for document in documents:
        document["lignes"] = lines_by_doc.get(document["id"], [])
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
        cursor.execute("""SELECT i.id, i.reference, i.designation, i.categorie, i.unite, i.photo_path,
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
        "categories": categories, "unites": units,
        "summary": {"articles": item_count, "alertes_stock": low_stock,
                    "lots_perimes": len([lot for lot in lots if lot.get("perime")]),
                    "lots_proches": expiring_count, "quantite_perimee": expired_qty},
    }


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
    uploaded_photo_path = ""
    uploaded_photo_file: Path | None = None
    photo_content: bytes | None = None
    photo_suffix = ""
    if isinstance(photo, UploadFile) and photo.filename:
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
                cursor.execute("SELECT photo_path FROM stock_items WHERE id=%s AND actif=TRUE FOR UPDATE", (item_id,))
                existing = cursor.fetchone()
                if not existing:
                    raise HTTPException(status_code=404, detail="Article introuvable ou archivé.")
                old_photo_path = existing["photo_path"] or ""
                photo_path = uploaded_photo_path or ("" if remove_photo else old_photo_path)
                cursor.execute("""UPDATE stock_items SET reference=%s, designation=%s, categorie=%s,
                    unite=%s, seuil_alerte=%s, suivi_peremption=%s, notes=%s, photo_path=%s,
                    updated_at=CURRENT_TIMESTAMP WHERE id=%s AND actif=TRUE RETURNING id""",
                    (reference, designation, category, unit, threshold, track_expiry, notes, photo_path, item_id))
                row = cursor.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Article introuvable ou archivé.")
                saved_id = item_id
            else:
                cursor.execute("""INSERT INTO stock_items
                    (reference, designation, categorie, unite, seuil_alerte, suivi_peremption, notes, photo_path, created_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (reference, designation, category, unit, threshold, track_expiry, notes, uploaded_photo_path, user["id"]))
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
    service = str(data.get("service_destination") or "").strip()
    requester = f"{user.get('prenom', '')} {user.get('nom', '')}".strip()
    reason = str(data.get("motif") or "").strip()
    notes = str(data.get("notes") or "").strip()
    if document_type in {"Sortie", "Commande interne"} and not service:
        raise HTTPException(status_code=400, detail="Le service destinataire est obligatoire pour ce bon.")
    if len(service) > 180 or len(reason) > 250 or len(notes) > 2000:
        raise HTTPException(status_code=400, detail="Les informations du bon dépassent la longueur autorisée.")
    normalized = []
    seen: set[tuple[int, int | None]] = set()
    for index, line in enumerate(lines, start=1):
        try:
            item_id = int(line.get("item_id"))
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(status_code=400, detail=f"Article invalide à la ligne {index}.") from exc
        quantity = _decimal(line.get("quantite"), f"ligne {index}")
        lot_id = None
        if document_type == "Sortie" and line.get("lot_id"):
            try:
                lot_id = int(line["lot_id"])
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=f"Lot invalide à la ligne {index}.") from exc
        if document_type != "Sortie":
            lot_id = None
        if (item_id, lot_id) in seen:
            raise HTTPException(status_code=400, detail=f"La ligne {index} répète le même article et le même lot.")
        seen.add((item_id, lot_id))
        normalized.append({
            "item_id": item_id, "quantite": quantity, "lot_id": lot_id,
            "numero_lot": str(line.get("numero_lot") or "").strip(),
            "date_peremption": _date(line.get("date_peremption"), "péremption"),
            "prix_unitaire": _decimal(line.get("prix_unitaire"), "prix unitaire", allow_zero=True)
                if line.get("prix_unitaire") not in (None, "") else None,
        })

    prefix = {"Entrée": "BE", "Sortie": "BS", "Commande interne": "BCI"}[document_type]
    status = "En attente" if document_type == "Commande interne" else "Validé"
    with get_db_cursor() as cursor:
        cursor.execute("""INSERT INTO stock_documents
            (type_document, statut, service_destination, demandeur, motif, notes,
             created_by, validated_by, validated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                    CASE WHEN %s='Validé' THEN CURRENT_TIMESTAMP ELSE NULL END)
            RETURNING id""",
            (document_type, status, service, requester, reason, notes, user["id"],
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
                    (item_id, numero_lot, date_peremption, quantite_initiale, quantite_actuelle, prix_unitaire)
                    VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (item["id"], lot_number, line["date_peremption"], line["quantite"], line["quantite"], line["prix_unitaire"]))
                lot_id = cursor.fetchone()["id"]
                line["lot_id"] = lot_id
                line["numero_lot"] = lot_number
                cursor.execute("""INSERT INTO stock_document_lines
                    (document_id, item_id, item_reference, item_designation, unite, quantite,
                     lot_id, numero_lot, date_peremption, prix_unitaire)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (document_id, item["id"], item["reference"], item["designation"], item["unite"],
                     line["quantite"], lot_id, lot_number, line["date_peremption"], line["prix_unitaire"]))
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
    track_action(request, "STOCK_DOCUMENT_CREATE", f"{document_type} {document_number} créé · {service or 'stock central'}")
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
    return r'''
<style>
@media print { body * { visibility:hidden !important; } #stockPrintModal, #stockPrintModal * { visibility:visible !important; } #stockPrintModal { position:absolute!important; inset:0!important; display:block!important; padding:0!important; background:#fff!important; } #stockPrintModal > div { width:100%!important; max-width:none!important; max-height:none!important; border:0!important; box-shadow:none!important; overflow:visible!important; } #stockPrintSheet { padding:8mm!important; color:#111!important; } .stock-no-print { display:none!important; } }
</style>
<section id="stockModule" class="space-y-5 text-slate-800" data-stock-manage="__MANAGE__">
 <header class="flex flex-wrap items-center justify-between gap-4 rounded-[2rem] bg-gradient-to-r from-[#101b13] to-[#183b20] px-7 py-6 text-white shadow-lg">
  <div class="flex items-center gap-4"><span class="grid h-12 w-12 place-items-center rounded-2xl bg-brand-neon text-emerald-950 shadow-lg"><i class="ph-bold ph-package text-2xl"></i></span><div><p class="text-xs font-bold uppercase tracking-[.18em] text-lime-300">Gestion opérationnelle · Inventaire</p><h1 class="mt-1 text-2xl font-extrabold">Gestion Stock</h1><p class="mt-1 text-sm text-slate-300">Pharmacie, informatique, technique, sanitaire et pièces.</p></div></div>
  <div class="flex flex-wrap gap-2">__ACTIONS__</div>
 </header>
 <section class="grid grid-cols-2 gap-3 xl:grid-cols-4">
  <article class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Articles actifs</span><i class="ph-bold ph-package text-xl text-brand-neon"></i></div><div id="stockKpiItems" class="mt-2 text-2xl font-extrabold dark:text-white">—</div></article>
  <article class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Sous le seuil</span><i class="ph-bold ph-warning-circle text-xl text-amber-500"></i></div><div id="stockKpiLow" class="mt-2 text-2xl font-extrabold dark:text-white">—</div></article>
  <article class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Péremption ≤ 90 j</span><i class="ph-bold ph-calendar-clock text-xl text-sky-500"></i></div><div id="stockKpiExpiring" class="mt-2 text-2xl font-extrabold dark:text-white">—</div></article>
  <article class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15]"><div class="flex items-center justify-between"><span class="text-[10px] font-bold uppercase tracking-wider text-slate-500">Lots périmés</span><i class="ph-bold ph-prohibit text-xl text-rose-500"></i></div><div id="stockKpiExpired" class="mt-2 text-2xl font-extrabold dark:text-white">—</div></article>
 </section>
 <nav class="flex w-fit max-w-full flex-wrap items-center gap-1 rounded-2xl border border-slate-200/80 bg-white p-1.5 shadow-sm dark:border-white/10 dark:bg-[#121c15]">
  <button type="button" data-stock-tab="inventory" onclick="stockSetTab('inventory')" class="rounded-xl bg-brand-neon px-4 py-2.5 text-xs font-bold text-black"><i class="ph-bold ph-books mr-1"></i>Inventaire & lots</button>
  <button type="button" data-stock-tab="documents" onclick="stockSetTab('documents')" class="rounded-xl px-4 py-2.5 text-xs font-bold text-slate-500 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-white/5"><i class="ph-bold ph-receipt mr-1"></i>Bons & mouvements</button>
 </nav>
 <section id="stockPanelInventory" data-stock-panel="inventory" class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15] sm:p-5">
  <div class="mb-4 flex flex-wrap items-center justify-between gap-3"><div><h2 class="font-extrabold dark:text-white">Inventaire des articles</h2><p class="text-xs text-slate-500">Le stock disponible est recalculé après chaque bon validé. Actualisation automatique.</p></div><div class="flex flex-wrap gap-2"><select id="stockCategoryFilter" onchange="stockRenderInventory()" class="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm dark:border-white/10 dark:bg-black/30 dark:text-white"><option value="">Toutes les catégories</option></select><input id="stockSearch" oninput="stockRenderInventory()" class="w-44 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm dark:border-white/10 dark:bg-black/30 dark:text-white" placeholder="Rechercher"></div></div>
  <div class="overflow-x-auto"><table class="w-full min-w-[940px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600 dark:bg-black/20 dark:text-slate-400"><tr><th class="rounded-l-xl px-3 py-3">Référence / Article</th><th class="px-3 py-3">Catégorie</th><th class="px-3 py-3">Lots</th><th class="px-3 py-3">Stock disponible</th><th class="px-3 py-3">Seuil</th><th class="px-3 py-3">Péremption</th><th class="rounded-r-xl px-3 py-3 text-right">Actions</th></tr></thead><tbody id="stockItemRows"><tr><td colspan="7" class="p-8 text-center text-slate-400">Chargement…</td></tr></tbody></table></div>
 </section>
 <section id="stockPanelDocuments" data-stock-panel="documents" class="hidden space-y-5">
  <div class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15] sm:p-5"><div class="mb-4 flex flex-wrap items-center justify-between gap-3"><div><h2 class="font-extrabold dark:text-white">Bons d’entrée, de sortie et commandes internes</h2><p class="text-xs text-slate-500">Les bons d’entrée et de sortie mettent le stock à jour dès leur validation.</p></div><input id="stockDocumentSearch" oninput="stockRenderDocuments()" class="w-52 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm dark:border-white/10 dark:bg-black/30 dark:text-white" placeholder="Rechercher un bon"></div><div class="overflow-x-auto"><table class="w-full min-w-[900px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600 dark:bg-black/20 dark:text-slate-400"><tr><th class="rounded-l-xl px-3 py-3">N° / Date</th><th class="px-3 py-3">Type de bon</th><th class="px-3 py-3">Service</th><th class="px-3 py-3">Demandeur</th><th class="px-3 py-3">Articles</th><th class="px-3 py-3">État</th><th class="rounded-r-xl px-3 py-3 text-right">Actions</th></tr></thead><tbody id="stockDocumentRows"><tr><td colspan="7" class="p-8 text-center text-slate-400">Chargement…</td></tr></tbody></table></div></div>
  <div class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm dark:border-white/5 dark:bg-[#121c15] sm:p-5"><div class="mb-4"><h2 class="font-extrabold dark:text-white">Mouvements récents</h2><p class="text-xs text-slate-500">Traçabilité par article, lot, bon, opérateur et date.</p></div><div class="overflow-x-auto"><table class="w-full min-w-[800px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600 dark:bg-black/20 dark:text-slate-400"><tr><th class="rounded-l-xl px-3 py-3">Date / Bon</th><th class="px-3 py-3">Article</th><th class="px-3 py-3">Lot</th><th class="px-3 py-3">Mouvement</th><th class="px-3 py-3">Quantité</th><th class="rounded-r-xl px-3 py-3">Opérateur</th></tr></thead><tbody id="stockMovementRows"><tr><td colspan="6" class="p-8 text-center text-slate-400">Aucun mouvement.</td></tr></tbody></table></div></div>
 </section>
 <div id="stockModal" class="fixed inset-0 z-[2147483600] hidden items-center justify-center bg-slate-950/65 p-3 backdrop-blur-sm"><div class="max-h-[94vh] w-full max-w-4xl overflow-y-auto rounded-3xl bg-white shadow-2xl dark:bg-[#121c15]"><div class="sticky top-0 z-10 flex items-center justify-between bg-gradient-to-r from-[#101b13] to-[#183b20] px-5 py-4 text-white"><div><div class="text-[10px] font-bold uppercase tracking-widest text-brand-neon">Gestion Stock</div><h3 id="stockModalTitle" class="text-lg font-black">Formulaire</h3></div><button type="button" onclick="stockCloseModal()" class="grid h-9 w-9 place-items-center rounded-xl bg-white/10 hover:bg-white/20" aria-label="Fermer"><i class="ph-bold ph-x"></i></button></div><form id="stockForm" onsubmit="stockSubmitForm(event)" class="space-y-4 p-5"><div id="stockFormFields"></div><div class="flex justify-end gap-2 border-t border-slate-100 pt-4 dark:border-white/10"><button type="button" onclick="stockCloseModal()" class="rounded-xl border border-slate-200 px-4 py-2.5 text-sm font-bold dark:border-white/10 dark:text-slate-200">Annuler</button><button id="stockSubmitButton" class="rounded-xl bg-brand-neon px-5 py-2.5 text-sm font-extrabold text-black shadow"><i class="ph-bold ph-check mr-1"></i>Valider</button></div></form></div></div>
 <div id="stockCatalogModal" class="fixed inset-0 z-[2147483604] hidden items-center justify-center bg-slate-950/60 p-3 backdrop-blur-sm"><div class="max-h-[86vh] w-full max-w-xl overflow-y-auto rounded-3xl bg-white shadow-2xl dark:bg-[#121c15]"><div class="sticky top-0 z-10 flex items-center justify-between bg-gradient-to-r from-[#101b13] to-[#183b20] px-5 py-4 text-white"><div><div class="text-[10px] font-bold uppercase tracking-widest text-brand-neon">Paramètres des articles</div><h3 id="stockCatalogTitle" class="text-lg font-black">Gérer la liste</h3></div><button type="button" onclick="stockCloseCatalog()" class="grid h-9 w-9 place-items-center rounded-xl bg-white/10 hover:bg-white/20" aria-label="Fermer"><i class="ph-bold ph-x"></i></button></div><form id="stockCatalogForm" onsubmit="stockCatalogSave(event)" class="space-y-4 p-5"><div><label id="stockCatalogLabel" for="stockCatalogName" class="block text-xs font-bold text-slate-600 dark:text-slate-300">Nom</label><div class="mt-2 flex gap-2"><input id="stockCatalogName" maxlength="80" required class="min-w-0 flex-1 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm dark:border-white/10 dark:bg-black/30 dark:text-white"><button id="stockCatalogSaveButton" class="rounded-xl bg-brand-neon px-4 py-2 text-xs font-extrabold text-black"><i class="ph-bold ph-plus mr-1"></i>Ajouter</button></div></div><div id="stockCatalogRows" class="max-h-[48vh] space-y-2 overflow-y-auto"></div><div class="flex justify-end border-t border-slate-100 pt-4 dark:border-white/10"><button type="button" onclick="stockCloseCatalog()" class="rounded-xl border border-slate-200 px-4 py-2.5 text-sm font-bold dark:border-white/10 dark:text-slate-200">Terminé</button></div></form></div></div>
 <div id="stockPrintModal" class="fixed inset-0 z-[2147483601] hidden items-center justify-center bg-slate-950/70 p-3 backdrop-blur-sm"><div class="max-h-[95vh] w-full max-w-3xl overflow-y-auto rounded-3xl bg-white shadow-2xl"><div class="stock-no-print sticky top-0 z-10 flex items-center justify-between bg-gradient-to-r from-[#101b13] to-[#183b20] px-5 py-3 text-white"><b id="stockPrintTitle">Bon de stock</b><div class="flex gap-2"><button type="button" onclick="window.print()" class="rounded-xl bg-brand-neon px-4 py-2 text-xs font-bold text-black"><i class="ph-bold ph-printer mr-1"></i>Imprimer / PDF</button><button type="button" onclick="stockClosePrint()" class="grid h-9 w-9 place-items-center rounded-xl bg-white/10" aria-label="Fermer"><i class="ph-bold ph-x"></i></button></div></div><div id="stockPrintSheet" class="p-8"></div></div></div>
 <div id="stockToast" class="fixed bottom-5 right-5 z-[2147483602] hidden max-w-sm rounded-xl bg-slate-900 px-4 py-3 text-sm font-semibold text-white shadow-xl"></div>
 <script>
 (()=>{
 const canManage=__MANAGE__, $=id=>document.getElementById(id), esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 let categories=[],units=[];let data={items:[],lots:[],documents:[],movements:[],summary:{}};let activeForm=null,catalogEdit=null;
 const toast=(msg,bad=false)=>{const t=$('stockToast');t.textContent=msg;t.className='fixed bottom-5 right-5 z-[2147483602] max-w-sm rounded-xl px-4 py-3 text-sm font-semibold text-white shadow-xl '+(bad?'bg-rose-700':'bg-slate-900');t.classList.remove('hidden');setTimeout(()=>t.classList.add('hidden'),4500)};
 const api=async(path,opts={})=>{const headers={...(opts.headers||{})};if(!(opts.body instanceof FormData))headers['Content-Type']='application/json';const r=await fetch('/api/gestion-stock'+path,{credentials:'same-origin',headers,...opts});const b=await r.json().catch(()=>({}));if(!r.ok)throw new Error(b.detail||'Une erreur est survenue.');return b;};
 window.stockSetTab=name=>{document.querySelectorAll('[data-stock-tab]').forEach(b=>{const on=b.dataset.stockTab===name;b.className='rounded-xl px-4 py-2.5 text-xs font-bold transition '+(on?'bg-brand-neon text-black shadow-md shadow-brand-neon/20':'text-slate-500 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-white/5')});document.querySelectorAll('[data-stock-panel]').forEach(p=>p.classList.toggle('hidden',p.dataset.stockPanel!==name));};
 function fillCategoryFilter(){const s=$('stockCategoryFilter'),selected=s.value;if(s.options.length===1)s.insertAdjacentHTML('beforeend',categories.map(c=>`<option>${esc(c)}</option>`).join(''));else{const options='<option value="">Toutes les catégories</option>'+categories.map(c=>`<option value="${esc(c)}">${esc(c)}</option>`).join('');s.innerHTML=options;s.value=selected;}}
 function stockRenderInventory(){fillCategoryFilter();const q=($('stockSearch').value||'').toLocaleLowerCase('fr'),cat=$('stockCategoryFilter').value;const rows=data.items.filter(i=>(!cat||i.categorie===cat)&&(!q||`${i.reference} ${i.designation} ${i.categorie}`.toLocaleLowerCase('fr').includes(q)));$('stockItemRows').innerHTML=rows.length?rows.map(i=>{const qty=Number(i.stock_disponible||0),low=Number(i.seuil_alerte||0)>0&&qty<=Number(i.seuil_alerte);const expiring=data.lots.filter(l=>Number(l.item_id)===Number(i.id)&&l.expire_bientot).length;const expired=Number(i.stock_perime||0)>0;return `<tr class="border-b border-slate-100 transition hover:bg-sky-50/60 dark:border-white/5 dark:hover:bg-white/[.03]"><td class="px-3 py-3"><div class="flex items-center gap-3"><span class="grid h-11 w-11 shrink-0 place-items-center overflow-hidden rounded-xl border border-slate-100 bg-slate-50 dark:border-white/10 dark:bg-white/5">${i.photo_path?`<img src="/api/gestion-stock/items/${i.id}/photo" alt="Photo ${esc(i.designation)}" loading="lazy" class="h-full w-full object-cover">`:'<i class="ph ph-image text-xl text-slate-300"></i>'}</span><span><b class="block text-slate-800 dark:text-white">${esc(i.designation)}</b><small class="text-xs text-slate-400">${esc(i.reference)}</small></span></div></td><td class="px-3 py-3"><span class="rounded-full bg-slate-100 px-2.5 py-1 text-[10px] font-bold text-slate-600 dark:bg-white/10 dark:text-slate-300">${esc(i.categorie)}</span></td><td class="px-3 py-3 font-semibold">${Number(i.nombre_lots||0)}</td><td class="px-3 py-3"><span class="font-extrabold ${low?'text-rose-600':'text-emerald-700 dark:text-emerald-300'}">${fmt(i.stock_disponible)} ${esc(i.unite)}</span>${expired?`<small class="block text-[10px] text-rose-500">${fmt(i.stock_perime)} périmé(s) bloqué(s)</small>`:''}</td><td class="px-3 py-3 text-slate-500">${fmt(i.seuil_alerte)} ${esc(i.unite)}</td><td class="px-3 py-3">${expired?'<span class="text-xs font-bold text-rose-600">Périmé</span>':expiring?`<span class="text-xs font-bold text-amber-600">${expiring} lot(s) bientôt</span>`:i.suivi_peremption?'<span class="text-xs text-emerald-600">Suivi actif</span>':'<span class="text-xs text-slate-400">—</span>'}</td><td class="px-3 py-3 text-right"><div class="flex justify-end gap-1"><button type="button" title="Lots" onclick="stockShowLots(${i.id})" class="grid h-8 w-8 place-items-center rounded-lg bg-sky-50 text-sky-700 hover:bg-sky-100"><i class="ph-bold ph-list-bullets"></i></button>${canManage?`<button type="button" title="Entrée" onclick="stockOpenDocument('Entrée',${i.id})" class="grid h-8 w-8 place-items-center rounded-lg bg-emerald-50 text-emerald-700 hover:bg-emerald-100"><i class="ph-bold ph-arrow-down-left"></i></button><button type="button" title="Sortie" onclick="stockOpenDocument('Sortie',${i.id})" class="grid h-8 w-8 place-items-center rounded-lg bg-amber-50 text-amber-700 hover:bg-amber-100"><i class="ph-bold ph-arrow-up-right"></i></button><button type="button" title="Modifier" onclick="stockEditItem(${i.id})" class="grid h-8 w-8 place-items-center rounded-lg bg-slate-100 text-slate-600 hover:bg-slate-200"><i class="ph-bold ph-pencil-simple"></i></button>`:''}</div></td></tr>`}).join(''):'<tr><td colspan="7" class="p-10 text-center text-slate-400">Aucun article ne correspond.</td></tr>';}
 function fmt(v){return Number(v||0).toLocaleString('fr-FR',{maximumFractionDigits:3});}
 function stockRenderDocuments(){const q=($('stockDocumentSearch').value||'').toLocaleLowerCase('fr');const docs=data.documents.filter(d=>`${d.numero} ${d.type_document} ${d.service_destination} ${d.demandeur}`.toLocaleLowerCase('fr').includes(q));$('stockDocumentRows').innerHTML=docs.length?docs.map(d=>`<tr class="border-b border-slate-100 hover:bg-sky-50/60 dark:border-white/5 dark:hover:bg-white/[.03]"><td class="px-3 py-3"><b class="block dark:text-white">${esc(d.numero)}</b><small class="text-xs text-slate-400">${dateTime(d.created_at)}</small></td><td class="px-3 py-3"><span class="rounded-full px-2.5 py-1 text-[10px] font-bold ${d.type_document==='Entrée'?'bg-emerald-50 text-emerald-700':d.type_document==='Sortie'?'bg-amber-50 text-amber-700':'bg-sky-50 text-sky-700'}">${esc(d.type_document)}</span></td><td class="px-3 py-3">${esc(d.service_destination||'Stock central')}</td><td class="px-3 py-3">${esc(d.demandeur)}</td><td class="px-3 py-3">${d.lignes.length} ligne(s)</td><td class="px-3 py-3"><span class="text-xs font-bold ${d.statut==='En attente'?'text-amber-600':d.statut==='Clôturé'?'text-slate-500':'text-emerald-700'}">${esc(d.statut)}</span></td><td class="px-3 py-3 text-right"><button onclick="stockPrintDocument(${d.id})" class="rounded-lg bg-slate-100 px-3 py-2 text-xs font-bold hover:bg-slate-200"><i class="ph-bold ph-eye mr-1"></i>Voir / Imprimer</button>${canManage&&d.type_document==='Commande interne'&&d.statut==='En attente'?`<button onclick="stockCloseOrder(${d.id})" class="ml-1 rounded-lg bg-emerald-100 px-3 py-2 text-xs font-bold text-emerald-800 hover:bg-emerald-200">Clôturer</button>`:''}</td></tr>`).join(''):'<tr><td colspan="7" class="p-10 text-center text-slate-400">Aucun bon trouvé.</td></tr>';}
 function dateTime(v){return v?new Date(v).toLocaleString('fr-FR'):'—';}
 function renderMovements(){const body=$('stockMovementRows');body.innerHTML=data.movements.length?data.movements.map(m=>`<tr class="border-b border-slate-100 hover:bg-sky-50/60 dark:border-white/5"><td class="px-3 py-3"><b>${esc(m.bon_numero)}</b><small class="block text-xs text-slate-400">${dateTime(m.created_at)}</small></td><td class="px-3 py-3 font-semibold dark:text-white">${esc(m.designation)} <small class="block text-xs text-slate-400">${esc(m.reference)}</small></td><td class="px-3 py-3">${esc(m.numero_lot)}${m.date_peremption?`<small class="block text-xs text-slate-400">${esc(m.date_peremption)}</small>`:''}</td><td class="px-3 py-3"><span class="rounded-full px-2.5 py-1 text-[10px] font-bold ${m.sens==='Entrée'?'bg-emerald-50 text-emerald-700':'bg-rose-50 text-rose-700'}">${esc(m.sens)}</span></td><td class="px-3 py-3 font-extrabold ${m.sens==='Entrée'?'text-emerald-700':'text-rose-600'}">${m.sens==='Entrée'?'+':'−'}${fmt(m.quantite)} ${esc(m.unite)}</td><td class="px-3 py-3 text-slate-500">${esc(m.utilisateur)}</td></tr>`).join(''):'<tr><td colspan="6" class="p-8 text-center text-slate-400">Aucun mouvement enregistré.</td></tr>';}
 async function refresh(){if(!$('stockModule'))return;try{data=await api('/data');if(!$('stockModule'))return;categories=data.categories||[];units=data.unites||[];$('stockKpiItems').textContent=data.summary.articles||0;$('stockKpiLow').textContent=data.summary.alertes_stock||0;$('stockKpiExpiring').textContent=data.summary.lots_proches||0;$('stockKpiExpired').textContent=fmt(data.summary.quantite_perimee||0);stockRenderInventory();stockRenderDocuments();renderMovements();}catch(e){if($('stockModule'))toast(e.message,true);}}
 window.stockOpenModal=(title,html,handler)=>{activeForm=handler;$('stockSubmitButton').classList.remove('hidden');$('stockSubmitButton').textContent='Valider';$('stockModalTitle').textContent=title;$('stockFormFields').innerHTML=html;$('stockModal').classList.remove('hidden');$('stockModal').classList.add('flex');document.body.classList.add('overflow-hidden');};
 window.stockCloseModal=()=>{$('stockModal').classList.add('hidden');$('stockModal').classList.remove('flex');window.stockCloseCatalog();if(window.stockPhotoPreviewUrl){URL.revokeObjectURL(window.stockPhotoPreviewUrl);window.stockPhotoPreviewUrl=null;}document.body.classList.remove('overflow-hidden');};
 window.stockCloseCatalog=()=>{$('stockCatalogModal').classList.add('hidden');$('stockCatalogModal').classList.remove('flex');};
 const field=(label,id,type='text',value='',extra='')=>`<label class="block text-xs font-bold text-slate-600 dark:text-slate-300">${label}<input id="${id}" type="${type}" value="${esc(value)}" ${extra} class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm font-medium outline-none focus:border-brand-neon dark:border-white/10 dark:bg-black/30 dark:text-white"></label>`;
 const catalogField=(label,id,values,selected,kind)=>`<label class="block text-xs font-bold text-slate-600 dark:text-slate-300">${label}<div class="mt-2 flex gap-2"><select id="${id}" class="min-w-0 flex-1 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm font-medium dark:border-white/10 dark:bg-black/30 dark:text-white">${values.map(v=>`<option value="${esc(v)}" ${v===selected?'selected':''}>${esc(v)}</option>`).join('')}</select>${canManage?`<button type="button" onclick="stockManageCatalog('${kind}')" class="shrink-0 rounded-xl border border-emerald-200 bg-emerald-50 px-3 text-xs font-bold text-emerald-800 hover:bg-emerald-100 dark:border-emerald-400/20 dark:bg-emerald-400/10 dark:text-emerald-200" title="Gérer ${label.toLowerCase()}"><i class="ph-bold ph-sliders-horizontal mr-1"></i>Gérer</button>`:''}</div></label>`;
 const photoField=(path='')=>`<section class="sm:col-span-2 overflow-hidden rounded-2xl border border-slate-200 bg-gradient-to-br from-slate-50 to-white shadow-sm dark:border-white/10 dark:from-white/[.04] dark:to-[#121c15]"><div class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200/80 px-4 py-3.5 dark:border-white/10"><div class="flex items-center gap-3"><span class="grid h-10 w-10 place-items-center rounded-xl bg-emerald-100 text-emerald-800 dark:bg-emerald-400/10 dark:text-emerald-300"><i class="ph-bold ph-image-square text-xl"></i></span><div><h4 class="text-sm font-extrabold text-slate-800 dark:text-white">Photo de l’article</h4><p class="mt-0.5 text-[11px] text-slate-500">Une image claire aide à reconnaître rapidement le produit.</p></div></div><span class="rounded-full border border-slate-200 bg-white px-2.5 py-1 text-[10px] font-bold uppercase tracking-wide text-slate-500 dark:border-white/10 dark:bg-white/5 dark:text-slate-300">Facultatif</span></div><div id="stockPhotoDropzone" class="group relative m-4 grid min-h-48 place-items-center overflow-hidden rounded-2xl border-2 border-dashed border-slate-300 bg-white transition hover:border-emerald-400 hover:bg-emerald-50/40 dark:border-white/15 dark:bg-black/20 dark:hover:border-emerald-400/60 dark:hover:bg-emerald-400/[.04]"><div id="siPhotoPlaceholder" class="${path?'hidden':''} pointer-events-none flex flex-col items-center px-5 py-8 text-center"><span class="grid h-14 w-14 place-items-center rounded-2xl bg-emerald-50 text-emerald-700 shadow-sm transition group-hover:scale-105 dark:bg-emerald-400/10 dark:text-emerald-300"><i class="ph-bold ph-upload-simple text-2xl"></i></span><b class="mt-3 text-sm font-bold text-slate-700 dark:text-slate-200">Dépose la photo ici</b><span class="mt-1 text-xs text-slate-400">ou choisis une image depuis ton ordinateur</span></div><img id="siPhotoPreview" src="${path?`/api/gestion-stock/items/${path}/photo`:''}" class="${path?'':'hidden'} pointer-events-none max-h-64 w-full object-contain p-3" alt="Aperçu de la photo de l’article"><input id="siPhoto" type="file" accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp" onchange="stockPreviewPhoto(event)" class="absolute inset-0 z-10 h-full w-full cursor-pointer opacity-0" aria-label="Choisir ou déposer une photo de l’article"><span class="pointer-events-none absolute bottom-3 right-3 z-0 rounded-lg bg-slate-900/75 px-2.5 py-1.5 text-[10px] font-bold text-white opacity-0 transition group-hover:opacity-100"><i class="ph-bold ph-arrows-out-simple mr-1"></i>Cliquer pour remplacer</span></div><div class="flex flex-wrap items-center justify-between gap-3 px-4 pb-4"><div class="flex min-w-0 items-center gap-2 text-[11px] text-slate-500"><i class="ph-bold ph-info text-emerald-600"></i><span>JPG, PNG ou WebP · 8 Mo maximum</span><span id="siPhotoFileName" class="hidden max-w-48 truncate rounded-full bg-emerald-50 px-2.5 py-1 font-semibold text-emerald-800 dark:bg-emerald-400/10 dark:text-emerald-200"></span></div>${path?'<label class="inline-flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-xs font-semibold text-rose-600 transition hover:bg-rose-50 dark:hover:bg-rose-400/10"><input id="siRemovePhoto" type="checkbox" class="h-4 w-4 rounded accent-rose-600">Retirer cette photo</label>':''}</div></section>`;
 function stockItemFormData(id=null){const d=new FormData();if(id)d.append('id',id);['designation','reference','categorie','unite','seuil_alerte','notes'].forEach(k=>{const fieldId={designation:'siName',reference:'siReference',categorie:'siCategory',unite:'siUnit',seuil_alerte:'siThreshold',notes:'siNotes'}[k];d.append(k,$(fieldId).value);});d.append('suivi_peremption',$('siExpiry').checked?'true':'false');d.append('supprimer_photo',$('siRemovePhoto')?.checked?'true':'false');const file=$('siPhoto').files?.[0];if(file)d.append('photo',file,file.name);return d;}
 window.stockPreviewPhoto=e=>{const file=e.target.files?.[0];if(!file)return;if(window.stockPhotoPreviewUrl)URL.revokeObjectURL(window.stockPhotoPreviewUrl);window.stockPhotoPreviewUrl=URL.createObjectURL(file);$('siPhotoPreview').src=window.stockPhotoPreviewUrl;$('siPhotoPreview').classList.remove('hidden');$('siPhotoPlaceholder').classList.add('hidden');const name=$('siPhotoFileName');name.textContent=file.name;name.classList.remove('hidden');};
 window.stockNewItem=()=>{if(!canManage)return;stockOpenModal('Nouvel article',`<div class="grid gap-3 sm:grid-cols-2">${field('Désignation *','siName')} ${field('Référence (facultative)','siReference')}${catalogField('Catégorie *','siCategory',categories,categories[0],'categorie')}${catalogField('Unité de stock *','siUnit',units,units[0],'unite')}${field('Seuil d’alerte','siThreshold','number','0','min="0" step="0.001"')}<label class="flex items-center gap-2 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm font-semibold text-amber-800"><input id="siExpiry" type="checkbox" class="h-4 w-4 accent-amber-600">Suivre la date de péremption</label>${photoField()}<label class="block text-xs font-bold text-slate-600 dark:text-slate-300 sm:col-span-2">Notes<textarea id="siNotes" rows="2" class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm dark:border-white/10 dark:bg-black/30 dark:text-white"></textarea></label></div>`,async()=>{const r=await api('/items/save',{method:'POST',body:stockItemFormData()});stockCloseModal();toast(`Article ${r.reference} enregistré.`);await refresh();});};
 window.stockEditItem=id=>{const i=data.items.find(x=>Number(x.id)===Number(id));if(!i)return;stockOpenModal('Modifier un article',`<div class="grid gap-3 sm:grid-cols-2">${field('Désignation *','siName','text',i.designation)}${field('Référence','siReference','text',i.reference)}${catalogField('Catégorie *','siCategory',categories,i.categorie,'categorie')}${catalogField('Unité *','siUnit',units,i.unite,'unite')}${field('Seuil d’alerte','siThreshold','number',i.seuil_alerte,'min="0" step="0.001"')}<label class="flex items-center gap-2 rounded-xl border p-3 text-sm font-semibold"><input id="siExpiry" type="checkbox" ${i.suivi_peremption?'checked':''}>Suivre la date de péremption</label>${photoField(i.photo_path?i.id:'')}<label class="block text-xs font-bold sm:col-span-2">Notes<textarea id="siNotes" rows="2" class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm">${esc(i.notes||'')}</textarea></label></div>`,async()=>{await api('/items/save',{method:'POST',body:stockItemFormData(i.id)});stockCloseModal();toast('Article modifié.');await refresh();});};
 window.stockManageCatalog=kind=>{if(!canManage||!['categorie','unite'].includes(kind))return;catalogEdit=null;$('stockCatalogTitle').textContent=kind==='categorie'?'Gérer les catégories':'Gérer les unités';$('stockCatalogLabel').textContent=kind==='categorie'?'Nom de la catégorie':'Nom de l’unité';$('stockCatalogName').value='';$('stockCatalogSaveButton').innerHTML='<i class="ph-bold ph-plus mr-1"></i>Ajouter';$('stockCatalogModal').dataset.kind=kind;stockRenderCatalog();$('stockCatalogModal').classList.remove('hidden');$('stockCatalogModal').classList.add('flex');};
 function stockRenderCatalog(){const modal=$('stockCatalogModal'),kind=modal.dataset.kind,values=kind==='categorie'?categories:units;$('stockCatalogRows').innerHTML=values.length?values.map((name,index)=>`<div class="flex items-center justify-between gap-2 rounded-xl border border-slate-100 bg-slate-50 px-3 py-2 dark:border-white/10 dark:bg-white/[.03]"><span class="min-w-0 truncate text-sm font-semibold dark:text-white">${esc(name)}</span><div class="flex shrink-0 gap-1"><button type="button" data-stock-catalog-edit="${index}" class="grid h-8 w-8 place-items-center rounded-lg bg-sky-50 text-sky-700 hover:bg-sky-100" title="Modifier"><i class="ph-bold ph-pencil-simple"></i></button><button type="button" data-stock-catalog-delete="${index}" class="grid h-8 w-8 place-items-center rounded-lg bg-rose-50 text-rose-700 hover:bg-rose-100" title="Supprimer"><i class="ph-bold ph-trash"></i></button></div></div>`).join(''):'<p class="rounded-xl bg-slate-50 p-4 text-center text-sm text-slate-500">Aucune valeur. Ajoute-en une ci-dessus.</p>'; $('stockCatalogRows').querySelectorAll('[data-stock-catalog-edit]').forEach(btn=>btn.addEventListener('click',()=>{const name=values[Number(btn.dataset.stockCatalogEdit)];catalogEdit=name;$('stockCatalogName').value=name;$('stockCatalogSaveButton').innerHTML='<i class="ph-bold ph-check mr-1"></i>Enregistrer';}));$('stockCatalogRows').querySelectorAll('[data-stock-catalog-delete]').forEach(btn=>btn.addEventListener('click',()=>stockCatalogDelete(kind,values[Number(btn.dataset.stockCatalogDelete)])));}
 window.stockCatalogSave=async e=>{e.preventDefault();const modal=$('stockCatalogModal'),kind=modal.dataset.kind,old=catalogEdit,name=$('stockCatalogName').value.trim(),button=$('stockCatalogSaveButton');if(!name)return;button.disabled=true;try{await api('/catalog/save',{method:'POST',body:JSON.stringify({type:kind,nom:name,ancien_nom:old||''})});catalogEdit=null;$('stockCatalogName').value='';await refresh();stockRenderCatalog();const select=$(kind==='categorie'?'siCategory':'siUnit');if(select){select.innerHTML=(kind==='categorie'?categories:units).map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');select.value=name;}button.innerHTML='<i class="ph-bold ph-plus mr-1"></i>Ajouter';toast(old?'Valeur mise à jour.':'Valeur ajoutée.');}catch(err){toast(err.message,true);}finally{button.disabled=false;}};
 async function stockCatalogDelete(kind,name){if(!confirm(`Supprimer « ${name} » de la liste ?`))return;try{await api('/catalog/delete',{method:'POST',body:JSON.stringify({type:kind,nom:name})});catalogEdit=null;await refresh();stockRenderCatalog();toast('Valeur supprimée.');}catch(err){toast(err.message,true);}}
 window.stockShowLots=id=>{const i=data.items.find(x=>Number(x.id)===Number(id));if(!i)return;const lots=data.lots.filter(l=>Number(l.item_id)===Number(id));stockOpenModal(`Lots · ${i.designation}`,lots.length?`<div class="overflow-x-auto"><table class="w-full min-w-[560px] text-sm"><thead class="bg-slate-100 text-left text-xs"><tr><th class="p-3">Lot</th><th class="p-3">Péremption</th><th class="p-3">Quantité</th><th class="p-3">État</th></tr></thead><tbody>${lots.map(l=>`<tr class="border-b"><td class="p-3 font-bold">${esc(l.numero_lot)}</td><td class="p-3">${esc(l.date_peremption||'—')}</td><td class="p-3">${fmt(l.quantite_actuelle)} ${esc(l.unite)}</td><td class="p-3">${l.perime?'<b class="text-rose-600">Périmé · sortie bloquée</b>':l.expire_bientot?'<b class="text-amber-600">Expire dans 90 jours</b>':'<span class="text-emerald-700">Disponible</span>'}</td></tr>`).join('')}</tbody></table></div>`:'<p class="rounded-2xl bg-slate-50 p-8 text-center text-sm text-slate-500">Aucun lot en stock pour cet article.</p>',null);$('stockSubmitButton').classList.add('hidden');};
 window.stockOpenDocument=(type,prefillId=null)=>{if(!canManage)return;stockOpenModal(`Nouveau bon · ${type}`,`<input id="sdType" type="hidden" value="${esc(type)}"><div class="grid gap-3 sm:grid-cols-2">${type!=='Entrée'?field('Service destinataire *','sdService'):''}${field('Motif','sdReason')}${field('Notes','sdNotes','text','','maxlength="1000"')}</div><div class="mt-4 flex items-center justify-between"><b class="text-sm dark:text-white">Articles du bon</b><button type="button" onclick="stockAddLine()" class="rounded-xl bg-slate-100 px-3 py-2 text-xs font-bold dark:bg-white/10 dark:text-white"><i class="ph-bold ph-plus mr-1"></i>Ajouter une ligne</button></div><div id="stockDocLines" class="mt-3 space-y-2"></div>`,async()=>{const type=$('sdType').value;const rows=[...$('stockDocLines').querySelectorAll('.stock-doc-line')];const lines=rows.map(r=>({item_id:r.querySelector('.sd-item').value,quantite:r.querySelector('.sd-qty').value,lot_id:r.querySelector('.sd-lot')?.value||null,numero_lot:r.querySelector('.sd-lot-code')?.value||'',date_peremption:r.querySelector('.sd-expiry')?.value||'',prix_unitaire:r.querySelector('.sd-price')?.value||''}));const res=await api('/documents/create',{method:'POST',body:JSON.stringify({type_document:type,service_destination:$('sdService')?.value||'',motif:$('sdReason').value,notes:$('sdNotes').value,lignes:lines})});stockCloseModal();toast(`${res.numero} créé · ${res.statut}.`);await refresh();stockSetTab('documents');});stockAddLine(prefillId);};
 window.stockAddLine=(prefillId=null)=>{const type=$('sdType').value;const row=document.createElement('div');row.className='stock-doc-line grid gap-2 rounded-2xl border border-slate-200 p-3 sm:grid-cols-[1.5fr_.7fr_1fr_1fr_.7fr_auto] dark:border-white/10';const itemOptions=data.items.map(i=>`<option value="${i.id}" ${Number(i.id)===Number(prefillId)?'selected':''}>${esc(i.designation)} · ${esc(i.reference)}</option>`).join('');row.innerHTML=`<label class="text-[10px] font-bold text-slate-500">Article<select class="sd-item mt-1 w-full rounded-lg border bg-slate-50 p-2 text-xs dark:border-white/10 dark:bg-black/30 dark:text-white"><option value="">Choisir…</option>${itemOptions}</select></label><label class="text-[10px] font-bold text-slate-500">Quantité<input class="sd-qty mt-1 w-full rounded-lg border bg-slate-50 p-2 text-xs dark:border-white/10 dark:bg-black/30 dark:text-white" type="number" min="0.001" step="0.001"></label>${type==='Entrée'?`<label class="text-[10px] font-bold text-slate-500">N° lot<input class="sd-lot-code mt-1 w-full rounded-lg border bg-slate-50 p-2 text-xs dark:border-white/10 dark:bg-black/30 dark:text-white" placeholder="Lot"></label><label class="text-[10px] font-bold text-slate-500">Péremption<input class="sd-expiry mt-1 w-full rounded-lg border bg-slate-50 p-2 text-xs dark:border-white/10 dark:bg-black/30 dark:text-white" type="date"></label><label class="text-[10px] font-bold text-slate-500">Prix unitaire<input class="sd-price mt-1 w-full rounded-lg border bg-slate-50 p-2 text-xs dark:border-white/10 dark:bg-black/30 dark:text-white" type="number" min="0" step="0.01"></label>`:type==='Sortie'?`<label class="text-[10px] font-bold text-slate-500">Lot (facultatif)<select class="sd-lot mt-1 w-full rounded-lg border bg-slate-50 p-2 text-xs dark:border-white/10 dark:bg-black/30 dark:text-white"><option value="">Auto · FEFO</option></select></label><span class="self-end pb-2 text-[10px] text-slate-400">Lots expirés bloqués</span>`:'<span class="sm:col-span-3 self-center text-[10px] text-slate-400">La commande interne ne modifie pas le stock.</span>'}<button type="button" onclick="this.closest(\'.stock-doc-line\').remove()" class="h-8 w-8 self-end rounded-lg bg-rose-50 text-rose-600" title="Retirer la ligne"><i class="ph-bold ph-trash"></i></button>`;row.querySelector('.sd-item').addEventListener('change',()=>stockFillLots(row));$('stockDocLines').appendChild(row);if(prefillId)stockFillLots(row);};
 function stockFillLots(row){const select=row.querySelector('.sd-lot');if(!select)return;const id=Number(row.querySelector('.sd-item').value);const lots=data.lots.filter(l=>Number(l.item_id)===id&&!l.perime);select.innerHTML='<option value="">Auto · FEFO</option>'+lots.map(l=>`<option value="${l.id}">${esc(l.numero_lot)} · ${fmt(l.quantite_actuelle)} ${esc(l.unite)}${l.date_peremption?' · '+esc(l.date_peremption):''}</option>`).join('');}
 window.stockSubmitForm=async e=>{e.preventDefault();const btn=$('stockSubmitButton');if(btn)btn.disabled=true;try{if(activeForm)await activeForm();}catch(err){toast(err.message,true);}finally{if(btn)btn.disabled=false;}};
 window.stockCloseOrder=async id=>{try{await api(`/documents/${id}/close`,{method:'POST',body:'{}'});toast('Commande interne clôturée.');await refresh();}catch(e){toast(e.message,true);}};
 window.stockPrintDocument=id=>{const d=data.documents.find(x=>Number(x.id)===Number(id));if(!d)return;const input=d.type_document==='Entrée'?'Bon d’entrée':d.type_document==='Sortie'?'Bon de sortie':'Bon de commande interne';$('stockPrintTitle').textContent=`${input} · ${d.numero}`;const total=d.lignes.reduce((a,l)=>a+Number(l.quantite||0)*Number(l.prix_unitaire||0),0);$('stockPrintSheet').innerHTML=`<div style="font-family:Arial,sans-serif;color:#17231b"><div style="display:flex;justify-content:space-between;align-items:flex-start;border-bottom:3px solid #1c482a;padding-bottom:16px"><div><div style="font-size:24px;font-weight:800;color:#183b20">CLINIVA</div><div style="font-size:11px;color:#64746a;margin-top:4px">Gestion des stocks · Document interne</div></div><div style="text-align:right"><div style="font-size:20px;font-weight:800">${esc(input)}</div><div style="font-size:13px;font-weight:700;color:#238342;margin-top:5px">${esc(d.numero)}</div></div></div><div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:18px 0;font-size:12px"><div><b>Date :</b> ${esc(dateTime(d.created_at))}</div><div><b>Statut :</b> ${esc(d.statut)}</div><div><b>Service destinataire :</b> ${esc(d.service_destination||'Stock central')}</div><div><b>Demandeur :</b> ${esc(d.demandeur)}</div>${d.motif?`<div style="grid-column:1/-1"><b>Motif :</b> ${esc(d.motif)}</div>`:''}</div><table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr style="background:#eaf1eb"><th style="padding:9px;border:1px solid #cbd5ce;text-align:left">Réf.</th><th style="padding:9px;border:1px solid #cbd5ce;text-align:left">Désignation</th><th style="padding:9px;border:1px solid #cbd5ce;text-align:left">Lot / péremption</th><th style="padding:9px;border:1px solid #cbd5ce;text-align:right">Quantité</th>${d.type_document==='Entrée'?'<th style="padding:9px;border:1px solid #cbd5ce;text-align:right">Prix unitaire</th>':''}</tr></thead><tbody>${d.lignes.map(l=>`<tr><td style="padding:9px;border:1px solid #cbd5ce">${esc(l.item_reference)}</td><td style="padding:9px;border:1px solid #cbd5ce">${esc(l.item_designation)}</td><td style="padding:9px;border:1px solid #cbd5ce">${esc(l.lots_utilises||'—')}${l.date_peremption?' · '+esc(l.date_peremption):''}</td><td style="padding:9px;border:1px solid #cbd5ce;text-align:right">${fmt(l.quantite)} ${esc(l.unite)}</td>${d.type_document==='Entrée'?`<td style="padding:9px;border:1px solid #cbd5ce;text-align:right">${l.prix_unitaire?fmt(l.prix_unitaire)+' DA':'—'}</td>`:''}</tr>`).join('')}</tbody>${d.type_document==='Entrée'&&total?`<tfoot><tr><td colspan="4" style="padding:9px;text-align:right;font-weight:bold">Total valorisé</td><td style="padding:9px;text-align:right;font-weight:bold">${fmt(total)} DA</td></tr></tfoot>`:''}</table>${d.notes?`<div style="margin-top:14px;padding:10px;border:1px solid #d5ded7"><b>Notes :</b> ${esc(d.notes)}</div>`:''}<div style="display:grid;grid-template-columns:1fr 1fr;gap:40px;margin-top:55px;font-size:12px;text-align:center"><div>Remis par :<br><br><br>Signature</div><div>Reçu par :<br><br><br>Signature</div></div><div style="margin-top:35px;border-top:1px solid #cbd5ce;padding-top:8px;text-align:center;font-size:9px;color:#77837a">Document généré par Cliniva · ${esc(d.numero)}</div></div>`;$('stockPrintModal').classList.remove('hidden');$('stockPrintModal').classList.add('flex');};
 window.stockClosePrint=()=>{$('stockPrintModal').classList.add('hidden');$('stockPrintModal').classList.remove('flex');};
 window.addEventListener('keydown',e=>{if(e.key==='Escape'){stockCloseModal();stockCloseCatalog();stockClosePrint();}});
 if(canManage){const actions=document.querySelector('#stockModule header .flex.flex-wrap.gap-2');actions.innerHTML='<button onclick="stockNewItem()" class="inline-flex items-center gap-2 rounded-xl border border-white/20 bg-white/10 px-4 py-2.5 text-xs font-bold text-white hover:bg-white/20"><i class="ph-bold ph-plus-circle"></i>Nouvel article</button><button onclick="stockOpenDocument(\'Entrée\')" class="inline-flex items-center gap-2 rounded-xl bg-brand-neon px-4 py-2.5 text-xs font-extrabold text-black shadow"><i class="ph-bold ph-arrow-down-left"></i>Bon d’entrée</button><button onclick="stockOpenDocument(\'Sortie\')" class="inline-flex items-center gap-2 rounded-xl bg-amber-400 px-4 py-2.5 text-xs font-extrabold text-black shadow"><i class="ph-bold ph-arrow-up-right"></i>Bon de sortie</button><button onclick="stockOpenDocument(\'Commande interne\')" class="inline-flex items-center gap-2 rounded-xl border border-white/20 bg-white/10 px-4 py-2.5 text-xs font-bold text-white hover:bg-white/20"><i class="ph-bold ph-clipboard-text"></i>Commande interne</button>';}
 refresh();window.setInterval(refresh,7000);
 })();
 </script>
</section>
'''.replace("__MANAGE__", "true" if can_manage else "false")
