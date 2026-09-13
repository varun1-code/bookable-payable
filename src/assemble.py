"""Turn one raw LLM-extracted payable into a final AUTODRAFT_SCHEMA record:
fill master-data codes deterministically (never trust the model's own codes),
strip the *_raw/_hint scratch fields, and normalise number formatting.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .master_match import MasterData

_NUM_STRIP = re.compile(r"[^\d.\-]")


def _to_decimal(s: str) -> Decimal | None:
    """Parse a canonical numeric string (as produced by `_clean_num`) into a
    Decimal. Used for the arithmetic assemble.py itself does (charge
    reclassification, withholding-sign normalisation) so that internal
    combining never goes through binary float and picks up rounding drift -
    erp.py is untouched and still receives plain decimal strings."""
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _format_decimal(d: Decimal) -> str:
    s = format(d, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


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
    d = _to_decimal(s2)
    return _format_decimal(d) if d is not None else ""


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
    reclassified_charge_total = Decimal("0")
    for t in raw.get("taxes") or []:
        assembled = _assemble_tax(t, master)
        if _is_charge_not_tax(assembled["tax_type"], assembled["tax_name"]):
            # A fuel/handling/service surcharge is a CHARGE, not a government tax,
            # even when the model puts it in taxes[]. Recovering it here (instead
            # of just dropping it) keeps the amount grounded in the document while
            # fixing its placement - the same "where it says it" requirement the
            # brief grades, just corrected after the fact rather than re-asked.
            # Decimal, not float: this sums across every reclassified tax on the
            # payable, and float addition can drift a cent on real invoice amounts.
            amt = _to_decimal(assembled["tax_amount"])
            if amt is not None:
                reclassified_charge_total += amt
            continue
        taxes.append(assembled)

    line_items = []
    for li in raw.get("line_items") or []:
        line_items.append(_assemble_line(li, master))

    extra_charges = _clean_num(raw.get("extra_charges"))
    if reclassified_charge_total:
        base = _to_decimal(extra_charges) or Decimal("0")
        extra_charges = _format_decimal(base + reclassified_charge_total)

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
        "extra_charges": extra_charges,
        "excise_duties": _clean_num(raw.get("excise_duties")),
        "taxes": taxes,
        "line_items": line_items,
    }


_WITHHOLDING_RE = re.compile(r"withhold|\bwht\b|retention|\btds\b", re.IGNORECASE)

_CHARGE_NOT_TAX_RE = re.compile(
    r"surcharge|\bhandling\b|delivery\s*fee|service\s*charge|agency\s*fee|management\s*fee|"
    r"processing\s*fee|convenience\s*fee|shipping\s*(fee|cost|charge)|\bfuel\b|"
    r"\bfreight\b(?!\s*(duty|levy|tax))",
    re.IGNORECASE,
)
_REAL_TAX_TYPE_RE = re.compile(
    r"\b(vat|gst|iva|km|sst|hst|moms|sales\s*tax|use\s*tax|withhold|wht|retention|tds|excise|"
    r"duty|levy|cess|nhil|getf|covid|cst)\b",
    re.IGNORECASE,
)


def _is_charge_not_tax(tax_type: str, tax_name: str) -> bool:
    combined = f"{tax_type} {tax_name}"
    return bool(_CHARGE_NOT_TAX_RE.search(combined)) and not _REAL_TAX_TYPE_RE.search(combined)


def _assemble_tax(t: dict, master: MasterData) -> dict:
    tax_type = _clean_str(t.get("tax_type"))
    tax_name = _clean_str(t.get("tax_name"))
    tax_rate = _clean_num(t.get("tax_rate"))
    tax_amount = _clean_num(t.get("tax_amount"))
    # A withholding tax reduces what's owed by definition - normalise the sign
    # regardless of how the model extracted it, rather than re-litigating this
    # per document. Both the label ("withholding"/"WHT"/...) and the magnitude
    # are grounded in the document; only the sign is corrected. Decimal (not
    # float) so a value like "1234.56" round-trips through the sign flip exactly.
    if tax_amount and _WITHHOLDING_RE.search(f"{tax_type} {tax_name}"):
        val = _to_decimal(tax_amount)
        if val is not None and val > 0:
            tax_amount = _format_decimal(-val)
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
