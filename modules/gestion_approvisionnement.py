"""Achats, réceptions, dettes et paiements fournisseurs de Cliniva."""

from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from app.auth import get_current_user, track_action
from database import get_db_cursor

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
        cur.execute("SELECT id, designation, actif, contact, telephone, email FROM stock_suppliers ORDER BY designation")
        suppliers = [dict(row) for row in cur.fetchall()]
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
        cur.execute("""SELECT d.id,d.numero,d.created_at,d.fournisseur_id,s.designation AS fournisseur,
                d.supplier_order_id,o.numero AS commande,
                COALESCE(SUM(CASE WHEN COALESCE(l.quantite_unite_stock,0)>0 AND l.prix_achat IS NOT NULL
                    THEN l.quantite_unite_stock*l.prix_achat
                    ELSE l.quantite*COALESCE(l.prix_unitaire,l.prix_achat/NULLIF(l.quantite_par_unite,0),0) END),0) AS montant,
                EXISTS(SELECT 1 FROM stock_supplier_invoices f WHERE f.stock_document_id=d.id) AS facture_liee
            FROM stock_documents d JOIN stock_suppliers s ON s.id=d.fournisseur_id
            LEFT JOIN stock_supplier_orders o ON o.id=d.supplier_order_id
            LEFT JOIN stock_document_lines l ON l.document_id=d.id
            WHERE d.type_document='Entrée' AND d.statut<>'Annulé' AND d.fournisseur_id IS NOT NULL
            GROUP BY d.id,s.designation ORDER BY d.created_at DESC,d.id DESC""")
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
                f.numero_facture,s.designation AS fournisseur
            FROM stock_supplier_payments p JOIN stock_supplier_invoices f ON f.id=p.invoice_id
            JOIN stock_suppliers s ON s.id=f.supplier_id ORDER BY p.date_paiement DESC,p.id DESC LIMIT 300""")
        payments = [dict(row) for row in cur.fetchall()]
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
    return {"suppliers": suppliers, "items": items, "orders": orders, "receipts": receipts,
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
    with get_db_cursor() as cur:
        cur.execute("SELECT montant FROM stock_supplier_invoices WHERE id=%s FOR UPDATE", (invoice_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Facture fournisseur introuvable.")
        cur.execute("SELECT COALESCE(SUM(montant),0) AS total FROM stock_supplier_payments WHERE invoice_id=%s", (invoice_id,))
        paid = cur.fetchone()["total"]
        balance = Decimal(str(row["montant"] or 0)) - Decimal(str(paid or 0))
        if amount > balance:
            raise HTTPException(status_code=400, detail=f"Le paiement dépasse le solde restant ({balance} DA).")
        cur.execute("""INSERT INTO stock_supplier_payments(invoice_id,date_paiement,montant,mode_paiement,reference,notes,created_by)
            VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (invoice_id, paid_date, amount, mode, reference, str(data.get("notes") or "")[:1000], user.get("id")))
    track_action(request, "STOCK_SUPPLIER_PAYMENT", f"Paiement de {amount} DA enregistré pour facture #{invoice_id}")
    return {"success": True}


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
 const api=async(path,opt={})=>{const r=await fetch('/api/approvisionnement'+path,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(opt.headers||{})},...opt}),b=await r.json().catch(()=>({}));if(!r.ok)throw Error(b.detail||'Opération impossible.');return b};
 const notify=msg=>{const t=$('procToast');t.textContent=msg;t.classList.remove('hidden');setTimeout(()=>t.classList.add('hidden'),4500)};
 const table=(headers,rows,empty)=>`<div class="overflow-x-auto rounded-2xl border border-slate-100"><table class="w-full min-w-[760px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600"><tr>${headers.map(h=>`<th class="px-3 py-3">${h}</th>`).join('')}</tr></thead><tbody>${rows||`<tr><td colspan="${headers.length}" class="p-8 text-center text-slate-400">${empty}</td></tr>`}</tbody></table></div>`;
 const control=(label,id,type='text',attrs='')=>`<label class="block text-xs font-bold text-slate-600">${label}<input id="${id}" type="${type}" ${attrs} class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm"></label>`;
 const select=(label,id,options,required=true)=>`<label class="block text-xs font-bold text-slate-600">${label}<select id="${id}" ${required?'required':''} class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm"><option value="">Choisir…</option>${options}</select></label>`;
 const modal=(title,fields,submit)=>{ $('procModalTitle').textContent=title;$('procFields').innerHTML=fields;$('procForm').onsubmit=async e=>{e.preventDefault();const btn=$('procForm').querySelector('[type=submit],button:not([type])');if(btn)btn.disabled=true;try{const result=await submit();$('procModal').classList.add('hidden');$('procModal').classList.remove('flex');notify(result?.numero?`Enregistré : ${result.numero}`:'Opération enregistrée.');await show(state.tab)}catch(error){notify(error.message)}finally{if(btn)btn.disabled=false}};$('procModal').classList.remove('hidden');$('procModal').classList.add('flex')};
 const getData=async()=>{state.data=await api('/data');return state.data};
 const setActive=name=>document.querySelectorAll('.proc-tab').forEach(b=>b.className='proc-tab rounded-xl px-4 py-2.5 text-xs font-bold '+(b.dataset.procTab===name?'bg-brand-neon':'text-slate-500 hover:bg-slate-100'));
 const warehouseOptions=()=>((state.data.warehouses||[]).map(x=>`<option>${esc(x)}</option>`).join(''));
 const locationOptions=()=>((state.data.locations||[]).map(x=>`<option>${esc(x)}</option>`).join(''));
 const runReceive=order=>{const lines=(order.lignes_detail||[]).filter(l=>Number(l.quantite)>Number(l.quantite_recue));if(!lines.length){notify('La commande est déjà entièrement réceptionnée.');return}const blocks=lines.map((l,i)=>{const rest=Math.max(0,Number(l.quantite)-Number(l.quantite_recue));return `<fieldset class="rounded-2xl border border-slate-200 p-3"><legend class="px-2 text-sm font-extrabold">${esc(l.designation)} · reste ${rest} ${esc(l.unite||'')}</legend><input type="hidden" class="recv-id" value="${l.id}"><div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">${control('Quantité reçue','recv_qty_'+i,'number',`min="0.001" max="${rest}" step="0.001" value="${rest}" required`)}${control('N° lot','recv_lot_'+i,'text','required maxlength="120"')}${control('Date de péremption','recv_exp_'+i,'date',l.suivi_peremption?'required':'')}<label class="block text-xs font-bold text-slate-600">Unité de stock<select id="recv_unit_${i}" class="mt-1.5 w-full rounded-xl border px-3 py-2.5">${(state.data.items||[]).find(x=>Number(x.id)===Number(l.item_id))?`<option>${esc(l.unite_article||l.unite||'unité')}</option>`:''}${(state.data.items||[]).map(x=>`<option>${esc(x.unite)}</option>`).join('')}</select></label>${control('Quantité par unité','recv_per_'+i,'number','min="0.001" step="0.001" value="1" required')}${control('Prix achat par unité commandée (DA)','recv_price_'+i,'number',`min="0" step="0.01" value="${Number(l.prix_unitaire||0)}" required`)}${control('Marge (%)','recv_margin_'+i,'number','min="0" step="0.01" value="15" required')}<label class="block text-xs font-bold text-slate-600">Entrepôt<select id="recv_wh_${i}" required class="mt-1.5 w-full rounded-xl border px-3 py-2.5">${warehouseOptions()}</select></label><label class="block text-xs font-bold text-slate-600">Emplacement<select id="recv_loc_${i}" required class="mt-1.5 w-full rounded-xl border px-3 py-2.5">${locationOptions()}</select></label></div></fieldset>`}).join('');modal(`Réception partielle · ${esc(order.numero)}`,`<p class="rounded-xl bg-sky-50 p-3 text-xs text-sky-800">Chaque réception crée un bon d’entrée, un lot et un mouvement. Tu peux réceptionner le solde en plusieurs fois.</p><div class="max-h-[58vh] space-y-3 overflow-y-auto">${blocks}</div>${control('Notes de réception','recv_notes')}`,async()=>{const rows=lines.map((l,i)=>({order_line_id:l.id,quantite:$('recv_qty_'+i).value,numero_lot:$('recv_lot_'+i).value,date_peremption:$('recv_exp_'+i).value,unite_stock:$('recv_unit_'+i).value,quantite_par_unite:$('recv_per_'+i).value,prix_achat:$('recv_price_'+i).value,marge_vente:$('recv_margin_'+i).value,entrepot:$('recv_wh_'+i).value,emplacement:$('recv_loc_'+i).value}));return api(`/orders/${order.id}/receive`,{method:'POST',body:JSON.stringify({lignes:rows,notes:$('recv_notes').value})})})};
 const orderForm=()=>window.procOpenOrder();
 const renderOrders=()=>{const d=state.data;const rows=(d.orders||[]).map(o=>{const remain=(o.lignes_detail||[]).reduce((a,l)=>a+Math.max(0,Number(l.quantite)-Number(l.quantite_recue)),0);return `<tr class="border-t"><td class="p-3 font-bold">${esc(o.numero)}<small class="block text-slate-400">${Number(o.quantite_recue||0)} / ${Number(o.quantite_commandee||0)} unité(s) reçue(s)</small></td><td class="p-3">${esc(o.fournisseur)}</td><td class="p-3">${esc(o.date_commande)}</td><td class="p-3">${esc(o.date_prevue||'—')}</td><td class="p-3">${o.lignes} ligne(s)</td><td class="p-3 font-bold">${money(o.montant)}</td><td class="p-3"><span class="rounded-full bg-sky-50 px-2 py-1 text-[10px] font-bold text-sky-800">${esc(o.statut)}</span></td><td class="p-3">${remain>0&&o.statut!=='Annulée'?`<button onclick="procReceive(${o.id})" class="rounded-lg bg-emerald-50 px-3 py-2 text-xs font-bold text-emerald-800">Réceptionner</button>`:'—'}</td></tr>`}).join('');$('procContent').innerHTML=`<div class="mb-4 flex items-center justify-between"><div><h2 class="font-extrabold">Commandes d’achat</h2><p class="text-xs text-slate-500">Réception partielle ou complète, article et lot par lot.</p></div><button onclick="procOpenOrder()" class="rounded-xl bg-brand-neon px-3 py-2 text-xs font-bold">＋ Nouvelle commande</button></div>${table(['Commande / réception','Fournisseur','Date','Livraison prévue','Articles','Montant prévu','État','Action'],rows,'Aucune commande.')}`};
 const renderReceipts=()=>{const rows=(state.data.receipts||[]).map(r=>`<tr class="border-t"><td class="p-3 font-bold">${esc(r.numero)}${r.commande?`<small class="block text-slate-400">${esc(r.commande)}</small>`:''}</td><td class="p-3">${esc(r.fournisseur)}</td><td class="p-3">${esc(String(r.created_at||'').slice(0,16))}</td><td class="p-3 font-bold">${money(r.montant)}</td><td class="p-3">${r.facture_liee?'<span class="text-emerald-700">Facture liée</span>':`<button onclick="procOpenInvoice(${r.id})" class="rounded-lg bg-sky-50 px-3 py-2 text-xs font-bold text-sky-800">Créer la créance</button>`}</td></tr>`).join('');$('procContent').innerHTML=`<h2 class="mb-1 font-extrabold">Réceptions de stock</h2><p class="mb-4 text-xs text-slate-500">Historique des réceptions et rapprochement avec la facture du fournisseur.</p>${table(['Bon / commande','Fournisseur','Date','Valeur reçue','Facture'],rows,'Aucune réception fournisseur.')}`};
 const renderCounts=()=>{const rows=(state.data.inventory_counts||[]).map(x=>`<tr class="border-t"><td class="p-3 font-bold">${esc(x.numero)}</td><td class="p-3">${esc(x.entrepot)}</td><td class="p-3">${esc(String(x.created_at||'').slice(0,16))}</td><td class="p-3">${x.lots}</td><td class="p-3 font-bold ${Number(x.ecarts)?'text-amber-700':'text-emerald-700'}">${x.ecarts} écart(s) · ${Number(x.quantite_ecart).toLocaleString('fr-FR')}</td><td class="p-3">${esc(x.statut)}</td></tr>`).join('');$('procContent').innerHTML=`<div class="mb-4 flex items-center justify-between"><div><h2 class="font-extrabold">Inventaires physiques</h2><p class="text-xs text-slate-500">Les écarts génèrent automatiquement des mouvements d’ajustement traçables.</p></div><button onclick="procOpenCount()" class="rounded-xl bg-brand-neon px-3 py-2 text-xs font-bold">＋ Démarrer un inventaire</button></div>${table(['N° inventaire','Entrepôt','Date','Lots comptés','Écarts','État'],rows,'Aucun inventaire validé.')}`};
 const renderTransfers=()=>{const rows=(state.data.transfers||[]).map(x=>`<tr class="border-t"><td class="p-3 font-bold">${esc(x.numero)}</td><td class="p-3">${esc(x.entrepot_source)} → ${esc(x.entrepot_destination)}</td><td class="p-3">${x.lignes} lot(s)</td><td class="p-3">${Number(x.quantite).toLocaleString('fr-FR')}</td><td class="p-3">${esc(String(x.created_at||'').slice(0,16))}</td></tr>`).join('');$('procContent').innerHTML=`<div class="mb-4 flex items-center justify-between"><div><h2 class="font-extrabold">Transferts entre entrepôts</h2><p class="text-xs text-slate-500">Les lots gardent leur numéro et leur date de péremption. Chaque transfert crée les deux mouvements correspondants.</p></div><button onclick="procOpenTransfer()" class="rounded-xl bg-brand-neon px-3 py-2 text-xs font-bold">＋ Nouveau transfert</button></div>${table(['Référence','Trajet','Lots','Quantité','Date'],rows,'Aucun transfert enregistré.')}`};
 const renderReturns=()=>{const rows=(state.data.returns||[]).map(x=>`<tr class="border-t"><td class="p-3 font-bold">${esc(x.numero)}</td><td class="p-3">${esc(x.fournisseur)}</td><td class="p-3">${esc(x.numero_facture||'Avoir non imputé')}</td><td class="p-3">${esc(x.bon_retour)}</td><td class="p-3 font-bold text-amber-700">${money(x.montant_avoir)}</td><td class="p-3">${esc(String(x.created_at||'').slice(0,16))}</td></tr>`).join('');$('procContent').innerHTML=`<div class="mb-4 flex items-center justify-between"><div><h2 class="font-extrabold">Retours et avoirs fournisseur</h2><p class="text-xs text-slate-500">Le stock est diminué par lot et l’avoir rattaché réduit le solde de la facture.</p></div><button onclick="procOpenReturn()" class="rounded-xl bg-brand-neon px-3 py-2 text-xs font-bold">＋ Enregistrer un retour</button></div>${table(['Avoir','Fournisseur','Facture','Bon retour','Montant','Date'],rows,'Aucun retour enregistré.')}`};
 const renderAlerts=()=>{const rows=(state.data.restock_alerts||[]).map(x=>`<tr class="border-t"><td class="p-3 font-bold">${esc(x.designation)}<small class="block text-slate-400">${esc(x.reference||'')}</small></td><td class="p-3">${Number(x.dispo).toLocaleString('fr-FR')} ${esc(x.unite)}</td><td class="p-3">${Number(x.seuil_alerte).toLocaleString('fr-FR')} ${esc(x.unite)}</td><td class="p-3">${Number(x.en_commande).toLocaleString('fr-FR')}</td><td class="p-3 font-extrabold text-amber-700">${Number(x.quantite_suggeree).toLocaleString('fr-FR')} ${esc(x.unite)}</td></tr>`).join('');$('procContent').innerHTML=`<h2 class="mb-1 font-extrabold">Suggestions de réapprovisionnement</h2><p class="mb-4 text-xs text-slate-500">Seuil cible : deux fois le seuil d’alerte, en tenant compte des commandes ouvertes. Vérifie les suggestions avant de créer une commande.</p>${table(['Article','Stock utilisable','Seuil','Déjà commandé','Quantité suggérée'],rows,'Aucun article en alerte.')}`};
 const renderTrace=()=>{const opts=(state.data.countable_lots||[]).map(l=>`<option value="${l.lot_id}">${esc(l.designation)} · lot ${esc(l.numero_lot)} · ${esc(l.entrepot)}</option>`).join('');$('procContent').innerHTML=`<h2 class="mb-1 font-extrabold">Traçabilité par lot</h2><p class="mb-4 text-xs text-slate-500">Réceptions, sorties, transferts, retours et inventaires conservés dans l’historique.</p><div class="mb-4 flex max-w-3xl flex-wrap items-end gap-3">${select('Lot à consulter','traceLot',opts)}<button onclick="procShowTrace()" class="rounded-xl bg-brand-neon px-4 py-2.5 text-xs font-extrabold">Afficher la traçabilité</button></div><div id="procTraceResult"></div>`};
 const renderReports=()=>{$('procContent').innerHTML=`<div class="mb-4"><h2 class="font-extrabold">Rapports de stock et fournisseurs</h2><p class="text-xs text-slate-500">Achats, réceptions, règlements, avoirs, consommation, soldes et état des entrepôts.</p></div><div class="mb-4 grid gap-3 rounded-2xl bg-slate-50 p-4 sm:grid-cols-2 lg:grid-cols-4">${control('Date début','rStart','date',`value="${new Date(Date.now()-30*864e5).toISOString().slice(0,10)}"`)}${control('Date fin','rEnd','date',`value="${today}"`)}${select('Fournisseur','rSupplier','<option value="">Tous</option>'+(state.data.suppliers||[]).map(s=>`<option value="${s.id}">${esc(s.designation)}</option>`).join(''),false)}${select('Entrepôt','rWarehouse','<option value="">Tous</option>'+(state.data.warehouses||[]).map(x=>`<option>${esc(x)}</option>`).join(''),false)}<label class="block text-xs font-bold text-slate-600 sm:col-span-2">Produit<select id="rItem" class="mt-1.5 w-full rounded-xl border px-3 py-2.5"><option value="">Tous les produits</option>${(state.data.items||[]).map(x=>`<option value="${x.id}">${esc(x.designation)}</option>`).join('')}</select></label><button onclick="procRunReport()" class="self-end rounded-xl bg-brand-neon px-4 py-2.5 text-xs font-extrabold">Afficher le rapport</button></div><div id="procReportResult" class="space-y-4"><p class="p-8 text-center text-sm text-slate-400">Choisis la période puis affiche le rapport.</p></div>`};
 const render=()=>{setActive(state.tab);const d=state.data;if(state.tab==='orders')renderOrders();else if(state.tab==='receipts')renderReceipts();else if(state.tab==='invoices'||state.tab==='payments'||state.tab==='suppliers')baseTab(state.tab);else if(state.tab==='counts')renderCounts();else if(state.tab==='transfers')renderTransfers();else if(state.tab==='returns')renderReturns();else if(state.tab==='alerts')renderAlerts();else if(state.tab==='trace')renderTrace();else renderReports();if(d.summary){$('procTotal').textContent=money(d.summary.total_facture);$('procPaid').textContent=money(d.summary.total_paye);$('procBalance').textContent=money(d.summary.reste);$('procLate').textContent=d.summary.retards||0}};
 const show=async name=>{state.tab=name;try{await getData();render()}catch(e){notify(e.message)}};
 const baseTab=window.procTab;window.procTab=name=>{if(['orders','receipts','invoices','payments','suppliers','counts','transfers','returns','alerts','trace','reports'].includes(name))show(name);else baseTab(name)};
 window.procReceive=id=>{const order=(state.data.orders||[]).find(x=>Number(x.id)===Number(id));if(order)runReceive(order)};
 const addLineControls=(lotOptions)=>`<div class="grid gap-2 sm:grid-cols-12"><label class="sm:col-span-8 text-[11px] font-bold">Lot<select class="x-lot mt-1 w-full rounded-lg border p-2" required>${lotOptions}</select></label><label class="sm:col-span-3 text-[11px] font-bold">Quantité<input class="x-qty mt-1 w-full rounded-lg border p-2" type="number" min="0.001" step="0.001" required></label><button type="button" onclick="this.parentElement.remove()" class="text-rose-700">Retirer</button></div>`;
 window.procOpenCount=()=>{const wh=warehouseOptions();modal('Inventaire physique',`${select('Entrepôt à compter','countWh',wh)}<div id="countLots" class="max-h-[56vh] space-y-2 overflow-y-auto rounded-xl border p-3 text-xs">Sélectionne un entrepôt.</div>${control('Notes','countNotes')}`,async()=>{const rows=[...document.querySelectorAll('.count-qty')].map(input=>({lot_id:input.dataset.lot,quantite_comptee:input.value}));if(!rows.length)throw Error('Aucun lot à compter dans cet entrepôt.');return api('/inventory-counts',{method:'POST',body:JSON.stringify({entrepot:$('countWh').value,lignes:rows,notes:$('countNotes').value})})});$('countWh').onchange=()=>{const lots=(state.data.countable_lots||[]).filter(l=>l.entrepot===$('countWh').value);$('countLots').innerHTML=lots.length?lots.map(l=>`<label class="grid grid-cols-[1fr_100px] items-center gap-3 border-b py-2"><span><b>${esc(l.designation)}</b> · ${esc(l.numero_lot)} <small class="text-slate-500">théorique ${Number(l.quantite_actuelle).toLocaleString('fr-FR')} ${esc(l.unite_article)}</small></span><input class="count-qty rounded-lg border px-2 py-1.5" type="number" min="0" step="0.001" data-lot="${l.lot_id}" value="${l.quantite_actuelle}"></label>`).join(''):'Aucun lot dans cet entrepôt.'}};
 window.procOpenTransfer=()=>{const lots=state.data.countable_lots||[];modal('Transfert entre entrepôts',`<div class="grid gap-3 sm:grid-cols-2">${select('Entrepôt source','trSource',warehouseOptions())}${select('Entrepôt destination','trDest',warehouseOptions())}${select('Emplacement de départ','trSourceLoc',locationOptions(),false)}${select('Emplacement d’arrivée','trDestLoc',locationOptions(),false)}</div><div class="my-3 flex items-center justify-between"><b class="text-sm">Lots à transférer</b><button type="button" onclick="procAddTransferLine()" class="rounded-lg bg-slate-100 px-3 py-2 text-xs font-bold">＋ Ajouter un lot</button></div><div id="transferLines" class="space-y-2"></div>${control('Motif','trReason')}`,async()=>{const src=$('trSource').value,dst=$('trDest').value;if(src===dst)throw Error('Les entrepôts source et destination doivent être différents.');const rows=[...document.querySelectorAll('#transferLines .transfer-line')].map(x=>({lot_id:x.querySelector('.x-lot').value,quantite:x.querySelector('.x-qty').value}));return api('/transfers',{method:'POST',body:JSON.stringify({entrepot_source:src,entrepot_destination:dst,emplacement_source:$('trSourceLoc').value,emplacement_destination:$('trDestLoc').value,lignes:rows,motif:$('trReason').value})})});window.procAddTransferLine=()=>{const block=document.createElement('div');block.className='transfer-line rounded-xl bg-slate-50 p-2';block.innerHTML=addLineControls(lots.filter(l=>l.entrepot===$('trSource').value).map(l=>`<option value="${l.lot_id}" data-stock="${l.quantite_actuelle}">${esc(l.designation)} · ${esc(l.numero_lot)} · ${Number(l.quantite_actuelle).toLocaleString('fr-FR')}</option>`).join(''));$('transferLines').appendChild(block)};$('trSource').onchange=()=>{$('transferLines').innerHTML=''}};
 window.procOpenReturn=()=>{const suppliers=state.data.suppliers||[],invoices=state.data.invoices||[];modal('Retourner des articles au fournisseur',`${select('Fournisseur','retSupplier',suppliers.map(s=>`<option value="${s.id}">${esc(s.designation)}</option>`).join(''))}${select('Imputer l’avoir sur la facture (facultatif)','retInvoice','',false)}<div class="my-3 flex items-center justify-between"><b class="text-sm">Lots à retourner</b><button type="button" onclick="procAddReturnLine()" class="rounded-lg bg-slate-100 px-3 py-2 text-xs font-bold">＋ Ajouter un lot</button></div><div id="returnLines" class="space-y-2"></div>${control('Motif','retReason')}`,async()=>{const rows=[...document.querySelectorAll('#returnLines .return-line')].map(x=>({lot_id:x.querySelector('.x-lot').value,quantite:x.querySelector('.x-qty').value}));return api('/returns',{method:'POST',body:JSON.stringify({supplier_id:$('retSupplier').value,invoice_id:$('retInvoice').value||null,lignes:rows,motif:$('retReason').value})})});const fill=()=>{$('retInvoice').innerHTML='<option value="">Avoir non imputé</option>'+invoices.filter(f=>Number(f.supplier_id)===Number($('retSupplier').value)).map(f=>`<option value="${f.id}">${esc(f.numero_facture)} · solde ${money(f.solde)}</option>`).join('');$('returnLines').innerHTML=''};$('retSupplier').onchange=fill;fill();window.procAddReturnLine=()=>{const id=Number($('retSupplier').value);const lots=(state.data.countable_lots||[]).filter(l=>Number(l.origine_fournisseur)===id&&Number(l.quantite_actuelle)>0);const block=document.createElement('div');block.className='return-line rounded-xl bg-slate-50 p-2';block.innerHTML=addLineControls(lots.map(l=>`<option value="${l.lot_id}" data-stock="${l.quantite_actuelle}">${esc(l.designation)} · ${esc(l.numero_lot)} · ${Number(l.quantite_actuelle).toLocaleString('fr-FR')}</option>`).join(''));$('returnLines').appendChild(block)}};
 window.procShowTrace=async()=>{const id=$('traceLot').value;if(!id)return notify('Choisis un lot.');try{const result=await api(`/lot-trace/${id}`);$('procTraceResult').innerHTML=`<div class="mb-3 rounded-xl bg-sky-50 p-3 text-sm"><b>${esc(result.lot.designation)}</b> · Lot ${esc(result.lot.numero_lot)} · ${esc(result.lot.entrepot)} · Stock ${Number(result.lot.quantite_actuelle).toLocaleString('fr-FR')}</div>${table(['Date','Mouvement','Bon','Commande / transfert / retour','Quantité','Utilisateur'],result.mouvements.map(m=>`<tr class="border-t"><td class="p-3">${esc(String(m.created_at).slice(0,16))}</td><td class="p-3">${esc(m.sens)}</td><td class="p-3 font-bold">${esc(m.numero)}</td><td class="p-3">${esc(m.numero_retour||m.numero_transfert||m.commande||m.motif||m.fournisseur||'—')}</td><td class="p-3">${esc(m.sens==='Sortie'?'−':'+')}${Number(m.quantite).toLocaleString('fr-FR')}</td><td class="p-3">${esc(m.utilisateur)}</td></tr>`).join(''),'Aucun mouvement.')}`}catch(e){notify(e.message)}};
 window.procRunReport=async()=>{try{const q=new URLSearchParams({date_debut:$('rStart').value,date_fin:$('rEnd').value,supplier_id:$('rSupplier').value,item_id:$('rItem').value,entrepot:$('rWarehouse').value});const r=await api('/reports?'+q);const cards=[['Commandes',money(r.commandes.total)],['Réceptions',money(r.receptions.total)],['Paiements période',money(r.paiements.total)],['Avoirs période',money(r.avoirs.total)]];$('procReportResult').innerHTML=`<div class="flex justify-end"><button onclick="window.print()" class="rounded-xl border px-3 py-2 text-xs font-bold"><i class="ph-bold ph-printer"></i> Imprimer</button></div><div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">${cards.map(x=>`<article class="rounded-xl border p-3"><span class="text-xs text-slate-500">${x[0]}</span><b class="mt-1 block text-lg">${x[1]}</b></article>`).join('')}</div><h3 class="font-extrabold">Consommation par article</h3>${table(['Article','Référence','Quantité','Valeur achat'],r.consommation.map(x=>`<tr class="border-t"><td class="p-3 font-bold">${esc(x.designation)}</td><td class="p-3">${esc(x.reference||'')}</td><td class="p-3">${Number(x.quantite).toLocaleString('fr-FR')} ${esc(x.unite)}</td><td class="p-3">${money(x.valeur)}</td></tr>`).join(''),'Aucune sortie sur la période.')}<h3 class="font-extrabold">Soldes par fournisseur</h3>${table(['Fournisseur','Facturé','Payé','Avoirs','Solde'],r.soldes_fournisseurs.map(x=>`<tr class="border-t"><td class="p-3 font-bold">${esc(x.designation)}</td><td class="p-3">${money(x.facture)}</td><td class="p-3">${money(x.paye)}</td><td class="p-3">${money(x.avoirs)}</td><td class="p-3 font-extrabold">${money(x.solde)}</td></tr>`).join(''),'Aucun fournisseur.')}<h3 class="font-extrabold">Valeur et péremption par entrepôt</h3>${table(['Entrepôt','Produits','Valeur du stock','Lots périmés','Quantité périmée'],r.entrepots.map(x=>`<tr class="border-t"><td class="p-3 font-bold">${esc(x.entrepot)}</td><td class="p-3">${x.produits}</td><td class="p-3">${money(x.valeur)}</td><td class="p-3">${x.lots_perimes}</td><td class="p-3">${Number(x.quantite_perimee).toLocaleString('fr-FR')}</td></tr>`).join(''),'Aucun entrepôt.')}`}catch(e){notify(e.message)}};
 const baseReport=window.procRunReport;window.procRunReport=async()=>{await baseReport();try{const q=new URLSearchParams({date_debut:$('rStart').value,date_fin:$('rEnd').value,supplier_id:$('rSupplier').value,item_id:$('rItem').value,entrepot:$('rWarehouse').value});const r=await api('/reports?'+q);$('procReportResult').insertAdjacentHTML('beforeend',`<h3 class="font-extrabold">Consommation par service</h3>${table(['Service','Article','Quantité','Valeur achat'],r.consommation_services.map(x=>`<tr class="border-t"><td class="p-3 font-bold">${esc(x.service)}</td><td class="p-3">${esc(x.designation)}</td><td class="p-3">${Number(x.quantite).toLocaleString('fr-FR')} ${esc(x.unite)}</td><td class="p-3">${money(x.valeur)}</td></tr>`).join(''),'Aucune consommation par service.')}`)}catch(e){notify(e.message)}};
 const nav=document.querySelector('.proc-tab')?.parentElement;
 if(nav){[['counts','Inventaires'],['transfers','Transferts'],['returns','Retours / avoirs'],['alerts','Réapprovisionnement'],['trace','Traçabilité lots'],['reports','Rapports']].forEach(([id,label])=>{if(nav.querySelector(`[data-proc-tab="${id}"]`))return;const b=document.createElement('button');b.type='button';b.dataset.procTab=id;b.className='proc-tab rounded-xl px-4 py-2.5 text-xs font-bold text-slate-500 hover:bg-slate-100';b.textContent=label;b.addEventListener('click',()=>window.procTab(id));nav.appendChild(b)})}
 window.procTab('orders');
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
<div id="procModal" class="fixed inset-0 z-[2147483605] hidden items-center justify-center bg-slate-950/65 p-3 backdrop-blur-sm"><section class="max-h-[92vh] w-full max-w-3xl overflow-y-auto rounded-3xl bg-white shadow-2xl"><header class="sticky top-0 z-10 flex items-center justify-between bg-gradient-to-r from-[#101b13] to-[#183b20] px-5 py-4 text-white"><h2 id="procModalTitle" class="text-lg font-black"></h2><button onclick="procClose()" class="grid h-9 w-9 place-items-center rounded-xl bg-white/10"><i class="ph-bold ph-x"></i></button></header><form id="procForm" class="space-y-4 p-5"><div id="procFields" class="space-y-3"></div><div class="flex justify-end gap-2 border-t pt-4"><button type="button" onclick="procClose()" class="rounded-xl border px-4 py-2 font-bold">Annuler</button><button class="rounded-xl bg-brand-neon px-5 py-2 font-extrabold text-black">Enregistrer</button></div></form></section></div><div id="procToast" class="fixed bottom-5 right-5 z-[2147483606] hidden rounded-xl bg-slate-900 px-4 py-3 text-sm font-semibold text-white shadow-xl"></div>
<script>(()=>{const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),money=v=>`${Number(v||0).toLocaleString('fr-FR',{minimumFractionDigits:2,maximumFractionDigits:2})} DA`,today=new Date().toISOString().slice(0,10);let data={};const api=async(path,opt={})=>{const r=await fetch('/api/approvisionnement'+path,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(opt.headers||{})},...opt}),b=await r.json().catch(()=>({}));if(!r.ok)throw Error(b.detail||'Opération impossible.');return b};const toast=s=>{const t=$('procToast');t.textContent=s;t.classList.remove('hidden');setTimeout(()=>t.classList.add('hidden'),3500)};const table=(heads,rows,empty)=>`<div class="overflow-x-auto rounded-2xl border border-slate-100"><table class="w-full min-w-[750px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600"><tr>${heads.map(x=>`<th class="px-3 py-3">${x}</th>`).join('')}</tr></thead><tbody>${rows||`<tr><td colspan="${heads.length}" class="p-9 text-center text-slate-400">${empty}</td></tr>`}</tbody></table></div>`;const suppliers=()=>data.suppliers||[];const field=(label,id,type='text',attrs='')=>`<label class="block text-xs font-bold text-slate-600">${label}<input id="${id}" type="${type}" ${attrs} class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm outline-none focus:ring-2 focus:ring-emerald-200"></label>`;const select=(label,id,options,required=true)=>`<label class="block text-xs font-bold text-slate-600">${label}<select id="${id}" ${required?'required':''} class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm"><option value="">Choisir…</option>${options}</select></label>`;window.procTab=name=>{document.querySelectorAll('.proc-tab').forEach(b=>b.className='proc-tab rounded-xl px-4 py-2.5 text-xs font-bold '+(b.dataset.procTab===name?'bg-brand-neon':'text-slate-500 hover:bg-slate-100'));const c=$('procContent');if(name==='orders')c.innerHTML=`<div class="mb-4 flex items-center justify-between"><div><h2 class="font-extrabold">Commandes d’achat</h2><p class="text-xs text-slate-500">Prévisions d’achat par fournisseur et par article.</p></div><button onclick="procOpenOrder()" class="rounded-xl bg-brand-neon px-3 py-2 text-xs font-bold"><i class="ph-bold ph-plus"></i> Nouvelle commande</button></div>${table(['N° commande','Fournisseur','Date','Livraison prévue','Articles','Montant estimé','Statut'],(data.orders||[]).map(o=>`<tr class="border-t"><td class="p-3 font-bold">${esc(o.numero)}</td><td class="p-3">${esc(o.fournisseur)}</td><td class="p-3">${esc(o.date_commande)}</td><td class="p-3">${esc(o.date_prevue||'—')}</td><td class="p-3">${o.lignes} ligne(s)</td><td class="p-3 font-bold">${money(o.montant)}</td><td class="p-3"><select onchange="procStatus(${o.id},this.value)" class="rounded-lg border px-2 py-1 text-xs">${['Commandée','Partiellement reçue','Reçue','Annulée'].map(s=>`<option ${o.statut===s?'selected':''}>${s}</option>`).join('')}</select></td></tr>`).join(''),'Aucune commande d’achat.')}`;else if(name==='receipts')c.innerHTML=`<div class="mb-4"><h2 class="font-extrabold">Réceptions de stock</h2><p class="text-xs text-slate-500">Les bons d’entrée validés sont saisis dans Gestion Stock. Une réception peut ensuite être rapprochée d’une facture fournisseur.</p></div>${table(['Bon d’entrée','Fournisseur','Date réception','Valeur reçue','Facture'],(data.receipts||[]).map(r=>`<tr class="border-t"><td class="p-3 font-bold">${esc(r.numero)}</td><td class="p-3">${esc(r.fournisseur)}</td><td class="p-3">${esc(String(r.created_at||'').slice(0,16))}</td><td class="p-3 font-bold">${money(r.montant)}</td><td class="p-3">${r.facture_liee?'<span class="text-emerald-700">Facture liée</span>':`<button onclick="procOpenInvoice(${r.id})" class="rounded-lg bg-sky-50 px-3 py-2 text-xs font-bold text-sky-800">Créer la créance</button>`}</td></tr>`).join(''),'Aucune réception fournisseur.')}`;else if(name==='invoices')c.innerHTML=`<div class="mb-4 flex items-center justify-between"><div><h2 class="font-extrabold">Créances fournisseurs</h2><p class="text-xs text-slate-500">Suivi du montant facturé, des règlements et du solde.</p></div><button onclick="procOpenInvoice()" class="rounded-xl bg-brand-neon px-3 py-2 text-xs font-bold"><i class="ph-bold ph-plus"></i> Ajouter une facture</button></div>${table(['Facture / réception','Fournisseur','Échéance','Montant','Déjà payé','Solde','État','Action'],(data.invoices||[]).map(f=>`<tr class="border-t"><td class="p-3 font-bold">${esc(f.numero_facture)}<small class="block text-slate-400">${esc(f.bon_reception||f.commande||'Facture libre')}</small></td><td class="p-3">${esc(f.fournisseur)}</td><td class="p-3">${esc(f.date_echeance||'—')}</td><td class="p-3">${money(f.montant)}</td><td class="p-3 text-sky-700">${money(f.montant_paye)}</td><td class="p-3 font-extrabold ${Number(f.solde)>0?'text-amber-700':'text-emerald-700'}">${money(f.solde)}</td><td class="p-3">${esc(f.statut)}</td><td class="p-3">${Number(f.solde)>0?`<button onclick="procOpenPayment(${f.id})" class="rounded-lg bg-emerald-50 px-3 py-2 text-xs font-bold text-emerald-800">Régler</button>`:'—'}</td></tr>`).join(''),'Aucune facture fournisseur enregistrée.')}`;else if(name==='payments')c.innerHTML=`<h2 class="mb-4 font-extrabold">Historique des paiements</h2>${table(['Date','Fournisseur','Facture','Montant','Mode','Référence'],(data.payments||[]).map(p=>`<tr class="border-t"><td class="p-3">${esc(p.date_paiement)}</td><td class="p-3">${esc(p.fournisseur)}</td><td class="p-3 font-bold">${esc(p.numero_facture)}</td><td class="p-3 font-extrabold text-emerald-700">${money(p.montant)}</td><td class="p-3">${esc(p.mode_paiement||'—')}</td><td class="p-3">${esc(p.reference||'—')}</td></tr>`).join(''),'Aucun paiement enregistré.')}`;else c.innerHTML=`<div class="mb-4 flex items-center justify-between"><div><h2 class="font-extrabold">Fournisseurs</h2><p class="text-xs text-slate-500">Fiches utilisées par les commandes et bons de réception du stock.</p></div><button onclick="loadModule('gestion_stock')" class="rounded-xl border px-3 py-2 text-xs font-bold"><i class="ph-bold ph-address-book"></i> Gérer les fiches fournisseurs</button></div>${table(['Fournisseur','Contact','Téléphone','E-mail','État'],suppliers().map(s=>`<tr class="border-t"><td class="p-3 font-bold">${esc(s.designation)}</td><td class="p-3">${esc(s.contact||'—')}</td><td class="p-3">${esc(s.telephone||'—')}</td><td class="p-3">${esc(s.email||'—')}</td><td class="p-3">${s.actif?'Actif':'Archivé'}</td></tr>`).join(''),'Aucun fournisseur enregistré.')}`};window.procClose=()=>{$('procModal').classList.add('hidden');$('procModal').classList.remove('flex')};const modal=(title,html,submit)=>{$('procModalTitle').textContent=title;$('procFields').innerHTML=html;$('procForm').onsubmit=async e=>{e.preventDefault();try{await submit();window.procClose();await refresh();toast('Enregistrement effectué.')}catch(err){toast(err.message)}};$('procModal').classList.remove('hidden');$('procModal').classList.add('flex')};window.procOpenOrder=()=>{let lines=[];const render=()=>{const target=$('poLines');if(target)target.innerHTML=lines.map((l,i)=>`<div class="grid gap-2 rounded-xl bg-slate-50 p-3 sm:grid-cols-12"><label class="sm:col-span-5 text-[11px] font-bold">Article<select onchange="procPickItem(${i},this.value)" class="mt-1 w-full rounded-lg border p-2"><option value="">Saisie libre</option>${(data.items||[]).map(x=>`<option value="${x.id}">${esc(x.designation)}${x.reference?' · '+esc(x.reference):''}</option>`).join('')}</select><input value="${esc(l.designation||'')}" oninput="procLineEdit(${i},'designation',this.value)" placeholder="Désignation" class="mt-1 w-full rounded-lg border p-2" required></label><label class="sm:col-span-2 text-[11px] font-bold">Quantité<input type="number" min="0.001" step="0.001" value="${l.quantite||1}" oninput="procLineEdit(${i},'quantite',this.value)" class="mt-1 w-full rounded-lg border p-2"></label><label class="sm:col-span-2 text-[11px] font-bold">Unité<input value="${esc(l.unite||'')}" oninput="procLineEdit(${i},'unite',this.value)" class="mt-1 w-full rounded-lg border p-2"></label><label class="sm:col-span-2 text-[11px] font-bold">Prix unitaire<input type="number" min="0" step="0.01" value="${l.prix_unitaire||0}" oninput="procLineEdit(${i},'prix_unitaire',this.value)" class="mt-1 w-full rounded-lg border p-2"></label><button type="button" onclick="procRemoveLine(${i})" class="text-rose-600">Supprimer</button></div>`).join('')||'<p class="text-center text-xs text-slate-400">Ajoute les articles de la commande.</p>'};window.procLineEdit=(i,k,v)=>lines[i][k]=v;window.procPickItem=(i,id)=>{const x=(data.items||[]).find(y=>Number(y.id)===Number(id));if(x){lines[i].item_id=x.id;lines[i].designation=x.designation;lines[i].unite=x.unite;render()}};window.procRemoveLine=i=>{lines.splice(i,1);render()};window.procAddLine=()=>{lines.push({quantite:1,prix_unitaire:0});render()};modal('Nouvelle commande fournisseur',`${select('Fournisseur *','poSupplier',suppliers().filter(s=>s.actif).map(s=>`<option value="${s.id}">${esc(s.designation)}</option>`).join(''))}<div class="grid gap-3 sm:grid-cols-2">${field('Date de commande','poDate','date',`value="${today}" required`)}${field('Livraison prévue','poExpected','date')}</div>${field('Notes','poNotes','text')}<div class="flex items-center justify-between"><b class="text-sm">Articles commandés</b><button type="button" onclick="procAddLine()" class="rounded-lg bg-slate-100 px-3 py-2 text-xs font-bold">+ Ajouter une ligne</button></div><div id="poLines"></div>`,async()=>api('/orders',{method:'POST',body:JSON.stringify({supplier_id:$('poSupplier').value,date_commande:$('poDate').value,date_prevue:$('poExpected').value,notes:$('poNotes').value,lignes:lines})}));render()};window.procStatus=async(id,statut)=>{try{await api(`/orders/${id}/status`,{method:'POST',body:JSON.stringify({statut})});await refresh();toast('Statut actualisé.')}catch(e){toast(e.message)}};window.procOpenInvoice=receiptId=>{const receipt=(data.receipts||[]).find(r=>Number(r.id)===Number(receiptId));const supplierOptions=suppliers().map(s=>`<option value="${s.id}" ${receipt&&Number(receipt.fournisseur_id)===Number(s.id)?'selected':''}>${esc(s.designation)}</option>`).join('');modal('Enregistrer une facture fournisseur',`${receiptId?`<div class="rounded-xl bg-sky-50 p-3 text-xs text-sky-800">Montant calculé depuis le bon d’entrée : <b>${money(receipt?.montant)}</b></div><input id="fiReceipt" type="hidden" value="${receiptId}">`:select('Bon d’entrée associé (facultatif)','fiReceipt',`<option value="">Aucun · saisir le montant</option>`+(data.receipts||[]).filter(r=>!r.facture_liee).map(r=>`<option value="${r.id}">${esc(r.numero)} · ${esc(r.fournisseur)} · ${money(r.montant)}</option>`).join(''),false)}${!receiptId?select('Fournisseur *','fiSupplier',supplierOptions):''}${field('N° de facture fournisseur *','fiNumber','text','required maxlength="120"')}<div class="grid gap-3 sm:grid-cols-2">${field('Date facture','fiDate','date',`value="${today}" required`)}${field('Date d’échéance','fiDue','date')}</div>${!receiptId?field('Montant total (DA)','fiAmount','number','min="0" step="0.01" required'):''}${field('Notes','fiNotes','text')}`,async()=>api('/invoices',{method:'POST',body:JSON.stringify({stock_document_id:$('fiReceipt')?.value||null,supplier_id:$('fiSupplier')?.value||null,numero_facture:$('fiNumber').value,date_facture:$('fiDate').value,date_echeance:$('fiDue').value,montant:$('fiAmount')?.value||null,notes:$('fiNotes').value})}))};window.procOpenPayment=id=>{const f=(data.invoices||[]).find(x=>Number(x.id)===Number(id));modal(`Paiement · ${esc(f?.numero_facture||'')}`,`<div class="rounded-xl bg-amber-50 p-3 text-sm text-amber-900">Solde restant : <b>${money(f?.solde)}</b></div><div class="grid gap-3 sm:grid-cols-2">${field('Date de paiement','ppDate','date',`value="${today}" required`)}${field('Montant payé (DA)','ppAmount','number',`min="0.01" max="${Number(f?.solde||0)}" step="0.01" required`)}</div><div class="grid gap-3 sm:grid-cols-2">${select('Mode de paiement','ppMode',['Espèces','Virement bancaire','Chèque','Carte','Autre'].map(x=>`<option>${x}</option>`).join(''),false)}${field('Référence','ppRef','text')}</div>${field('Notes','ppNotes','text')}`,async()=>api(`/invoices/${id}/payments`,{method:'POST',body:JSON.stringify({date_paiement:$('ppDate').value,montant:$('ppAmount').value,mode_paiement:$('ppMode').value,reference:$('ppRef').value,notes:$('ppNotes').value})}))};async function refresh(){if(!$('procurementModule'))return;try{data=await api('/data');$('procTotal').textContent=money(data.summary.total_facture);$('procPaid').textContent=money(data.summary.total_paye);$('procBalance').textContent=money(data.summary.reste);$('procLate').textContent=data.summary.retards||0;const active=document.querySelector('.proc-tab.bg-brand-neon')?.dataset.procTab||'orders';window.procTab(active)}catch(e){toast(e.message)}}window.procReload=refresh;refresh()})();</script></section>''' + PROCUREMENT_ADVANCED_SCRIPT

