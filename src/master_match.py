"""Deterministic matching against master_data/*.json.

Loaded once per run and indexed (dicts keyed by normalised exact-match keys)
rather than scanned per lookup, so this stays cheap even if the sample
master files here were swapped for the "hundreds of thousands of rows"
the brief describes - exact-key lookups are O(1); only the fuzzy fallback
(name matching, where no stronger key exists) is O(n), and only runs when
the cheap exact paths fail.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

from rapidfuzz import fuzz

from . import config

NAME_MATCH_THRESHOLD = 82  # rapidfuzz token_sort_ratio, 0-100
_STOPWORDS = {
    "ltd", "limited", "inc", "gmbh", "llc", "plc", "pty", "sdn", "bhd", "lda",
    "oy", "ou", "as", "co", "company", "corp", "corporation", "the", "and",
}


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _norm_key(s: str | None) -> str:
    """Aggressively normalised key for exact-ish matching (VAT ids, IBANs...)."""
    return re.sub(r"[^a-z0-9]", "", _norm(s))


def _name_tokens(s: str | None) -> str:
    words = [w for w in re.split(r"[^a-z0-9]+", _norm(s)) if w and w not in _STOPWORDS]
    return " ".join(words)


@lru_cache(maxsize=1)
def _load_json(name: str) -> dict:
    with open(config.MASTER_DATA_DIR / name, encoding="utf-8") as fh:
        return json.load(fh)


class MasterData:
    def __init__(self):
        suppliers = _load_json("suppliers.json")["suppliers"]
        self.suppliers = suppliers
        self._supplier_by_vat = {_norm_key(s["vat_id"]): s for s in suppliers if s.get("vat_id")}
        self._supplier_by_name = {_name_tokens(s["name"]): s for s in suppliers}

        cob = _load_json("chart_of_books.json")["companies"]
        self.company_code = cob[0]["company_code"] if cob else ""
        self._bu_rows = []  # (bu_code, bu_name, loc_code, loc_name, address_norm)
        for company in cob:
            for bu in company.get("business_units", []):
                for loc in bu.get("locations", []):
                    self._bu_rows.append(
                        (
                            bu["business_unit_code"],
                            bu["business_unit_name"],
                            loc["location_code"],
                            loc["location_name"],
                            _norm(loc.get("invoice_to_address", "")),
                        )
                    )

        taxes = _load_json("tax_master.json")["taxes"]
        self.taxes = taxes

        terms = _load_json("payment_terms.json")["payment_terms"]
        self.payment_terms = terms
        self._term_alias = {}
        for t in terms:
            for alias in t.get("text_aliases", []):
                self._term_alias[_norm(alias)] = t["payment_term_id"]

        po_rows = _load_json("po_master.json")["purchase_orders"]
        self._po_by_number = {_norm_key(p["po_number"]): p for p in po_rows}

    # ---- supplier --------------------------------------------------
    def match_supplier(self, name: str, vat_id: str, country_hint: str = "") -> str:
        key = _norm_key(vat_id)
        if key and key in self._supplier_by_vat:
            return self._supplier_by_vat[key]["supplier_id"]

        target = _name_tokens(name)
        if not target:
            return ""
        best, best_score = None, 0
        for s in self.suppliers:
            if country_hint and s.get("country") and s["country"].upper() != country_hint.upper():
                continue
            score = fuzz.token_sort_ratio(target, _name_tokens(s["name"]))
            if score > best_score:
                best, best_score = s, score
        if best is not None and best_score >= NAME_MATCH_THRESHOLD:
            return best["supplier_id"]
        return ""

    # ---- buyer / chart of books -------------------------------------
    def match_buyer(self, name_raw: str, address_raw: str, country_hint: str = "") -> tuple[str, str, str]:
        addr = _norm(address_raw)
        candidates = []
        if addr:
            for row in self._bu_rows:
                bu_code, bu_name, loc_code, loc_name, loc_addr = row
                if loc_addr and (loc_addr in addr or addr in loc_addr or _address_overlap(loc_addr, addr)):
                    candidates.append(row)
        if not candidates and country_hint:
            # fall back: any BU/location in this country (location codes embed country).
            cc = country_hint.upper()
            candidates = [r for r in self._bu_rows if f"_{cc}_" in r[2].upper()]
        if not candidates:
            return ("", "", "")

        company_code = self.company_code
        if len(candidates) == 1:
            bu_code, _, loc_code, _, _ = candidates[0]
            return (company_code, bu_code, loc_code)

        # Ambiguous: several business units share this location/country. Break the
        # tie with fuzzy similarity between the document's buyer name and each
        # candidate business unit's name; only trust a decisive margin.
        target = _name_tokens(name_raw)
        scored = sorted(
            ((fuzz.token_sort_ratio(target, _name_tokens(r[1])), r) for r in candidates),
            key=lambda x: x[0],
            reverse=True,
        )
        if len(scored) >= 2 and scored[0][0] - scored[1][0] >= 10:
            bu_code, _, loc_code, _, _ = scored[0][1]
            return (company_code, bu_code, loc_code)
        # Genuinely ambiguous: honest partial match - company + location known,
        # business unit not confidently resolvable.
        loc_code = candidates[0][2]
        return (company_code, "", loc_code)

    # ---- tax ----------------------------------------------------------
    def match_tax(self, name: str, rate: str, tax_type: str, country_hint: str = "") -> str:
        try:
            rate_val = float(str(rate).replace("%", "").strip()) if str(rate).strip() else None
        except ValueError:
            rate_val = None
        cc = (country_hint or "").upper()
        best, best_score = None, -1
        for t in self.taxes:
            if cc and t["country"].upper() != cc:
                continue
            score = 0
            if rate_val is not None and abs(t["rate"] - rate_val) < 0.01:
                score += 2
            if tax_type and _norm_key(tax_type) == _norm_key(t["tax_type"]):
                score += 2
            elif name and _norm_key(tax_type) in _norm_key(t.get("tax_type", "")):
                score += 1
            if score > best_score:
                best, best_score = t, score
        if best is not None and best_score >= 2:
            return best["code"]
        return ""

    # ---- payment term ---------------------------------------------------
    def match_payment_term(self, text_raw: str) -> str:
        norm = _norm(text_raw)
        if not norm:
            return ""
        if norm in self._term_alias:
            return self._term_alias[norm]
        for alias, term_id in self._term_alias.items():
            if alias in norm or norm in alias:
                return term_id
        m = re.search(r"(\d+)\s*day", norm)
        if m:
            days = m.group(1)
            for t in self.payment_terms:
                if str(t["days"]) == days:
                    return t["payment_term_id"]
        return ""

    # ---- PO ---------------------------------------------------------------
    def match_po(self, po_number: str) -> str:
        key = _norm_key(po_number)
        if key and key in self._po_by_number:
            return self._po_by_number[key]["po_id"]
        return ""


def _address_overlap(a: str, b: str) -> bool:
    """Loose address match: shares postcode-ish/numeric+street tokens."""
    ta = set(re.findall(r"[a-z0-9]{3,}", a))
    tb = set(re.findall(r"[a-z0-9]{3,}", b))
    if not ta or not tb:
        return False
    overlap = ta & tb
    return len(overlap) >= 2
