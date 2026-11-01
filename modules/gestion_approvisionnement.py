"""Achats, réceptions, dettes et paiements fournisseurs de Cliniva."""

from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape
import asyncio
import base64
from io import BytesIO
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from app.auth import get_current_user, track_action
from app.smtp_mailer import build_cliniva_email_html, get_stock_smtp_configuration, send_smtp_email
from database import DOCS_DIR, get_db_cursor

procurement_router = APIRouter(prefix="/api/approvisionnement", tags=["Approvisionnement fournisseurs"])


def _allowed(user: dict[str, Any], manage: bool = False) -> bool:
    return user.get("role") == "Administrateur" or bool((user.get("permissions") or {}).get("stock_manage" if manage else "stock_view") or (user.get("permissions") or {}).get("stock_manage"))


def _require(request: Request, manage: bool = False) -> dict[str, Any]:
    user = get_current_user(request)
    if not _allowed(user, manage):
        raise HTTPException(status_code=403, detail="Accès à l’approvisionnement non autorisé.")
    return user


def _money(value: Any, label: str, *, allow_zero: bool = True) -> Decimal:
    try:
        result = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=f"Montant invalide : {label}.") from exc
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        raise HTTPException(status_code=400, detail=f"Montant invalide : {label}.")
    return result.quantize(Decimal("0.01"))


def _date(value: Any, label: str, default_today: bool = False) -> date | None:
    if not str(value or "").strip():
        return date.today() if default_today else None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Date invalide : {label}.") from exc


def _decode_receipt_file(data: dict[str, Any], key: str = "justificatif") -> tuple[bytes, str, str] | None:
    raw = data.get(key)
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Le fichier justificatif est invalide.")
    data_url = str(raw.get("data_url") or "")
    try:
        header, encoded = data_url.split(",", 1)
        mime = header.split(";", 1)[0].removeprefix("data:").lower()
        if mime not in {"application/pdf", "image/jpeg", "image/png"}:
            raise ValueError("mime")
        content = base64.b64decode(encoded, validate=True)
        if not content or len(content) > 12 * 1024 * 1024:
            raise ValueError("size")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Joins un PDF, un JPG ou un PNG de 12 Mo maximum.") from exc
    name = Path(str(raw.get("name") or "justificatif")).name[:180]
    extension = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png"}[mime]
    return content, name, extension


def _store_procurement_file(folder: str, extension: str, content: bytes) -> tuple[str, Path]:
    root = (Path(DOCS_DIR) / "approvisionnement" / folder).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{uuid4().hex}{extension}"
    path.write_bytes(content)
    return str(path), path


def _create_stock_document(cur, user: dict[str, Any], document_type: str, *, supplier_id: int | None = None,
                           supplier: str = "", supplier_order_id: int | None = None,
                           service: str = "", reason: str = "", notes: str = "") -> tuple[int, str]:
    requester = f"{user.get('prenom', '')} {user.get('nom', '')}".strip()
    cur.execute("""INSERT INTO stock_documents
        (type_document,statut,service_destination,fournisseur,fournisseur_id,agent_reception,
         agent_preleveur,personne_retrait,demandeur,motif,notes,created_by,validated_by,validated_at,supplier_order_id)
        VALUES(%s,'Validé',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP,%s) RETURNING id""",
        (document_type, service, supplier, supplier_id,
         requester if document_type == "Entrée" else "",
         requester if document_type == "Sortie" else "",
         supplier if document_type == "Sortie" else "", requester, reason, notes[:2000],
         user.get("id"), user.get("id"), supplier_order_id))
    document_id = int(cur.fetchone()["id"])
    prefix = {"Entrée": "BE", "Sortie": "BS", "Commande interne": "BCI"}[document_type]
    number = f"{prefix}-{document_id:08d}"
    cur.execute("UPDATE stock_documents SET numero=%s WHERE id=%s", (number, document_id))
    return document_id, number


def _insert_stock_line(cur, user: dict[str, Any], document_id: int, item: dict[str, Any], lot: dict[str, Any],
                       quantity: Decimal, *, direction: str, packages: Decimal | None = None,
                       unit: str = "", per_unit: Decimal = Decimal("1"), purchase_price: Decimal | None = None,
                       sale_price: Decimal | None = None, margin: Decimal = Decimal("15"), warehouse: str = "",
                       location: str = "", source_order_line_id: int | None = None) -> int:
    if direction == "Entrée":
        cur.execute("""INSERT INTO stock_document_lines
            (document_id,item_id,item_reference,item_designation,unite,quantite,lot_id,numero_lot,date_peremption,
             prix_unitaire,quantite_unite_stock,unite_stock,quantite_par_unite,prix_achat,prix_vente,marge_vente,
             emplacement,entrepot,supplier_order_line_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (document_id,item["id"],item["reference"],item["designation"],item["unite"],quantity,lot["id"],
             lot["numero_lot"],lot["date_peremption"],(purchase_price/per_unit).quantize(Decimal("0.01")) if purchase_price is not None else None,
             packages,unit,per_unit,purchase_price,sale_price,margin,location,warehouse,source_order_line_id))
    else:
        cur.execute("""INSERT INTO stock_document_lines
            (document_id,item_id,item_reference,item_designation,unite,quantite,lot_id,numero_lot,date_peremption,prix_unitaire)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (document_id,item["id"],item["reference"],item["designation"],item["unite"],quantity,lot["id"],
             lot["numero_lot"],lot["date_peremption"],lot.get("prix_unitaire")))
    line_id = int(cur.fetchone()["id"])
    requester = f"{user.get('prenom', '')} {user.get('nom', '')}".strip()
    cur.execute("""INSERT INTO stock_movements(item_id,lot_id,document_id,document_line_id,sens,quantite,utilisateur,user_id)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
        (item["id"],lot["id"],document_id,line_id,direction,quantity,requester,user.get("id")))
    return line_id


@procurement_router.get("/data")
def procurement_data(request: Request):
    _require(request)
    with get_db_cursor() as cur:
        cur.execute("SELECT id, designation, actif, contact, telephone, email, email_2, email_3 FROM stock_suppliers ORDER BY designation")
        suppliers = [dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT u.id,u.prenom,u.nom,u.role,
                string_agg(DISTINCT NULLIF(s.type_personnel,''), ', ' ORDER BY NULLIF(s.type_personnel,'')) AS fonction_staff,
                string_agg(DISTINCT NULLIF(s.service_nom,''), ', ' ORDER BY NULLIF(s.service_nom,'')) AS service_staff
            FROM clinic_staff s JOIN users u
              ON lower(trim(s.nom))=lower(trim(u.nom)) AND lower(trim(s.prenom))=lower(trim(u.prenom))
            WHERE s.actif=TRUE AND u.status='Actif'
              AND (u.role='Administrateur' OR COALESCE(u.permissions->>'stock_view','false')='true'
                   OR COALESCE(u.permissions->>'stock_manage','false')='true')
            GROUP BY u.id,u.prenom,u.nom,u.role ORDER BY u.nom,u.prenom""")
        approvers = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT id, reference, designation, unite FROM stock_items WHERE actif=TRUE ORDER BY designation")
        items = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT nom FROM stock_warehouses WHERE actif=TRUE ORDER BY nom")
        warehouses = [row["nom"] for row in cur.fetchall()]
        cur.execute("SELECT nom FROM stock_locations WHERE actif=TRUE ORDER BY nom")
        locations = [row["nom"] for row in cur.fetchall()]
        cur.execute("""SELECT o.id,o.numero,o.supplier_id,s.designation AS fournisseur,o.date_commande,o.date_prevue,o.statut,o.notes,o.created_at,
                COALESCE(SUM(l.quantite*l.prix_unitaire),0) AS montant, COUNT(l.id) AS lignes,
                COALESCE(SUM(l.quantite),0) AS quantite_commandee,COALESCE(SUM(l.quantite_recue),0) AS quantite_recue
            FROM stock_supplier_orders o JOIN stock_suppliers s ON s.id=o.supplier_id
            LEFT JOIN stock_supplier_order_lines l ON l.order_id=o.id
            GROUP BY o.id,s.designation ORDER BY o.date_commande DESC,o.id DESC""")
        orders = [dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT p.id,p.numero,p.supplier_id,s.designation AS fournisseur,s.email AS fournisseur_email,
                s.email_2 AS fournisseur_email_2,s.email_3 AS fournisseur_email_3,p.date_demande,p.date_limite,
                p.statut,p.montant_propose,p.notes,p.offre_nom,p.offre_mime,p.converted_order_id,o.numero AS commande_convertie,p.created_at,
                COUNT(l.id) AS lignes
            FROM stock_proforma_requests p JOIN stock_suppliers s ON s.id=p.supplier_id
            LEFT JOIN stock_supplier_orders o ON o.id=p.converted_order_id
            LEFT JOIN stock_proforma_request_lines l ON l.request_id=p.id
            GROUP BY p.id,s.designation,s.email,s.email_2,s.email_3,o.numero ORDER BY p.date_demande DESC,p.id DESC""")
        proformas = [dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT l.request_id,l.id,l.item_id,COALESCE(i.designation,l.designation) AS designation,
                l.quantite,l.unite,l.prix_reponse,l.is_custom,COALESCE(i.reference,l.reference) AS reference
            FROM stock_proforma_request_lines l LEFT JOIN stock_items i ON i.id=l.item_id
            ORDER BY l.id""")
        proforma_lines = [dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT d.id,d.numero,d.numero_bon_livraison AS bon_livraison,d.created_at,d.fournisseur_id,s.designation AS fournisseur,
                (SELECT a.id FROM stock_document_attachments a WHERE a.document_id=d.id ORDER BY a.page_number,a.id LIMIT 1) AS attachment_id,
                d.supplier_order_id,o.numero AS commande,
                COALESCE(SUM(CASE WHEN COALESCE(l.quantite_unite_stock,0)>0 AND l.prix_achat IS NOT NULL
                    THEN l.quantite_unite_stock*l.prix_achat
                    ELSE l.quantite*COALESCE(l.prix_unitaire,l.prix_achat/NULLIF(l.quantite_par_unite,0),0) END),0) AS montant,
                EXISTS(SELECT 1 FROM stock_supplier_invoices f WHERE f.stock_document_id=d.id) AS facture_liee
            FROM stock_documents d JOIN stock_suppliers s ON s.id=d.fournisseur_id
            LEFT JOIN stock_supplier_orders o ON o.id=d.supplier_order_id
            LEFT JOIN stock_document_lines l ON l.document_id=d.id
            WHERE d.type_document='Entrée' AND d.statut<>'Annulé' AND d.fournisseur_id IS NOT NULL
            GROUP BY d.id,s.designation,o.numero ORDER BY d.created_at DESC,d.id DESC""")
        receipts = [dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT f.id,f.supplier_id,s.designation AS fournisseur,f.order_id,o.numero AS commande,
                f.stock_document_id,d.numero AS bon_reception,f.numero_facture,f.date_facture,f.date_echeance,
                f.montant,f.notes,f.created_at,COALESCE(p.paye,0) AS montant_paye,COALESCE(cr.credit,0) AS montant_avoir,
                GREATEST(f.montant-COALESCE(p.paye,0)-COALESCE(cr.credit,0),0) AS solde,
                GREATEST(COALESCE(p.paye,0)+COALESCE(cr.credit,0)-f.montant,0) AS credit_restant,
                CASE WHEN COALESCE(p.paye,0)+COALESCE(cr.credit,0)>=f.montant THEN 'Réglée'
                     WHEN COALESCE(p.paye,0)+COALESCE(cr.credit,0)>0 THEN 'Partiellement réglée'
                     WHEN f.date_echeance<CURRENT_DATE THEN 'En retard' ELSE 'À régler' END AS statut
            FROM stock_supplier_invoices f JOIN stock_suppliers s ON s.id=f.supplier_id
            LEFT JOIN stock_supplier_orders o ON o.id=f.order_id
            LEFT JOIN stock_documents d ON d.id=f.stock_document_id
            LEFT JOIN (SELECT invoice_id,SUM(montant) AS paye FROM stock_supplier_payments GROUP BY invoice_id) p ON p.invoice_id=f.id
            LEFT JOIN (SELECT invoice_id,SUM(montant_avoir) AS credit FROM stock_supplier_returns WHERE invoice_id IS NOT NULL GROUP BY invoice_id) cr ON cr.invoice_id=f.id
            ORDER BY f.date_echeance NULLS LAST,f.date_facture DESC,f.id DESC""")
        invoices = [dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT p.id,p.invoice_id,p.date_paiement,p.montant,p.mode_paiement,p.reference,p.notes,
                p.justificatif_nom,
                f.numero_facture,s.designation AS fournisseur
            FROM stock_supplier_payments p JOIN stock_supplier_invoices f ON f.id=p.invoice_id
            JOIN stock_suppliers s ON s.id=f.supplier_id ORDER BY p.date_paiement DESC,p.id DESC LIMIT 300""")
        payments = [dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT a.id,a.request_id,a.approver_id,a.statut,a.commentaire,a.assigned_at,a.decided_at,
                u.prenom,u.nom,u.role FROM stock_proforma_approvals a
            JOIN users u ON u.id=a.approver_id ORDER BY a.request_id,a.id""")
        approvals = [dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT COALESCE(SUM(f.montant),0) AS total_facture,
                COALESCE(SUM(LEAST(f.montant,COALESCE(p.paye,0))),0) AS total_paye,
                COALESCE(SUM(COALESCE(cr.credit,0)),0) AS total_avoirs,
                COALESCE(SUM(GREATEST(f.montant-COALESCE(p.paye,0)-COALESCE(cr.credit,0),0)),0) AS reste,
                COUNT(*) FILTER (WHERE f.date_echeance<CURRENT_DATE AND COALESCE(p.paye,0)+COALESCE(cr.credit,0)<f.montant) AS retards
            FROM stock_supplier_invoices f
            LEFT JOIN (SELECT invoice_id,SUM(montant) AS paye FROM stock_supplier_payments GROUP BY invoice_id) p ON p.invoice_id=f.id
            LEFT JOIN (SELECT invoice_id,SUM(montant_avoir) AS credit FROM stock_supplier_returns WHERE invoice_id IS NOT NULL GROUP BY invoice_id) cr ON cr.invoice_id=f.id""")
        summary = dict(cur.fetchone())
        cur.execute("""SELECT l.order_id,l.id,l.item_id,l.designation,l.quantite,l.unite,l.prix_unitaire,l.quantite_recue,
                COALESCE(i.suivi_peremption,FALSE) AS suivi_peremption,COALESCE(i.categorie,'') AS categorie,
                COALESCE(i.unite,'') AS unite_article,COALESCE(i.reference,'') AS reference
            FROM stock_supplier_order_lines l LEFT JOIN stock_items i ON i.id=l.item_id ORDER BY l.id""")
        lines = [dict(row) for row in cur.fetchall()]
    lines_by_order: dict[int, list[dict[str, Any]]] = {}
    for line in lines:
        lines_by_order.setdefault(line.pop("order_id"), []).append(line)
    for order in orders:
        order["lignes_detail"] = lines_by_order.get(order["id"], [])
    proforma_lines_by_request: dict[int, list[dict[str, Any]]] = {}
    for line in proforma_lines:
        proforma_lines_by_request.setdefault(line.pop("request_id"), []).append(line)
    for proforma in proformas:
        proforma["lignes_detail"] = proforma_lines_by_request.get(proforma["id"], [])
        proforma["approbations"] = [a for a in approvals if int(a["request_id"]) == int(proforma["id"])]
    with get_db_cursor() as cur:
        cur.execute("""SELECT r.id,r.numero,r.created_at,s.designation AS fournisseur,i.numero_facture,
                d.numero AS bon_retour,r.montant_avoir,r.motif
            FROM stock_supplier_returns r JOIN stock_suppliers s ON s.id=r.supplier_id
            JOIN stock_documents d ON d.id=r.document_id LEFT JOIN stock_supplier_invoices i ON i.id=r.invoice_id
            ORDER BY r.created_at DESC,r.id DESC""")
        returns = [dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT c.id,c.numero,c.entrepot,c.statut,c.created_at,COUNT(l.id) AS lots,
                COUNT(l.id) FILTER (WHERE l.ecart<>0) AS ecarts,COALESCE(SUM(ABS(l.ecart)),0) AS quantite_ecart
            FROM stock_inventory_counts c LEFT JOIN stock_inventory_count_lines l ON l.count_id=c.id
            GROUP BY c.id ORDER BY c.created_at DESC LIMIT 100""")
        inventory_counts = [dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT t.id,t.numero,t.entrepot_source,t.entrepot_destination,t.created_at,
                COUNT(l.id) AS lignes,COALESCE(SUM(l.quantite),0) AS quantite
            FROM stock_transfers t LEFT JOIN stock_transfer_lines l ON l.transfer_id=t.id
            GROUP BY t.id ORDER BY t.created_at DESC LIMIT 100""")
        transfers = [dict(row) for row in cur.fetchall()]
        cur.execute("""WITH stock AS (
                SELECT i.id,i.reference,i.designation,i.unite,i.seuil_alerte,COALESCE(SUM(l.quantite_actuelle),0) AS dispo
                FROM stock_items i LEFT JOIN stock_lots l ON l.item_id=i.id AND (l.date_peremption IS NULL OR l.date_peremption>=CURRENT_DATE)
                WHERE i.actif=TRUE GROUP BY i.id
            ), pending AS (
                SELECT ol.item_id,SUM(GREATEST(ol.quantite-ol.quantite_recue,0)) AS en_commande
                FROM stock_supplier_order_lines ol JOIN stock_supplier_orders o ON o.id=ol.order_id
                WHERE o.statut IN ('Commandée','Partiellement reçue') AND ol.item_id IS NOT NULL GROUP BY ol.item_id
            )
            SELECT s.*,COALESCE(p.en_commande,0) AS en_commande,
                GREATEST(s.seuil_alerte*2-s.dispo-COALESCE(p.en_commande,0),0) AS quantite_suggeree
            FROM stock s LEFT JOIN pending p ON p.item_id=s.id WHERE s.seuil_alerte>0 AND s.dispo+COALESCE(p.en_commande,0)<=s.seuil_alerte
            ORDER BY s.designation""")
        restock_alerts = [dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT l.id AS lot_id,l.item_id,i.designation,i.reference,i.unite AS unite_article,
                l.numero_lot,l.date_peremption,l.quantite_actuelle,l.prix_unitaire,l.prix_achat,l.prix_vente,
                l.marge_vente,l.unite_stock,l.quantite_par_unite,l.emplacement,l.entrepot,l.fournisseur_origine_id AS origine_fournisseur
            FROM stock_lots l JOIN stock_items i ON i.id=l.item_id
            WHERE i.actif=TRUE AND BTRIM(l.entrepot)<>'' ORDER BY l.entrepot,i.designation,l.date_peremption NULLS LAST,l.id""")
        countable_lots=[dict(row) for row in cur.fetchall()]
    return {"suppliers": suppliers, "items": items, "orders": orders, "proformas": proformas, "approvers": approvers, "receipts": receipts,
            "invoices": invoices, "payments": payments, "returns": returns,
            "inventory_counts": inventory_counts, "transfers": transfers,
            "restock_alerts": restock_alerts, "countable_lots": countable_lots,
            "warehouses": warehouses, "locations": locations, "summary": summary}


@procurement_router.post("/orders")
async def procurement_order_save(request: Request):
    user = _require(request, True)
    data = await request.json()
    try:
        supplier_id = int(data.get("supplier_id"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Choisis un fournisseur.") from exc
    lines = data.get("lignes") or []
    if not isinstance(lines, list) or not lines:
        raise HTTPException(status_code=400, detail="Ajoute au moins un article à la commande.")
    normalized = []
    for i, line in enumerate(lines, 1):
        name = str(line.get("designation") or "").strip()
        try:
            item_id = int(line.get("item_id") or 0)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Sélectionne un article du stock à la ligne {i}.") from exc
        try:
            quantity = Decimal(str(line.get("quantite", "")).replace(",", "."))
        except (InvalidOperation, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Quantité invalide à la ligne {i}.") from exc
        if not name or not item_id or not quantity.is_finite() or quantity <= 0:
            raise HTTPException(status_code=400, detail=f"Article ou quantité invalide à la ligne {i}.")
        price = _money(line.get("prix_unitaire", 0), f"ligne {i}")
        normalized.append((item_id, name[:240], quantity, str(line.get("unite") or "")[:80], price))
    with get_db_cursor() as cur:
        cur.execute("SELECT designation FROM stock_suppliers WHERE id=%s AND actif=TRUE", (supplier_id,))
        supplier = cur.fetchone()
        if not supplier:
            raise HTTPException(status_code=404, detail="Fournisseur actif introuvable.")
        for index, (item_id, name, quantity, _unit, price) in enumerate(normalized):
            cur.execute("SELECT designation,unite FROM stock_items WHERE id=%s AND actif=TRUE", (item_id,))
            item = cur.fetchone()
            if not item:
                raise HTTPException(status_code=400, detail=f"Article actif introuvable : {name}.")
            normalized[index] = (item_id, item["designation"], quantity, item["unite"], price)
        cur.execute("SELECT nextval(pg_get_serial_sequence('stock_supplier_orders','id')) AS id")
        order_id = int(cur.fetchone()["id"])
        numero = f"ACH-{date.today():%Y%m%d}-{order_id:05d}"
        cur.execute("""INSERT INTO stock_supplier_orders(id,numero,supplier_id,date_commande,date_prevue,statut,notes,created_by)
            VALUES(%s,%s,%s,%s,%s,'Commandée',%s,%s)""",
            (order_id, numero, supplier_id, _date(data.get("date_commande"), "commande", True), _date(data.get("date_prevue"), "prévue"), str(data.get("notes") or "")[:2000], user.get("id")))
        for item_id, name, quantity, unit, price in normalized:
            cur.execute("""INSERT INTO stock_supplier_order_lines(order_id,item_id,designation,quantite,unite,prix_unitaire)
                VALUES(%s,%s,%s,%s,%s,%s)""", (order_id, item_id, name, quantity, unit, price))
    track_action(request, "STOCK_SUPPLIER_ORDER", f"Commande fournisseur {numero} créée · {supplier['designation']}")
    return {"success": True, "numero": numero}


@procurement_router.post("/proformas")
async def proforma_request_create(request: Request):
    user = _require(request, True)
    data = await request.json()
    try:
        supplier_id = int(data.get("supplier_id"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Choisis un fournisseur.") from exc
    rows = data.get("lignes") or []
    if not isinstance(rows, list) or not rows or len(rows) > 100:
        raise HTTPException(status_code=400, detail="Ajoute de 1 à 100 articles à la demande de proforma.")
    normalized = []
    seen: set[int] = set()
    for index, raw in enumerate(rows, 1):
        custom = bool(raw.get("is_custom"))
        designation = str(raw.get("designation") or "").strip()[:240]
        reference = str(raw.get("reference") or "").strip()[:120]
        unit = str(raw.get("unite") or "").strip()[:80]
        try:
            item_id = int(raw.get("item_id") or 0) or None
            quantity = Decimal(str(raw.get("quantite") or "").replace(",", "."))
        except (TypeError, ValueError, InvalidOperation) as exc:
            raise HTTPException(status_code=400, detail=f"Article ou quantité invalide à la ligne {index}.") from exc
        if (not quantity.is_finite() or quantity <= 0 or
                (custom and (not designation or not unit)) or (not custom and not item_id)):
            raise HTTPException(status_code=400, detail=f"Article dupliqué ou quantité invalide à la ligne {index}.")
        if item_id and item_id in seen:
            raise HTTPException(status_code=400, detail=f"Article dupliqué à la ligne {index}.")
        if item_id:
            seen.add(item_id)
        normalized.append({"item_id": item_id, "quantity": quantity, "custom": custom,
                           "designation": designation, "reference": reference, "unit": unit})
    demand_date = _date(data.get("date_demande"), "demande", True)
    deadline = _date(data.get("date_limite"), "date limite")
    if deadline and deadline < demand_date:
        raise HTTPException(status_code=400, detail="La date limite ne peut pas précéder la date de demande.")
    notes = str(data.get("notes") or "").strip()[:2000]
    with get_db_cursor() as cur:
        cur.execute("SELECT designation FROM stock_suppliers WHERE id=%s AND actif=TRUE", (supplier_id,))
        supplier = cur.fetchone()
        if not supplier:
            raise HTTPException(status_code=404, detail="Fournisseur actif introuvable.")
        prepared = []
        for index, line in enumerate(normalized, 1):
            if line["custom"]:
                prepared.append(line)
                continue
            cur.execute("SELECT designation,reference,unite FROM stock_items WHERE id=%s AND actif=TRUE", (line["item_id"],))
            item = cur.fetchone()
            if not item:
                raise HTTPException(status_code=400, detail=f"Article actif introuvable à la ligne {index}.")
            prepared.append({**line,"designation":item["designation"],"reference":item["reference"],"unit":item["unite"]})
        cur.execute("SELECT nextval(pg_get_serial_sequence('stock_proforma_requests','id')) AS id")
        request_id = int(cur.fetchone()["id"])
        number = f"DPF-{date.today():%Y%m%d}-{request_id:05d}"
        cur.execute("""INSERT INTO stock_proforma_requests(id,numero,supplier_id,date_demande,date_limite,statut,notes,created_by)
            VALUES(%s,%s,%s,%s,%s,'À envoyer',%s,%s)""",
            (request_id, number, supplier_id, demand_date, deadline, notes, user.get("id")))
        for line in prepared:
            cur.execute("""INSERT INTO stock_proforma_request_lines(request_id,item_id,reference,designation,quantite,unite,is_custom)
                VALUES(%s,%s,%s,%s,%s,%s,%s)""", (request_id, line["item_id"], line["reference"], line["designation"],
                line["quantity"], line["unit"], line["custom"]))
    track_action(request, "STOCK_PROFORMA_REQUEST", f"Demande de proforma {number} créée · {supplier['designation']}")
    return {"success": True, "id": request_id, "numero": number}


@procurement_router.post("/proformas/{proforma_id}/status")
async def proforma_request_status(proforma_id: int, request: Request):
    _require(request, True)
    data = await request.json()
    status = str(data.get("statut") or "")
    if status not in {"Envoyée", "Refusée"}:
        raise HTTPException(status_code=400, detail="Statut de demande de proforma invalide.")
    with get_db_cursor() as cur:
        cur.execute("""UPDATE stock_proforma_requests SET statut=%s,updated_at=CURRENT_TIMESTAMP
            WHERE id=%s AND statut IN ('À envoyer','Envoyée','Reçue','Acceptée','Refusée') RETURNING numero""",
            (status, proforma_id))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Demande introuvable ou déjà convertie en commande.")
    track_action(request, "STOCK_PROFORMA_STATUS", f"Demande de proforma {row['numero']} : {status}")
    return {"success": True}


@procurement_router.post("/proformas/{proforma_id}/share")
async def proforma_request_share(proforma_id: int, request: Request):
    user = _require(request, True)
    data = await request.json()
    try:
        approver_ids = list(dict.fromkeys(int(value) for value in data.get("approver_ids", [])))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="La liste des approbateurs est invalide.") from exc
    if not approver_ids or len(approver_ids) > 20:
        raise HTTPException(status_code=400, detail="Sélectionne de 1 à 20 approbateurs internes.")
    with get_db_cursor() as cur:
        cur.execute("SELECT numero,statut FROM stock_proforma_requests WHERE id=%s FOR UPDATE", (proforma_id,))
        proforma = cur.fetchone()
        if not proforma:
            raise HTTPException(status_code=404, detail="Demande de proforma introuvable.")
        if proforma["statut"] != "Reçue":
            raise HTTPException(status_code=409, detail="Saisis d’abord l’offre fournisseur avant de la partager pour validation.")
        cur.execute("""SELECT u.id,u.prenom,u.nom FROM users u
            WHERE u.id=ANY(%s) AND u.status='Actif'
              AND (u.role='Administrateur' OR COALESCE(u.permissions->>'stock_view','false')='true'
                   OR COALESCE(u.permissions->>'stock_manage','false')='true')
              AND EXISTS (SELECT 1 FROM clinic_staff s WHERE s.actif=TRUE
                  AND lower(trim(s.nom))=lower(trim(u.nom)) AND lower(trim(s.prenom))=lower(trim(u.prenom)))""", (approver_ids,))
        selected = cur.fetchall()
        if len(selected) != len(approver_ids):
            raise HTTPException(status_code=400, detail="Un approbateur est inactif ou n’a pas accès au stock.")
        cur.execute("SELECT COUNT(*) AS decisions FROM stock_proforma_approvals WHERE request_id=%s AND statut<>'En attente'", (proforma_id,))
        if int(cur.fetchone()["decisions"] or 0):
            raise HTTPException(status_code=409, detail="Une décision interne existe déjà ; crée une nouvelle demande pour relancer une validation.")
        cur.execute("DELETE FROM stock_proforma_approvals WHERE request_id=%s", (proforma_id,))
        for approver in selected:
            cur.execute("""INSERT INTO stock_proforma_approvals(request_id,approver_id,assigned_by)
                VALUES(%s,%s,%s)""", (proforma_id, approver["id"], user.get("id")))
            if int(approver["id"]) != int(user.get("id") or 0):
                cur.execute("""INSERT INTO cliniva_notifications(user_id,categorie,titre,message)
                    VALUES(%s,'approvisionnement',%s,%s)""",
                    (approver["id"], f"Validation d’offre · {proforma['numero']}",
                     f"Une offre fournisseur vous a été assignée pour validation : {proforma['numero']}. Ouvrez le module Approvisionnement."))
    track_action(request, "STOCK_PROFORMA_SHARE", f"Offre {proforma['numero']} partagée avec {len(selected)} approbateur(s)")
    return {"success": True, "approbateurs": len(selected), "regle": "Tous les approbateurs sélectionnés doivent valider."}


@procurement_router.post("/proformas/{proforma_id}/approvals/{approval_id}")
async def proforma_request_approval_decide(proforma_id: int, approval_id: int, request: Request):
    user = _require(request)
    data = await request.json()
    decision = str(data.get("decision") or "").strip()
    comment = str(data.get("commentaire") or "").strip()[:1000]
    if decision not in {"Validée", "Refusée"}:
        raise HTTPException(status_code=400, detail="Choisis Valider ou Refuser l’offre.")
    with get_db_cursor() as cur:
        cur.execute("""SELECT a.id,a.statut,p.numero FROM stock_proforma_approvals a
            JOIN stock_proforma_requests p ON p.id=a.request_id
            WHERE a.id=%s AND a.request_id=%s AND a.approver_id=%s FOR UPDATE OF a,p""",
            (approval_id, proforma_id, user.get("id")))
        approval = cur.fetchone()
        if not approval:
            raise HTTPException(status_code=403, detail="Cette offre ne t’a pas été assignée pour validation.")
        if approval["statut"] != "En attente":
            raise HTTPException(status_code=409, detail="Tu as déjà pris une décision sur cette offre.")
        cur.execute("UPDATE stock_proforma_approvals SET statut=%s,commentaire=%s,decided_at=CURRENT_TIMESTAMP WHERE id=%s",
                    (decision, comment, approval_id))
        if decision == "Refusée":
            cur.execute("UPDATE stock_proforma_requests SET statut='Refusée',updated_at=CURRENT_TIMESTAMP WHERE id=%s", (proforma_id,))
        else:
            cur.execute("""SELECT COUNT(*) FILTER(WHERE statut='En attente') AS pending,
                    COUNT(*) FILTER(WHERE statut='Refusée') AS refusees
                FROM stock_proforma_approvals WHERE request_id=%s""", (proforma_id,))
            decisions = cur.fetchone()
            if int(decisions["pending"] or 0) == 0 and int(decisions["refusees"] or 0) == 0:
                cur.execute("UPDATE stock_proforma_requests SET statut='Acceptée',updated_at=CURRENT_TIMESTAMP WHERE id=%s", (proforma_id,))
    track_action(request, "STOCK_PROFORMA_APPROVAL", f"Offre {approval['numero']} : {decision}")
    return {"success": True, "statut": decision}


@procurement_router.post("/proformas/{proforma_id}/offer")
async def proforma_request_offer_upload(proforma_id: int, request: Request):
    user = _require(request, True)
    data = await request.json()
    decoded = _decode_receipt_file(data, "offre")
    if not decoded:
        raise HTTPException(status_code=400, detail="Sélectionne le fichier de l’offre fournisseur.")
    content, filename, extension = decoded
    stored_path, path = _store_procurement_file("offres", extension, content)
    try:
        with get_db_cursor() as cur:
            cur.execute("""UPDATE stock_proforma_requests SET offre_path=%s,offre_nom=%s,offre_mime=%s,
                statut='Reçue',updated_at=CURRENT_TIMESTAMP WHERE id=%s
                AND statut IN ('Envoyée','À envoyer','Reçue') RETURNING numero""",
                (stored_path, filename, data["offre"]["data_url"].split(";",1)[0].removeprefix("data:"), proforma_id))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=409, detail="Cette demande n’est plus ouverte pour la réception d’une offre fournisseur.")
    except Exception:
        path.unlink(missing_ok=True)
        raise
    track_action(request, "STOCK_PROFORMA_OFFER_FILE", f"Offre fournisseur jointe à {row['numero']}")
    return {"success": True, "nom": filename}


@procurement_router.get("/proformas/{proforma_id}/offer")
def proforma_request_offer_download(proforma_id: int, request: Request):
    _require(request)
    with get_db_cursor() as cur:
        cur.execute("SELECT offre_path,offre_nom,offre_mime FROM stock_proforma_requests WHERE id=%s", (proforma_id,))
        row = cur.fetchone()
    if not row or not row["offre_path"]:
        raise HTTPException(status_code=404, detail="Aucun document d’offre n’est joint à cette demande.")
    stored = Path(row["offre_path"]).resolve()
    root = (Path(DOCS_DIR) / "approvisionnement" / "offres").resolve()
    if root not in stored.parents or not stored.is_file():
        raise HTTPException(status_code=404, detail="Fichier d’offre introuvable.")
    from fastapi.responses import FileResponse
    return FileResponse(stored, media_type=row["offre_mime"] or "application/octet-stream",
                        filename=Path(row["offre_nom"] or stored.name).name,
                        headers={"Cache-Control": "private, no-store"})


def _build_proforma_pdf(proforma: dict[str, Any], lines: list[dict[str, Any]]) -> bytes:
    """Construit le document PDF imprimable et joignable à la demande fournisseur."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT, TA_RIGHT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="La génération PDF est indisponible : installe ReportLab sur le serveur Cliniva.") from exc

    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=17*mm, leftMargin=17*mm,
                            topMargin=16*mm, bottomMargin=16*mm, title=f"Demande de proforma {proforma['numero']}")
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ClinivaTitle", parent=styles["Title"], fontName="Helvetica-Bold",
                              fontSize=21, leading=25, textColor=colors.HexColor("#183b20"), alignment=TA_LEFT, spaceAfter=3*mm))
    styles.add(ParagraphStyle(name="ClinivaSmall", parent=styles["Normal"], fontSize=9, leading=13,
                              textColor=colors.HexColor("#526157")))
    styles.add(ParagraphStyle(name="ClinivaRight", parent=styles["Normal"], fontSize=9, leading=13, alignment=TA_RIGHT))
    safe = lambda value: escape(str(value or "—"))
    story = [Paragraph("CLINIVA · APPROVISIONNEMENT", ParagraphStyle("brand", parent=styles["Normal"],
                          fontName="Helvetica-Bold", fontSize=9, textColor=colors.HexColor("#4d7c0f"))),
             Spacer(1, 3*mm), Paragraph("Demande de proforma", styles["ClinivaTitle"]),
             Paragraph(f"Référence : <b>{safe(proforma['numero'])}</b>", styles["ClinivaSmall"]), Spacer(1, 8*mm)]
    supplier_emails = " · ".join(filter(None, (str(proforma.get("fournisseur_email") or "").strip(),
        str(proforma.get("fournisseur_email_2") or "").strip(), str(proforma.get("fournisseur_email_3") or "").strip()))) or "—"
    meta = [[Paragraph(f"<b>Fournisseur</b><br/>{safe(proforma.get('fournisseur'))}", styles["Normal"]),
             Paragraph(f"<b>Date de demande</b><br/>{safe(proforma.get('date_demande'))}", styles["Normal"])],
            [Paragraph(f"<b>E-mail(s)</b><br/>{safe(supplier_emails)}", styles["Normal"]),
             Paragraph(f"<b>Réponse souhaitée avant</b><br/>{safe(proforma.get('date_limite'))}", styles["Normal"])]]
    meta_table = Table(meta, colWidths=[88*mm, 88*mm], hAlign="LEFT")
    meta_table.setStyle(TableStyle([("BACKGROUND", (0,0),(-1,-1), colors.HexColor("#f5f8f5")),
        ("BOX",(0,0),(-1,-1),.6,colors.HexColor("#dbe5dc")),("INNERGRID",(0,0),(-1,-1),.4,colors.HexColor("#e2e8e2")),
        ("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),10),("RIGHTPADDING",(0,0),(-1,-1),10),
        ("TOPPADDING",(0,0),(-1,-1),9),("BOTTOMPADDING",(0,0),(-1,-1),9)]))
    story += [meta_table, Spacer(1, 8*mm), Paragraph("Veuillez nous transmettre votre meilleure offre de prix pour les articles suivants :", styles["Normal"]), Spacer(1, 3*mm)]
    data = [["Référence", "Désignation", "Quantité", "Unité"]]
    for line in lines:
        data.append([safe(line.get("reference")), Paragraph(safe(line.get("designation")), styles["Normal"]),
                     f"{Decimal(str(line.get('quantite') or 0)):,.3f}".replace(",", " ").rstrip("0").rstrip("."), safe(line.get("unite"))])
    table = Table(data, colWidths=[31*mm, 91*mm, 27*mm, 27*mm], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#183b20")),
        ("TEXTCOLOR",(0,0),(-1,0),colors.white),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
        ("FONTSIZE",(0,0),(-1,-1),9),("GRID",(0,0),(-1,-1),.45,colors.HexColor("#cbd5ce")),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#f7faf7")]),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),("ALIGN",(2,1),(2,-1),"RIGHT"),
        ("LEFTPADDING",(0,0),(-1,-1),7),("RIGHTPADDING",(0,0),(-1,-1),7),
        ("TOPPADDING",(0,0),(-1,-1),8),("BOTTOMPADDING",(0,0),(-1,-1),8)]))
    story += [table]
    if proforma.get("notes"):
        story += [Spacer(1, 7*mm), Paragraph("Notes et conditions", styles["Heading3"]),
                  Paragraph(safe(proforma["notes"]), styles["Normal"])]
    story += [Spacer(1, 14*mm), Paragraph("Service des approvisionnements", styles["ClinivaRight"])]
    doc.build(story)
    return output.getvalue()


@procurement_router.get("/proformas/{proforma_id}/pdf")
def proforma_request_pdf(proforma_id: int, request: Request):
    _require(request)
    with get_db_cursor() as cur:
        cur.execute("""SELECT p.id,p.numero,p.date_demande,p.date_limite,p.notes,s.designation AS fournisseur,
                s.email AS fournisseur_email,s.email_2 AS fournisseur_email_2,s.email_3 AS fournisseur_email_3
            FROM stock_proforma_requests p JOIN stock_suppliers s ON s.id=p.supplier_id WHERE p.id=%s""", (proforma_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Demande de proforma introuvable.")
        proforma = dict(row)
        cur.execute("""SELECT l.reference,COALESCE(i.designation,l.designation) AS designation,l.quantite,
                COALESCE(i.unite,l.unite) AS unite FROM stock_proforma_request_lines l
                LEFT JOIN stock_items i ON i.id=l.item_id WHERE l.request_id=%s ORDER BY l.id""", (proforma_id,))
        lines = [dict(line) for line in cur.fetchall()]
    pdf = _build_proforma_pdf(proforma, lines)
    filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"Demande_proforma_{proforma['numero']}") + ".pdf"
    return Response(content=pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "no-store"})


@procurement_router.post("/proformas/{proforma_id}/email")
async def proforma_request_email(proforma_id: int, request: Request):
    user = _require(request, True)
    data = await request.json()
    requested_recipients = data.get("emails")
    with get_db_cursor() as cur:
        cur.execute("""SELECT p.id,p.numero,p.date_demande,p.date_limite,p.notes,s.designation AS fournisseur,
                s.email AS fournisseur_email,s.email_2 AS fournisseur_email_2,s.email_3 AS fournisseur_email_3
            FROM stock_proforma_requests p JOIN stock_suppliers s ON s.id=p.supplier_id WHERE p.id=%s""", (proforma_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Demande de proforma introuvable.")
        proforma = dict(row)
        allowed_recipients = list(dict.fromkeys(str(proforma.get(key) or "").strip() for key in
            ("fournisseur_email", "fournisseur_email_2", "fournisseur_email_3") if str(proforma.get(key) or "").strip()))
        if requested_recipients is None:
            requested_recipients = allowed_recipients[:1]
        if not isinstance(requested_recipients, list):
            raise HTTPException(status_code=400, detail="Sélectionne une ou plusieurs adresses de destination.")
        recipients = list(dict.fromkeys(str(email or "").strip() for email in requested_recipients if str(email or "").strip()))
        if not recipients or len(recipients) > 3 or any(not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) for email in recipients):
            raise HTTPException(status_code=400, detail="Sélectionne de 1 à 3 adresses e-mail valides.")
        if any(email not in allowed_recipients for email in recipients):
            raise HTTPException(status_code=400, detail="Les destinataires doivent être choisis parmi les adresses enregistrées sur le fournisseur.")
        cur.execute("""SELECT l.reference,COALESCE(i.designation,l.designation) AS designation,l.quantite,
                COALESCE(i.unite,l.unite) AS unite FROM stock_proforma_request_lines l
                LEFT JOIN stock_items i ON i.id=l.item_id WHERE l.request_id=%s ORDER BY l.id""", (proforma_id,))
        lines = [dict(line) for line in cur.fetchall()]
    try:
        configuration = get_stock_smtp_configuration()
        pdf = _build_proforma_pdf(proforma, lines)
        subject = f"Demande de proforma {proforma['numero']} · Cliniva"
        content = (f"Bonjour,\n\nVeuillez trouver en pièce jointe notre demande de proforma {proforma['numero']}.\n\n"
                   "Cordialement,\nService des approvisionnements")
        html = build_cliniva_email_html("Demande de proforma", proforma["numero"],
            f"<p>Bonjour,</p><p>Veuillez trouver en pièce jointe notre demande de proforma <strong>{escape(proforma['numero'])}</strong>.</p><p>Cordialement,<br>Service des approvisionnements</p>",
            f"Demande de proforma {proforma['numero']}")
        await asyncio.to_thread(send_smtp_email, configuration, recipients, subject, content, html,
                                [(f"Demande_proforma_{proforma['numero']}.pdf", pdf, "application/pdf")])
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Envoi de la demande par e-mail impossible : {exc}") from exc
    with get_db_cursor() as cur:
        cur.execute("""UPDATE stock_proforma_requests SET statut='Envoyée',updated_at=CURRENT_TIMESTAMP
            WHERE id=%s AND statut='À envoyer'""", (proforma_id,))
    track_action(request, "STOCK_PROFORMA_EMAIL", f"Demande de proforma {proforma['numero']} envoyée à {', '.join(recipients)}")
    return {"success": True, "numero": proforma["numero"], "emails": recipients}


@procurement_router.post("/proformas/{proforma_id}/response")
async def proforma_request_response(proforma_id: int, request: Request):
    user = _require(request, True)
    data = await request.json()
    rows = data.get("lignes") or []
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=400, detail="Saisis les prix reçus sur la proforma fournisseur.")
    prepared = []
    seen: set[int] = set()
    for index, raw in enumerate(rows, 1):
        try:
            line_id = int(raw.get("ligne_id"))
            price = _money(raw.get("prix_unitaire"), f"ligne {index}")
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Prix invalide à la ligne {index}.") from exc
        if line_id in seen:
            raise HTTPException(status_code=400, detail=f"Ligne dupliquée : {index}.")
        seen.add(line_id)
        prepared.append((line_id, price))
    with get_db_cursor() as cur:
        cur.execute("SELECT numero,statut FROM stock_proforma_requests WHERE id=%s FOR UPDATE", (proforma_id,))
        proforma = cur.fetchone()
        if not proforma:
            raise HTTPException(status_code=404, detail="Demande introuvable ou déjà convertie.")
        if proforma["statut"] not in {"À envoyer", "Envoyée", "Reçue"}:
            raise HTTPException(status_code=409, detail="Cette offre est déjà refusée, validée ou convertie et ne peut plus être modifiée.")
        cur.execute("SELECT COUNT(*) AS total FROM stock_proforma_approvals WHERE request_id=%s", (proforma_id,))
        if int(cur.fetchone()["total"] or 0):
            raise HTTPException(status_code=409, detail="L’offre a été partagée pour validation et ne peut plus être modifiée.")
        cur.execute("SELECT id FROM stock_proforma_request_lines WHERE request_id=%s ORDER BY id", (proforma_id,))
        expected = {int(row["id"]) for row in cur.fetchall()}
        if seen != expected:
            raise HTTPException(status_code=400, detail="Renseigne un prix pour chaque ligne de la proforma.")
        total = Decimal("0")
        for line_id, price in prepared:
            cur.execute("UPDATE stock_proforma_request_lines SET prix_reponse=%s WHERE id=%s AND request_id=%s RETURNING quantite", (price, line_id, proforma_id))
            line = cur.fetchone()
            if not line:
                raise HTTPException(status_code=400, detail="Une ligne ne correspond pas à cette demande.")
            total += Decimal(str(line["quantite"])) * price
        cur.execute("""UPDATE stock_proforma_requests SET statut='Reçue',montant_propose=%s,
            updated_at=CURRENT_TIMESTAMP WHERE id=%s""", (total.quantize(Decimal("0.01")), proforma_id))
    track_action(request, "STOCK_PROFORMA_RESPONSE", f"Réponse fournisseur enregistrée pour {proforma['numero']} · {total} DA")
    return {"success": True, "montant": str(total.quantize(Decimal("0.01")))}


@procurement_router.post("/proformas/{proforma_id}/convert")
async def proforma_request_convert(proforma_id: int, request: Request):
    user = _require(request, True)
    with get_db_cursor() as cur:
        cur.execute("""SELECT p.id,p.numero,p.supplier_id,p.statut,s.designation AS fournisseur
            FROM stock_proforma_requests p JOIN stock_suppliers s ON s.id=p.supplier_id
            WHERE p.id=%s FOR UPDATE OF p""", (proforma_id,))
        proforma = cur.fetchone()
        if not proforma:
            raise HTTPException(status_code=404, detail="Demande de proforma introuvable.")
        if proforma["statut"] != "Acceptée":
            raise HTTPException(status_code=409, detail="Tous les approbateurs internes doivent valider l’offre avant de créer la commande.")
        cur.execute("""SELECT COUNT(*) AS total,COUNT(*) FILTER(WHERE statut='Validée') AS approuvees
            FROM stock_proforma_approvals WHERE request_id=%s""", (proforma_id,))
        approval_summary = cur.fetchone()
        if not int(approval_summary["total"] or 0) or int(approval_summary["total"]) != int(approval_summary["approuvees"] or 0):
            raise HTTPException(status_code=409, detail="La validation interne complète est obligatoire avant de créer la commande.")
        cur.execute("""SELECT l.id AS line_id,l.item_id,l.reference,l.designation,l.quantite,l.unite,l.prix_reponse,
                COALESCE(i.actif,FALSE) AS actif
            FROM stock_proforma_request_lines l LEFT JOIN stock_items i ON i.id=l.item_id
            WHERE l.request_id=%s ORDER BY l.id""", (proforma_id,))
        lines = cur.fetchall()
        if not lines or any(line["prix_reponse"] is None or (line["item_id"] and not line["actif"]) for line in lines):
            raise HTTPException(status_code=409, detail="La proforma doit avoir un prix pour chaque article actif.")
        cur.execute("SELECT nextval(pg_get_serial_sequence('stock_supplier_orders','id')) AS id")
        order_id = int(cur.fetchone()["id"])
        order_number = f"ACH-{date.today():%Y%m%d}-{order_id:05d}"
        cur.execute("""INSERT INTO stock_supplier_orders(id,numero,supplier_id,date_commande,statut,notes,created_by)
            VALUES(%s,%s,%s,CURRENT_DATE,'Commandée',%s,%s)""",
            (order_id, order_number, proforma["supplier_id"], f"Commande créée depuis la proforma {proforma['numero']}", user.get("id")))
        for line in lines:
            item_id = line["item_id"]
            if not item_id:
                cur.execute("""SELECT id FROM stock_items
                    WHERE LOWER(BTRIM(designation))=LOWER(BTRIM(%s)) AND LOWER(unite)=LOWER(%s) AND actif=TRUE
                    ORDER BY id LIMIT 1""", (line["designation"], line["unite"]))
                existing = cur.fetchone()
                if existing:
                    item_id = int(existing["id"])
                else:
                    reference = str(line["reference"] or "").strip()
                    if reference:
                        cur.execute("SELECT 1 FROM stock_items WHERE reference=%s", (reference,))
                        if cur.fetchone():
                            reference = ""
                    cur.execute("""INSERT INTO stock_items(reference,designation,categorie,unite,suivi_peremption,actif,notes,created_by)
                        VALUES(%s,%s,'Autre',%s,FALSE,TRUE,%s,%s) RETURNING id""",
                        (reference,line["designation"],line["unite"],f"Créé depuis la demande de proforma {proforma['numero']}",user.get("id")))
                    item_id = int(cur.fetchone()["id"])
                cur.execute("UPDATE stock_proforma_request_lines SET item_id=%s WHERE id=%s", (item_id,line["line_id"]))
            cur.execute("""INSERT INTO stock_supplier_order_lines(order_id,item_id,designation,quantite,unite,prix_unitaire)
                VALUES(%s,%s,%s,%s,%s,%s)""",
                (order_id, item_id, line["designation"], line["quantite"], line["unite"], line["prix_reponse"]))
        cur.execute("""UPDATE stock_proforma_requests SET statut='Convertie',converted_order_id=%s,
            updated_at=CURRENT_TIMESTAMP WHERE id=%s""", (order_id, proforma_id))
    track_action(request, "STOCK_PROFORMA_CONVERT", f"Proforma {proforma['numero']} convertie en commande {order_number}")
    return {"success": True, "numero": order_number}


@procurement_router.post("/orders/{order_id}/status")
async def procurement_order_status(order_id: int, request: Request):
    user = _require(request, True)
    data = await request.json()
    status = str(data.get("statut") or "")
    if status not in {"Commandée", "Annulée"}:
        raise HTTPException(status_code=400, detail="Statut de commande invalide.")
    with get_db_cursor() as cur:
        cur.execute("UPDATE stock_supplier_orders SET statut=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND statut<>'Reçue' RETURNING numero", (status, order_id))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Commande fournisseur introuvable.")
    track_action(request, "STOCK_SUPPLIER_ORDER_STATUS", f"Commande {row['numero']} : {status}")
    return {"success": True}


@procurement_router.post("/orders/{order_id}/receive")
async def supplier_order_receive(order_id: int, request: Request):
    user = _require(request, True)
    data = await request.json()
    delivery_note = str(data.get("bon_livraison") or "").strip()[:120]
    delivery_receipt = _decode_receipt_file(data, "bon_livraison_fichier")
    lines = data.get("lignes") or []
    if not isinstance(lines, list) or not lines or len(lines) > 100:
        raise HTTPException(status_code=400, detail="Ajoute au moins une ligne à la réception.")
    normalized = []
    seen: set[int] = set()
    for index, line in enumerate(lines, 1):
        try:
            order_line_id = int(line.get("order_line_id"))
            packages = Decimal(str(line.get("quantite", "")).replace(",", "."))
            per_unit = Decimal(str(line.get("quantite_par_unite") or "1").replace(",", "."))
            margin = Decimal(str(line.get("marge_vente") or "15").replace(",", "."))
            price = _money(line.get("prix_achat"), f"ligne {index}")
        except (TypeError, ValueError, InvalidOperation) as exc:
            raise HTTPException(status_code=400, detail=f"Données invalides à la ligne {index}.") from exc
        lot_number = str(line.get("numero_lot") or "").strip()[:120]
        expiry = _date(line.get("date_peremption"), f"péremption ligne {index}")
        warehouse, location = str(line.get("entrepot") or "").strip(), str(line.get("emplacement") or "").strip()
        unit = str(line.get("unite_stock") or "").strip()[:80]
        if (order_line_id in seen or not packages.is_finite() or not per_unit.is_finite() or not margin.is_finite()
                or packages <= 0 or per_unit <= 0 or margin < 0):
            raise HTTPException(status_code=400, detail=f"Quantité ou conditionnement invalide à la ligne {index}.")
        if not lot_number or not warehouse or not location or not unit:
            raise HTTPException(status_code=400, detail=f"Lot, unité, entrepôt et emplacement sont obligatoires à la ligne {index}.")
        seen.add(order_line_id)
        sale_price = _money(line.get("prix_vente") if line.get("prix_vente") not in (None, "") else price * (1 + margin / 100), f"prix de vente ligne {index}")
        normalized.append({"order_line_id": order_line_id, "packages": packages, "per_unit": per_unit,
            "margin": margin, "purchase_price": price, "sale_price": sale_price, "lot_number": lot_number,
            "expiry": expiry, "warehouse": warehouse, "location": location, "unit": unit})

    with get_db_cursor() as cur:
        cur.execute("""SELECT o.id,o.numero,o.supplier_id,o.statut,s.designation AS fournisseur
            FROM stock_supplier_orders o JOIN stock_suppliers s ON s.id=o.supplier_id
            WHERE o.id=%s FOR UPDATE OF o""", (order_id,))
        order = cur.fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Commande fournisseur introuvable.")
        if order["statut"] in {"Annulée", "Reçue"}:
            raise HTTPException(status_code=409, detail="Cette commande est annulée ou déjà complètement reçue.")
        prepared = []
        for index, row in enumerate(normalized, 1):
            cur.execute("""SELECT l.id,l.item_id,l.designation,l.quantite,l.unite,l.prix_unitaire,l.quantite_recue,
                    i.reference,i.unite AS unite_article,i.categorie,i.suivi_peremption,i.actif
                FROM stock_supplier_order_lines l JOIN stock_items i ON i.id=l.item_id
                WHERE l.id=%s AND l.order_id=%s FOR UPDATE OF l,i""", (row["order_line_id"], order_id))
            order_line = cur.fetchone()
            if not order_line or not order_line["actif"]:
                raise HTTPException(status_code=400, detail=f"Ligne {index} : article absent ou archivé.")
            remaining = Decimal(str(order_line["quantite"])) - Decimal(str(order_line["quantite_recue"]))
            if row["packages"] > remaining:
                raise HTTPException(status_code=400, detail=f"Ligne {index} : il ne reste que {remaining} {order_line['unite']} à réceptionner.")
            if order_line["categorie"] == "Pharmaceutique" and not row["lot_number"]:
                raise HTTPException(status_code=400, detail=f"Le numéro de lot pharmaceutique est obligatoire à la ligne {index}.")
            if order_line["suivi_peremption"] and not row["expiry"]:
                raise HTTPException(status_code=400, detail=f"La date de péremption est obligatoire à la ligne {index}.")
            cur.execute("SELECT 1 FROM stock_warehouses WHERE nom=%s AND actif=TRUE", (row["warehouse"],))
            if not cur.fetchone():
                raise HTTPException(status_code=400, detail=f"Entrepôt inactif à la ligne {index}.")
            cur.execute("SELECT 1 FROM stock_locations WHERE nom=%s AND actif=TRUE", (row["location"],))
            if not cur.fetchone():
                raise HTTPException(status_code=400, detail=f"Emplacement inactif à la ligne {index}.")
            item = {"id":order_line["item_id"],"reference":order_line["reference"],
                    "designation":order_line["designation"],"unite":order_line["unite_article"]}
            prepared.append({**row,"order_line":order_line,"item":item,
                "quantity":(row["packages"]*row["per_unit"]).quantize(Decimal("0.001"))})
        document_id, document_number = _create_stock_document(cur,user,"Entrée",supplier_id=order["supplier_id"],
            supplier=order["fournisseur"],supplier_order_id=order_id,
            reason=f"Réception commande fournisseur {order['numero']}",notes=str(data.get("notes") or ""))
        cur.execute("UPDATE stock_documents SET numero_bon_livraison=%s WHERE id=%s", (delivery_note, document_id))
        if delivery_receipt:
            content, original_name, extension = delivery_receipt
            storage_dir = Path(DOCS_DIR) / "gestion_stock" / "bons_entree" / document_number
            storage_dir.mkdir(parents=True, exist_ok=True)
            safe_name = f"bon_livraison_fournisseur{extension}"
            file_path = storage_dir / safe_name
            file_path.write_bytes(content)
            mime = str(data.get("bon_livraison_fichier", {}).get("data_url", "")).split(";",1)[0].removeprefix("data:")
            cur.execute("""INSERT INTO stock_document_attachments
                (document_id,file_path,file_name,mime_type,page_number,file_size,created_by)
                VALUES(%s,%s,%s,%s,1,%s,%s)""",
                (document_id, str(file_path), original_name, mime, len(content), user.get("id")))
        for row in prepared:
            cur.execute("""INSERT INTO stock_lots
                (item_id,numero_lot,date_peremption,quantite_initiale,quantite_actuelle,prix_unitaire,prix_achat,
                prix_vente,marge_vente,unite_stock,quantite_par_unite,emplacement,entrepot,fournisseur_origine_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (row["item"]["id"],row["lot_number"],row["expiry"],row["quantity"],row["quantity"],
                 (row["purchase_price"]/row["per_unit"]).quantize(Decimal("0.01")),row["purchase_price"],
                 row["sale_price"],row["margin"],row["unit"],row["per_unit"],row["location"],row["warehouse"],order["supplier_id"]))
            lot={"id":int(cur.fetchone()["id"]),"numero_lot":row["lot_number"],"date_peremption":row["expiry"]}
            _insert_stock_line(cur,user,document_id,row["item"],lot,row["quantity"],direction="Entrée",
                packages=row["packages"],unit=row["unit"],per_unit=row["per_unit"],purchase_price=row["purchase_price"],
                sale_price=row["sale_price"],margin=row["margin"],warehouse=row["warehouse"],location=row["location"],
                source_order_line_id=row["order_line_id"])
            cur.execute("UPDATE stock_supplier_order_lines SET quantite_recue=quantite_recue+%s WHERE id=%s",
                        (row["packages"],row["order_line_id"]))
        cur.execute("""UPDATE stock_supplier_orders o SET statut=CASE
                WHEN NOT EXISTS(SELECT 1 FROM stock_supplier_order_lines l WHERE l.order_id=o.id AND l.quantite_recue<l.quantite) THEN 'Reçue'
                ELSE 'Partiellement reçue' END,updated_at=CURRENT_TIMESTAMP WHERE o.id=%s""", (order_id,))
    track_action(request,"STOCK_SUPPLIER_RECEIPT",f"Réception {document_number} créée depuis {order['numero']} · {len(prepared)} ligne(s)")
    return {"success":True,"numero":document_number}


@procurement_router.post("/invoices")
async def supplier_invoice_create(request: Request):
    user = _require(request, True)
    data = await request.json()
    number = str(data.get("numero_facture") or "").strip()[:120]
    if not number:
        raise HTTPException(status_code=400, detail="Le numéro de facture fournisseur est obligatoire.")
    receipt_id = int(data.get("stock_document_id") or 0) or None
    order_id = int(data.get("order_id") or 0) or None
    invoice_date = _date(data.get("date_facture"), "facture", True)
    due_date = _date(data.get("date_echeance"), "échéance")
    with get_db_cursor() as cur:
        if receipt_id:
            cur.execute("""SELECT d.fournisseur_id,d.supplier_order_id,d.numero,d.type_document,d.statut,
                    COALESCE(SUM(CASE WHEN COALESCE(l.quantite_unite_stock,0)>0 AND l.prix_achat IS NOT NULL
                        THEN l.quantite_unite_stock*l.prix_achat
                        ELSE l.quantite*COALESCE(l.prix_unitaire,l.prix_achat/NULLIF(l.quantite_par_unite,0),0) END),0) AS total
                FROM stock_documents d LEFT JOIN stock_document_lines l ON l.document_id=d.id WHERE d.id=%s
                GROUP BY d.id""", (receipt_id,))
            receipt = cur.fetchone()
            if not receipt or receipt["type_document"] != "Entrée" or receipt["statut"] == "Annulé" or not receipt["fournisseur_id"]:
                raise HTTPException(status_code=400, detail="Choisis un bon d’entrée validé et lié à un fournisseur.")
            supplier_id, amount = receipt["fournisseur_id"], _money(receipt["total"], "réception")
            linked_order_id = int(receipt["supplier_order_id"] or 0) or None
            if order_id and linked_order_id and order_id != linked_order_id:
                raise HTTPException(status_code=400, detail="La réception est rattachée à une autre commande fournisseur.")
            order_id = order_id or linked_order_id
        else:
            try:
                supplier_id = int(data.get("supplier_id"))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Choisis un fournisseur.") from exc
            amount = _money(data.get("montant"), "facture")
        cur.execute("SELECT id FROM stock_suppliers WHERE id=%s", (supplier_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Fournisseur introuvable.")
        if order_id:
            cur.execute("SELECT supplier_id FROM stock_supplier_orders WHERE id=%s", (order_id,))
            linked_order=cur.fetchone()
            if not linked_order or int(linked_order["supplier_id"])!=int(supplier_id):
                raise HTTPException(status_code=400,detail="La commande sélectionnée n’appartient pas à ce fournisseur.")
        try:
            cur.execute("""INSERT INTO stock_supplier_invoices
                (supplier_id,order_id,stock_document_id,numero_facture,date_facture,date_echeance,montant,notes,created_by)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (supplier_id, order_id, receipt_id, number, invoice_date, due_date, amount,
                 str(data.get("notes") or "")[:2000], user.get("id")))
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise HTTPException(status_code=409, detail="Cette réception possède déjà une facture fournisseur liée.") from exc
            raise
        invoice_id = cur.fetchone()["id"]
    track_action(request, "STOCK_SUPPLIER_INVOICE", f"Facture fournisseur {number} enregistrée · {amount} DA")
    return {"success": True, "id": invoice_id}


@procurement_router.post("/invoices/{invoice_id}/payments")
async def supplier_payment_create(invoice_id: int, request: Request):
    user = _require(request, True)
    data = await request.json()
    amount = _money(data.get("montant"), "paiement", allow_zero=False)
    paid_date = _date(data.get("date_paiement"), "paiement", True)
    mode = str(data.get("mode_paiement") or "").strip()[:80]
    reference = str(data.get("reference") or "").strip()[:160]
    if mode not in {"Chèque bancaire", "Comptant", "Virement bancaire"}:
        raise HTTPException(status_code=400, detail="Choisis chèque bancaire, comptant ou virement bancaire.")
    if mode in {"Chèque bancaire", "Virement bancaire"} and not reference:
        raise HTTPException(status_code=400, detail="Indique le numéro du chèque ou la référence du virement.")
    receipt = _decode_receipt_file(data)
    stored_path = ""
    stored_file: Path | None = None
    if receipt:
        content, receipt_name, extension = receipt
        stored_path, stored_file = _store_procurement_file("justificatifs_paiement", extension, content)
    else:
        receipt_name = ""
        content = b""
    with get_db_cursor() as cur:
        try:
            cur.execute("SELECT montant FROM stock_supplier_invoices WHERE id=%s FOR UPDATE", (invoice_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Facture fournisseur introuvable.")
            cur.execute("SELECT COALESCE(SUM(montant),0) AS total FROM stock_supplier_payments WHERE invoice_id=%s", (invoice_id,))
            paid = cur.fetchone()["total"]
            balance = Decimal(str(row["montant"] or 0)) - Decimal(str(paid or 0))
            if amount > balance:
                raise HTTPException(status_code=400, detail=f"Le paiement dépasse le solde restant ({balance} DA).")
            mime = receipt[0] and str(data.get("justificatif", {}).get("data_url", "")).split(";",1)[0].removeprefix("data:") if receipt else ""
            cur.execute("""INSERT INTO stock_supplier_payments(invoice_id,date_paiement,montant,mode_paiement,reference,notes,created_by,
                    justificatif_path,justificatif_nom,justificatif_mime)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (invoice_id, paid_date, amount, mode, reference, str(data.get("notes") or "")[:1000], user.get("id"),
                 stored_path, receipt_name, mime))
            payment_id = int(cur.fetchone()["id"])
        except Exception:
            if stored_file:
                stored_file.unlink(missing_ok=True)
            raise
    track_action(request, "STOCK_SUPPLIER_PAYMENT", f"Paiement {mode} de {amount} DA enregistré pour facture #{invoice_id}" + (f" · justificatif : {receipt_name}" if receipt else ""))
    return {"success": True, "id": payment_id, "justificatif": receipt_name}


@procurement_router.get("/payments/{payment_id}/receipt")
def supplier_payment_receipt(payment_id: int, request: Request):
    _require(request)
    with get_db_cursor() as cur:
        cur.execute("SELECT justificatif_path,justificatif_nom,justificatif_mime FROM stock_supplier_payments WHERE id=%s", (payment_id,))
        row = cur.fetchone()
    if not row or not row["justificatif_path"]:
        raise HTTPException(status_code=404, detail="Aucun reçu n’est joint à ce paiement.")
    stored = Path(row["justificatif_path"]).resolve()
    root = (Path(DOCS_DIR) / "approvisionnement" / "justificatifs_paiement").resolve()
    if root not in stored.parents or not stored.is_file():
        raise HTTPException(status_code=404, detail="Reçu de paiement introuvable.")
    from fastapi.responses import FileResponse
    return FileResponse(stored, media_type=row["justificatif_mime"] or "application/octet-stream",
                        filename=Path(row["justificatif_nom"] or stored.name).name,
                        headers={"Cache-Control": "private, no-store"})


@procurement_router.post("/returns")
async def supplier_return_create(request: Request):
    user = _require(request, True)
    data = await request.json()
    try:
        supplier_id = int(data.get("supplier_id"))
        invoice_id = int(data.get("invoice_id") or 0) or None
        rows = data.get("lignes") or []
        if not rows:
            raise ValueError("empty")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Choisis le fournisseur et au moins un lot à retourner.") from exc
    reason = str(data.get("motif") or "").strip()[:500]
    with get_db_cursor() as cur:
        cur.execute("SELECT designation FROM stock_suppliers WHERE id=%s AND actif=TRUE", (supplier_id,))
        supplier = cur.fetchone()
        if not supplier:
            raise HTTPException(status_code=404, detail="Fournisseur actif introuvable.")
        if invoice_id:
            cur.execute("SELECT supplier_id FROM stock_supplier_invoices WHERE id=%s", (invoice_id,))
            inv = cur.fetchone()
            if not inv or int(inv["supplier_id"]) != supplier_id:
                raise HTTPException(status_code=400, detail="La facture choisie n’appartient pas à ce fournisseur.")
        normalized = []
        seen = set()
        for index, raw in enumerate(rows, 1):
            try:
                lot_id = int(raw.get("lot_id"))
                quantity = Decimal(str(raw.get("quantite") or "").replace(",", "."))
            except (TypeError, ValueError, InvalidOperation) as exc:
                raise HTTPException(status_code=400, detail=f"Lot ou quantité invalide à la ligne {index}.") from exc
            if lot_id in seen or not quantity.is_finite() or quantity <= 0:
                raise HTTPException(status_code=400, detail=f"Quantité invalide à la ligne {index}.")
            seen.add(lot_id)
            cur.execute("""SELECT l.*,i.reference,i.designation,i.unite AS unite_article,
                    COALESCE(l.prix_unitaire,l.prix_achat/NULLIF(l.quantite_par_unite,0),0) AS cout_unitaire,
                    COALESCE(l.fournisseur_origine_id,(SELECT d.fournisseur_id FROM stock_movements m JOIN stock_documents d ON d.id=m.document_id
                     WHERE m.lot_id=l.id AND m.sens='Entrée' AND d.fournisseur_id IS NOT NULL
                     ORDER BY m.created_at,m.id LIMIT 1)) AS origine_fournisseur
                FROM stock_lots l JOIN stock_items i ON i.id=l.item_id WHERE l.id=%s FOR UPDATE OF l""", (lot_id,))
            lot = cur.fetchone()
            if not lot or int(lot["origine_fournisseur"] or 0) != supplier_id:
                raise HTTPException(status_code=400, detail=f"Le lot de la ligne {index} ne provient pas de ce fournisseur.")
            if quantity > Decimal(str(lot["quantite_actuelle"])):
                raise HTTPException(status_code=400, detail=f"La quantité à retourner dépasse le stock du lot {lot['numero_lot']}.")
            normalized.append((dict(lot),quantity))
        document_id, document_number = _create_stock_document(cur,user,"Sortie",supplier_id=supplier_id,
            supplier=supplier["designation"],service="Retour fournisseur",reason=reason or "Retour fournisseur")
        total_credit=Decimal("0")
        for lot, quantity in normalized:
            item={"id":lot["item_id"],"reference":lot["reference"],"designation":lot["designation"],"unite":lot["unite_article"]}
            cur.execute("UPDATE stock_lots SET quantite_actuelle=quantite_actuelle-%s WHERE id=%s",(quantity,lot["id"]))
            _insert_stock_line(cur,user,document_id,item,lot,quantity,direction="Sortie")
            total_credit += quantity*Decimal(str(lot["cout_unitaire"] or 0))
        cur.execute("SELECT nextval(pg_get_serial_sequence('stock_supplier_returns','id')) AS id")
        return_id=int(cur.fetchone()["id"])
        return_number=f"RSF-{date.today():%Y%m%d}-{return_id:05d}"
        cur.execute("""INSERT INTO stock_supplier_returns(id,supplier_id,invoice_id,document_id,numero,montant_avoir,motif,created_by)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (return_id,supplier_id,invoice_id,document_id,return_number,total_credit,reason,user.get("id")))
    track_action(request,"STOCK_SUPPLIER_RETURN",f"Retour {return_number} · {supplier['designation']} · avoir {total_credit} DA")
    return {"success":True,"numero":return_number,"bon":document_number,"montant_avoir":str(total_credit)}


@procurement_router.post("/transfers")
async def stock_transfer_create(request: Request):
    user=_require(request,True)
    data=await request.json()
    source=str(data.get("entrepot_source") or "").strip()
    destination=str(data.get("entrepot_destination") or "").strip()
    source_location=str(data.get("emplacement_source") or "").strip()
    destination_location=str(data.get("emplacement_destination") or "").strip()
    rows=data.get("lignes") or []
    if not source or not destination or source==destination or not rows:
        raise HTTPException(status_code=400,detail="Choisis deux entrepôts différents et au moins un lot.")
    with get_db_cursor() as cur:
        for name in (source,destination):
            cur.execute("SELECT 1 FROM stock_warehouses WHERE nom=%s AND actif=TRUE",(name,))
            if not cur.fetchone(): raise HTTPException(status_code=400,detail=f"Entrepôt inactif : {name}.")
        normalized=[]; seen=set()
        for idx,raw in enumerate(rows,1):
            try: lot_id=int(raw.get("lot_id")); qty=Decimal(str(raw.get("quantite") or "").replace(",","."))
            except (TypeError,ValueError,InvalidOperation) as exc: raise HTTPException(status_code=400,detail=f"Lot ou quantité invalide à la ligne {idx}.") from exc
            if lot_id in seen or not qty.is_finite() or qty<=0: raise HTTPException(status_code=400,detail=f"Quantité invalide à la ligne {idx}.")
            seen.add(lot_id)
            cur.execute("""SELECT l.*,i.reference,i.designation,i.unite AS unite_article FROM stock_lots l JOIN stock_items i ON i.id=l.item_id WHERE l.id=%s FOR UPDATE OF l""",(lot_id,))
            lot=cur.fetchone()
            if not lot or lot["entrepot"]!=source or qty>Decimal(str(lot["quantite_actuelle"])):
                raise HTTPException(status_code=400,detail=f"Le lot sélectionné n’est pas disponible dans {source}.")
            normalized.append((dict(lot),qty))
        src_doc,src_num=_create_stock_document(cur,user,"Sortie",service=f"Transfert vers {destination}",reason="Transfert inter-entrepôts")
        dst_doc,dst_num=_create_stock_document(cur,user,"Entrée",service=f"Transfert depuis {source}",reason="Transfert inter-entrepôts")
        cur.execute("SELECT nextval(pg_get_serial_sequence('stock_transfers','id')) AS id")
        transfer_id=int(cur.fetchone()["id"]); number=f"TRF-{date.today():%Y%m%d}-{transfer_id:05d}"
        cur.execute("""INSERT INTO stock_transfers(id,numero,entrepot_source,emplacement_source,entrepot_destination,emplacement_destination,source_document_id,destination_document_id,motif,created_by)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",(transfer_id,number,source,source_location,destination,destination_location,src_doc,dst_doc,str(data.get("motif") or "")[:500],user.get("id")))
        for lot,qty in normalized:
            item={"id":lot["item_id"],"reference":lot["reference"],"designation":lot["designation"],"unite":lot["unite_article"]}
            cur.execute("UPDATE stock_lots SET quantite_actuelle=quantite_actuelle-%s WHERE id=%s",(qty,lot["id"]))
            _insert_stock_line(cur,user,src_doc,item,lot,qty,direction="Sortie")
            cur.execute("""INSERT INTO stock_lots(item_id,numero_lot,date_peremption,quantite_initiale,quantite_actuelle,prix_unitaire,prix_achat,prix_vente,marge_vente,unite_stock,quantite_par_unite,emplacement,entrepot,fournisseur_origine_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (lot["item_id"],lot["numero_lot"],lot["date_peremption"],qty,qty,lot["prix_unitaire"],lot["prix_achat"],lot["prix_vente"],lot["marge_vente"],lot["unite_stock"],lot["quantite_par_unite"],destination_location or lot["emplacement"],destination,lot.get("fournisseur_origine_id")))
            dest_lot_id=int(cur.fetchone()["id"]); dest_lot={**lot,"id":dest_lot_id,"quantite_actuelle":qty,"emplacement":destination_location or lot["emplacement"],"entrepot":destination}
            _insert_stock_line(cur,user,dst_doc,item,dest_lot,qty,direction="Entrée",packages=(qty/Decimal(str(lot["quantite_par_unite"] or 1))),unit=lot["unite_stock"],per_unit=Decimal(str(lot["quantite_par_unite"] or 1)),purchase_price=Decimal(str(lot["prix_achat"] or 0)),sale_price=Decimal(str(lot["prix_vente"] or 0)),margin=Decimal(str(lot["marge_vente"] or 0)),warehouse=destination,location=destination_location or lot["emplacement"])
            cur.execute("UPDATE stock_document_lines SET prix_unitaire=%s,prix_achat=%s,prix_vente=%s WHERE document_id=%s AND lot_id=%s",(lot["prix_unitaire"],lot["prix_achat"],lot["prix_vente"],dst_doc,dest_lot_id))
            cur.execute("INSERT INTO stock_transfer_lines(transfer_id,source_lot_id,destination_lot_id,item_id,quantite,numero_lot) VALUES(%s,%s,%s,%s,%s,%s)",(transfer_id,lot["id"],dest_lot_id,lot["item_id"],qty,lot["numero_lot"]))
    track_action(request,"STOCK_TRANSFER",f"Transfert {number} · {source} vers {destination} · {len(normalized)} lot(s)")
    return {"success":True,"numero":number,"bon_sortie":src_num,"bon_entree":dst_num}


@procurement_router.post("/inventory-counts")
async def stock_inventory_count_create(request: Request):
    user=_require(request,True); data=await request.json()
    warehouse=str(data.get("entrepot") or "").strip(); rows=data.get("lignes") or []
    if not warehouse or not isinstance(rows,list) or not rows:
        raise HTTPException(status_code=400,detail="Choisis un entrepôt et saisis au moins un lot compté.")
    with get_db_cursor() as cur:
        cur.execute("SELECT 1 FROM stock_warehouses WHERE nom=%s AND actif=TRUE",(warehouse,))
        if not cur.fetchone(): raise HTTPException(status_code=400,detail="Entrepôt inactif ou introuvable.")
        prepared=[]; seen=set()
        for idx,raw in enumerate(rows,1):
            try:
                lot_id=int(raw.get("lot_id")); counted=Decimal(str(raw.get("quantite_comptee") or "").replace(",","."))
            except (TypeError,ValueError,InvalidOperation) as exc: raise HTTPException(status_code=400,detail=f"Quantité comptée invalide à la ligne {idx}.") from exc
            if lot_id in seen or not counted.is_finite() or counted<0: raise HTTPException(status_code=400,detail=f"Quantité comptée invalide à la ligne {idx}.")
            seen.add(lot_id)
            cur.execute("""SELECT l.*,i.reference,i.designation,i.unite AS unite_article FROM stock_lots l JOIN stock_items i ON i.id=l.item_id WHERE l.id=%s FOR UPDATE OF l""",(lot_id,))
            lot=cur.fetchone()
            if not lot or lot["entrepot"]!=warehouse: raise HTTPException(status_code=400,detail=f"Lot de la ligne {idx} absent de l’entrepôt sélectionné.")
            expected=Decimal(str(lot["quantite_actuelle"])); delta=counted-expected
            prepared.append((dict(lot),expected,counted,delta))
        cur.execute("SELECT nextval(pg_get_serial_sequence('stock_inventory_counts','id')) AS id")
        count_id=int(cur.fetchone()["id"]); number=f"INV-{date.today():%Y%m%d}-{count_id:05d}"
        cur.execute("""INSERT INTO stock_inventory_counts(id,numero,entrepot,notes,created_by,validated_at)
            VALUES(%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)""",(count_id,number,warehouse,str(data.get("notes") or "")[:2000],user.get("id")))
        positive=[row for row in prepared if row[3]>0]; negative=[row for row in prepared if row[3]<0]
        in_doc=in_num=out_doc=out_num=None
        if positive: in_doc,in_num=_create_stock_document(cur,user,"Entrée",service=f"Ajustement inventaire · {warehouse}",reason=f"Écart positif inventaire {number}")
        if negative: out_doc,out_num=_create_stock_document(cur,user,"Sortie",service=f"Ajustement inventaire · {warehouse}",reason=f"Écart négatif inventaire {number}")
        for lot,expected,counted,delta in prepared:
            cur.execute("""INSERT INTO stock_inventory_count_lines(count_id,lot_id,item_id,numero_lot,quantite_theorique,quantite_comptee,ecart)
                VALUES(%s,%s,%s,%s,%s,%s,%s)""",(count_id,lot["id"],lot["item_id"],lot["numero_lot"],expected,counted,delta))
            if delta==0: continue
            item={"id":lot["item_id"],"reference":lot["reference"],"designation":lot["designation"],"unite":lot["unite_article"]}
            if delta>0:
                cur.execute("UPDATE stock_lots SET quantite_initiale=quantite_initiale+%s,quantite_actuelle=%s WHERE id=%s",(delta,counted,lot["id"]))
                _insert_stock_line(cur,user,in_doc,item,lot,delta,direction="Entrée",packages=delta,unit=lot["unite_stock"] or lot["unite_article"],
                    per_unit=Decimal("1"),purchase_price=Decimal(str(lot["prix_unitaire"] or 0)),sale_price=Decimal(str(lot["prix_vente"] or 0)),
                    margin=Decimal(str(lot["marge_vente"] or 0)),warehouse=warehouse,location=lot["emplacement"] or "")
            else:
                correction=abs(delta)
                cur.execute("UPDATE stock_lots SET quantite_actuelle=%s WHERE id=%s",(counted,lot["id"]))
                _insert_stock_line(cur,user,out_doc,item,lot,correction,direction="Sortie")
        cur.execute("UPDATE stock_inventory_counts SET adjustment_in_document_id=%s,adjustment_out_document_id=%s WHERE id=%s",(in_doc,out_doc,count_id))
    track_action(request,"STOCK_INVENTORY_COUNT",f"Inventaire {number} validé · {warehouse} · {len(prepared)} lot(s), {sum(1 for row in prepared if row[3])} écart(s)")
    return {"success":True,"numero":number,"ecarts":sum(1 for row in prepared if row[3]),"bon_entree":in_num,"bon_sortie":out_num}


@procurement_router.get("/reports")
def stock_procurement_report(request: Request):
    user=_require(request)
    start=_date(request.query_params.get("date_debut"),"début",True)
    end=_date(request.query_params.get("date_fin"),"fin",True)
    if start>end: raise HTTPException(status_code=400,detail="La date de début doit précéder la date de fin.")
    supplier_id=int(request.query_params.get("supplier_id") or 0) or None
    item_id=int(request.query_params.get("item_id") or 0) or None
    warehouse=str(request.query_params.get("entrepot") or "").strip()
    with get_db_cursor() as cur:
        cur.execute("""SELECT COALESCE(SUM(ol.quantite*ol.prix_unitaire),0) AS total,
                COUNT(DISTINCT o.id) AS commandes
            FROM stock_supplier_orders o LEFT JOIN stock_supplier_order_lines ol ON ol.order_id=o.id
            WHERE o.date_commande BETWEEN %s AND %s AND (%s::int IS NULL OR o.supplier_id=%s)""",
            (start,end,supplier_id,supplier_id))
        orders=dict(cur.fetchone())
        cur.execute("""SELECT COALESCE(SUM(CASE WHEN COALESCE(l.quantite_unite_stock,0)>0 AND l.prix_achat IS NOT NULL
                    THEN l.quantite_unite_stock*l.prix_achat
                    ELSE l.quantite*COALESCE(l.prix_unitaire,l.prix_achat/NULLIF(l.quantite_par_unite,0),0) END),0) AS total,
                COUNT(DISTINCT d.id) AS receptions
            FROM stock_documents d JOIN stock_document_lines l ON l.document_id=d.id
            WHERE d.type_document='Entrée' AND d.statut<>'Annulé' AND d.supplier_order_id IS NOT NULL
                AND d.created_at::date BETWEEN %s AND %s
                AND (%s::int IS NULL OR d.fournisseur_id=%s) AND (%s::int IS NULL OR l.item_id=%s)
                AND (%s='' OR l.entrepot=%s)""",
            (start,end,supplier_id,supplier_id,item_id,item_id,warehouse,warehouse))
        receipts=dict(cur.fetchone())
        cur.execute("""SELECT COALESCE(SUM(m.montant),0) AS total,COUNT(*) AS nombre
            FROM stock_supplier_payments m JOIN stock_supplier_invoices f ON f.id=m.invoice_id
            WHERE m.date_paiement BETWEEN %s AND %s AND (%s::int IS NULL OR f.supplier_id=%s)""",
            (start,end,supplier_id,supplier_id))
        payments=dict(cur.fetchone())
        cur.execute("""SELECT COALESCE(SUM(r.montant_avoir),0) AS total,COUNT(*) AS nombre
            FROM stock_supplier_returns r WHERE r.created_at::date BETWEEN %s AND %s AND (%s::int IS NULL OR r.supplier_id=%s)""",
            (start,end,supplier_id,supplier_id))
        returns=dict(cur.fetchone())
        cur.execute("""SELECT i.id,i.designation,i.reference,i.unite,
                COALESCE(SUM(m.quantite),0) AS quantite,
                COALESCE(SUM(m.quantite*COALESCE(l.prix_unitaire,l.prix_achat/NULLIF(l.quantite_par_unite,0),0)),0) AS valeur
            FROM stock_movements m JOIN stock_documents d ON d.id=m.document_id
            JOIN stock_items i ON i.id=m.item_id JOIN stock_lots l ON l.id=m.lot_id
            WHERE m.sens='Sortie' AND d.type_document='Sortie' AND d.statut<>'Annulé'
                AND d.created_at::date BETWEEN %s AND %s
                AND d.service_destination NOT LIKE 'Transfert %%' AND d.service_destination NOT LIKE 'Retour fournisseur%%'
                AND d.service_destination NOT LIKE 'Ajustement inventaire%%'
                AND (%s::int IS NULL OR i.id=%s) AND (%s='' OR l.entrepot=%s)
            GROUP BY i.id ORDER BY valeur DESC,i.designation""",
            (start,end,item_id,item_id,warehouse,warehouse))
        consumption=[dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT COALESCE(NULLIF(BTRIM(d.service_destination),''),'Service non précisé') AS service,
                i.id,i.designation,i.reference,i.unite,COALESCE(SUM(m.quantite),0) AS quantite,
                COALESCE(SUM(m.quantite*COALESCE(l.prix_unitaire,l.prix_achat/NULLIF(l.quantite_par_unite,0),0)),0) AS valeur
            FROM stock_movements m JOIN stock_documents d ON d.id=m.document_id
            JOIN stock_items i ON i.id=m.item_id JOIN stock_lots l ON l.id=m.lot_id
            WHERE m.sens='Sortie' AND d.type_document='Sortie' AND d.statut<>'Annulé'
                AND d.created_at::date BETWEEN %s AND %s
                AND d.service_destination NOT LIKE 'Transfert %%' AND d.service_destination NOT LIKE 'Retour fournisseur%%'
                AND d.service_destination NOT LIKE 'Ajustement inventaire%%'
                AND (%s::int IS NULL OR i.id=%s) AND (%s='' OR l.entrepot=%s)
            GROUP BY service,i.id ORDER BY service,i.designation""",
            (start,end,item_id,item_id,warehouse,warehouse))
        consumption_services=[dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT s.id,s.designation,COALESCE(SUM(f.montant),0) AS facture,
                COALESCE(SUM(p.paye),0) AS paye,COALESCE(SUM(r.avoirs),0) AS avoirs,
                GREATEST(COALESCE(SUM(f.montant-p.paye-r.avoirs),0),0) AS solde
            FROM stock_suppliers s LEFT JOIN stock_supplier_invoices f ON f.supplier_id=s.id
            LEFT JOIN (SELECT invoice_id,SUM(montant) AS paye FROM stock_supplier_payments GROUP BY invoice_id) p ON p.invoice_id=f.id
            LEFT JOIN (SELECT invoice_id,SUM(montant_avoir) AS avoirs FROM stock_supplier_returns WHERE invoice_id IS NOT NULL GROUP BY invoice_id) r ON r.invoice_id=f.id
            WHERE (%s::int IS NULL OR s.id=%s) GROUP BY s.id ORDER BY solde DESC,s.designation""",(supplier_id,supplier_id))
        supplier_balances=[dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT l.entrepot,COUNT(DISTINCT i.id) AS produits,
                COALESCE(SUM(l.quantite_actuelle*COALESCE(l.prix_unitaire,l.prix_achat/NULLIF(l.quantite_par_unite,0),0)),0) AS valeur,
                COUNT(*) FILTER (WHERE l.date_peremption<CURRENT_DATE AND l.quantite_actuelle>0) AS lots_perimes,
                COALESCE(SUM(l.quantite_actuelle) FILTER (WHERE l.date_peremption<CURRENT_DATE),0) AS quantite_perimee
            FROM stock_lots l JOIN stock_items i ON i.id=l.item_id WHERE (%s='' OR l.entrepot=%s)
            GROUP BY l.entrepot ORDER BY l.entrepot""",(warehouse,warehouse))
        warehouse_stats=[dict(row) for row in cur.fetchall()]
    return {"date_debut":start.isoformat(),"date_fin":end.isoformat(),"commandes":orders,"receptions":receipts,
            "paiements":payments,"avoirs":returns,"consommation":consumption,"consommation_services":consumption_services,
            "soldes_fournisseurs":supplier_balances,"entrepots":warehouse_stats}


@procurement_router.get("/lot-trace/{lot_id}")
def stock_lot_trace(lot_id: int, request: Request):
    _require(request)
    with get_db_cursor() as cur:
        cur.execute("""SELECT l.id,l.numero_lot,l.date_peremption,l.quantite_initiale,l.quantite_actuelle,l.entrepot,l.emplacement,
                i.designation,i.reference FROM stock_lots l JOIN stock_items i ON i.id=l.item_id WHERE l.id=%s""",(lot_id,))
        lot=cur.fetchone()
        if not lot: raise HTTPException(status_code=404,detail="Lot introuvable.")
        cur.execute("""SELECT m.sens,m.quantite,m.utilisateur,m.created_at,d.numero,d.type_document,d.motif,
                d.fournisseur,COALESCE(ret.numero,'') AS numero_retour,COALESCE(tr.numero,'') AS numero_transfert,
                COALESCE(co.numero,'') AS commande
            FROM stock_movements m JOIN stock_documents d ON d.id=m.document_id
            LEFT JOIN stock_supplier_returns ret ON ret.document_id=d.id
            LEFT JOIN stock_transfers tr ON tr.source_document_id=d.id OR tr.destination_document_id=d.id
            LEFT JOIN stock_supplier_orders co ON co.id=d.supplier_order_id
            WHERE m.lot_id=%s ORDER BY m.created_at,m.id""",(lot_id,))
        movements=[dict(row) for row in cur.fetchall()]
    return {"lot":dict(lot),"mouvements":movements}


PROCUREMENT_ADVANCED_SCRIPT = r'''<script>
(()=>{
 const $=id=>document.getElementById(id), esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])), money=v=>`${Number(v||0).toLocaleString('fr-FR',{minimumFractionDigits:2,maximumFractionDigits:2})} DA`, today=new Date().toISOString().slice(0,10);
 const state={data:{},tab:'orders'};
 const api=async(path,opt={})=>{const r=await fetch('/api/approvisionnement'+path,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(opt.headers||{})},...opt}),b=await r.json().catch(()=>({}));if(!r.ok)throw Error(b.detail||('Erreur HTTP '+r.status+': '+(r.statusText||'réponse invalide')));return b};
 const notify=msg=>{const t=$('procToast');t.textContent=msg;t.classList.remove('hidden');setTimeout(()=>t.classList.add('hidden'),4500)};
 const table=(headers,rows,empty)=>`<div class="overflow-x-auto rounded-2xl border border-slate-100"><table class="w-full min-w-[760px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600"><tr>${headers.map(h=>`<th class="px-3 py-3">${h}</th>`).join('')}</tr></thead><tbody>${rows||`<tr><td colspan="${headers.length}" class="p-8 text-center text-slate-400">${empty}</td></tr>`}</tbody></table></div>`;
 const control=(label,id,type='text',attrs='')=>`<label class="block text-xs font-bold text-slate-600">${label}<input id="${id}" type="${type}" ${attrs} class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm"></label>`;
 const select=(label,id,options,required=true)=>`<label class="block text-xs font-bold text-slate-600">${label}<select id="${id}" ${required?'required':''} class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm"><option value="">Choisir…</option>${options}</select></label>`;
 const modal=(title,fields,submit)=>{ $('procModalTitle').textContent=title;$('procFields').innerHTML=fields;$('procForm').onsubmit=async e=>{e.preventDefault();const btn=$('procForm').querySelector('[type=submit],button:not([type])');if(btn)btn.disabled=true;try{const result=await submit();$('procModal').classList.add('hidden');$('procModal').classList.remove('flex');notify(result?.numero?`Enregistré : ${result.numero}`:'Opération enregistrée.');await show(state.tab)}catch(error){notify(error.message)}finally{if(btn)btn.disabled=false}};$('procModal').classList.remove('hidden');$('procModal').classList.add('flex')};
 const getData=async()=>{state.data=await api('/data');return state.data};
 const setActive=name=>document.querySelectorAll('.proc-tab').forEach(b=>b.className='proc-tab rounded-xl px-4 py-2.5 text-xs font-bold '+(b.dataset.procTab===name?'bg-brand-neon':'text-slate-500 hover:bg-slate-100'));
 const warehouseOptions=()=>((state.data.warehouses||[]).map(x=>`<option>${esc(x)}</option>`).join(''));
 const locationOptions=()=>((state.data.locations||[]).map(x=>`<option>${esc(x)}</option>`).join(''));
 const runReceive=order=>{const lines=(order.lignes_detail||[]).filter(l=>Number(l.quantite)>Number(l.quantite_recue));if(!lines.length){notify('La commande est déjà entièrement réceptionnée.');return}const blocks=lines.map((l,i)=>{const rest=Math.max(0,Number(l.quantite)-Number(l.quantite_recue));return `<fieldset class="rounded-2xl border border-slate-200 p-3"><legend class="px-2 text-sm font-extrabold">${esc(l.designation)} · reste ${rest} ${esc(l.unite||'')}</legend><input type="hidden" class="recv-id" value="${l.id}"><div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">${control('Quantité reçue','recv_qty_'+i,'number',`min="0.001" max="${rest}" step="0.001" value="${rest}" required`)}${control('N° lot','recv_lot_'+i,'text','required maxlength="120"')}${control('Date de péremption','recv_exp_'+i,'date',l.suivi_peremption?'required':'')}<label class="block text-xs font-bold text-slate-600">Unité de stock<select id="recv_unit_${i}" class="mt-1.5 w-full rounded-xl border px-3 py-2.5">${(state.data.items||[]).find(x=>Number(x.id)===Number(l.item_id))?`<option>${esc(l.unite_article||l.unite||'unité')}</option>`:''}${(state.data.items||[]).map(x=>`<option>${esc(x.unite)}</option>`).join('')}</select></label>${control('Quantité par unité','recv_per_'+i,'number','min="0.001" step="0.001" value="1" required')}${control('Prix achat par unité commandée (DA)','recv_price_'+i,'number',`min="0" step="0.01" value="${Number(l.prix_unitaire||0)}" required`)}${control('Marge (%)','recv_margin_'+i,'number','min="0" step="0.01" value="15" required')}<label class="block text-xs font-bold text-slate-600">Entrepôt<select id="recv_wh_${i}" required class="mt-1.5 w-full rounded-xl border px-3 py-2.5">${warehouseOptions()}</select></label><label class="block text-xs font-bold text-slate-600">Emplacement<select id="recv_loc_${i}" required class="mt-1.5 w-full rounded-xl border px-3 py-2.5">${locationOptions()}</select></label></div></fieldset>`}).join('');modal(`Réception partielle · ${esc(order.numero)}`,`<p class="rounded-xl bg-sky-50 p-3 text-xs text-sky-800">Chaque réception crée un bon d’entrée, un lot et un mouvement. Tu peux réceptionner le solde en plusieurs fois.</p><div class="max-h-[58vh] space-y-3 overflow-y-auto">${blocks}</div>${control('N° du bon de livraison fournisseur','recv_delivery_note','text')}<label class="block text-xs font-bold text-slate-600">Bon de livraison fournisseur (PDF, JPG ou PNG)<input id="recvDeliveryFile" type="file" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png" class="mt-2 block w-full rounded-xl border p-3 text-sm"></label>${control('Notes de réception','recv_notes')}`,async()=>{const rows=lines.map((l,i)=>({order_line_id:l.id,quantite:$('recv_qty_'+i).value,numero_lot:$('recv_lot_'+i).value,date_peremption:$('recv_exp_'+i).value,unite_stock:$('recv_unit_'+i).value,quantite_par_unite:$('recv_per_'+i).value,prix_achat:$('recv_price_'+i).value,marge_vente:$('recv_margin_'+i).value,entrepot:$('recv_wh_'+i).value,emplacement:$('recv_loc_'+i).value}));const payload={lignes:rows,bon_livraison:$('recv_delivery_note').value,notes:$('recv_notes').value};const deliveryFile=$('recvDeliveryFile').files?.[0];if(deliveryFile){if(deliveryFile.size>12*1024*1024)throw Error('Le bon de livraison dépasse 12 Mo.');payload.bon_livraison_fichier=await window.procFileToDataUrl(deliveryFile)}return api(`/orders/${order.id}/receive`,{method:'POST',body:JSON.stringify(payload)})})};
 const orderForm=()=>window.procOpenOrder();
 const renderOrders=()=>{const d=state.data;const rows=(d.orders||[]).map(o=>{const remain=(o.lignes_detail||[]).reduce((a,l)=>a+Math.max(0,Number(l.quantite)-Number(l.quantite_recue)),0);return `<tr class="border-t"><td class="p-3 font-bold">${esc(o.numero)}<small class="block text-slate-400">${Number(o.quantite_recue||0)} / ${Number(o.quantite_commandee||0)} unité(s) reçue(s)</small></td><td class="p-3">${esc(o.fournisseur)}</td><td class="p-3">${esc(o.date_commande)}</td><td class="p-3">${esc(o.date_prevue||'—')}</td><td class="p-3">${o.lignes} ligne(s)</td><td class="p-3 font-bold">${money(o.montant)}</td><td class="p-3"><span class="rounded-full bg-sky-50 px-2 py-1 text-[10px] font-bold text-sky-800">${esc(o.statut)}</span></td><td class="p-3">${remain>0&&o.statut!=='Annulée'?`<button onclick="procReceive(${o.id})" class="rounded-lg bg-emerald-50 px-3 py-2 text-xs font-bold text-emerald-800">Réceptionner</button>`:'—'}</td></tr>`}).join('');$('procContent').innerHTML=`<div class="mb-4 flex items-center justify-between"><div><h2 class="font-extrabold">Commandes d’achat</h2><p class="text-xs text-slate-500">Réception partielle ou complète, article et lot par lot.</p></div><button onclick="procOpenOrder()" class="rounded-xl bg-brand-neon px-3 py-2 text-xs font-bold">＋ Nouvelle commande</button></div>${table(['Commande / réception','Fournisseur','Date','Livraison prévue','Articles','Montant prévu','État','Action'],rows,'Aucune commande.')}`};
 const renderReceipts=()=>{const rows=(state.data.receipts||[]).map(r=>`<tr class="border-t"><td class="p-3 font-bold">${esc(r.numero)}${r.commande?`<small class="block text-slate-400">${esc(r.commande)}</small>`:''}</td><td class="p-3">${esc(r.fournisseur)}</td><td class="p-3">${esc(String(r.created_at||'').slice(0,16))}</td><td class="p-3 font-bold">${money(r.montant)}</td><td class="p-3">${r.facture_liee?'<span class="text-emerald-700">Facture liée</span>':`<button onclick="procOpenInvoice(${r.id})" class="rounded-lg bg-sky-50 px-3 py-2 text-xs font-bold text-sky-800">Créer la créance</button>`}</td></tr>`).join('');$('procContent').innerHTML=`<h2 class="mb-1 font-extrabold">Réceptions de stock</h2><p class="mb-4 text-xs text-slate-500">Historique des réceptions et rapprochement avec la facture du fournisseur.</p>${table(['Bon / commande','Fournisseur','Date','Valeur reçue','Facture'],rows,'Aucune réception fournisseur.')}`};
 const renderProformas=()=>{const rows=(state.data.proformas||[]).map(p=>{const statuses=['À envoyer','Envoyée','Reçue','Acceptée','Refusée','Convertie'];return `<tr class="border-t"><td class="p-3 font-extrabold">${esc(p.numero)}<small class="block text-slate-400">${p.lignes} article(s)</small></td><td class="p-3">${esc(p.fournisseur)}</td><td class="p-3">${esc(p.date_demande)}</td><td class="p-3">${esc(p.date_limite||'—')}</td><td class="p-3 font-bold">${money(p.montant_propose)}</td><td class="p-3"><select ${p.statut==='Convertie'?'disabled':''} onchange="procProformaStatus(${p.id},this.value)" class="rounded-lg border px-2 py-1 text-xs">${statuses.map(s=>`<option ${p.statut===s?'selected':''}>${s}</option>`).join('')}</select></td><td class="p-3"><div class="flex flex-wrap gap-1">${p.statut!=='Convertie'?`<button onclick="procProformaResponse(${p.id})" class="rounded-lg bg-sky-50 px-2 py-1.5 text-[11px] font-bold text-sky-800">Saisir réponse</button>`:''}${p.statut==='Acceptée'?`<button onclick="procProformaConvert(${p.id})" class="rounded-lg bg-emerald-50 px-2 py-1.5 text-[11px] font-bold text-emerald-800">Créer commande</button>`:''}${p.commande_convertie?`<span class="rounded-lg bg-emerald-50 px-2 py-1.5 text-[11px] font-bold text-emerald-800">${esc(p.commande_convertie)}</span>`:''}<button onclick="procPrintProforma(${p.id})" class="rounded-lg border px-2 py-1.5 text-[11px] font-bold"><i class="ph-bold ph-file-pdf mr-1"></i>Voir PDF</button></div></td></tr>`}).join('');$('procContent').innerHTML=`<div class="mb-4 flex items-center justify-between"><div><h2 class="font-extrabold">Demandes de proforma</h2><p class="text-xs text-slate-500">Prépare les demandes par fournisseur, suis les réponses reçues et transforme une offre acceptée en commande d’achat.</p></div><button onclick="procOpenProforma()" class="rounded-xl bg-brand-neon px-3 py-2 text-xs font-extrabold">＋ Nouvelle demande</button></div>${table(['Référence','Fournisseur','Demande','Réponse attendue','Montant reçu','État','Actions'],rows,'Aucune demande de proforma.')}`};
 const renderCounts=()=>{const rows=(state.data.inventory_counts||[]).map(x=>`<tr class="border-t"><td class="p-3 font-bold">${esc(x.numero)}</td><td class="p-3">${esc(x.entrepot)}</td><td class="p-3">${esc(String(x.created_at||'').slice(0,16))}</td><td class="p-3">${x.lots}</td><td class="p-3 font-bold ${Number(x.ecarts)?'text-amber-700':'text-emerald-700'}">${x.ecarts} écart(s) · ${Number(x.quantite_ecart).toLocaleString('fr-FR')}</td><td class="p-3">${esc(x.statut)}</td></tr>`).join('');$('procContent').innerHTML=`<div class="mb-4 flex items-center justify-between"><div><h2 class="font-extrabold">Inventaires physiques</h2><p class="text-xs text-slate-500">Les écarts génèrent automatiquement des mouvements d’ajustement traçables.</p></div><button onclick="procOpenCount()" class="rounded-xl bg-brand-neon px-3 py-2 text-xs font-bold">＋ Démarrer un inventaire</button></div>${table(['N° inventaire','Entrepôt','Date','Lots comptés','Écarts','État'],rows,'Aucun inventaire validé.')}`};
 const renderTransfers=()=>{const rows=(state.data.transfers||[]).map(x=>`<tr class="border-t"><td class="p-3 font-bold">${esc(x.numero)}</td><td class="p-3">${esc(x.entrepot_source)} → ${esc(x.entrepot_destination)}</td><td class="p-3">${x.lignes} lot(s)</td><td class="p-3">${Number(x.quantite).toLocaleString('fr-FR')}</td><td class="p-3">${esc(String(x.created_at||'').slice(0,16))}</td></tr>`).join('');$('procContent').innerHTML=`<div class="mb-4 flex items-center justify-between"><div><h2 class="font-extrabold">Transferts entre entrepôts</h2><p class="text-xs text-slate-500">Les lots gardent leur numéro et leur date de péremption. Chaque transfert crée les deux mouvements correspondants.</p></div><button onclick="procOpenTransfer()" class="rounded-xl bg-brand-neon px-3 py-2 text-xs font-bold">＋ Nouveau transfert</button></div>${table(['Référence','Trajet','Lots','Quantité','Date'],rows,'Aucun transfert enregistré.')}`};
 const renderReturns=()=>{const rows=(state.data.returns||[]).map(x=>`<tr class="border-t"><td class="p-3 font-bold">${esc(x.numero)}</td><td class="p-3">${esc(x.fournisseur)}</td><td class="p-3">${esc(x.numero_facture||'Avoir non imputé')}</td><td class="p-3">${esc(x.bon_retour)}</td><td class="p-3 font-bold text-amber-700">${money(x.montant_avoir)}</td><td class="p-3">${esc(String(x.created_at||'').slice(0,16))}</td></tr>`).join('');$('procContent').innerHTML=`<div class="mb-4 flex items-center justify-between"><div><h2 class="font-extrabold">Retours et avoirs fournisseur</h2><p class="text-xs text-slate-500">Le stock est diminué par lot et l’avoir rattaché réduit le solde de la facture.</p></div><button onclick="procOpenReturn()" class="rounded-xl bg-brand-neon px-3 py-2 text-xs font-bold">＋ Enregistrer un retour</button></div>${table(['Avoir','Fournisseur','Facture','Bon retour','Montant','Date'],rows,'Aucun retour enregistré.')}`};
 const renderAlerts=()=>{const rows=(state.data.restock_alerts||[]).map(x=>`<tr class="border-t"><td class="p-3 font-bold">${esc(x.designation)}<small class="block text-slate-400">${esc(x.reference||'')}</small></td><td class="p-3">${Number(x.dispo).toLocaleString('fr-FR')} ${esc(x.unite)}</td><td class="p-3">${Number(x.seuil_alerte).toLocaleString('fr-FR')} ${esc(x.unite)}</td><td class="p-3">${Number(x.en_commande).toLocaleString('fr-FR')}</td><td class="p-3 font-extrabold text-amber-700">${Number(x.quantite_suggeree).toLocaleString('fr-FR')} ${esc(x.unite)}</td></tr>`).join('');$('procContent').innerHTML=`<h2 class="mb-1 font-extrabold">Suggestions de réapprovisionnement</h2><p class="mb-4 text-xs text-slate-500">Seuil cible : deux fois le seuil d’alerte, en tenant compte des commandes ouvertes. Vérifie les suggestions avant de créer une commande.</p>${table(['Article','Stock utilisable','Seuil','Déjà commandé','Quantité suggérée'],rows,'Aucun article en alerte.')}`};
 const renderTrace=()=>{const opts=(state.data.countable_lots||[]).map(l=>`<option value="${l.lot_id}">${esc(l.designation)} · lot ${esc(l.numero_lot)} · ${esc(l.entrepot)}</option>`).join('');$('procContent').innerHTML=`<h2 class="mb-1 font-extrabold">Traçabilité par lot</h2><p class="mb-4 text-xs text-slate-500">Réceptions, sorties, transferts, retours et inventaires conservés dans l’historique.</p><div class="mb-4 flex max-w-3xl flex-wrap items-end gap-3">${select('Lot à consulter','traceLot',opts)}<button onclick="procShowTrace()" class="rounded-xl bg-brand-neon px-4 py-2.5 text-xs font-extrabold">Afficher la traçabilité</button></div><div id="procTraceResult"></div>`};
 const renderReports=()=>{$('procContent').innerHTML=`<div class="mb-4"><h2 class="font-extrabold">Rapports de stock et fournisseurs</h2><p class="text-xs text-slate-500">Achats, réceptions, règlements, avoirs, consommation, soldes et état des entrepôts.</p></div><div class="mb-4 grid gap-3 rounded-2xl bg-slate-50 p-4 sm:grid-cols-2 lg:grid-cols-4">${control('Date début','rStart','date',`value="${new Date(Date.now()-30*864e5).toISOString().slice(0,10)}"`)}${control('Date fin','rEnd','date',`value="${today}"`)}${select('Fournisseur','rSupplier','<option value="">Tous</option>'+(state.data.suppliers||[]).map(s=>`<option value="${s.id}">${esc(s.designation)}</option>`).join(''),false)}${select('Entrepôt','rWarehouse','<option value="">Tous</option>'+(state.data.warehouses||[]).map(x=>`<option>${esc(x)}</option>`).join(''),false)}<label class="block text-xs font-bold text-slate-600 sm:col-span-2">Produit<select id="rItem" class="mt-1.5 w-full rounded-xl border px-3 py-2.5"><option value="">Tous les produits</option>${(state.data.items||[]).map(x=>`<option value="${x.id}">${esc(x.designation)}</option>`).join('')}</select></label><button onclick="procRunReport()" class="self-end rounded-xl bg-brand-neon px-4 py-2.5 text-xs font-extrabold">Afficher le rapport</button></div><div id="procReportResult" class="space-y-4"><p class="p-8 text-center text-sm text-slate-400">Choisis la période puis affiche le rapport.</p></div>`};
 const render=()=>{setActive(state.tab);const d=state.data;if(state.tab==='orders')renderOrders();else if(state.tab==='receipts')renderReceipts();else if(state.tab==='invoices'||state.tab==='payments'||state.tab==='suppliers')baseTab(state.tab);else if(state.tab==='proformas')renderProformas();else if(state.tab==='counts')renderCounts();else if(state.tab==='transfers')renderTransfers();else if(state.tab==='returns')renderReturns();else if(state.tab==='alerts')renderAlerts();else if(state.tab==='trace')renderTrace();else renderReports();if(d.summary){$('procTotal').textContent=money(d.summary.total_facture);$('procPaid').textContent=money(d.summary.total_paye);$('procBalance').textContent=money(d.summary.reste);$('procLate').textContent=d.summary.retards||0}};
 const show=async name=>{state.tab=name;try{await getData();render()}catch(e){notify(e.message)}};
 const baseTab=window.procTab;window.procTab=name=>{if(['orders','receipts','invoices','payments','suppliers','proformas','counts','transfers','returns','alerts','trace','reports'].includes(name))show(name);else baseTab(name)};
 window.procReceive=id=>{const order=(state.data.orders||[]).find(x=>Number(x.id)===Number(id));if(order)runReceive(order)};
 const addLineControls=(lotOptions)=>`<div class="grid gap-2 sm:grid-cols-12"><label class="sm:col-span-8 text-[11px] font-bold">Lot<select class="x-lot mt-1 w-full rounded-lg border p-2" required>${lotOptions}</select></label><label class="sm:col-span-3 text-[11px] font-bold">Quantité<input class="x-qty mt-1 w-full rounded-lg border p-2" type="number" min="0.001" step="0.001" required></label><button type="button" onclick="this.parentElement.remove()" class="text-rose-700">Retirer</button></div>`;
 window.procOpenProforma=()=>{let lines=[];const draw=()=>{const target=$('pfLines');if(!target)return;const rows=lines.map((line,i)=>`<tr class="border-t"><td class="px-3 py-2">${line.is_custom?`<div class="grid gap-2 sm:grid-cols-3"><input aria-label="Désignation article libre ${i+1}" value="${esc(line.designation||'')}" placeholder="Désignation" oninput="procEditProformaCustom(${i},'designation',this.value)" class="min-w-[180px] rounded-lg border px-3 py-2 text-sm"><input aria-label="Référence article libre ${i+1}" value="${esc(line.reference||'')}" placeholder="Référence (facultative)" oninput="procEditProformaCustom(${i},'reference',this.value)" class="min-w-[130px] rounded-lg border px-3 py-2 text-sm"><input aria-label="Unité article libre ${i+1}" value="${esc(line.unite||'')}" placeholder="Unité" oninput="procEditProformaCustom(${i},'unite',this.value)" class="min-w-[100px] rounded-lg border px-3 py-2 text-sm"></div>`:`<select aria-label="Article ${i+1}" onchange="procPickProformaItem(${i},this.value)" class="w-full min-w-[280px] rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm"><option value="">Choisir un article…</option><option value="__custom__">＋ Article non répertorié…</option>${(state.data.items||[]).map(x=>`<option value="${x.id}" ${Number(line.item_id)===Number(x.id)?'selected':''}>${esc(x.designation)}${x.reference?' · '+esc(x.reference):''}</option>`).join('')}</select>`}</td><td class="px-3 py-2"><input aria-label="Quantité demandée ligne ${i+1}" type="number" min="0.001" step="0.001" value="${line.quantite||1}" oninput="procEditProformaLine(${i},this.value)" class="w-full min-w-[110px] rounded-lg border border-slate-200 px-3 py-2 text-sm"></td><td class="px-3 py-2 text-right"><button type="button" onclick="procRemoveProformaLine(${i})" class="rounded-lg px-3 py-2 text-xs font-bold text-rose-700 hover:bg-rose-50">Retirer</button></td></tr>`).join('');target.innerHTML=`<div class="overflow-x-auto rounded-xl border border-slate-200"><table class="w-full min-w-[620px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600"><tr><th class="px-3 py-3">Article / désignation</th><th class="px-3 py-3">Quantité</th><th class="px-3 py-3 text-right">Action</th></tr></thead><tbody>${rows||`<tr><td colspan="3" class="p-6 text-center text-xs text-slate-400">Ajoute les articles à chiffrer.</td></tr>`}</tbody></table></div>`};window.procPickProformaItem=(i,id)=>{if(id==='__custom__')lines[i]={is_custom:true,designation:'',reference:'',unite:'',quantite:lines[i].quantite||1};else{const item=(state.data.items||[]).find(x=>Number(x.id)===Number(id));lines[i]={item_id:Number(id),designation:item?.designation||'',reference:item?.reference||'',unite:item?.unite||'',quantite:lines[i].quantite||1}}draw()};window.procEditProformaLine=(i,value)=>lines[i].quantite=value;window.procEditProformaCustom=(i,key,value)=>lines[i][key]=value;window.procRemoveProformaLine=i=>{lines.splice(i,1);draw()};window.procAddProformaLine=()=>{lines.push({quantite:1});draw()};modal('Nouvelle demande de proforma',`${select('Fournisseur *','pfSupplier',(state.data.suppliers||[]).filter(s=>s.actif).map(s=>`<option value="${s.id}">${esc(s.designation)}</option>`).join(''))}<div class="grid gap-3 sm:grid-cols-2">${control('Date de demande','pfDate','date',`value="${today}" required`)}${control('Réponse souhaitée avant le','pfDeadline','date')}</div><div class="flex items-center justify-between"><b class="text-sm">Articles à chiffrer</b><button type="button" onclick="procAddProformaLine()" class="rounded-lg bg-slate-100 px-3 py-2 text-xs font-bold">＋ Ajouter un article</button></div><div id="pfLines" class="max-h-[45vh] space-y-2 overflow-y-auto"></div>${control('Notes ou conditions demandées','pfNotes')}`,async()=>{const result=await api('/proformas',{method:'POST',body:JSON.stringify({supplier_id:$('pfSupplier').value,date_demande:$('pfDate').value,date_limite:$('pfDeadline').value,notes:$('pfNotes').value,lignes:lines})});const supplier=state.data.suppliers?.find(s=>Number(s.id)===Number($('pfSupplier').value))||{};const supplierEmails=[supplier.email,supplier.email_2,supplier.email_3].filter(Boolean);setTimeout(()=>window.procViewProforma(result.id,result.numero,supplierEmails),250);return result});draw()};
 window.procProformaStatus=async(id,statut)=>{try{await api(`/proformas/${id}/status`,{method:'POST',body:JSON.stringify({statut})});await show('proformas')}catch(e){notify(e.message)}};
 window.procProformaResponse=id=>{const p=(state.data.proformas||[]).find(x=>Number(x.id)===Number(id));if(!p)return;modal(`Réponse · ${esc(p.numero)}`,`<p class="rounded-xl bg-sky-50 p-3 text-xs text-sky-800">Saisis le prix unitaire proposé pour chaque article. Le montant total sera calculé automatiquement.</p>${(p.lignes_detail||[]).map(l=>`<label class="grid grid-cols-[1fr_150px] items-center gap-3 border-b py-2 text-xs"><span><b>${esc(l.designation)}</b><small class="block text-slate-500">${Number(l.quantite).toLocaleString('fr-FR')} ${esc(l.unite)} · ${esc(l.reference||'')}</small></span><input class="pf-quote-price rounded-lg border px-2 py-2" type="number" min="0" step="0.01" data-line="${l.id}" value="${l.prix_reponse??''}" placeholder="DA / unité" required></label>`).join('')}`,async()=>api(`/proformas/${id}/response`,{method:'POST',body:JSON.stringify({lignes:[...document.querySelectorAll('.pf-quote-price')].map(x=>({ligne_id:x.dataset.line,prix_unitaire:x.value}))})}))};
 window.procProformaConvert=async id=>{const p=(state.data.proformas||[]).find(x=>Number(x.id)===Number(id));if(!p||!window.confirm(`Créer une commande d’achat pour ${p.fournisseur} à partir de la proforma ${p.numero} ?`))return;try{const result=await api(`/proformas/${id}/convert`,{method:'POST',body:'{}'});notify(`Commande créée : ${result.numero}`);await show('proformas')}catch(e){notify(e.message)}};
 window.procViewProforma=async(id,number='',emailHint=[])=>{try{const response=await fetch(`/api/approvisionnement/proformas/${id}/pdf`,{credentials:'same-origin'});if(!response.ok){const payload=await response.json().catch(()=>({}));throw Error(payload.detail||'Impossible de générer le PDF.')}const blob=await response.blob();if(window.procPdfUrl)URL.revokeObjectURL(window.procPdfUrl);window.procPdfUrl=URL.createObjectURL(blob);const p=(state.data.proformas||[]).find(x=>Number(x.id)===Number(id));const emails=p?[p.fournisseur_email,p.fournisseur_email_2,p.fournisseur_email_3].filter(Boolean):(Array.isArray(emailHint)?emailHint:[emailHint]).filter(Boolean);$('procPdfTitle').textContent=`Demande de proforma ${number}`;$('procPdfFrame').src=window.procPdfUrl;$('procPdfRecipients').innerHTML=emails.length?emails.map((email,index)=>`<label class="flex min-w-0 items-center gap-2 rounded-xl border border-emerald-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700"><input type="checkbox" class="proc-pdf-recipient h-4 w-4 accent-emerald-700" value="${esc(email)}" checked><span class="truncate">${esc(email)}</span></label>`).join(''):'<p class="text-xs text-amber-800">Aucune adresse e-mail n’est enregistrée pour ce fournisseur.</p>';$('procPdfSend').disabled=!emails.length;$('procPdfEmailPanel').classList.add('hidden');$('procPdfModal').dataset.proformaId=id;$('procPdfModal').dataset.numero=number;$('procPdfModal').classList.remove('hidden');$('procPdfModal').classList.add('flex')}catch(error){notify(error.message)}};
 window.procPrintProforma=id=>{const p=(state.data.proformas||[]).find(x=>Number(x.id)===Number(id));window.procViewProforma(id,p?.numero||'',p?[p.fournisseur_email,p.fournisseur_email_2,p.fournisseur_email_3].filter(Boolean):[])};
 window.procClosePdf=()=>{$('procPdfModal').classList.add('hidden');$('procPdfModal').classList.remove('flex');$('procPdfFrame').src='about:blank';if(window.procPdfUrl){URL.revokeObjectURL(window.procPdfUrl);window.procPdfUrl=null}};
 window.procPrintPdf=()=>{const frame=$('procPdfFrame');try{frame.contentWindow.focus();frame.contentWindow.print()}catch(_){notify('Utilise l’icône d’impression intégrée au lecteur PDF.')}};
 window.procShowPdfEmail=()=>{$('procPdfEmailPanel').classList.toggle('hidden');$('procPdfEmail').focus()};
 window.procSendProformaPdf=async()=>{const id=$('procPdfModal').dataset.proformaId,emails=[...document.querySelectorAll('.proc-pdf-recipient:checked')].map(input=>input.value);if(!emails.length)return notify('Coche au moins une adresse de destination.');const button=$('procPdfSend');button.disabled=true;button.textContent='Envoi…';try{const result=await api(`/proformas/${id}/email`,{method:'POST',body:JSON.stringify({emails})});notify(`PDF ${result.numero} envoyé à ${result.emails.join(', ')}.`);$('procPdfEmailPanel').classList.add('hidden')}catch(error){notify(error.message)}finally{button.disabled=false;button.innerHTML='<i class="ph-bold ph-paper-plane-tilt"></i> Envoyer le PDF'}};
 window.procOpenCount=()=>{const wh=warehouseOptions();modal('Inventaire physique',`${select('Entrepôt à compter','countWh',wh)}<div id="countLots" class="max-h-[56vh] space-y-2 overflow-y-auto rounded-xl border p-3 text-xs">Sélectionne un entrepôt.</div>${control('Notes','countNotes')}`,async()=>{const rows=[...document.querySelectorAll('.count-qty')].map(input=>({lot_id:input.dataset.lot,quantite_comptee:input.value}));if(!rows.length)throw Error('Aucun lot à compter dans cet entrepôt.');return api('/inventory-counts',{method:'POST',body:JSON.stringify({entrepot:$('countWh').value,lignes:rows,notes:$('countNotes').value})})});$('countWh').onchange=()=>{const lots=(state.data.countable_lots||[]).filter(l=>l.entrepot===$('countWh').value);$('countLots').innerHTML=lots.length?lots.map(l=>`<label class="grid grid-cols-[1fr_100px] items-center gap-3 border-b py-2"><span><b>${esc(l.designation)}</b> · ${esc(l.numero_lot)} <small class="text-slate-500">théorique ${Number(l.quantite_actuelle).toLocaleString('fr-FR')} ${esc(l.unite_article)}</small></span><input class="count-qty rounded-lg border px-2 py-1.5" type="number" min="0" step="0.001" data-lot="${l.lot_id}" value="${l.quantite_actuelle}"></label>`).join(''):'Aucun lot dans cet entrepôt.'}};
 window.procOpenTransfer=()=>{const lots=state.data.countable_lots||[];modal('Transfert entre entrepôts',`<div class="grid gap-3 sm:grid-cols-2">${select('Entrepôt source','trSource',warehouseOptions())}${select('Entrepôt destination','trDest',warehouseOptions())}${select('Emplacement de départ','trSourceLoc',locationOptions(),false)}${select('Emplacement d’arrivée','trDestLoc',locationOptions(),false)}</div><div class="my-3 flex items-center justify-between"><b class="text-sm">Lots à transférer</b><button type="button" onclick="procAddTransferLine()" class="rounded-lg bg-slate-100 px-3 py-2 text-xs font-bold">＋ Ajouter un lot</button></div><div id="transferLines" class="space-y-2"></div>${control('Motif','trReason')}`,async()=>{const src=$('trSource').value,dst=$('trDest').value;if(src===dst)throw Error('Les entrepôts source et destination doivent être différents.');const rows=[...document.querySelectorAll('#transferLines .transfer-line')].map(x=>({lot_id:x.querySelector('.x-lot').value,quantite:x.querySelector('.x-qty').value}));return api('/transfers',{method:'POST',body:JSON.stringify({entrepot_source:src,entrepot_destination:dst,emplacement_source:$('trSourceLoc').value,emplacement_destination:$('trDestLoc').value,lignes:rows,motif:$('trReason').value})})});window.procAddTransferLine=()=>{const block=document.createElement('div');block.className='transfer-line rounded-xl bg-slate-50 p-2';block.innerHTML=addLineControls(lots.filter(l=>l.entrepot===$('trSource').value).map(l=>`<option value="${l.lot_id}" data-stock="${l.quantite_actuelle}">${esc(l.designation)} · ${esc(l.numero_lot)} · ${Number(l.quantite_actuelle).toLocaleString('fr-FR')}</option>`).join(''));$('transferLines').appendChild(block)};$('trSource').onchange=()=>{$('transferLines').innerHTML=''}};
 window.procOpenReturn=()=>{const suppliers=state.data.suppliers||[],invoices=state.data.invoices||[];modal('Retourner des articles au fournisseur',`${select('Fournisseur','retSupplier',suppliers.map(s=>`<option value="${s.id}">${esc(s.designation)}</option>`).join(''))}${select('Imputer l’avoir sur la facture (facultatif)','retInvoice','',false)}<div class="my-3 flex items-center justify-between"><b class="text-sm">Lots à retourner</b><button type="button" onclick="procAddReturnLine()" class="rounded-lg bg-slate-100 px-3 py-2 text-xs font-bold">＋ Ajouter un lot</button></div><div id="returnLines" class="space-y-2"></div>${control('Motif','retReason')}`,async()=>{const rows=[...document.querySelectorAll('#returnLines .return-line')].map(x=>({lot_id:x.querySelector('.x-lot').value,quantite:x.querySelector('.x-qty').value}));return api('/returns',{method:'POST',body:JSON.stringify({supplier_id:$('retSupplier').value,invoice_id:$('retInvoice').value||null,lignes:rows,motif:$('retReason').value})})});const fill=()=>{$('retInvoice').innerHTML='<option value="">Avoir non imputé</option>'+invoices.filter(f=>Number(f.supplier_id)===Number($('retSupplier').value)).map(f=>`<option value="${f.id}">${esc(f.numero_facture)} · solde ${money(f.solde)}</option>`).join('');$('returnLines').innerHTML=''};$('retSupplier').onchange=fill;fill();window.procAddReturnLine=()=>{const id=Number($('retSupplier').value);const lots=(state.data.countable_lots||[]).filter(l=>Number(l.origine_fournisseur)===id&&Number(l.quantite_actuelle)>0);const block=document.createElement('div');block.className='return-line rounded-xl bg-slate-50 p-2';block.innerHTML=addLineControls(lots.map(l=>`<option value="${l.lot_id}" data-stock="${l.quantite_actuelle}">${esc(l.designation)} · ${esc(l.numero_lot)} · ${Number(l.quantite_actuelle).toLocaleString('fr-FR')}</option>`).join(''));$('returnLines').appendChild(block)}};
 window.procShowTrace=async()=>{const id=$('traceLot').value;if(!id)return notify('Choisis un lot.');try{const result=await api(`/lot-trace/${id}`);$('procTraceResult').innerHTML=`<div class="mb-3 rounded-xl bg-sky-50 p-3 text-sm"><b>${esc(result.lot.designation)}</b> · Lot ${esc(result.lot.numero_lot)} · ${esc(result.lot.entrepot)} · Stock ${Number(result.lot.quantite_actuelle).toLocaleString('fr-FR')}</div>${table(['Date','Mouvement','Bon','Commande / transfert / retour','Quantité','Utilisateur'],result.mouvements.map(m=>`<tr class="border-t"><td class="p-3">${esc(String(m.created_at).slice(0,16))}</td><td class="p-3">${esc(m.sens)}</td><td class="p-3 font-bold">${esc(m.numero)}</td><td class="p-3">${esc(m.numero_retour||m.numero_transfert||m.commande||m.motif||m.fournisseur||'—')}</td><td class="p-3">${esc(m.sens==='Sortie'?'−':'+')}${Number(m.quantite).toLocaleString('fr-FR')}</td><td class="p-3">${esc(m.utilisateur)}</td></tr>`).join(''),'Aucun mouvement.')}`}catch(e){notify(e.message)}};
 window.procRunReport=async()=>{try{const q=new URLSearchParams({date_debut:$('rStart').value,date_fin:$('rEnd').value,supplier_id:$('rSupplier').value,item_id:$('rItem').value,entrepot:$('rWarehouse').value});const r=await api('/reports?'+q);const cards=[['Commandes',money(r.commandes.total)],['Réceptions',money(r.receptions.total)],['Paiements période',money(r.paiements.total)],['Avoirs période',money(r.avoirs.total)]];$('procReportResult').innerHTML=`<div class="flex justify-end"><button onclick="window.print()" class="rounded-xl border px-3 py-2 text-xs font-bold"><i class="ph-bold ph-printer"></i> Imprimer</button></div><div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">${cards.map(x=>`<article class="rounded-xl border p-3"><span class="text-xs text-slate-500">${x[0]}</span><b class="mt-1 block text-lg">${x[1]}</b></article>`).join('')}</div><h3 class="font-extrabold">Consommation par article</h3>${table(['Article','Référence','Quantité','Valeur achat'],r.consommation.map(x=>`<tr class="border-t"><td class="p-3 font-bold">${esc(x.designation)}</td><td class="p-3">${esc(x.reference||'')}</td><td class="p-3">${Number(x.quantite).toLocaleString('fr-FR')} ${esc(x.unite)}</td><td class="p-3">${money(x.valeur)}</td></tr>`).join(''),'Aucune sortie sur la période.')}<h3 class="font-extrabold">Soldes par fournisseur</h3>${table(['Fournisseur','Facturé','Payé','Avoirs','Solde'],r.soldes_fournisseurs.map(x=>`<tr class="border-t"><td class="p-3 font-bold">${esc(x.designation)}</td><td class="p-3">${money(x.facture)}</td><td class="p-3">${money(x.paye)}</td><td class="p-3">${money(x.avoirs)}</td><td class="p-3 font-extrabold">${money(x.solde)}</td></tr>`).join(''),'Aucun fournisseur.')}<h3 class="font-extrabold">Valeur et péremption par entrepôt</h3>${table(['Entrepôt','Produits','Valeur du stock','Lots périmés','Quantité périmée'],r.entrepots.map(x=>`<tr class="border-t"><td class="p-3 font-bold">${esc(x.entrepot)}</td><td class="p-3">${x.produits}</td><td class="p-3">${money(x.valeur)}</td><td class="p-3">${x.lots_perimes}</td><td class="p-3">${Number(x.quantite_perimee).toLocaleString('fr-FR')}</td></tr>`).join(''),'Aucun entrepôt.')}`}catch(e){notify(e.message)}};
 const baseReport=window.procRunReport;window.procRunReport=async()=>{await baseReport();try{const q=new URLSearchParams({date_debut:$('rStart').value,date_fin:$('rEnd').value,supplier_id:$('rSupplier').value,item_id:$('rItem').value,entrepot:$('rWarehouse').value});const r=await api('/reports?'+q);$('procReportResult').insertAdjacentHTML('beforeend',`<h3 class="font-extrabold">Consommation par service</h3>${table(['Service','Article','Quantité','Valeur achat'],r.consommation_services.map(x=>`<tr class="border-t"><td class="p-3 font-bold">${esc(x.service)}</td><td class="p-3">${esc(x.designation)}</td><td class="p-3">${Number(x.quantite).toLocaleString('fr-FR')} ${esc(x.unite)}</td><td class="p-3">${money(x.valeur)}</td></tr>`).join(''),'Aucune consommation par service.')}`)}catch(e){notify(e.message)}};
 const nav=document.querySelector('.proc-tab')?.parentElement;
 if(nav){[['proformas','Demandes de proforma'],['counts','Inventaires'],['transfers','Transferts'],['returns','Retours / avoirs'],['alerts','Réapprovisionnement'],['trace','Traçabilité lots'],['reports','Rapports']].forEach(([id,label])=>{if(nav.querySelector(`[data-proc-tab="${id}"]`))return;const b=document.createElement('button');b.type='button';b.dataset.procTab=id;b.className='proc-tab rounded-xl px-4 py-2.5 text-xs font-bold text-slate-500 hover:bg-slate-100';b.textContent=label;b.addEventListener('click',()=>window.procTab(id));nav.appendChild(b)})}
 window.procTab('orders');
})();
</script>'''


PROCUREMENT_WORKFLOW_SCRIPT = r'''<script>
(()=>{
 const $=id=>document.getElementById(id),currentUserId=Number('__USER_ID__');let workflowData={};
 const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const money=v=>`${Number(v||0).toLocaleString('fr-FR',{minimumFractionDigits:2,maximumFractionDigits:2})} DA`;
 const api=async(path,options={})=>{const response=await fetch('/api/approvisionnement'+path,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(options.headers||{})},...options});const body=await response.json().catch(()=>({}));if(!response.ok)throw Error(body.detail||`Erreur HTTP ${response.status}`);return body};
 window.procFileToDataUrl=file=>new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve({name:file.name,data_url:String(reader.result||'')});reader.onerror=()=>reject(Error('Lecture du fichier impossible.'));reader.readAsDataURL(file)});
 const notify=message=>{const toast=$('procToast');if(toast){toast.textContent=message;toast.classList.remove('hidden');setTimeout(()=>toast.classList.add('hidden'),4500)}else window.showToast?.(message)};
 const table=(heads,rows,empty)=>`<div class="overflow-x-auto rounded-2xl border border-slate-100"><table class="w-full min-w-[950px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600"><tr>${heads.map(x=>`<th class="px-3 py-3">${x}</th>`).join('')}</tr></thead><tbody>${rows||`<tr><td colspan="${heads.length}" class="p-8 text-center text-slate-400">${empty}</td></tr>`}</tbody></table></div>`;
 const openModal=(title,html,submit)=>{ $('procModalTitle').textContent=title;$('procFields').innerHTML=html;$('procForm').onsubmit=async event=>{event.preventDefault();const button=event.submitter;if(button)button.disabled=true;try{await submit();window.procClose();await refreshWorkflow();notify('Enregistrement effectué.')}catch(error){notify(error.message)}finally{if(button)button.disabled=false}};$('procModal').classList.remove('hidden');$('procModal').classList.add('flex')};
 const refreshWorkflow=async()=>{workflowData=await api('/data');const active=document.querySelector('.proc-tab.bg-brand-neon')?.dataset.procTab;if(active==='proformas')renderProformas();else if(active==='payments')renderPayments();else if(active==='receipts')renderReceipts();return workflowData};
 const statusBadge=status=>`<span class="inline-flex rounded-full bg-slate-100 px-2.5 py-1 text-[10px] font-extrabold text-slate-700">${esc(status)}</span>`;
 const renderProformas=()=>{const rows=(workflowData.proformas||[]).map(p=>{const approvals=p.approbations||[],pending=approvals.filter(a=>a.statut==='En attente'),mine=pending.find(a=>Number(a.approver_id)===currentUserId),approvalText=approvals.length?approvals.map(a=>`${esc(a.prenom)} ${esc(a.nom)} · ${esc(a.statut)}`).join('<br>'):'Pas encore partagée';return `<tr class="border-t align-top"><td class="p-3 font-extrabold">${esc(p.numero)}<small class="block font-normal text-slate-500">${Number(p.lignes||0)} article(s)</small></td><td class="p-3">${esc(p.fournisseur)}</td><td class="p-3">${esc(p.date_demande)}</td><td class="p-3 font-bold">${money(p.montant_propose)}</td><td class="p-3">${statusBadge(p.statut)}<small class="mt-2 block text-[10px] leading-4 text-slate-500">${approvalText}</small></td><td class="p-3"><div class="flex min-w-[225px] flex-wrap gap-1.5">${p.statut==='À envoyer'?`<button onclick="procWorkflowStatus(${p.id},'Envoyée')" class="rounded-lg bg-emerald-50 px-2.5 py-2 text-[11px] font-bold text-emerald-800">Marquer envoyée</button>`:''}${(['À envoyer','Envoyée','Reçue'].includes(p.statut)&&!approvals.length)?`<button onclick="procProformaResponse(${p.id})" class="rounded-lg bg-sky-50 px-2.5 py-2 text-[11px] font-bold text-sky-800">Saisir l’offre</button><button onclick="procWorkflowAttachOffer(${p.id})" class="rounded-lg border px-2.5 py-2 text-[11px] font-bold"><i class="ph-bold ph-paperclip mr-1"></i>Joindre l’offre</button>`:''}${p.offre_nom?`<a href="/api/approvisionnement/proformas/${p.id}/offer" target="_blank" rel="noopener" class="rounded-lg bg-violet-50 px-2.5 py-2 text-[11px] font-bold text-violet-800"><i class="ph-bold ph-file-pdf mr-1"></i>Voir document</a>`:''}${p.statut==='Reçue'?`<button onclick="procWorkflowShare(${p.id})" class="rounded-lg bg-amber-50 px-2.5 py-2 text-[11px] font-bold text-amber-900"><i class="ph-bold ph-users-three mr-1"></i>Partager pour validation</button>`:''}${mine?`<button onclick="procWorkflowDecide(${p.id},${mine.id})" class="rounded-lg bg-emerald-600 px-2.5 py-2 text-[11px] font-bold text-white"><i class="ph-bold ph-stamp mr-1"></i>Examiner l’offre</button>`:''}${p.statut==='Acceptée'?`<button onclick="procProformaConvert(${p.id})" class="rounded-lg bg-emerald-50 px-2.5 py-2 text-[11px] font-bold text-emerald-800">Créer la commande</button>`:''}<button onclick="procPrintProforma(${p.id})" class="rounded-lg border px-2.5 py-2 text-[11px] font-bold"><i class="ph-bold ph-file-pdf mr-1"></i>Demande PDF</button></div></td></tr>`}).join('');$('procContent').innerHTML=`<div class="mb-4"><h2 class="font-extrabold">Parcours d’achat · demandes de proforma</h2><p class="text-xs leading-5 text-slate-500">Saisis l’offre reçue, joins son document, puis partage-la aux approbateurs. La commande est déverrouillée après validation de tous les approbateurs sélectionnés.</p></div>${table(['Demande','Fournisseur','Date','Offre proposée','État / validations internes','Actions'],rows,'Aucune demande de proforma.')}`};
 const renderPayments=()=>{const rows=(workflowData.payments||[]).map(p=>`<tr class="border-t"><td class="p-3">${esc(p.date_paiement)}</td><td class="p-3">${esc(p.fournisseur)}</td><td class="p-3 font-bold">${esc(p.numero_facture)}</td><td class="p-3 font-extrabold text-emerald-700">${money(p.montant)}</td><td class="p-3">${esc(p.mode_paiement||'—')}</td><td class="p-3">${esc(p.reference||'—')}</td><td class="p-3">${p.justificatif_nom?`<a class="font-bold text-sky-700 underline" target="_blank" rel="noopener" href="/api/approvisionnement/payments/${p.id}/receipt">${esc(p.justificatif_nom)}</a>`:'—'}</td></tr>`).join('');$('procContent').innerHTML=`<h2 class="mb-1 font-extrabold">Historique des paiements fournisseurs</h2><p class="mb-4 text-xs text-slate-500">Chaque règlement conserve son mode, sa référence et son justificatif éventuel.</p>${table(['Date','Fournisseur','Facture','Montant','Mode','N° chèque / réf. virement','Reçu'],rows,'Aucun paiement enregistré.')}`;};
 const renderReceipts=()=>{const rows=(workflowData.receipts||[]).map(r=>`<tr class="border-t"><td class="p-3 font-extrabold">${esc(r.numero)}${r.commande?`<small class="block text-slate-500">${esc(r.commande)}</small>`:''}</td><td class="p-3">${esc(r.bon_livraison||'—')}${r.attachment_id?`<a class="ml-2 text-sky-700 underline" target="_blank" rel="noopener" href="/api/gestion-stock/attachments/${r.attachment_id}/file">Voir le bon</a>`:''}</td><td class="p-3">${esc(r.fournisseur)}</td><td class="p-3">${esc(String(r.created_at||'').slice(0,16))}</td><td class="p-3 font-bold">${money(r.montant)}</td><td class="p-3">${r.facture_liee?'<span class="text-emerald-700">Facture liée</span>':`<button onclick="procOpenInvoice(${r.id})" class="rounded-lg bg-sky-50 px-3 py-2 text-xs font-bold text-sky-800">Créer la facture</button>`}</td></tr>`).join('');$('procContent').innerHTML=`<h2 class="mb-1 font-extrabold">Livraisons et bons de livraison</h2><p class="mb-4 text-xs text-slate-500">La réception physique crée le bon d’entrée et les lots en stock ; le numéro du bon fournisseur reste associé à la réception.</p>${table(['Bon d’entrée Cliniva / commande','Bon de livraison fournisseur','Fournisseur','Réception','Valeur reçue','Facturation'],rows,'Aucune livraison réceptionnée.')}`;};
 window.procWorkflowStatus=async(id,statut)=>{try{await api(`/proformas/${id}/status`,{method:'POST',body:JSON.stringify({statut})});await refreshWorkflow();notify('État de la demande actualisé.')}catch(error){notify(error.message)}};
 const fileData=async file=>new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve({name:file.name,data_url:String(reader.result||'')});reader.onerror=()=>reject(Error('Lecture du fichier impossible.'));reader.readAsDataURL(file)});
 window.procWorkflowAttachOffer=id=>openModal('Joindre l’offre fournisseur',`<p class="rounded-xl border border-sky-100 bg-sky-50 p-3 text-xs text-sky-900">Archive l’offre reçue avec la demande. Formats PDF, JPG ou PNG · 12 Mo maximum.</p><label class="block text-xs font-bold text-slate-600">Document de l’offre<input id="workflowOfferFile" type="file" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png" required class="mt-2 block w-full rounded-xl border border-slate-200 bg-slate-50 p-3 text-sm"></label>`,async()=>{const file=$('workflowOfferFile').files?.[0];if(!file)throw Error('Sélectionne le document de l’offre.');if(file.size>12*1024*1024)throw Error('Le fichier dépasse 12 Mo.');await api(`/proformas/${id}/offer`,{method:'POST',body:JSON.stringify({offre:await fileData(file)})})});
 window.procWorkflowShare=id=>{const approvers=workflowData.approvers||[];if(!approvers.length)return notify('Aucun utilisateur actif autorisé à consulter le stock.');openModal('Partager l’offre pour validation interne',`<p class="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs leading-5 text-amber-950">Tous les approbateurs sélectionnés doivent valider l’offre avant la création de la commande. Au moins un approbateur est requis.</p><label class="block text-xs font-bold text-slate-600">Approbateurs du Staff<select id="workflowApprovers" multiple required size="${Math.min(approvers.length,7)}" class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 p-3 text-sm">${approvers.map(u=>`<option value="${u.id}">${esc(u.prenom)} ${esc(u.nom)} · ${esc(u.role)}</option>`).join('')}</select><small class="mt-1 block font-normal text-slate-500">Maintiens Ctrl pour sélectionner plusieurs personnes.</small></label>`,async()=>{const ids=[...$('workflowApprovers').selectedOptions].map(option=>Number(option.value));if(!ids.length)throw Error('Sélectionne au moins un approbateur.');await api(`/proformas/${id}/share`,{method:'POST',body:JSON.stringify({approver_ids:ids})})})};
 window.procWorkflowShare=id=>{const approvers=workflowData.approvers||[];if(!approvers.length)return notify('Aucun collaborateur actif du staff avec accès au stock n’est disponible pour la validation.');const rows=approvers.map(u=>`<tr class="border-t"><td class="p-3"><input type="checkbox" class="workflow-approver-check h-4 w-4 accent-emerald-600" value="${u.id}" aria-label="Sélectionner ${esc(u.prenom)} ${esc(u.nom)}"></td><td class="p-3 font-bold">${esc(u.prenom)} ${esc(u.nom)}</td><td class="p-3">${esc(u.fonction_staff||u.role||'—')}</td><td class="p-3">${esc(u.service_staff||'—')}</td><td class="p-3"><span class="rounded-full bg-emerald-50 px-2 py-1 text-[10px] font-bold text-emerald-800">Compte actif</span></td></tr>`).join('');openModal('Ajouter les collaborateurs approbateurs',`<p class="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs leading-5 text-amber-950">Choisis dans le staff les collaborateurs qui valideront cette offre. Tous les collaborateurs sélectionnés doivent approuver avant la création de la commande.</p><label class="block text-xs font-bold text-slate-600">Rechercher un collaborateur<input id="workflowApproverSearch" type="search" placeholder="Nom, fonction ou service" class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm" oninput="document.querySelectorAll('#workflowApproverRows tr').forEach(r=>r.hidden=!r.textContent.toLocaleLowerCase().includes(this.value.toLocaleLowerCase()))"></label><div class="overflow-x-auto rounded-2xl border border-slate-200"><table class="w-full min-w-[620px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600"><tr><th class="p-3"><input type="checkbox" aria-label="Sélectionner tous les collaborateurs visibles" onchange="document.querySelectorAll('#workflowApproverRows tr:not([hidden]) .workflow-approver-check').forEach(c=>c.checked=this.checked)"></th><th class="p-3">Collaborateur</th><th class="p-3">Fonction / rôle</th><th class="p-3">Service</th><th class="p-3">Accès</th></tr></thead><tbody id="workflowApproverRows">${rows}</tbody></table></div><p class="text-xs text-slate-500">La validation nécessite un compte utilisateur actif avec accès au module Gestion de stock.</p>`,async()=>{const ids=[...document.querySelectorAll('.workflow-approver-check:checked')].map(option=>Number(option.value));if(!ids.length)throw Error('Sélectionne au moins un collaborateur du staff.');await api(`/proformas/${id}/share`,{method:'POST',body:JSON.stringify({approver_ids:ids})})})};
 window.procWorkflowDecide=(requestId,approvalId)=>openModal('Décision sur l’offre fournisseur',`<p class="rounded-xl bg-sky-50 p-3 text-xs text-sky-900">Ta décision sera enregistrée dans l’historique de validation interne.</p><label class="block text-xs font-bold text-slate-600">Décision<select id="workflowDecision" required class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5"><option value="Validée">Valider l’offre</option><option value="Refusée">Refuser l’offre</option></select></label><label class="block text-xs font-bold text-slate-600">Commentaire<textarea id="workflowDecisionComment" rows="3" maxlength="1000" class="mt-2 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5" placeholder="Commentaire ou motif"></textarea></label>`,async()=>api(`/proformas/${requestId}/approvals/${approvalId}`,{method:'POST',body:JSON.stringify({decision:$('workflowDecision').value,commentaire:$('workflowDecisionComment').value})}));
 window.procOpenPayment=id=>{const invoice=(workflowData.invoices||[]).find(item=>Number(item.id)===Number(id));if(!invoice)return notify('Facture fournisseur introuvable.');openModal(`Paiement · ${esc(invoice.numero_facture)}`,`<div class="rounded-xl bg-amber-50 p-3 text-sm text-amber-950">Solde restant : <b>${money(invoice.solde)}</b></div><div class="grid gap-3 sm:grid-cols-2"><label class="text-xs font-bold text-slate-600">Date de paiement<input id="workflowPayDate" type="date" required value="${new Date().toISOString().slice(0,10)}" class="mt-1.5 w-full rounded-xl border px-3 py-2.5"></label><label class="text-xs font-bold text-slate-600">Montant payé (DA)<input id="workflowPayAmount" type="number" min="0.01" max="${Number(invoice.solde||0)}" step="0.01" required class="mt-1.5 w-full rounded-xl border px-3 py-2.5"></label></div><div class="grid gap-3 sm:grid-cols-2"><label class="text-xs font-bold text-slate-600">Mode de paiement<select id="workflowPayMode" required class="mt-1.5 w-full rounded-xl border px-3 py-2.5"><option>Chèque bancaire</option><option>Comptant</option><option>Virement bancaire</option></select></label><label class="text-xs font-bold text-slate-600">N° du chèque / référence du virement<input id="workflowPayReference" maxlength="160" class="mt-1.5 w-full rounded-xl border px-3 py-2.5" placeholder="Référence bancaire"></label></div><label class="block text-xs font-bold text-slate-600">Reçu ou justificatif (PDF, JPG, PNG · 12 Mo maximum)<input id="workflowPayReceipt" type="file" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png" class="mt-2 block w-full rounded-xl border p-3 text-sm"></label><label class="block text-xs font-bold text-slate-600">Notes<textarea id="workflowPayNotes" rows="2" maxlength="1000" class="mt-1.5 w-full rounded-xl border px-3 py-2.5"></textarea></label>`,async()=>{const file=$('workflowPayReceipt').files?.[0];if(file&&file.size>12*1024*1024)throw Error('Le justificatif dépasse 12 Mo.');const payload={date_paiement:$('workflowPayDate').value,montant:$('workflowPayAmount').value,mode_paiement:$('workflowPayMode').value,reference:$('workflowPayReference').value,notes:$('workflowPayNotes').value};if(file)payload.justificatif=await fileData(file);await api(`/invoices/${id}/payments`,{method:'POST',body:JSON.stringify(payload)})})};
 const baseTab=window.procTab;window.procTab=name=>{baseTab(name);if(['proformas','payments','receipts'].includes(name))refreshWorkflow().catch(error=>notify(error.message))};
 const baseReload=window.procReload;window.procReload=async()=>{await baseReload?.();try{await refreshWorkflow()}catch(error){notify(error.message)}};
 if(document.querySelector('.proc-tab.bg-brand-neon')?.dataset.procTab==='proformas')refreshWorkflow().catch(error=>notify(error.message));
})();
</script>'''


def render_html(user: dict[str, Any] | None = None) -> str:
    if not user or not _allowed(user):
        return '<div class="rounded-2xl border border-rose-200 bg-rose-50 p-5 font-semibold text-rose-700">Accès à l’approvisionnement non autorisé.</div>'
    return r'''<section id="procurementModule" class="space-y-5 text-slate-800">
<header class="flex flex-wrap items-center justify-between gap-4 rounded-[2rem] bg-gradient-to-r from-[#101b13] to-[#183b20] px-7 py-6 text-white shadow-lg"><div class="flex items-center gap-4"><span class="grid h-12 w-12 place-items-center rounded-2xl bg-brand-neon text-emerald-950"><i class="ph-bold ph-truck text-2xl"></i></span><div><p class="text-xs font-bold uppercase tracking-[.18em] text-lime-300">Gestion Stock · Achats</p><h1 class="mt-1 text-2xl font-extrabold">Approvisionnement & fournisseurs</h1><p class="mt-1 text-sm text-slate-300">Commandes, réceptions, créances fournisseurs et règlements.</p></div></div><button onclick="procOpenOrder()" class="inline-flex items-center gap-2 rounded-xl bg-brand-neon px-4 py-2.5 text-sm font-extrabold text-black"><i class="ph-bold ph-plus-circle"></i>Nouvelle commande</button></header>
<section class="grid grid-cols-2 gap-3 xl:grid-cols-4"><article class="rounded-2xl border border-slate-100 bg-white p-4 shadow-sm"><span class="text-xs font-bold text-slate-500">Achats facturés</span><b id="procTotal" class="mt-2 block text-xl">—</b></article><article class="rounded-2xl border border-sky-100 bg-white p-4 shadow-sm"><span class="text-xs font-bold text-slate-500">Paiements effectués</span><b id="procPaid" class="mt-2 block text-xl text-sky-700">—</b></article><article class="rounded-2xl border border-amber-100 bg-white p-4 shadow-sm"><span class="text-xs font-bold text-slate-500">Créances restantes</span><b id="procBalance" class="mt-2 block text-xl text-amber-700">—</b></article><article class="rounded-2xl border border-rose-100 bg-white p-4 shadow-sm"><span class="text-xs font-bold text-slate-500">Factures en retard</span><b id="procLate" class="mt-2 block text-xl text-rose-700">—</b></article></section>
<nav class="flex flex-wrap gap-1 rounded-2xl border border-slate-200 bg-white p-1.5 shadow-sm"><button data-proc-tab="orders" onclick="procTab('orders')" class="proc-tab rounded-xl bg-brand-neon px-4 py-2.5 text-xs font-bold">Commandes d’achat</button><button data-proc-tab="receipts" onclick="procTab('receipts')" class="proc-tab rounded-xl px-4 py-2.5 text-xs font-bold">Réceptions</button><button data-proc-tab="invoices" onclick="procTab('invoices')" class="proc-tab rounded-xl px-4 py-2.5 text-xs font-bold">Créances / factures</button><button data-proc-tab="payments" onclick="procTab('payments')" class="proc-tab rounded-xl px-4 py-2.5 text-xs font-bold">Paiements</button><button data-proc-tab="suppliers" onclick="procTab('suppliers')" class="proc-tab rounded-xl px-4 py-2.5 text-xs font-bold">Fournisseurs</button></nav>
<section id="procContent" class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm sm:p-5"></section>
<div id="procModal" class="fixed inset-0 z-[2147483605] hidden items-center justify-center bg-slate-950/65 p-3 backdrop-blur-sm"><section class="max-h-[92vh] w-full max-w-3xl overflow-y-auto rounded-3xl bg-white shadow-2xl"><header class="sticky top-0 z-10 flex items-center justify-between bg-gradient-to-r from-[#101b13] to-[#183b20] px-5 py-4 text-white"><h2 id="procModalTitle" class="text-lg font-black"></h2><button onclick="procClose()" class="grid h-9 w-9 place-items-center rounded-xl bg-white/10"><i class="ph-bold ph-x"></i></button></header><form id="procForm" class="space-y-4 p-5"><div id="procFields" class="space-y-3"></div><div class="flex justify-end gap-2 border-t pt-4"><button type="button" onclick="procClose()" class="rounded-xl border px-4 py-2 font-bold">Annuler</button><button class="rounded-xl bg-brand-neon px-5 py-2 font-extrabold text-black">Enregistrer</button></div></form></section></div>
<div id="procPdfModal" data-proforma-id="" class="fixed inset-0 z-[2147483640] hidden items-center justify-center bg-slate-950/75 p-2 backdrop-blur-sm sm:p-4"><section class="flex h-[94vh] w-full max-w-6xl flex-col overflow-hidden rounded-3xl bg-white shadow-2xl"><header class="flex flex-wrap items-center justify-between gap-3 bg-gradient-to-r from-[#101b13] to-[#183b20] px-4 py-3 text-white sm:px-5"><div class="flex items-center gap-3"><span class="grid h-10 w-10 place-items-center rounded-xl bg-lime-300/15 text-lime-300"><i class="ph-bold ph-file-pdf text-xl"></i></span><div><h2 id="procPdfTitle" class="font-black">Demande de proforma</h2><p class="text-xs text-emerald-100/75">Document PDF prêt à imprimer ou envoyer au fournisseur</p></div></div><div class="flex flex-wrap items-center gap-2"><button onclick="procPrintPdf()" class="inline-flex items-center gap-2 rounded-xl bg-lime-300 px-3 py-2 text-sm font-extrabold text-emerald-950"><i class="ph-bold ph-printer"></i> Imprimer</button><button onclick="procShowPdfEmail()" class="inline-flex items-center gap-2 rounded-xl border border-white/20 bg-white/10 px-3 py-2 text-sm font-bold"><i class="ph-bold ph-envelope"></i> Envoyer par e-mail</button><button onclick="procClosePdf()" aria-label="Fermer le PDF" class="grid h-10 w-10 place-items-center rounded-xl bg-white/10"><i class="ph-bold ph-x"></i></button></div></header><div id="procPdfEmailPanel" class="hidden border-b border-emerald-100 bg-emerald-50 p-3"><div class="mx-auto max-w-4xl"><p class="mb-2 text-xs font-extrabold text-emerald-950">Sélectionne les adresses de destination</p><div id="procPdfRecipients" class="mb-3 flex flex-wrap gap-2"></div><div class="flex justify-end"><button id="procPdfSend" onclick="procSendProformaPdf()" class="inline-flex items-center justify-center gap-2 rounded-xl bg-emerald-700 px-4 py-2.5 text-sm font-bold text-white"><i class="ph-bold ph-paper-plane-tilt"></i> Envoyer le PDF</button></div></div></div><iframe id="procPdfFrame" title="Aperçu de la demande de proforma en PDF" class="min-h-0 flex-1 bg-slate-100"></iframe></section></div>
<div id="procToast" class="fixed bottom-5 right-5 z-[2147483606] hidden rounded-xl bg-slate-900 px-4 py-3 text-sm font-semibold text-white shadow-xl"></div>
<script>(()=>{const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),money=v=>`${Number(v||0).toLocaleString('fr-FR',{minimumFractionDigits:2,maximumFractionDigits:2})} DA`,today=new Date().toISOString().slice(0,10);let data={};const api=async(path,opt={})=>{const r=await fetch('/api/approvisionnement'+path,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(opt.headers||{})},...opt}),b=await r.json().catch(()=>({}));if(!r.ok)throw Error(b.detail||('Erreur HTTP '+r.status+': '+(r.statusText||'réponse invalide')));return b};const toast=s=>{const t=$('procToast');t.textContent=s;t.classList.remove('hidden');setTimeout(()=>t.classList.add('hidden'),3500)};const table=(heads,rows,empty)=>`<div class="overflow-x-auto rounded-2xl border border-slate-100"><table class="w-full min-w-[750px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600"><tr>${heads.map(x=>`<th class="px-3 py-3">${x}</th>`).join('')}</tr></thead><tbody>${rows||`<tr><td colspan="${heads.length}" class="p-9 text-center text-slate-400">${empty}</td></tr>`}</tbody></table></div>`;const suppliers=()=>data.suppliers||[];const field=(label,id,type='text',attrs='')=>`<label class="block text-xs font-bold text-slate-600">${label}<input id="${id}" type="${type}" ${attrs} class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm outline-none focus:ring-2 focus:ring-emerald-200"></label>`;const select=(label,id,options,required=true)=>`<label class="block text-xs font-bold text-slate-600">${label}<select id="${id}" ${required?'required':''} class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm"><option value="">Choisir…</option>${options}</select></label>`;window.procTab=name=>{document.querySelectorAll('.proc-tab').forEach(b=>b.className='proc-tab rounded-xl px-4 py-2.5 text-xs font-bold '+(b.dataset.procTab===name?'bg-brand-neon':'text-slate-500 hover:bg-slate-100'));const c=$('procContent');if(name==='orders')c.innerHTML=`<div class="mb-4 flex items-center justify-between"><div><h2 class="font-extrabold">Commandes d’achat</h2><p class="text-xs text-slate-500">Prévisions d’achat par fournisseur et par article.</p></div><button onclick="procOpenOrder()" class="rounded-xl bg-brand-neon px-3 py-2 text-xs font-bold"><i class="ph-bold ph-plus"></i> Nouvelle commande</button></div>${table(['N° commande','Fournisseur','Date','Livraison prévue','Articles','Montant estimé','Statut'],(data.orders||[]).map(o=>`<tr class="border-t"><td class="p-3 font-bold">${esc(o.numero)}</td><td class="p-3">${esc(o.fournisseur)}</td><td class="p-3">${esc(o.date_commande)}</td><td class="p-3">${esc(o.date_prevue||'—')}</td><td class="p-3">${o.lignes} ligne(s)</td><td class="p-3 font-bold">${money(o.montant)}</td><td class="p-3"><select onchange="procStatus(${o.id},this.value)" class="rounded-lg border px-2 py-1 text-xs">${['Commandée','Partiellement reçue','Reçue','Annulée'].map(s=>`<option ${o.statut===s?'selected':''}>${s}</option>`).join('')}</select></td></tr>`).join(''),'Aucune commande d’achat.')}`;else if(name==='receipts')c.innerHTML=`<div class="mb-4"><h2 class="font-extrabold">Réceptions de stock</h2><p class="text-xs text-slate-500">Les bons d’entrée validés sont saisis dans Gestion Stock. Une réception peut ensuite être rapprochée d’une facture fournisseur.</p></div>${table(['Bon d’entrée','Fournisseur','Date réception','Valeur reçue','Facture'],(data.receipts||[]).map(r=>`<tr class="border-t"><td class="p-3 font-bold">${esc(r.numero)}</td><td class="p-3">${esc(r.fournisseur)}</td><td class="p-3">${esc(String(r.created_at||'').slice(0,16))}</td><td class="p-3 font-bold">${money(r.montant)}</td><td class="p-3">${r.facture_liee?'<span class="text-emerald-700">Facture liée</span>':`<button onclick="procOpenInvoice(${r.id})" class="rounded-lg bg-sky-50 px-3 py-2 text-xs font-bold text-sky-800">Créer la créance</button>`}</td></tr>`).join(''),'Aucune réception fournisseur.')}`;else if(name==='invoices')c.innerHTML=`<div class="mb-4 flex items-center justify-between"><div><h2 class="font-extrabold">Créances fournisseurs</h2><p class="text-xs text-slate-500">Suivi du montant facturé, des règlements et du solde.</p></div><button onclick="procOpenInvoice()" class="rounded-xl bg-brand-neon px-3 py-2 text-xs font-bold"><i class="ph-bold ph-plus"></i> Ajouter une facture</button></div>${table(['Facture / réception','Fournisseur','Échéance','Montant','Déjà payé','Solde','État','Action'],(data.invoices||[]).map(f=>`<tr class="border-t"><td class="p-3 font-bold">${esc(f.numero_facture)}<small class="block text-slate-400">${esc(f.bon_reception||f.commande||'Facture libre')}</small></td><td class="p-3">${esc(f.fournisseur)}</td><td class="p-3">${esc(f.date_echeance||'—')}</td><td class="p-3">${money(f.montant)}</td><td class="p-3 text-sky-700">${money(f.montant_paye)}</td><td class="p-3 font-extrabold ${Number(f.solde)>0?'text-amber-700':'text-emerald-700'}">${money(f.solde)}</td><td class="p-3">${esc(f.statut)}</td><td class="p-3">${Number(f.solde)>0?`<button onclick="procOpenPayment(${f.id})" class="rounded-lg bg-emerald-50 px-3 py-2 text-xs font-bold text-emerald-800">Régler</button>`:'—'}</td></tr>`).join(''),'Aucune facture fournisseur enregistrée.')}`;else if(name==='payments')c.innerHTML=`<h2 class="mb-4 font-extrabold">Historique des paiements</h2>${table(['Date','Fournisseur','Facture','Montant','Mode','Référence'],(data.payments||[]).map(p=>`<tr class="border-t"><td class="p-3">${esc(p.date_paiement)}</td><td class="p-3">${esc(p.fournisseur)}</td><td class="p-3 font-bold">${esc(p.numero_facture)}</td><td class="p-3 font-extrabold text-emerald-700">${money(p.montant)}</td><td class="p-3">${esc(p.mode_paiement||'—')}</td><td class="p-3">${esc(p.reference||'—')}</td></tr>`).join(''),'Aucun paiement enregistré.')}`;else c.innerHTML=`<div class="mb-4 flex items-center justify-between"><div><h2 class="font-extrabold">Fournisseurs</h2><p class="text-xs text-slate-500">Fiches utilisées par les commandes et bons de réception du stock.</p></div><button onclick="loadModule('gestion_stock')" class="rounded-xl border px-3 py-2 text-xs font-bold"><i class="ph-bold ph-address-book"></i> Gérer les fiches fournisseurs</button></div>${table(['Fournisseur','Contact','Téléphone','E-mail','État'],suppliers().map(s=>`<tr class="border-t"><td class="p-3 font-bold">${esc(s.designation)}</td><td class="p-3">${esc(s.contact||'—')}</td><td class="p-3">${esc(s.telephone||'—')}</td><td class="p-3">${esc(s.email||'—')}</td><td class="p-3">${s.actif?'Actif':'Archivé'}</td></tr>`).join(''),'Aucun fournisseur enregistré.')}`};window.procClose=()=>{$('procModal').classList.add('hidden');$('procModal').classList.remove('flex')};const modal=(title,html,submit)=>{$('procModalTitle').textContent=title;$('procFields').innerHTML=html;$('procForm').onsubmit=async e=>{e.preventDefault();try{await submit();window.procClose();await refresh();toast('Enregistrement effectué.')}catch(err){toast(err.message)}};$('procModal').classList.remove('hidden');$('procModal').classList.add('flex')};window.procOpenOrder=()=>{let lines=[];const render=()=>{const target=$('poLines');if(target)target.innerHTML=lines.map((l,i)=>`<div class="grid gap-2 rounded-xl bg-slate-50 p-3 sm:grid-cols-12"><label class="sm:col-span-5 text-[11px] font-bold">Article<select onchange="procPickItem(${i},this.value)" class="mt-1 w-full rounded-lg border p-2"><option value="">Saisie libre</option>${(data.items||[]).map(x=>`<option value="${x.id}">${esc(x.designation)}${x.reference?' · '+esc(x.reference):''}</option>`).join('')}</select><input value="${esc(l.designation||'')}" oninput="procLineEdit(${i},'designation',this.value)" placeholder="Désignation" class="mt-1 w-full rounded-lg border p-2" required></label><label class="sm:col-span-2 text-[11px] font-bold">Quantité<input type="number" min="0.001" step="0.001" value="${l.quantite||1}" oninput="procLineEdit(${i},'quantite',this.value)" class="mt-1 w-full rounded-lg border p-2"></label><label class="sm:col-span-2 text-[11px] font-bold">Unité<input value="${esc(l.unite||'')}" oninput="procLineEdit(${i},'unite',this.value)" class="mt-1 w-full rounded-lg border p-2"></label><label class="sm:col-span-2 text-[11px] font-bold">Prix unitaire<input type="number" min="0" step="0.01" value="${l.prix_unitaire||0}" oninput="procLineEdit(${i},'prix_unitaire',this.value)" class="mt-1 w-full rounded-lg border p-2"></label><button type="button" onclick="procRemoveLine(${i})" class="text-rose-600">Supprimer</button></div>`).join('')||'<p class="text-center text-xs text-slate-400">Ajoute les articles de la commande.</p>'};window.procLineEdit=(i,k,v)=>lines[i][k]=v;window.procPickItem=(i,id)=>{const x=(data.items||[]).find(y=>Number(y.id)===Number(id));if(x){lines[i].item_id=x.id;lines[i].designation=x.designation;lines[i].unite=x.unite;render()}};window.procRemoveLine=i=>{lines.splice(i,1);render()};window.procAddLine=()=>{lines.push({quantite:1,prix_unitaire:0});render()};modal('Nouvelle commande fournisseur',`${select('Fournisseur *','poSupplier',suppliers().filter(s=>s.actif).map(s=>`<option value="${s.id}">${esc(s.designation)}</option>`).join(''))}<div class="grid gap-3 sm:grid-cols-2">${field('Date de commande','poDate','date',`value="${today}" required`)}${field('Livraison prévue','poExpected','date')}</div>${field('Notes','poNotes','text')}<div class="flex items-center justify-between"><b class="text-sm">Articles commandés</b><button type="button" onclick="procAddLine()" class="rounded-lg bg-slate-100 px-3 py-2 text-xs font-bold">+ Ajouter une ligne</button></div><div id="poLines"></div>`,async()=>api('/orders',{method:'POST',body:JSON.stringify({supplier_id:$('poSupplier').value,date_commande:$('poDate').value,date_prevue:$('poExpected').value,notes:$('poNotes').value,lignes:lines})}));render()};window.procStatus=async(id,statut)=>{try{await api(`/orders/${id}/status`,{method:'POST',body:JSON.stringify({statut})});await refresh();toast('Statut actualisé.')}catch(e){toast(e.message)}};window.procOpenInvoice=receiptId=>{const receipt=(data.receipts||[]).find(r=>Number(r.id)===Number(receiptId));const supplierOptions=suppliers().map(s=>`<option value="${s.id}" ${receipt&&Number(receipt.fournisseur_id)===Number(s.id)?'selected':''}>${esc(s.designation)}</option>`).join('');modal('Enregistrer une facture fournisseur',`${receiptId?`<div class="rounded-xl bg-sky-50 p-3 text-xs text-sky-800">Montant calculé depuis le bon d’entrée : <b>${money(receipt?.montant)}</b></div><input id="fiReceipt" type="hidden" value="${receiptId}">`:select('Bon d’entrée associé (facultatif)','fiReceipt',`<option value="">Aucun · saisir le montant</option>`+(data.receipts||[]).filter(r=>!r.facture_liee).map(r=>`<option value="${r.id}">${esc(r.numero)} · ${esc(r.fournisseur)} · ${money(r.montant)}</option>`).join(''),false)}${!receiptId?select('Fournisseur *','fiSupplier',supplierOptions):''}${field('N° de facture fournisseur *','fiNumber','text','required maxlength="120"')}<div class="grid gap-3 sm:grid-cols-2">${field('Date facture','fiDate','date',`value="${today}" required`)}${field('Date d’échéance','fiDue','date')}</div>${!receiptId?field('Montant total (DA)','fiAmount','number','min="0" step="0.01" required'):''}${field('Notes','fiNotes','text')}`,async()=>api('/invoices',{method:'POST',body:JSON.stringify({stock_document_id:$('fiReceipt')?.value||null,supplier_id:$('fiSupplier')?.value||null,numero_facture:$('fiNumber').value,date_facture:$('fiDate').value,date_echeance:$('fiDue').value,montant:$('fiAmount')?.value||null,notes:$('fiNotes').value})}))};window.procOpenPayment=id=>{const f=(data.invoices||[]).find(x=>Number(x.id)===Number(id));modal(`Paiement · ${esc(f?.numero_facture||'')}`,`<div class="rounded-xl bg-amber-50 p-3 text-sm text-amber-900">Solde restant : <b>${money(f?.solde)}</b></div><div class="grid gap-3 sm:grid-cols-2">${field('Date de paiement','ppDate','date',`value="${today}" required`)}${field('Montant payé (DA)','ppAmount','number',`min="0.01" max="${Number(f?.solde||0)}" step="0.01" required`)}</div><div class="grid gap-3 sm:grid-cols-2">${select('Mode de paiement','ppMode',['Espèces','Virement bancaire','Chèque','Carte','Autre'].map(x=>`<option>${x}</option>`).join(''),false)}${field('Référence','ppRef','text')}</div>${field('Notes','ppNotes','text')}`,async()=>api(`/invoices/${id}/payments`,{method:'POST',body:JSON.stringify({date_paiement:$('ppDate').value,montant:$('ppAmount').value,mode_paiement:$('ppMode').value,reference:$('ppRef').value,notes:$('ppNotes').value})}))};async function refresh(){if(!$('procurementModule'))return;try{data=await api('/data');$('procTotal').textContent=money(data.summary.total_facture);$('procPaid').textContent=money(data.summary.total_paye);$('procBalance').textContent=money(data.summary.reste);$('procLate').textContent=data.summary.retards||0;const active=document.querySelector('.proc-tab.bg-brand-neon')?.dataset.procTab||'orders';window.procTab(active)}catch(e){toast(e.message)}}window.procReload=refresh;refresh()})();</script></section>''' + PROCUREMENT_ADVANCED_SCRIPT + PROCUREMENT_WORKFLOW_SCRIPT.replace('__USER_ID__', str(int(user.get('id') or 0)))

