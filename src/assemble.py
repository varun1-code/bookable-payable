"""Turn one raw LLM-extracted payable into a final AUTODRAFT_SCHEMA record:
fill master-data codes deterministically (never trust the model's own codes),
strip the *_raw/_hint scratch fields, and normalise number formatting.
"""
from __future__ import annotations

import re

from .master_match import MasterData

_NUM_STRIP = re.compile(r"[^\d.\-]")


def _clean_num(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    neg = s.strip().startswith("(") and s.strip().endswith(")")
    s2 = s.replace(",", "")
    s2 = _NUM_STRIP.sub("", s2)
    if not s2 or s2 in {"-", "."}:
        return ""
    if neg and not s2.startswith("-"):
        s2 = "-" + s2
    try:
        return format(float(s2), "f").rstrip("0").rstrip(".") if "." in s2 else s2
    except ValueError:
        return ""


def _clean_str(v) -> str:
    return "" if v is None else str(v).strip()


def assemble_payable(raw: dict, master: MasterData) -> dict:
    supplier = raw.get("supplier") or {}
    buyer = raw.get("buyer") or {}

    supplier_id = master.match_supplier(
        supplier.get("name", ""), supplier.get("vat_id", ""), supplier.get("country_hint", "")
    )
    company_code, bu_code, loc_code = master.match_buyer(
        buyer.get("name_raw", ""), buyer.get("address_raw", ""), buyer.get("country_hint", "")
    )
    payment_term_id = master.match_payment_term(raw.get("payment_term_text_raw", ""))
    po_number = _clean_str(raw.get("po_number", ""))
    po_id = master.match_po(po_number) if po_number else ""

    taxes = []
    for t in raw.get("taxes") or []:
        taxes.append(_assemble_tax(t, master))

    line_items = []
    for li in raw.get("line_items") or []:
        line_items.append(_assemble_line(li, master))

    return {
        "invoice_number": _clean_str(raw.get("invoice_number")),
        "invoice_date": _clean_str(raw.get("invoice_date")),
        "due_date": _clean_str(raw.get("due_date")),
        "invoice_type": _clean_str(raw.get("invoice_type")) or "INVOICE",
        "currency": _clean_str(raw.get("currency")).upper(),
        "supplier": {
            "name": _clean_str(supplier.get("name")),
            "supplier_id": supplier_id,
            "address": _clean_str(supplier.get("address")),
            "vat_id": _clean_str(supplier.get("vat_id")),
        },
        "buyer": {
            "company_code": company_code,
            "business_unit_code": bu_code,
            "location_code": loc_code,
        },
        "payment_term_id": payment_term_id,
        "po_number": po_number,
        "po_id": po_id,
        "gross_total": _clean_num(raw.get("gross_total")),
        "subtotal": _clean_num(raw.get("subtotal")),
        "total_tax_amount": _clean_num(raw.get("total_tax_amount")),
        "discount_amount": _clean_num(raw.get("discount_amount")),
        "freight_charges": _clean_num(raw.get("freight_charges")),
        "insurance_charges": _clean_num(raw.get("insurance_charges")),
        "extra_charges": _clean_num(raw.get("extra_charges")),
        "excise_duties": _clean_num(raw.get("excise_duties")),
        "taxes": taxes,
        "line_items": line_items,
    }


_WITHHOLDING_RE = re.compile(r"withhold|\bwht\b|retention|\btds\b", re.IGNORECASE)


def _assemble_tax(t: dict, master: MasterData) -> dict:
    tax_type = _clean_str(t.get("tax_type"))
    tax_name = _clean_str(t.get("tax_name"))
    tax_rate = _clean_num(t.get("tax_rate"))
    tax_amount = _clean_num(t.get("tax_amount"))
    # A withholding tax reduces what's owed by definition - normalise the sign
    # regardless of how the model extracted it, rather than re-litigating this
    # per document. Both the label ("withholding"/"WHT"/...) and the magnitude
    # are grounded in the document; only the sign is corrected.
    if tax_amount and _WITHHOLDING_RE.search(f"{tax_type} {tax_name}"):
        try:
            val = float(tax_amount)
            if val > 0:
                tax_amount = _clean_num(-val)
        except ValueError:
            pass
    code = master.match_tax(tax_name, tax_rate, tax_type, t.get("country_hint", ""))
    return {
        "tax_type": tax_type,
        "tax_name": tax_name,
        "tax_rate": tax_rate,
        "tax_amount": tax_amount,
        "tax_type_code": code,
    }


def _assemble_line(li: dict, master: MasterData) -> dict:
    taxes = [_assemble_tax(t, master) for t in (li.get("taxes") or [])]
    return {
        "description": _clean_str(li.get("description")),
        "item_type": _clean_str(li.get("item_type")) or "SERVICE",
        "uom": _clean_str(li.get("uom")),
        "quantity": _clean_num(li.get("quantity")),
        "unit_price": _clean_num(li.get("unit_price")),
        "total": _clean_num(li.get("total")),
        "discount": _clean_num(li.get("discount")),
        "discount_percentage": _clean_num(li.get("discount_percentage")),
        "tax_rate": _clean_num(li.get("tax_rate")),
        "tax_amount": _clean_num(li.get("tax_amount")),
        "taxes": taxes,
    }
