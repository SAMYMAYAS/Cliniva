"""Classification et répartition comptable des prestations spéciales de caisse."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import unicodedata


CATEGORY_KEYS = ("principal", "contraste", "sedation")
CENT = Decimal("0.01")


def money(value):
    try:
        result = Decimal(str(value or 0))
        return result if result.is_finite() else Decimal("0")
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def classify_prestation(act):
    if not isinstance(act, dict):
        return "principal"
    text = " ".join(str(act.get(key) or "") for key in ("nom", "designation", "categorie", "specialite"))
    normalized = "".join(
        char for char in unicodedata.normalize("NFKD", text.lower())
        if not unicodedata.combining(char)
    )
    if any(term in normalized for term in ("contrast", "contraste", "gadolinium", "p.contrast", "produit iode")):
        return "contraste"
    if any(term in normalized for term in ("sedat", "anesth")):
        return "sedation"
    return "principal"


def passage_category_totals(acts, gross_total, discount=0):
    """Montant net dû par catégorie, avec remise répartie proportionnellement."""
    gross = {key: Decimal("0") for key in CATEGORY_KEYS}
    for act in acts or []:
        if not isinstance(act, dict):
            continue
        subtotal = money(act.get("sous_total"))
        if subtotal == 0:
            subtotal = money(act.get("tarif") or act.get("prix") or act.get("prix_jour")) * money(act.get("quantite") or 1)
        gross[classify_prestation(act)] += max(Decimal("0"), subtotal)

    declared = max(Decimal("0"), money(gross_total))
    sum_acts = sum(gross.values())
    if sum_acts > 0 and declared > 0 and sum_acts != declared:
        factor = declared / sum_acts
        gross = {key: value * factor for key, value in gross.items()}
    elif sum_acts <= 0 and declared > 0:
        gross["principal"] = declared

    net = max(Decimal("0"), declared - max(Decimal("0"), money(discount)))
    base = sum(gross.values())
    if base <= 0 or net <= 0:
        return {key: Decimal("0.00") for key in CATEGORY_KEYS}
    shares = {key: (gross[key] * net / base).quantize(CENT, rounding=ROUND_HALF_UP) for key in CATEGORY_KEYS}
    remainder = net - sum(shares.values())
    largest = max(CATEGORY_KEYS, key=lambda key: gross[key])
    shares[largest] += remainder
    return shares


def allocate_movement_categories(passages, movements):
    """Répartit chaque mouvement dans l'ordre chronologique et conserve son signe."""
    by_passage = {}
    for movement in movements or []:
        passage_id = movement.get("id_passage")
        by_passage.setdefault(passage_id, []).append(movement)
    allocations = {}
    for passage_id, entries in by_passage.items():
        passage = passages.get(passage_id) or {}
        acts = passage.get("actes")
        if not isinstance(acts, list):
            acts = passage.get("actes_json") or []
        due = passage_category_totals(acts, passage.get("montant_total"), passage.get("remise"))
        paid = {key: Decimal("0") for key in CATEGORY_KEYS}
        entries.sort(key=lambda item: (
            str(item.get("date_mouvement") or ""),
            str(item.get("heure_mouvement") or ""),
            int(item.get("id_mouvement") or item.get("id") or 0),
        ))
        for movement in entries:
            amount = money(movement.get("montant_mouvement") if "montant_mouvement" in movement else movement.get("montant"))
            split = {key: Decimal("0.00") for key in CATEGORY_KEYS}
            if amount > 0:
                remaining = {key: max(Decimal("0"), due[key] - paid[key]) for key in CATEGORY_KEYS}
                available = sum(remaining.values())
                to_allocate = amount
                if available > 0:
                    bounded = min(amount, available)
                    split = {key: (bounded * remaining[key] / available).quantize(CENT, rounding=ROUND_HALF_UP) for key in CATEGORY_KEYS}
                    remainder = bounded - sum(split.values())
                    split[max(CATEGORY_KEYS, key=lambda key: remaining[key])] += remainder
                    to_allocate -= bounded
                split["principal"] += to_allocate
                paid = {key: paid[key] + split[key] for key in CATEGORY_KEYS}
            elif amount < 0:
                refundable = sum(paid.values())
                refund = min(abs(amount), refundable)
                if refundable > 0:
                    split = {key: -(refund * paid[key] / refundable).quantize(CENT, rounding=ROUND_HALF_UP) for key in CATEGORY_KEYS}
                    remainder = refund - sum(abs(value) for value in split.values())
                    split[max(CATEGORY_KEYS, key=lambda key: paid[key])] -= remainder
                    paid = {key: max(Decimal("0"), paid[key] + split[key]) for key in CATEGORY_KEYS}
                split["principal"] -= abs(amount) - refund
            identifier = movement.get("id_mouvement") or movement.get("id")
            allocations[identifier] = split
    return allocations
