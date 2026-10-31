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


@procurement_router.get("/data")
def procurement_data(request: Request):
    _require(request)
    with get_db_cursor() as cur:
        cur.execute("SELECT id, designation, actif, contact, telephone, email FROM stock_suppliers ORDER BY designation")
        suppliers = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT id, reference, designation, unite FROM stock_items WHERE actif=TRUE ORDER BY designation")
        items = [dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT o.id,o.numero,o.supplier_id,s.designation AS fournisseur,o.date_commande,o.date_prevue,o.statut,o.notes,o.created_at,
                COALESCE(SUM(l.quantite*l.prix_unitaire),0) AS montant, COUNT(l.id) AS lignes
            FROM stock_supplier_orders o JOIN stock_suppliers s ON s.id=o.supplier_id
            LEFT JOIN stock_supplier_order_lines l ON l.order_id=o.id
            GROUP BY o.id,s.designation ORDER BY o.date_commande DESC,o.id DESC""")
        orders = [dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT d.id,d.numero,d.created_at,d.fournisseur_id,s.designation AS fournisseur,
                COALESCE(SUM((CASE WHEN COALESCE(l.quantite_unite_stock,0)>0 THEN l.quantite_unite_stock ELSE l.quantite END)*COALESCE(l.prix_achat,l.prix_unitaire,0)),0) AS montant,
                EXISTS(SELECT 1 FROM stock_supplier_invoices f WHERE f.stock_document_id=d.id) AS facture_liee
            FROM stock_documents d JOIN stock_suppliers s ON s.id=d.fournisseur_id
            LEFT JOIN stock_document_lines l ON l.document_id=d.id
            WHERE d.type_document='Entrée' AND d.statut<>'Annulé' AND d.fournisseur_id IS NOT NULL
            GROUP BY d.id,s.designation ORDER BY d.created_at DESC,d.id DESC""")
        receipts = [dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT f.id,f.supplier_id,s.designation AS fournisseur,f.order_id,o.numero AS commande,
                f.stock_document_id,d.numero AS bon_reception,f.numero_facture,f.date_facture,f.date_echeance,
                f.montant,f.notes,f.created_at,COALESCE(p.paye,0) AS montant_paye,
                GREATEST(f.montant-COALESCE(p.paye,0),0) AS solde,
                CASE WHEN COALESCE(p.paye,0)>=f.montant THEN 'Réglée'
                     WHEN COALESCE(p.paye,0)>0 THEN 'Partiellement réglée'
                     WHEN f.date_echeance<CURRENT_DATE THEN 'En retard' ELSE 'À régler' END AS statut
            FROM stock_supplier_invoices f JOIN stock_suppliers s ON s.id=f.supplier_id
            LEFT JOIN stock_supplier_orders o ON o.id=f.order_id
            LEFT JOIN stock_documents d ON d.id=f.stock_document_id
            LEFT JOIN (SELECT invoice_id,SUM(montant) AS paye FROM stock_supplier_payments GROUP BY invoice_id) p ON p.invoice_id=f.id
            ORDER BY f.date_echeance NULLS LAST,f.date_facture DESC,f.id DESC""")
        invoices = [dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT p.id,p.invoice_id,p.date_paiement,p.montant,p.mode_paiement,p.reference,p.notes,
                f.numero_facture,s.designation AS fournisseur
            FROM stock_supplier_payments p JOIN stock_supplier_invoices f ON f.id=p.invoice_id
            JOIN stock_suppliers s ON s.id=f.supplier_id ORDER BY p.date_paiement DESC,p.id DESC LIMIT 300""")
        payments = [dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT COALESCE(SUM(f.montant),0) AS total_facture,
                COALESCE(SUM(LEAST(f.montant,COALESCE(p.paye,0))),0) AS total_paye,
                COALESCE(SUM(GREATEST(f.montant-COALESCE(p.paye,0),0)),0) AS reste,
                COUNT(*) FILTER (WHERE f.date_echeance<CURRENT_DATE AND COALESCE(p.paye,0)<f.montant) AS retards
            FROM stock_supplier_invoices f LEFT JOIN (SELECT invoice_id,SUM(montant) AS paye FROM stock_supplier_payments GROUP BY invoice_id) p ON p.invoice_id=f.id""")
        summary = dict(cur.fetchone())
        cur.execute("""SELECT l.order_id,l.id,l.item_id,l.designation,l.quantite,l.unite,l.prix_unitaire,l.quantite_recue
            FROM stock_supplier_order_lines l ORDER BY l.id""")
        lines = [dict(row) for row in cur.fetchall()]
    lines_by_order: dict[int, list[dict[str, Any]]] = {}
    for line in lines:
        lines_by_order.setdefault(line.pop("order_id"), []).append(line)
    for order in orders:
        order["lignes_detail"] = lines_by_order.get(order["id"], [])
    return {"suppliers": suppliers, "items": items, "orders": orders, "receipts": receipts,
            "invoices": invoices, "payments": payments, "summary": summary}


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
            quantity = Decimal(str(line.get("quantite", "")).replace(",", "."))
        except (InvalidOperation, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Quantité invalide à la ligne {i}.") from exc
        if not name or not quantity.is_finite() or quantity <= 0:
            raise HTTPException(status_code=400, detail=f"Article ou quantité invalide à la ligne {i}.")
        price = _money(line.get("prix_unitaire", 0), f"ligne {i}")
        normalized.append((line.get("item_id") or None, name[:240], quantity, str(line.get("unite") or "")[:80], price))
    with get_db_cursor() as cur:
        cur.execute("SELECT designation FROM stock_suppliers WHERE id=%s AND actif=TRUE", (supplier_id,))
        supplier = cur.fetchone()
        if not supplier:
            raise HTTPException(status_code=404, detail="Fournisseur actif introuvable.")
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
    if status not in {"Commandée", "Partiellement reçue", "Reçue", "Annulée"}:
        raise HTTPException(status_code=400, detail="Statut de commande invalide.")
    with get_db_cursor() as cur:
        cur.execute("UPDATE stock_supplier_orders SET statut=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING numero", (status, order_id))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Commande fournisseur introuvable.")
    track_action(request, "STOCK_SUPPLIER_ORDER_STATUS", f"Commande {row['numero']} : {status}")
    return {"success": True}


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
            cur.execute("""SELECT d.fournisseur_id,d.numero,d.type_document,d.statut,
                    COALESCE(SUM((CASE WHEN COALESCE(l.quantite_unite_stock,0)>0 THEN l.quantite_unite_stock ELSE l.quantite END)*COALESCE(l.prix_achat,l.prix_unitaire,0)),0) AS total
                FROM stock_documents d LEFT JOIN stock_document_lines l ON l.document_id=d.id WHERE d.id=%s
                GROUP BY d.id""", (receipt_id,))
            receipt = cur.fetchone()
            if not receipt or receipt["type_document"] != "Entrée" or receipt["statut"] == "Annulé" or not receipt["fournisseur_id"]:
                raise HTTPException(status_code=400, detail="Choisis un bon d’entrée validé et lié à un fournisseur.")
            supplier_id, amount = receipt["fournisseur_id"], _money(receipt["total"], "réception")
        else:
            try:
                supplier_id = int(data.get("supplier_id"))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Choisis un fournisseur.") from exc
            amount = _money(data.get("montant"), "facture")
        cur.execute("SELECT id FROM stock_suppliers WHERE id=%s", (supplier_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Fournisseur introuvable.")
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


def render_html(user: dict[str, Any] | None = None) -> str:
    if not user or not _allowed(user):
        return '<div class="rounded-2xl border border-rose-200 bg-rose-50 p-5 font-semibold text-rose-700">Accès à l’approvisionnement non autorisé.</div>'
    return r'''<section id="procurementModule" class="space-y-5 text-slate-800">
<header class="flex flex-wrap items-center justify-between gap-4 rounded-[2rem] bg-gradient-to-r from-[#101b13] to-[#183b20] px-7 py-6 text-white shadow-lg"><div class="flex items-center gap-4"><span class="grid h-12 w-12 place-items-center rounded-2xl bg-brand-neon text-emerald-950"><i class="ph-bold ph-truck text-2xl"></i></span><div><p class="text-xs font-bold uppercase tracking-[.18em] text-lime-300">Gestion Stock · Achats</p><h1 class="mt-1 text-2xl font-extrabold">Approvisionnement & fournisseurs</h1><p class="mt-1 text-sm text-slate-300">Commandes, réceptions, créances fournisseurs et règlements.</p></div></div><button onclick="procOpenOrder()" class="inline-flex items-center gap-2 rounded-xl bg-brand-neon px-4 py-2.5 text-sm font-extrabold text-black"><i class="ph-bold ph-plus-circle"></i>Nouvelle commande</button></header>
<section class="grid grid-cols-2 gap-3 xl:grid-cols-4"><article class="rounded-2xl border border-slate-100 bg-white p-4 shadow-sm"><span class="text-xs font-bold text-slate-500">Achats facturés</span><b id="procTotal" class="mt-2 block text-xl">—</b></article><article class="rounded-2xl border border-sky-100 bg-white p-4 shadow-sm"><span class="text-xs font-bold text-slate-500">Paiements effectués</span><b id="procPaid" class="mt-2 block text-xl text-sky-700">—</b></article><article class="rounded-2xl border border-amber-100 bg-white p-4 shadow-sm"><span class="text-xs font-bold text-slate-500">Créances restantes</span><b id="procBalance" class="mt-2 block text-xl text-amber-700">—</b></article><article class="rounded-2xl border border-rose-100 bg-white p-4 shadow-sm"><span class="text-xs font-bold text-slate-500">Factures en retard</span><b id="procLate" class="mt-2 block text-xl text-rose-700">—</b></article></section>
<nav class="flex flex-wrap gap-1 rounded-2xl border border-slate-200 bg-white p-1.5 shadow-sm"><button data-proc-tab="orders" onclick="procTab('orders')" class="proc-tab rounded-xl bg-brand-neon px-4 py-2.5 text-xs font-bold">Commandes d’achat</button><button data-proc-tab="receipts" onclick="procTab('receipts')" class="proc-tab rounded-xl px-4 py-2.5 text-xs font-bold">Réceptions</button><button data-proc-tab="invoices" onclick="procTab('invoices')" class="proc-tab rounded-xl px-4 py-2.5 text-xs font-bold">Créances / factures</button><button data-proc-tab="payments" onclick="procTab('payments')" class="proc-tab rounded-xl px-4 py-2.5 text-xs font-bold">Paiements</button><button data-proc-tab="suppliers" onclick="procTab('suppliers')" class="proc-tab rounded-xl px-4 py-2.5 text-xs font-bold">Fournisseurs</button></nav>
<section id="procContent" class="rounded-3xl border border-slate-100 bg-white p-4 shadow-sm sm:p-5"></section>
<div id="procModal" class="fixed inset-0 z-[2147483605] hidden items-center justify-center bg-slate-950/65 p-3 backdrop-blur-sm"><section class="max-h-[92vh] w-full max-w-3xl overflow-y-auto rounded-3xl bg-white shadow-2xl"><header class="sticky top-0 z-10 flex items-center justify-between bg-gradient-to-r from-[#101b13] to-[#183b20] px-5 py-4 text-white"><h2 id="procModalTitle" class="text-lg font-black"></h2><button onclick="procClose()" class="grid h-9 w-9 place-items-center rounded-xl bg-white/10"><i class="ph-bold ph-x"></i></button></header><form id="procForm" class="space-y-4 p-5"><div id="procFields" class="space-y-3"></div><div class="flex justify-end gap-2 border-t pt-4"><button type="button" onclick="procClose()" class="rounded-xl border px-4 py-2 font-bold">Annuler</button><button class="rounded-xl bg-brand-neon px-5 py-2 font-extrabold text-black">Enregistrer</button></div></form></section></div><div id="procToast" class="fixed bottom-5 right-5 z-[2147483606] hidden rounded-xl bg-slate-900 px-4 py-3 text-sm font-semibold text-white shadow-xl"></div>
<script>(()=>{const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),money=v=>`${Number(v||0).toLocaleString('fr-FR',{minimumFractionDigits:2,maximumFractionDigits:2})} DA`,today=new Date().toISOString().slice(0,10);let data={};const api=async(path,opt={})=>{const r=await fetch('/api/approvisionnement'+path,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(opt.headers||{})},...opt}),b=await r.json().catch(()=>({}));if(!r.ok)throw Error(b.detail||'Opération impossible.');return b};const toast=s=>{const t=$('procToast');t.textContent=s;t.classList.remove('hidden');setTimeout(()=>t.classList.add('hidden'),3500)};const table=(heads,rows,empty)=>`<div class="overflow-x-auto rounded-2xl border border-slate-100"><table class="w-full min-w-[750px] text-left text-sm"><thead class="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-600"><tr>${heads.map(x=>`<th class="px-3 py-3">${x}</th>`).join('')}</tr></thead><tbody>${rows||`<tr><td colspan="${heads.length}" class="p-9 text-center text-slate-400">${empty}</td></tr>`}</tbody></table></div>`;const suppliers=()=>data.suppliers||[];const field=(label,id,type='text',attrs='')=>`<label class="block text-xs font-bold text-slate-600">${label}<input id="${id}" type="${type}" ${attrs} class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm outline-none focus:ring-2 focus:ring-emerald-200"></label>`;const select=(label,id,options,required=true)=>`<label class="block text-xs font-bold text-slate-600">${label}<select id="${id}" ${required?'required':''} class="mt-1.5 w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm"><option value="">Choisir…</option>${options}</select></label>`;window.procTab=name=>{document.querySelectorAll('.proc-tab').forEach(b=>b.className='proc-tab rounded-xl px-4 py-2.5 text-xs font-bold '+(b.dataset.procTab===name?'bg-brand-neon':'text-slate-500 hover:bg-slate-100'));const c=$('procContent');if(name==='orders')c.innerHTML=`<div class="mb-4 flex items-center justify-between"><div><h2 class="font-extrabold">Commandes d’achat</h2><p class="text-xs text-slate-500">Prévisions d’achat par fournisseur et par article.</p></div><button onclick="procOpenOrder()" class="rounded-xl bg-brand-neon px-3 py-2 text-xs font-bold"><i class="ph-bold ph-plus"></i> Nouvelle commande</button></div>${table(['N° commande','Fournisseur','Date','Livraison prévue','Articles','Montant estimé','Statut'],(data.orders||[]).map(o=>`<tr class="border-t"><td class="p-3 font-bold">${esc(o.numero)}</td><td class="p-3">${esc(o.fournisseur)}</td><td class="p-3">${esc(o.date_commande)}</td><td class="p-3">${esc(o.date_prevue||'—')}</td><td class="p-3">${o.lignes} ligne(s)</td><td class="p-3 font-bold">${money(o.montant)}</td><td class="p-3"><select onchange="procStatus(${o.id},this.value)" class="rounded-lg border px-2 py-1 text-xs">${['Commandée','Partiellement reçue','Reçue','Annulée'].map(s=>`<option ${o.statut===s?'selected':''}>${s}</option>`).join('')}</select></td></tr>`).join(''),'Aucune commande d’achat.')}`;else if(name==='receipts')c.innerHTML=`<div class="mb-4"><h2 class="font-extrabold">Réceptions de stock</h2><p class="text-xs text-slate-500">Les bons d’entrée validés sont saisis dans Gestion Stock. Une réception peut ensuite être rapprochée d’une facture fournisseur.</p></div>${table(['Bon d’entrée','Fournisseur','Date réception','Valeur reçue','Facture'],(data.receipts||[]).map(r=>`<tr class="border-t"><td class="p-3 font-bold">${esc(r.numero)}</td><td class="p-3">${esc(r.fournisseur)}</td><td class="p-3">${esc(String(r.created_at||'').slice(0,16))}</td><td class="p-3 font-bold">${money(r.montant)}</td><td class="p-3">${r.facture_liee?'<span class="text-emerald-700">Facture liée</span>':`<button onclick="procOpenInvoice(${r.id})" class="rounded-lg bg-sky-50 px-3 py-2 text-xs font-bold text-sky-800">Créer la créance</button>`}</td></tr>`).join(''),'Aucune réception fournisseur.')}`;else if(name==='invoices')c.innerHTML=`<div class="mb-4 flex items-center justify-between"><div><h2 class="font-extrabold">Créances fournisseurs</h2><p class="text-xs text-slate-500">Suivi du montant facturé, des règlements et du solde.</p></div><button onclick="procOpenInvoice()" class="rounded-xl bg-brand-neon px-3 py-2 text-xs font-bold"><i class="ph-bold ph-plus"></i> Ajouter une facture</button></div>${table(['Facture / réception','Fournisseur','Échéance','Montant','Déjà payé','Solde','État','Action'],(data.invoices||[]).map(f=>`<tr class="border-t"><td class="p-3 font-bold">${esc(f.numero_facture)}<small class="block text-slate-400">${esc(f.bon_reception||f.commande||'Facture libre')}</small></td><td class="p-3">${esc(f.fournisseur)}</td><td class="p-3">${esc(f.date_echeance||'—')}</td><td class="p-3">${money(f.montant)}</td><td class="p-3 text-sky-700">${money(f.montant_paye)}</td><td class="p-3 font-extrabold ${Number(f.solde)>0?'text-amber-700':'text-emerald-700'}">${money(f.solde)}</td><td class="p-3">${esc(f.statut)}</td><td class="p-3">${Number(f.solde)>0?`<button onclick="procOpenPayment(${f.id})" class="rounded-lg bg-emerald-50 px-3 py-2 text-xs font-bold text-emerald-800">Régler</button>`:'—'}</td></tr>`).join(''),'Aucune facture fournisseur enregistrée.')}`;else if(name==='payments')c.innerHTML=`<h2 class="mb-4 font-extrabold">Historique des paiements</h2>${table(['Date','Fournisseur','Facture','Montant','Mode','Référence'],(data.payments||[]).map(p=>`<tr class="border-t"><td class="p-3">${esc(p.date_paiement)}</td><td class="p-3">${esc(p.fournisseur)}</td><td class="p-3 font-bold">${esc(p.numero_facture)}</td><td class="p-3 font-extrabold text-emerald-700">${money(p.montant)}</td><td class="p-3">${esc(p.mode_paiement||'—')}</td><td class="p-3">${esc(p.reference||'—')}</td></tr>`).join(''),'Aucun paiement enregistré.')}`;else c.innerHTML=`<div class="mb-4 flex items-center justify-between"><div><h2 class="font-extrabold">Fournisseurs</h2><p class="text-xs text-slate-500">Fiches utilisées par les commandes et bons de réception du stock.</p></div><button onclick="loadModule('gestion_stock')" class="rounded-xl border px-3 py-2 text-xs font-bold"><i class="ph-bold ph-address-book"></i> Gérer les fiches fournisseurs</button></div>${table(['Fournisseur','Contact','Téléphone','E-mail','État'],suppliers().map(s=>`<tr class="border-t"><td class="p-3 font-bold">${esc(s.designation)}</td><td class="p-3">${esc(s.contact||'—')}</td><td class="p-3">${esc(s.telephone||'—')}</td><td class="p-3">${esc(s.email||'—')}</td><td class="p-3">${s.actif?'Actif':'Archivé'}</td></tr>`).join(''),'Aucun fournisseur enregistré.')}`};window.procClose=()=>{$('procModal').classList.add('hidden');$('procModal').classList.remove('flex')};const modal=(title,html,submit)=>{$('procModalTitle').textContent=title;$('procFields').innerHTML=html;$('procForm').onsubmit=async e=>{e.preventDefault();try{await submit();window.procClose();await refresh();toast('Enregistrement effectué.')}catch(err){toast(err.message)}};$('procModal').classList.remove('hidden');$('procModal').classList.add('flex')};window.procOpenOrder=()=>{let lines=[];const render=()=>{const target=$('poLines');if(target)target.innerHTML=lines.map((l,i)=>`<div class="grid gap-2 rounded-xl bg-slate-50 p-3 sm:grid-cols-12"><label class="sm:col-span-5 text-[11px] font-bold">Article<select onchange="procPickItem(${i},this.value)" class="mt-1 w-full rounded-lg border p-2"><option value="">Saisie libre</option>${(data.items||[]).map(x=>`<option value="${x.id}">${esc(x.designation)}${x.reference?' · '+esc(x.reference):''}</option>`).join('')}</select><input value="${esc(l.designation||'')}" oninput="procLineEdit(${i},'designation',this.value)" placeholder="Désignation" class="mt-1 w-full rounded-lg border p-2" required></label><label class="sm:col-span-2 text-[11px] font-bold">Quantité<input type="number" min="0.001" step="0.001" value="${l.quantite||1}" oninput="procLineEdit(${i},'quantite',this.value)" class="mt-1 w-full rounded-lg border p-2"></label><label class="sm:col-span-2 text-[11px] font-bold">Unité<input value="${esc(l.unite||'')}" oninput="procLineEdit(${i},'unite',this.value)" class="mt-1 w-full rounded-lg border p-2"></label><label class="sm:col-span-2 text-[11px] font-bold">Prix unitaire<input type="number" min="0" step="0.01" value="${l.prix_unitaire||0}" oninput="procLineEdit(${i},'prix_unitaire',this.value)" class="mt-1 w-full rounded-lg border p-2"></label><button type="button" onclick="procRemoveLine(${i})" class="text-rose-600">Supprimer</button></div>`).join('')||'<p class="text-center text-xs text-slate-400">Ajoute les articles de la commande.</p>'};window.procLineEdit=(i,k,v)=>lines[i][k]=v;window.procPickItem=(i,id)=>{const x=(data.items||[]).find(y=>Number(y.id)===Number(id));if(x){lines[i].item_id=x.id;lines[i].designation=x.designation;lines[i].unite=x.unite;render()}};window.procRemoveLine=i=>{lines.splice(i,1);render()};window.procAddLine=()=>{lines.push({quantite:1,prix_unitaire:0});render()};modal('Nouvelle commande fournisseur',`${select('Fournisseur *','poSupplier',suppliers().filter(s=>s.actif).map(s=>`<option value="${s.id}">${esc(s.designation)}</option>`).join(''))}<div class="grid gap-3 sm:grid-cols-2">${field('Date de commande','poDate','date',`value="${today}" required`)}${field('Livraison prévue','poExpected','date')}</div>${field('Notes','poNotes','text')}<div class="flex items-center justify-between"><b class="text-sm">Articles commandés</b><button type="button" onclick="procAddLine()" class="rounded-lg bg-slate-100 px-3 py-2 text-xs font-bold">+ Ajouter une ligne</button></div><div id="poLines"></div>`,async()=>api('/orders',{method:'POST',body:JSON.stringify({supplier_id:$('poSupplier').value,date_commande:$('poDate').value,date_prevue:$('poExpected').value,notes:$('poNotes').value,lignes:lines})}));render()};window.procStatus=async(id,statut)=>{try{await api(`/orders/${id}/status`,{method:'POST',body:JSON.stringify({statut})});await refresh();toast('Statut actualisé.')}catch(e){toast(e.message)}};window.procOpenInvoice=receiptId=>{const receipt=(data.receipts||[]).find(r=>Number(r.id)===Number(receiptId));const supplierOptions=suppliers().map(s=>`<option value="${s.id}" ${receipt&&Number(receipt.fournisseur_id)===Number(s.id)?'selected':''}>${esc(s.designation)}</option>`).join('');modal('Enregistrer une facture fournisseur',`${receiptId?`<div class="rounded-xl bg-sky-50 p-3 text-xs text-sky-800">Montant calculé depuis le bon d’entrée : <b>${money(receipt?.montant)}</b></div><input id="fiReceipt" type="hidden" value="${receiptId}">`:select('Bon d’entrée associé (facultatif)','fiReceipt',`<option value="">Aucun · saisir le montant</option>`+(data.receipts||[]).filter(r=>!r.facture_liee).map(r=>`<option value="${r.id}">${esc(r.numero)} · ${esc(r.fournisseur)} · ${money(r.montant)}</option>`).join(''),false)}${!receiptId?select('Fournisseur *','fiSupplier',supplierOptions):''}${field('N° de facture fournisseur *','fiNumber','text','required maxlength="120"')}<div class="grid gap-3 sm:grid-cols-2">${field('Date facture','fiDate','date',`value="${today}" required`)}${field('Date d’échéance','fiDue','date')}</div>${!receiptId?field('Montant total (DA)','fiAmount','number','min="0" step="0.01" required'):''}${field('Notes','fiNotes','text')}`,async()=>api('/invoices',{method:'POST',body:JSON.stringify({stock_document_id:$('fiReceipt')?.value||null,supplier_id:$('fiSupplier')?.value||null,numero_facture:$('fiNumber').value,date_facture:$('fiDate').value,date_echeance:$('fiDue').value,montant:$('fiAmount')?.value||null,notes:$('fiNotes').value})}))};window.procOpenPayment=id=>{const f=(data.invoices||[]).find(x=>Number(x.id)===Number(id));modal(`Paiement · ${esc(f?.numero_facture||'')}`,`<div class="rounded-xl bg-amber-50 p-3 text-sm text-amber-900">Solde restant : <b>${money(f?.solde)}</b></div><div class="grid gap-3 sm:grid-cols-2">${field('Date de paiement','ppDate','date',`value="${today}" required`)}${field('Montant payé (DA)','ppAmount','number',`min="0.01" max="${Number(f?.solde||0)}" step="0.01" required`)}</div><div class="grid gap-3 sm:grid-cols-2">${select('Mode de paiement','ppMode',['Espèces','Virement bancaire','Chèque','Carte','Autre'].map(x=>`<option>${x}</option>`).join(''),false)}${field('Référence','ppRef','text')}</div>${field('Notes','ppNotes','text')}`,async()=>api(`/invoices/${id}/payments`,{method:'POST',body:JSON.stringify({date_paiement:$('ppDate').value,montant:$('ppAmount').value,mode_paiement:$('ppMode').value,reference:$('ppRef').value,notes:$('ppNotes').value})}))};async function refresh(){if(!$('procurementModule'))return;try{data=await api('/data');$('procTotal').textContent=money(data.summary.total_facture);$('procPaid').textContent=money(data.summary.total_paye);$('procBalance').textContent=money(data.summary.reste);$('procLate').textContent=data.summary.retards||0;const active=document.querySelector('.proc-tab.bg-brand-neon')?.dataset.procTab||'orders';window.procTab(active)}catch(e){toast(e.message)}}window.procReload=refresh;refresh()})();</script></section>'''

